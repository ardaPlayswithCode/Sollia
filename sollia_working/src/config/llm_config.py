"""
Shared LLM Configuration for ALL Sollia Agents.

Hardened 2026-06-25 (final pipeline audit):
  * Thread-safe rate limiting (the previous global timestamp raced under the
    concurrent agent executor in sollia_web/backend/agents.py).
  * Jittered exponential backoff (1s -> 2s -> 4s -> 8s -> 16s, +/-25%).
  * Honors `Retry-After` / `retry-after-ms` headers from 429 responses.
  * Global RATE_LIMIT_COOLDOWN: any provider 429 pauses ALL calls briefly.
  * Raises a clear, typed error after the whole provider chain 429s 3x in a row
    (previously a hammering loop that surfaced "All LLM providers failed").
  * Records EVERY call (prompt + response + perf + retries) to
    .cache/llm_history/ for future fine-tuning (Part 6 of the audit spec).

Public surface preserved for callers: LLM_PROVIDERS, RATE_LIMIT, call_llm,
get_primary_model, verify_llm_client.
"""

import os
import re
import time
import json
import random
import hashlib
import threading
from datetime import datetime, timezone
from typing import Tuple, Dict, Optional, Any, List

import requests


# ---------------------------------------------------------------------------
# .env loader (dependency-free). Loads sollia_working/.env at import so the
# project is self-contained: a fresh shell (or one carrying a stale key) still
# picks up the authoritative keys. `.env` OVERRIDES the process env on purpose —
# the prior OPENROUTER_API_KEY in many shells was a DEAD provisioning key, so
# .env is the single source of truth for the project's keys.
# ---------------------------------------------------------------------------
def _load_dotenv() -> None:
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"
    )
    try:
        with open(env_path, "r") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    os.environ[key] = val  # .env is authoritative for the project
    except FileNotFoundError:
        pass
    except Exception:
        pass


_load_dotenv()

# ---------------------------------------------------------------------------
# LLM PROVIDER PRIORITY LIST
# All entries are OpenAI-compatible /chat/completions endpoints so a single
# request/parse path serves every provider. Free providers only (per project
# policy); paid keys may be present in the env but are not added here.
# `tier`: primary | fallback | backup  (informational, used by the dashboard)
# ---------------------------------------------------------------------------
# FINAL chain (2026-06-25 model-selection audit): 8 tiers, 3 INDEPENDENT providers
# (Groq, Google/Gemini, OpenRouter), all FREE — matches project "free providers only"
# policy. Selected by benchmarking the top OpenRouter models against the 5 REAL Sollia
# pipeline prompts (tools/model_benchmark.py). See FINAL_LLM_PRIORITY_LIST.md.
#
# KEY FINDING: the new OpenRouter key is a $0 free-tier key. Paid frontier models
# (opus-4.8, gpt-5.x, gemini-pro) are reachable but the free balance caps affordable
# OUTPUT to ~80 tokens — they cannot generate full pipeline JSON on this key. The
# `:free` OpenRouter models DO generate full-length output, so the OpenRouter tiers
# below use `:free` slugs (gemma-4-31b:free scored 100/100 on all 5 prompts).
LLM_PROVIDERS: List[Dict[str, Any]] = [
    # ── #1 PRIMARY — Claude Fable 5. GEOBLOCKED for US IPs: OpenRouter returns 404
    #    "Claude Fable 5 is not available" (an access restriction, NOT a wrong ID — the
    #    model is in the catalog). status="blocked" + auto_promote=True: call_llm skips
    #    it WITHOUT spending a request, but re-probes every FABLE_REPROBE_SECONDS. The
    #    instant the ban lifts it returns 200, flips to active, and serves as #1 — zero
    #    code change. THIS IS THE FUTURE #1 MODEL.
    {"name": "Fable5", "url": "https://openrouter.ai/api/v1/chat/completions",
     "model": "anthropic/claude-fable-5", "key_env": "OPENROUTER_API_KEY",
     "tier": "primary", "status": "blocked", "auto_promote": True},
    # ── #2 PRIMARY (ACTIVE) — Groq llama-3.3-70b. Free, fast (~300ms), deterministic;
    #    the proven workhorse that served every prior pipeline run.
    {"name": "Groq-70B", "url": "https://api.groq.com/openai/v1/chat/completions",
     "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY", "tier": "primary"},
    # ── #3 FALLBACK — Groq llama-3.1-8b. Fastest; same account as Groq-70B.
    {"name": "Groq-8B", "url": "https://api.groq.com/openai/v1/chat/completions",
     "model": "llama-3.1-8b-instant", "key_env": "GROQ_API_KEY", "tier": "fallback"},
    # ── #4 FALLBACK — Gemini 2.5 Flash. INDEPENDENT Google account = real resilience
    #    when Groq's single account rate-limits. OpenAI-compatible endpoint, ~1.3s.
    {"name": "Gemini-2.5-flash",
     "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
     "model": "gemini-2.5-flash", "key_env": "GEMINI_API_KEY", "tier": "fallback"},
    # ── #5 BACKUP — OpenRouter Gemma-4-31B (FREE). INDEPENDENT provider. Benchmarked
    #    100/100 quality on all 5 Sollia prompts (verbatim quotes + valid JSON at temp 0).
    {"name": "OR-Gemma4-31B", "url": "https://openrouter.ai/api/v1/chat/completions",
     "model": "google/gemma-4-31b-it:free", "key_env": "OPENROUTER_API_KEY", "tier": "backup"},
    # ── #6 BACKUP — OpenRouter Gemma-4-26B (FREE). Also benchmarked 100/100 — second
    #    free pick; kept adjacent to #5 as the strongest free pair.
    {"name": "OR-Gemma4-26B", "url": "https://openrouter.ai/api/v1/chat/completions",
     "model": "google/gemma-4-26b-a4b-it:free", "key_env": "OPENROUTER_API_KEY", "tier": "backup"},
    # ── #7 BACKUP — OpenRouter Nemotron-3-Super 120B (FREE). Partial (40/100: solid on
    #    test1/test3, weaker JSON on test2/4/5) but a large independent last-but-one tier.
    {"name": "OR-Nemotron3-Super", "url": "https://openrouter.ai/api/v1/chat/completions",
     "model": "nvidia/nemotron-3-super-120b-a12b:free", "key_env": "OPENROUTER_API_KEY", "tier": "backup"},
    # ── #8 BACKUP — OpenRouter llama-3.3-70b (FREE). A llama-70b instance INDEPENDENT of
    #    Groq's account — last-resort free tier.
    {"name": "OR-Llama33-70B", "url": "https://openrouter.ai/api/v1/chat/completions",
     "model": "meta-llama/llama-3.3-70b-instruct:free", "key_env": "OPENROUTER_API_KEY", "tier": "backup"},
]

# ---------------------------------------------------------------------------
# PER-AGENT MODEL ALLOCATION (PART 4c)
# ---------------------------------------------------------------------------
# The backend runs all four agents CONCURRENTLY (ThreadPoolExecutor). If every
# agent starts the chain at the same tier (Groq-70B, idx 1), Agent 2's
# high-volume Stage-1 batching exhausts that model's per-minute token budget and
# starves Agent 1's quality-sensitive classify calls -> the "All providers
# rate-limited" stall (a cold cache once took 227 s).
#
# There are 3 INDEPENDENT rate-limit domains in the chain:
#   * Groq account  -> idx 1 (llama-70B) and idx 2 (llama-8B) — SAME account, but
#     Groq enforces SEPARATE per-MODEL token/min, so 70B and 8B don't share TPM.
#   * Gemini account -> idx 3 (independent).
#   * OpenRouter     -> idx 4..7 (independent).
#
# Each agent therefore STARTS the chain at a tier matched to its need; on failure
# it still falls through the remaining tiers, so resilience is unchanged. This
# spreads concurrent load across all three domains instead of piling on one.
PROVIDER_IDX = {"FABLE": 0, "GROQ_70B": 1, "GROQ_8B": 2, "GEMINI": 3, "OPENROUTER": 4}
AGENT_START_TIER: Dict[str, int] = {
    # agent (or "agent:stage")          start idx  rationale
    "corporate_discrepancy": 1,   # classify: quality-sensitive, moderate volume -> Groq-70B
    "promise_audit:stage1":  2,   # HIGH VOLUME (many batches) -> Groq-8B (separate per-model TPM)
    "promise_audit:stage2":  1,   # single high-quality verdict pass -> Groq-70B
    "legal_proceedings":     3,   # low volume -> Gemini (fully independent of Groq)
    "risk_factors":          1,   # single high-quality call -> Groq-70B
    "constraint_auditor":    1,   # one sector-classification call -> Groq-70B (Agent 5)
    "moat_auditor":          1,   # competitor/moat classify + select + analyze -> Groq-70B (Agent 6)
}


def start_tier(name: str, default: int = 0) -> int:
    """Return the chain start index for an agent (or 'agent:stage'); see
    AGENT_START_TIER. default=0 reproduces the legacy 'start at the top' behavior
    (idx 0 = Fable5, which is skipped while blocked, so effectively Groq-70B)."""
    return AGENT_START_TIER.get(name, default)


# Providers marked status="blocked" are skipped without spending a request, then
# re-probed every FABLE_REPROBE_SECONDS so a geo-unban auto-promotes with no redeploy.
FABLE_REPROBE_SECONDS = float(os.environ.get("SOLLIA_FABLE_REPROBE", "1800"))
_blocked_probe: Dict[str, float] = {}   # provider name -> last re-probe epoch (guarded by _rate_lock)

# ---------------------------------------------------------------------------
# Rate limiting / retry tuning (Part 1c). All overridable via env for ops.
# ---------------------------------------------------------------------------
RATE_LIMIT = float(os.environ.get("SOLLIA_LLM_RATE_LIMIT", "0.3"))   # base inter-call spacing (was 0.15)
RETRY_DELAY = 1.0          # legacy constant kept for any external reference
MAX_RETRIES = 3            # legacy constant kept for any external reference
BACKOFF_BASE = 1.0         # exponential backoff base seconds
BACKOFF_MAX = 16.0         # exponential backoff ceiling seconds
BACKOFF_JITTER = 0.25      # +/- jitter fraction to avoid thundering herd
RATE_LIMIT_COOLDOWN = float(os.environ.get("SOLLIA_LLM_COOLDOWN", "30"))  # global pause after a 429
MAX_CHAIN_RETRIES = int(os.environ.get("SOLLIA_LLM_MAX_CHAIN_RETRIES", "3"))  # full-chain 429 passes before raising
REQUEST_TIMEOUT = int(os.environ.get("SOLLIA_LLM_TIMEOUT", "60"))
RETRY_AFTER_CAP = 60.0     # never honor an absurd Retry-After

# Approx public prices (USD per 1M tokens) for the cost dashboard only. Groq/
# OpenRouter-free dev usage is effectively $0; these let us estimate "what this
# WOULD cost at list price". Clearly an estimate, not a bill.
PRICING: Dict[str, Dict[str, float]] = {
    # Active chain. `:free` OpenRouter slugs and Groq dev usage are effectively $0;
    # list prices shown so the dashboard can estimate "what this WOULD cost".
    "anthropic/claude-fable-5":                 {"in": 5.00, "out": 25.00},  # est. (blocked)
    "llama-3.3-70b-versatile":                  {"in": 0.59, "out": 0.79},
    "llama-3.1-8b-instant":                     {"in": 0.05, "out": 0.08},
    "gemini-2.5-flash":                         {"in": 0.30, "out": 2.50},
    "google/gemma-4-31b-it:free":               {"in": 0.0,  "out": 0.0},
    "nvidia/nemotron-3-super-120b-a12b:free":   {"in": 0.0,  "out": 0.0},
    "google/gemma-4-26b-a4b-it:free":           {"in": 0.0,  "out": 0.0},
    "meta-llama/llama-3.3-70b-instruct:free":   {"in": 0.0,  "out": 0.0},
    "meta-llama/llama-3.1-70b-instruct":        {"in": 0.59, "out": 0.79},
}


def estimate_cost(model: str, usage: Optional[Dict[str, Any]]) -> float:
    """Estimate USD cost of a call from token usage + PRICING (0.0 if unknown/free)."""
    p = PRICING.get(model)
    if not p or not usage:
        return 0.0
    pin = (usage.get("prompt_tokens") or 0) * p["in"] / 1e6
    pout = (usage.get("completion_tokens") or 0) * p["out"] / 1e6
    return round(pin + pout, 6)

# ---------------------------------------------------------------------------
# Cache locations
# ---------------------------------------------------------------------------
_SOLLIA_WORKING = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LLM_CACHE_DIR = os.path.join(_SOLLIA_WORKING, ".llm_cache")            # byte-identical prompt cache
LLM_HISTORY_DIR = os.path.join(_SOLLIA_WORKING, ".cache", "llm_history")  # full training record
os.makedirs(LLM_CACHE_DIR, exist_ok=True)
os.makedirs(LLM_HISTORY_DIR, exist_ok=True)

# Force live calls (skip the byte-identical cache) without deleting anyone's
# cache — used for cold-cache rate-limit validation. Set SOLLIA_LLM_NO_CACHE=1.
NO_CACHE = os.environ.get("SOLLIA_LLM_NO_CACHE", "") not in ("", "0", "false", "False")
# Disable history recording entirely if ever needed (default: on).
HISTORY_ENABLED = os.environ.get("SOLLIA_LLM_HISTORY", "1") not in ("0", "false", "False")

# ---------------------------------------------------------------------------
# Thread-safe shared state
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()       # guards _last_request_time + _cooldown_until
_index_lock = threading.Lock()      # guards index.jsonl appends
_last_request_time = 0.0
_cooldown_until = 0.0
_ctx = threading.local()            # per-thread {agent, ticker, stage} attribution


# ===========================================================================
# Call-context attribution (so agents need ~zero edits to get good history)
# ===========================================================================
def set_call_context(agent: Optional[str] = None, ticker: Optional[str] = None,
                     stage: Optional[str] = None) -> None:
    """Set the current thread's LLM attribution context. Any field left None is
    preserved. The test harness / backend set this once per agent run."""
    cur = getattr(_ctx, "ctx", {"agent": None, "ticker": None, "stage": None})
    if agent is not None:
        cur["agent"] = agent
    if ticker is not None:
        cur["ticker"] = ticker
    if stage is not None:
        cur["stage"] = stage
    _ctx.ctx = cur


def clear_call_context() -> None:
    _ctx.ctx = {"agent": None, "ticker": None, "stage": None}


class call_context:
    """Context manager: `with call_context(agent='agent_2', ticker='NVDA', stage='stage1'): ...`"""
    def __init__(self, agent=None, ticker=None, stage=None):
        self._new = {"agent": agent, "ticker": ticker, "stage": stage}
        self._prev = None

    def __enter__(self):
        self._prev = getattr(_ctx, "ctx", None)
        base = dict(self._prev) if self._prev else {"agent": None, "ticker": None, "stage": None}
        for k, v in self._new.items():
            if v is not None:
                base[k] = v
        _ctx.ctx = base
        return self

    def __exit__(self, *exc):
        _ctx.ctx = self._prev
        return False


def _resolve_ctx(agent, stage, ticker):
    cur = getattr(_ctx, "ctx", None) or {}
    agent = agent or cur.get("agent") or "unknown"
    stage = stage or cur.get("stage") or "call"
    ticker = ticker or cur.get("ticker") or "unknown"
    return _safe(agent), _safe(stage), _safe(ticker)


def _safe(s: str) -> str:
    """Filesystem-safe path component."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(s))[:64] or "unknown"


# ===========================================================================
# Token / usage helpers
# ===========================================================================
def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) used only when the API does not
    return real usage numbers."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _classify_response(text: str) -> Dict[str, Any]:
    """Best-effort response shape info for the history record."""
    out = {"non_empty": bool(text and text.strip()), "is_json": False, "json": None}
    if not text:
        return out
    candidate = text.strip()
    # strip ```json ... ``` fences commonly emitted by llama models
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\n?", "", candidate)
        candidate = re.sub(r"\n?```$", "", candidate).strip()
    for opener, closer in (("[", "]"), ("{", "}")):
        if candidate.startswith(opener):
            try:
                out["json"] = json.loads(candidate[: candidate.rfind(closer) + 1])
                out["is_json"] = True
            except Exception:
                pass
            break
    return out


# ===========================================================================
# LLM history recording (Part 6)
# ===========================================================================
def _record_history(record: Dict[str, Any]) -> None:
    """Persist one immutable call record + append a one-line index entry.
    Never raises — history must not break an LLM call."""
    if not HISTORY_ENABLED:
        return
    try:
        agent = _safe(record["agent"])
        ticker = _safe(record["ticker"])
        folder = os.path.join(LLM_HISTORY_DIR, agent, ticker)
        os.makedirs(folder, exist_ok=True)
        fname = record["call_id"] + ".json"
        path = os.path.join(folder, fname)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(record, f, indent=2)
        os.replace(tmp, path)  # atomic
        # Append compact index line (JSONL is append-safe; rebuildable from files).
        idx = {
            "call_id": record["call_id"], "timestamp": record["timestamp"],
            "agent": record["agent"], "stage": record["stage"], "ticker": record["ticker"],
            "provider": record["provider"], "model": record["model"],
            "cached": record["response"].get("cached", False),
            "prompt_tokens": record["prompt"]["token_count"],
            "response_tokens": record["response"]["token_count"],
            "total_time_ms": record["performance"]["total_time_ms"],
            "attempts": record["retry_info"]["total_attempts"],
            "parse_success": record["response"]["parse_success"],
            "path": os.path.relpath(path, LLM_HISTORY_DIR),
        }
        with _index_lock:
            with open(os.path.join(LLM_HISTORY_DIR, "index.jsonl"), "a") as f:
                f.write(json.dumps(idx) + "\n")
    except Exception:
        pass  # history is best-effort


def _build_record(call_id, agent, stage, ticker, provider, model, temperature,
                  max_tokens, prompt, response_text, usage, cached, total_ms,
                  attempt, total_attempts, prev_errors) -> Dict[str, Any]:
    shape = _classify_response(response_text)
    p_tokens = (usage or {}).get("prompt_tokens") or estimate_tokens(prompt)
    r_tokens = (usage or {}).get("completion_tokens") or estimate_tokens(response_text)
    tps = round(r_tokens / (total_ms / 1000.0), 1) if total_ms and r_tokens else 0.0
    return {
        "call_id": call_id,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "agent": agent, "stage": stage, "ticker": ticker,
        "provider": provider, "model": model,
        "temperature": temperature, "max_tokens": max_tokens,
        "prompt": {
            "full_text": prompt,
            "token_count": p_tokens,
            "word_count": len(prompt.split()),
            "char_count": len(prompt),
            "sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        },
        "response": {
            "full_text": response_text,
            "token_count": r_tokens,
            "cached": cached,
            "parse_success": shape["non_empty"],
            "is_json": shape["is_json"],
            "raw_json": shape["json"],
        },
        "usage": usage or {},
        "performance": {
            "time_to_first_token_ms": None,  # non-streaming requests; not measured
            "total_time_ms": round(total_ms, 1),
            "tokens_per_second": tps,
        },
        "retry_info": {
            "attempt": attempt,
            "total_attempts": total_attempts,
            "previous_errors": prev_errors,
        },
        "cache_key": "sha256:" + hashlib.sha256((prompt + "|" + model).encode()).hexdigest(),
    }


# ===========================================================================
# Rate-limit gate, cooldown, backoff (Part 1c)
# ===========================================================================
def _rate_gate() -> None:
    """Block until it is safe to issue the next request: honors the global 429
    cooldown, then enforces minimum inter-call spacing. Thread-safe."""
    global _last_request_time
    with _rate_lock:
        now = time.time()
        if now < _cooldown_until:
            time.sleep(_cooldown_until - now)  # deliberate: global pause, all callers wait
            now = time.time()
        elapsed = now - _last_request_time
        if elapsed < RATE_LIMIT:
            time.sleep(RATE_LIMIT - elapsed)
        _last_request_time = time.time()


def _trigger_cooldown(retry_after: Optional[float]) -> None:
    """A provider returned 429 -> pause ALL calls. Honors Retry-After if larger."""
    global _cooldown_until
    dur = RATE_LIMIT_COOLDOWN
    if retry_after:
        dur = max(dur, min(retry_after, RETRY_AFTER_CAP))
    with _rate_lock:
        _cooldown_until = max(_cooldown_until, time.time() + dur)


def _parse_retry_after(resp) -> Optional[float]:
    """Parse a wait hint (in SECONDS) from a 429 response. Handles, in order:
    `retry-after-ms` (milliseconds), `Retry-After` (seconds), Groq's
    `x-ratelimit-reset-*` reset duration, and a "try again in 12.5s" body."""
    try:
        h = resp.headers
        ms = h.get("retry-after-ms")
        if ms:
            m = re.search(r"[\d.]+", str(ms))
            if m:
                return float(m.group()) / 1000.0   # header is milliseconds by definition
        ra = h.get("Retry-After")
        if ra:
            m = re.search(r"[\d.]+", str(ra))
            if m:
                return float(m.group())
        reset = h.get("x-ratelimit-reset-tokens") or h.get("x-ratelimit-reset-requests")
        if reset:
            m = re.search(r"([\d.]+)", str(reset))   # e.g. "2.5s"
            if m:
                return float(m.group(1))
        body = resp.text or ""
        m = re.search(r"in\s+([\d.]+)\s*s", body)     # "try again in 12.5s"
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def _backoff_seconds(chain_pass: int, retry_after: Optional[float]) -> float:
    """Jittered exponential backoff for chain pass N, never below Retry-After."""
    base = min(BACKOFF_BASE * (2 ** chain_pass), BACKOFF_MAX)
    jittered = base * (1 + random.uniform(-BACKOFF_JITTER, BACKOFF_JITTER))
    if retry_after:
        jittered = max(jittered, min(retry_after, RETRY_AFTER_CAP))
    return max(0.0, jittered)


class LLMError(RuntimeError):
    """Raised when the whole provider chain is exhausted."""


class LLMRateLimited(LLMError):
    """Raised when every provider 429s for MAX_CHAIN_RETRIES consecutive passes."""


# ===========================================================================
# Main entry point
# ===========================================================================
def call_llm(prompt: str, max_tokens: int = 400, temperature: float = 0.0,
             provider_idx: int = 0, agent: Optional[str] = None,
             stage: Optional[str] = None, ticker: Optional[str] = None) -> Tuple[str, Dict]:
    """Call the LLM provider chain with hardened retry + full history recording.

    Backwards compatible: returns (response_text, meta). New optional kwargs
    (agent/stage/ticker) attribute the call in .cache/llm_history/; if omitted
    they fall back to the thread-local call_context().
    """
    r_agent, r_stage, r_ticker = _resolve_ctx(agent, stage, ticker)
    call_id = "%s_%s_%s_%s" % (
        r_agent, r_ticker, r_stage,
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f") + "Z",
    )

    cache_key = hashlib.sha256(f"{prompt}_{max_tokens}_{temperature}".encode()).hexdigest()[:16]
    cache_path = os.path.join(LLM_CACHE_DIR, f"{cache_key}.json")

    # ---- byte-identical cache (skipped under NO_CACHE) ----
    if not NO_CACHE and os.path.exists(cache_path):
        try:
            with open(cache_path, "r") as f:
                cached = json.load(f)
            rec = _build_record(call_id, r_agent, r_stage, r_ticker, "cache",
                                 cached.get("model", "unknown"), temperature, max_tokens,
                                 prompt, cached["response"], None, True, 0.0, 0, 0, [])
            _record_history(rec)
            return cached["response"], {"cached": True, "provider": cached.get("provider", "unknown")}
        except Exception:
            pass

    prev_errors: List[str] = []
    consecutive_chain_429 = 0
    total_attempts = 0

    for chain_pass in range(MAX_CHAIN_RETRIES):
        pass_had_429 = False
        pass_retry_after: Optional[float] = None

        for idx in range(provider_idx, len(LLM_PROVIDERS)):
            provider = LLM_PROVIDERS[idx]
            api_key = os.environ.get(provider["key_env"], "")
            if not api_key:
                continue

            # Skip a geoblocked provider (e.g. Fable 5) WITHOUT spending a request,
            # but re-probe every FABLE_REPROBE_SECONDS so a geo-unban auto-promotes.
            if provider.get("status") == "blocked":
                with _rate_lock:
                    last = _blocked_probe.get(provider["name"], 0.0)
                    due = (time.time() - last) >= FABLE_REPROBE_SECONDS
                    if due:
                        _blocked_probe[provider["name"]] = time.time()
                if not due:
                    continue  # still believed blocked — skip silently

            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
            if "openrouter.ai" in provider["url"]:
                headers["HTTP-Referer"] = "https://sollia.app"
                headers["X-Title"] = "Sollia"
            payload = {"model": provider["model"],
                       "messages": [{"role": "user", "content": prompt}],
                       "temperature": temperature, "max_tokens": max_tokens}

            _rate_gate()
            total_attempts += 1
            t0 = time.time()
            try:
                resp = requests.post(provider["url"], headers=headers, json=payload,
                                     timeout=REQUEST_TIMEOUT)
            except Exception as exc:  # network/timeout -> try next provider
                prev_errors.append(f"{provider['name']}: {type(exc).__name__}: {str(exc)[:120]}")
                continue

            if resp.status_code == 200:
                data = resp.json()
                try:
                    result = data["choices"][0]["message"]["content"].strip()
                except (KeyError, IndexError, TypeError):
                    prev_errors.append(f"{provider['name']}: malformed 200 (no choices/content)")
                    continue
                if not result:
                    # e.g. a reasoning model that spent the whole budget thinking —
                    # an empty 200 must NOT count as success; fall through to next provider.
                    prev_errors.append(f"{provider['name']}: empty 200 response")
                    continue
                usage = data.get("usage")
                total_ms = (time.time() - t0) * 1000.0
                # AUTO-PROMOTE: a blocked provider that just returned 200 is no longer
                # blocked (e.g. Fable 5 geo-unbanned) — flip it active for this process
                # so it serves as #1 from now on with zero redeploy.
                if provider.get("status") == "blocked":
                    provider["status"] = "active"
                # persist byte-identical cache
                try:
                    with open(cache_path, "w") as f:
                        json.dump({"response": result, "provider": provider["name"],
                                   "model": provider["model"]}, f)
                except Exception:
                    pass
                rec = _build_record(call_id, r_agent, r_stage, r_ticker, provider["name"],
                                    provider["model"], temperature, max_tokens, prompt,
                                    result, usage, False, total_ms,
                                    chain_pass + 1, total_attempts, prev_errors)
                _record_history(rec)
                return result, {"cached": False, "provider": provider["name"],
                                "model": provider["model"], "retries": chain_pass,
                                "attempts": total_attempts,
                                "cost_usd": estimate_cost(provider["model"], usage)}

            if resp.status_code == 429:
                pass_had_429 = True
                ra = _parse_retry_after(resp)
                if ra:
                    pass_retry_after = max(pass_retry_after or 0.0, ra)
                prev_errors.append(f"{provider['name']}: 429 (retry_after={ra})")
                continue  # try next provider in the chain immediately

            # other non-200 (4xx/5xx): record and try next provider
            prev_errors.append(f"{provider['name']}: HTTP {resp.status_code}: {resp.text[:120]}")

        # ---- end of one full pass over the chain ----
        if pass_had_429:
            consecutive_chain_429 += 1
            _trigger_cooldown(pass_retry_after)
            if consecutive_chain_429 >= MAX_CHAIN_RETRIES:
                break
            time.sleep(_backoff_seconds(chain_pass, pass_retry_after))
        else:
            # the chain failed for non-rate-limit reasons; another pass won't help
            break

    # ---- exhausted ----
    rec = _build_record(call_id, r_agent, r_stage, r_ticker, "FAILED", "none",
                        temperature, max_tokens, prompt, "", None, False, 0.0,
                        chain_pass + 1, total_attempts, prev_errors)
    _record_history(rec)
    summary = " | ".join(prev_errors[-4:]) if prev_errors else "no providers had API keys"
    if consecutive_chain_429 >= MAX_CHAIN_RETRIES:
        raise LLMRateLimited(
            f"All LLM providers rate-limited (429) for {MAX_CHAIN_RETRIES} consecutive "
            f"passes. Last errors: {summary}")
    raise LLMError(f"All LLM providers failed. Last errors: {summary}")


def get_primary_model() -> str:
    return LLM_PROVIDERS[0]["model"]


def verify_llm_client(fatal: bool = False) -> bool:
    """Startup verification that the LLM provider chain is reachable (the 'OK'
    test). Returns True when a provider replies with 'OK'."""
    try:
        text, meta = call_llm("Respond with exactly: OK", max_tokens=5, temperature=0.0,
                              agent="system", stage="healthcheck", ticker="none")
        if "OK" not in (text or ""):
            raise RuntimeError(f"unexpected LLM response: {text!r}")
        return True
    except Exception as exc:  # noqa: BLE001
        if fatal:
            raise RuntimeError(f"LLM client failed initialization: {exc}") from exc
        return False
