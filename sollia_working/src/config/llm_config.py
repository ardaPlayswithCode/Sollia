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
# request/parse path serves every provider. Tier 1 (Fable 5) is PAID and
# reserved for the hardest analytical tasks; tiers 2-8 remain free.
# `tier`: primary | fallback | backup  (informational, used by the dashboard)
# ---------------------------------------------------------------------------
# FINAL chain (2026-06-25 model-selection audit, Fable 5 activated 2026-07-01):
# 8 tiers, 3 INDEPENDENT providers (Groq, Google/Gemini, OpenRouter). Tiers 2-8 are
# free; tier 1 (Fable 5) bills the funded OpenRouter key and is reserved for the
# hardest analytical tasks via AGENT_START_TIER. Selected by benchmarking against
# the 5 REAL Sollia pipeline prompts (tools/model_benchmark.py).
#
# MULTI-KEY REALITY (2026-07-03 empirical audit — LLM_ROUTING_REPORT.md):
# All 11 Groq keys belong to ONE organization and Groq enforces limits at the
# ORG level (verified: 3 keys hammered in parallel 429 together; one key alone
# gets the same 30 RPM). All 8 OpenRouter keys belong to ONE account (identical
# credits/usage) and OpenRouter limits are per-ACCOUNT. So key rotation buys
# ZERO extra throughput today — it exists for (a) resilience if a key is
# revoked, and (b) automatic N× scaling the day the keys are re-issued from
# separate accounts. _KEY_429_CUTOFF stops the doomed try-every-key cascade.
# Measured org limits: 70B = 30 RPM / 1k RPD / 12k TPM / 100k TPD;
# 8B = 30 RPM / 14.4k RPD / 6k TPM / 500k TPD (per-MODEL buckets, so the two
# Groq tiers add up to ~18k TPM). OpenRouter :free = 20 RPM + 1000 req/DAY
# account-wide (the funded account's "high-balance" tier); paid models have no
# daily cap and every key can bill the one funded balance.
_GROQ_KEYS = ["GROQ_API_KEY"] + [f"GROQ_API_KEY_{i}" for i in range(2, 12)]
_OPENROUTER_KEYS = ["OPENROUTER_API_KEY"] + [
    f"OPENROUTER_API_KEY_{i}" for i in (2, 3, 4, 5, 6, 8, 9)  # no _7 (never issued)
]
LLM_PROVIDERS: List[Dict[str, Any]] = [
    # ── #1 PRIMARY — Claude Fable 5 (ACTIVE 2026-07-01; the former geoblock is lifted,
    #    OpenRouter resolves it to anthropic/claude-5-fable-20260609). Reasoning is
    #    MANDATORY on this endpoint and counts against max_tokens; the reasoning
    #    budget is now PROPORTIONAL to prompt complexity (fable5_effort_level) and
    #    the same amount pads max_tokens so the caller's output length survives the
    #    thinking phase. `billed: True` = this tier spends real money: it is skipped
    #    once the per-run cost cap (MAX_COST_PER_RUN_USD) is reached, and the
    #    adaptive router (select_tier_for_prompt) keeps simple prompts off it
    #    entirely. Empty-content 200s (budget spent thinking) fall through to the
    #    next tier. Paid: $10/1M in, $50/1M out.
    {"name": "Fable5", "url": "https://openrouter.ai/api/v1/chat/completions",
     "model": "anthropic/claude-fable-5", "key_env": "OPENROUTER_API_KEY",
     "tier": "primary", "adaptive_reasoning": True, "billed": True,
     "timeout": 180, "key_envs": _OPENROUTER_KEYS},
    # ── #2 PRIMARY (ACTIVE) — Groq llama-3.3-70b. Free, fast (~300ms), deterministic;
    #    the proven workhorse that served every prior pipeline run. All 11 keys are
    #    wired but share ONE org bucket (see MULTI-KEY REALITY above): rotation is
    #    for key-revocation resilience, not throughput, until the keys span accounts.
    {"name": "Groq-70B", "url": "https://api.groq.com/openai/v1/chat/completions",
     "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY", "tier": "primary",
     "key_envs": _GROQ_KEYS},
    # ── #3 FALLBACK — Groq llama-3.1-8b. Fastest; same account as Groq-70B but a
    #    SEPARATE per-model TPM bucket (6k), so 70B+8B together ≈ 18k TPM.
    {"name": "Groq-8B", "url": "https://api.groq.com/openai/v1/chat/completions",
     "model": "llama-3.1-8b-instant", "key_env": "GROQ_API_KEY", "tier": "fallback",
     "key_envs": _GROQ_KEYS},
    # ── #4 FALLBACK — Gemini 2.5 Flash. INDEPENDENT Google account = real resilience
    #    when Groq's single account rate-limits. OpenAI-compatible endpoint, ~1.3s.
    #    payload_extra (2026-07-03 JSON-format audit): 2.5-flash THINKS by default
    #    and thinking bills against max_tokens — measured finish=length at ~400
    #    chars on a 2500-token request, i.e. every long JSON truncated mid-string.
    #    reasoning_effort "none" disables thinking per Google's OpenAI-compat docs
    #    (verify when the free quota resets; a rejected param would 400 and fall
    #    through the chain, which still beats parsing truncated garbage).
    {"name": "Gemini-2.5-flash",
     "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
     "model": "gemini-2.5-flash", "key_env": "GEMINI_API_KEY", "tier": "fallback",
     "payload_extra": {"reasoning_effort": "none"}},
    # ── #5 BACKUP — OpenRouter Gemma-4-31B (FREE). INDEPENDENT provider. Benchmarked
    #    100/100 quality on all 5 Sollia prompts (verbatim quotes + valid JSON at temp 0).
    {"name": "OR-Gemma4-31B", "url": "https://openrouter.ai/api/v1/chat/completions",
     "model": "google/gemma-4-31b-it:free", "key_env": "OPENROUTER_API_KEY", "tier": "backup",
     "key_envs": _OPENROUTER_KEYS},
    # ── #6 BACKUP — OpenRouter Gemma-4-26B (FREE). Also benchmarked 100/100 — second
    #    free pick; kept adjacent to #5 as the strongest free pair.
    {"name": "OR-Gemma4-26B", "url": "https://openrouter.ai/api/v1/chat/completions",
     "model": "google/gemma-4-26b-a4b-it:free", "key_env": "OPENROUTER_API_KEY", "tier": "backup",
     "key_envs": _OPENROUTER_KEYS},
    # ── #7 BACKUP — OpenRouter Nemotron-3-Super 120B (FREE). Partial (40/100: solid on
    #    test1/test3, weaker JSON on test2/4/5) but a large independent last-but-one tier.
    #    json_weak (2026-07-03 Part-2d burst test): on the Agent-4 strict-JSON shape it
    #    returned prose ("We need to compare…"), a truncated string, and empty [] — 0/5
    #    useful. Strict-JSON calls skip it; free-form calls still use it.
    {"name": "OR-Nemotron3-Super", "url": "https://openrouter.ai/api/v1/chat/completions",
     "model": "nvidia/nemotron-3-super-120b-a12b:free", "key_env": "OPENROUTER_API_KEY", "tier": "backup",
     "key_envs": _OPENROUTER_KEYS, "json_weak": True},
    # ── #8 BACKUP — OpenRouter llama-3.3-70b (FREE). A llama-70b instance INDEPENDENT of
    #    Groq's account — last-resort free tier.
    {"name": "OR-Llama33-70B", "url": "https://openrouter.ai/api/v1/chat/completions",
     "model": "meta-llama/llama-3.3-70b-instruct:free", "key_env": "OPENROUTER_API_KEY", "tier": "backup",
     "key_envs": _OPENROUTER_KEYS},
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
# Fable 5 (idx 0) is paid — route it the HARDEST analytical tasks (cross-document
# reasoning, evidence matching, legal reading, long-document comparison, moat
# verdicts); keep high-volume batching on free Groq tiers. Every agent still falls
# through the full chain on failure, so resilience is unchanged.
AGENT_START_TIER: Dict[str, int] = {
    # agent (or "agent:stage")          start idx  rationale
    # NOTE (2026-07-01 cost audit): these are the agents' NOMINAL tiers. The
    # adaptive router (select_tier_for_prompt) can only DEMOTE a call to a
    # cheaper tier when the actual prompt is simple — it never promotes a cheap
    # agent to the paid tier. Values below reflect MEASURED prompt sizes from
    # .cache/llm_history (600+ recorded calls), not assumed ones:
    #   * corporate_discrepancy classify batches measure ~1,030 words median
    #     (763-1456) -> the router sends them to Groq-70B; only a genuinely long
    #     cross-document batch (>=2000 words) ever bills Fable 5.
    #   * legal_proceedings prompts measure ~2,400 words -> Groq-70B handles the
    #     nuance fine (per-agent routing table, 2026-07-01); starts at idx 1.
    #   * constraint_auditor classify is ~290 tokens -> trivial -> Groq-8B.
    #   * moat_auditor stages measure 250-1,600 words (classify_moats up to ~6k)
    #     -> Groq-70B; analysis prompts (~253 words median) demote further.
    # COST RECONCILIATION (2026-07-02): the Part-1b routing table nominally put
    # promise_audit:stage2 and risk_factors on Fable 5 ("needs the context").
    # Measured, that is INCOMPATIBLE with the hard <$0.50/run ceiling: risk_factors
    # issues TWO ~4,500-word calls/run (10-K delta + 10-Q phase) at ~$0.30 each
    # (~$0.60), and stage2 fires several ≥2,000-word evidence calls/run — either
    # alone blows the budget. Cost control is THIS pass's overriding goal, so the
    # ceiling wins: both route to Groq-70B, which has a 128k context (ample for a
    # 4,500-word prompt) and benchmarked 100/100 on the 5 real Sollia prompts.
    # Fable 5 is reserved for ONE low-volume, highest-value judgment call: Agent 1's
    # severity audit (pin_tier, ~$0.037/run). Result: full cold pipeline ≈ $0.04.
    "corporate_discrepancy": 1,   # classify batches are strict-JSON -> Groq-70B anyway.
                                  #   Was 0 (nominal Fable 5): with the 2026-07-03 policy,
                                  #   Fable 5 is reachable ONLY via pin_tier (the severity
                                  #   audit) or the >90k-word long-context promotion — a
                                  #   nominal tier-0 gave unflagged >=2000-word prompts a
                                  #   third, unintended path onto the paid tier.
    "promise_audit:stage1":  2,   # HIGH VOLUME (many batches) -> Groq-8B (separate per-model TPM)
    "promise_audit:stage2":  1,   # evidence matching -> Groq-70B (128k ctx; was Fable 5, cost)
    "legal_proceedings":     1,   # medium complexity, free -> Groq-70B (was Fable 5)
    "risk_factors":          1,   # 2 long calls/run -> Groq-70B (was Fable 5, cost — see above)
    "constraint_auditor":    2,   # trivial sector selection (~290 tokens) -> Groq-8B (was Fable 5)
    "moat_auditor":          1,   # medium classify/select prompts -> Groq-70B (was Fable 5)
}


def start_tier(name: str, default: int = 0) -> int:
    """Return the chain start index for an agent (or 'agent:stage'); see
    AGENT_START_TIER. default=0 starts at the top of the chain (idx 0 = Fable 5)."""
    return AGENT_START_TIER.get(name, default)


# ---------------------------------------------------------------------------
# ADAPTIVE MODEL ROUTING (2026-07-01 cost optimization, Part 1b)
# ---------------------------------------------------------------------------
# The paid tier (Fable 5) was burning ~$1.80 per cold run because EVERY call
# from a tier-0 agent hit it, including ~1,000-word classify batches a free
# 70B model handles identically. select_tier_for_prompt() is applied inside
# call_llm to every live call: AGENT_START_TIER is a SUGGESTION and the
# complexity check overrides it — in the CHEAP direction only. A simple prompt
# can never bill the paid tier; a cheap agent can never be promoted into it
# (that would let one oversized document, or an attacker, force paid calls).
# Tier indices: larger = cheaper/further down the chain, so "demote" = max().
def select_tier_for_prompt(prompt: str, default_tier: int = 0) -> int:
    """Route simple prompts to free tiers regardless of the agent's nominal tier.

    <500 words  -> at least Groq-8B (trivial classification / selection)
    <2000 words -> at least Groq-70B (standard extraction / analysis)
    >=2000 words-> the agent's nominal tier (Fable 5 for reasoning agents:
                   cross-document matching, long risk comparisons need the
                   context + reasoning quality).
    """
    wc = len(prompt.split())
    if wc < 500:
        return max(default_tier, PROVIDER_IDX["GROQ_8B"])
    if wc < 2000:
        return max(default_tier, PROVIDER_IDX["GROQ_70B"])
    return default_tier


# ---------------------------------------------------------------------------
# DYNAMIC ROUTER (production polish Part 1c) — empirically calibrated.
# ---------------------------------------------------------------------------
# MODEL_PROFILES holds the MEASURED behavior of the chain tiers against the 3
# REAL recorded pipeline prompts (tools/routing_benchmark.py, 2026-07-03):
#   A = Agent 4 risk comparison (2,832w, strict JSON array)
#   B = Agent 1 discrepancy classify (1,178w, strict JSON + sentence indices)
#   C = Agent 2 Stage-1 promise extraction (685w, category allowlist)
# clean = raw response parses as JSON; recoverable = extract_json() parses it;
# valid = task-specific schema/index/category checks pass.
# The router's decisions DERIVE from these numbers — see select_best_tier.
MODEL_PROFILES: Dict[int, Dict[str, Any]] = {
    0: {  # Fable 5 (paid, via OpenRouter; reasoning mandatory on this endpoint)
        "clean_json_rate": {"A": 0.0, "B": 1.0, "C": 0.0},
        "recoverable_rate": {"A": 0.33, "B": 1.0, "C": 1.0},
        "valid_rate": {"A": 0.0, "B": 1.0, "C": 1.0},
        "avg_latency_ms": {"A": 41477, "B": 7946, "C": 16946},
        "deterministic": {"A": False, "B": True, "C": False},
        "cost_per_call_usd": "~0.20 on a 4.5k-word call ($10/$50 per 1M)",
        "max_context": 200_000,
        "best_for": ["short/medium strict-JSON judgment calls (severity audit)",
                      ">90k-word overflow beyond Groq's 128k context"],
        "notes": "On the LONG strict-JSON shape (test A) it returned one fenced "
                 "response, one EMPTY (reasoning starved content) and one "
                 "truncated — unusable for Agent 4 despite the original plan. "
                 "Perfect and byte-deterministic on classify-with-indices (B).",
    },
    1: {  # Groq llama-3.3-70b (free) — THE workhorse
        "clean_json_rate": {"A": 1.0, "B": 1.0, "C": 0.0},
        "recoverable_rate": {"A": 1.0, "B": 1.0, "C": 1.0},
        "valid_rate": {"A": 1.0, "B": 1.0, "C": 1.0},
        "avg_latency_ms": {"A": 3887, "B": 964, "C": 1469},
        "deterministic": {"A": False, "B": False, "C": False},
        "max_context": 128_000, "tpm_free_tier": 12_000,
        "best_for": ["strict-JSON comparison (Agent 4: 3/3 clean+valid at ~4s "
                      "— vs Fable 5's 0/3 at 41s)", "classify with indices",
                      "medium/long extraction"],
        "notes": "12k tokens/min free TPM = ONE jumbo call per minute — the "
                 "TSLA 'Could not parse' failures were 429 fallthrough to "
                 "weaker tiers, not 70B quality. The LLM semaphore + key "
                 "cycling keep it inside its TPM window.",
    },
    2: {  # Groq llama-3.1-8b (free, fastest)
        "clean_json_rate": {"A": 1.0, "B": 0.0, "C": 0.0},
        "recoverable_rate": {"A": 1.0, "B": 0.0, "C": 1.0},
        "valid_rate": {"A": 1.0, "B": 0.0, "C": 1.0},
        "avg_latency_ms": {"A": 2407, "B": 1469, "C": 1270},
        "max_context": 128_000, "tpm_free_tier": 6_000,
        "best_for": ["trivial/high-volume classification (Stage 1, Agent 5)"],
        "notes": "Answers sentence-index tasks with PROSE (0/3 on B) — never "
                 "route classify there. Fine on category extraction (C 3/3).",
    },
    3: {  # Gemini 2.5 Flash (free, independent account) — fallback
        "recoverable_rate": {"A": 0.0},
        "avg_latency_ms": {"A": 11040},
        "notes": "Independent 429-refuge, not a primary: its 2 live test-A "
                 "runs truncated mid-array (unrecoverable), and the free "
                 "daily quota was consumed by pipeline fallbacks during "
                 "the 2026-07-03 window.",
    },
    # 4-7: OpenRouter :free backups. Accounts under $10 balance get 50
    # requests/DAY across ALL :free models — they are last-resort refuges by
    # construction. Quality (Gemma pair 100/100) was established in the
    # 2026-06-25 benchmark (FINAL_LLM_PRIORITY_LIST.md).
}

# ---------------------------------------------------------------------------
# RELIABILITY UNDER STRESS (network-condition pass, 2026-07-03).
# Chain-level behaviors were verified against the REAL chain code with local
# mock providers (tools/network_condition_test.py, 9/9): 429-storm key-cycling
# + cutoff, provider-down/404 fallthrough, 5s-latency served without tripping
# the guard, slow-trickle bounded at timeout+slack, hang memo + reroute.
# Per-provider REAL stress evidence backing the routing decisions:
STRESS_PROFILES: Dict[str, str] = {
    "Fable5": "transport = OpenRouter/Cloudflare — the 2026-07-02 13-minute "
              "production stall lived here. Wall-clock guard bounds it; hang "
              "memo clamps its next timeout and drops its >90k promotion. "
              "Reliability-weighted: pinned severity audit + guarded overflow "
              "ONLY (a 99%-clean model that can stall loses to a 95%-clean "
              "one that always answers).",
    "Groq-70B": "org-level 30 RPM/12k TPM 429 storms measured (hammer test): "
                "keys 429 together; cutoff=2 avoids doomed cycling; recovers "
                "next TPM window. Never observed hanging (~4s worst case). "
                "Highest reliability-under-stress -> stays THE strict-JSON "
                "primary.",
    "Groq-8B": "same org bucket, separate 6k-TPM model bucket. Survived the "
               "Part-2d saturation burst serving valid A4 JSON. Reliable "
               "high-volume fallback.",
    "Gemini-2.5-flash": "independent account = real 429 refuge, but daily "
                        "quota exhausts under sustained fallthrough (measured "
                        "twice). Thinking-starvation truncation FIXED "
                        "(reasoning_effort:none, verified live 5/5 clean).",
    "OR-Gemma4-31B": "served clean JSON under the Part-2d saturation burst; "
                     "subject to account-shared :free limits + upstream "
                     "capacity 429s.",
    "OR-Gemma4-26B": "upstream capacity exhausted for HOURS on 2026-07-03 "
                     "(third :free limit layer — provider-side, all-users). "
                     "Do not rely on availability.",
    "OR-Nemotron3-Super": "json_weak: prose/truncation/empty on strict shapes "
                          "(0/5 useful) -> skipped for requires_strict_json "
                          "even on 429 fallthrough.",
    "OR-Llama33-70B": "clean when reachable; one transient 402 observed "
                      "(key-cycling now advances past 401/402/403).",
}


def select_best_tier(prompt_word_count: int,
                     requires_strict_json: bool = False,
                     requires_long_context: bool = False,
                     max_cost_cents: float = 40.0) -> int:
    """Route to the BEST tier for the task, derived from MODEL_PROFILES.

    Decision logic (ordered by priority; empirical justification inline):

    1. requires_strict_json AND prompt > 4000 words -> Groq-70B (tier 1).
       The original plan sent these to Fable 5, but measured behavior on the
       REAL Agent-4 prompt was 0/3 clean JSON (one empty response — the
       reasoning budget starved the completion — one truncated) at 41s/call.
       Groq-70B + the shared tolerant extractor is the reliable pairing; its
       128k context fits any strict-JSON prompt this pipeline builds.
    2. requires_long_context -> Groq-70B up to ~90k words (fits 128k with
       output margin); ONLY beyond that is Fable 5's 200k context genuinely
       needed (cost-gated; the per-run cap still applies).
    3. prompt > 1500 words -> Groq-70B: fast, free, measured 100/100 quality.
    4. otherwise -> Groq-8B: fastest; valid 1.0 on the Stage-1 shape.

    Fable 5's measured sweet spot — short/medium strict judgment calls (test
    B: clean 1.0, valid 1.0, byte-deterministic) — is reached via pin_tier
    (Agent 1's severity audit), a deliberate opt-in, not a size rule.
    Tiers 3-7 stay BACKUP-ONLY — the chain falls through on 429/failure.

    RELIABILITY UNDER STRESS (network-condition pass, 2026-07-03): clean-JSON
    rate alone is not fitness. The weighting applied here and in the chain:
      * a provider that recently HUNG past the wall-clock guard loses its
        >90k-word promotion (Groq-70B takes the call; a truncated-but-served
        answer beats a 180s stall — the 2026-07-02 production hang was
        OpenRouter/Cloudflare, i.e. exactly Fable 5's transport);
      * `json_weak` providers (Nemotron: prose/empty on strict shapes) are
        skipped for requires_strict_json calls even on 429 fallthrough;
      * hung providers also get a clamped read timeout (HUNG_PROVIDER_TIMEOUT)
        so a repeat stall costs ~35s, not minutes.
    """
    if requires_long_context and prompt_word_count > 90_000:
        if _recently_hung("Fable5"):
            return PROVIDER_IDX["GROQ_70B"]  # hang memo: don't promote into a stall
        if _projected_fable5_cents(prompt_word_count) <= max_cost_cents * 10:
            return PROVIDER_IDX["FABLE"]  # beyond Groq's 128k window
        return PROVIDER_IDX["GROQ_70B"]
    if requires_strict_json or requires_long_context:
        # Strict tasks NEVER go below Groq-70B: Groq-8B measured 0/3 on the
        # sentence-index classify shape (MODEL_PROFILES[2], test B).
        return PROVIDER_IDX["GROQ_70B"]
    if prompt_word_count > 1500:
        return PROVIDER_IDX["GROQ_70B"]
    return PROVIDER_IDX["GROQ_8B"]


def _projected_fable5_cents(prompt_word_count: int) -> float:
    """Projected Fable 5 cost for one call, in cents: $10/1M input tokens +
    $50/1M output tokens (reasoning bills as output; use its size budget as the
    output proxy — actual completions are shorter than the reasoning budget)."""
    in_tokens = prompt_word_count * 1.35
    out_tokens = max(fable5_effort_level(prompt_word_count), FABLE5_MIN_REASONING) + 800
    return (in_tokens * 0.001 + out_tokens * 0.005)


# Part 1c — Fable 5 reasoning budget proportional to task complexity. Reasoning
# tokens are billed as OUTPUT tokens ($50/1M), so a flat budget overpays on
# short prompts. The returned budget is sent as reasoning:{max_tokens:N} and
# ALSO added to max_tokens (reasoning counts against it on this endpoint).
# NOTE: the Anthropic backend enforces a minimum thinking budget of 1024 —
# OpenRouter rejects smaller values — so tiers below that are clamped at call
# time (the <1000-word branch is unreachable in practice anyway: the adaptive
# router keeps sub-2000-word prompts off Fable 5 entirely).
FABLE5_MIN_REASONING = 1024

def fable5_effort_level(prompt_word_count: int) -> int:
    """Return the Fable 5 reasoning-token budget for a prompt of this size."""
    if prompt_word_count < 1000:
        return 200    # light reasoning for simple classification
    if prompt_word_count < 4000:
        return 500    # medium for standard analysis
    if prompt_word_count < 8000:
        return 1000   # heavy for complex cross-document work
    return 2000       # max for very long contexts

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
# Stop cycling a provider's keys after this many CONSECUTIVE 429s: today's keys
# share one org/account bucket per provider (2026-07-03 audit), so a second 429
# proves the bucket is dry for every remaining key.
_KEY_429_CUTOFF = int(os.environ.get("SOLLIA_LLM_KEY_429_CUTOFF", "2"))
MAX_CHAIN_RETRIES = int(os.environ.get("SOLLIA_LLM_MAX_CHAIN_RETRIES", "3"))  # full-chain 429 passes before raising
REQUEST_TIMEOUT = int(os.environ.get("SOLLIA_LLM_TIMEOUT", "60"))
RETRY_AFTER_CAP = 60.0     # never honor an absurd Retry-After

# Approx public prices (USD per 1M tokens) for the cost dashboard only. Groq/
# OpenRouter-free dev usage is effectively $0; these let us estimate "what this
# WOULD cost at list price". Clearly an estimate, not a bill.
PRICING: Dict[str, Dict[str, float]] = {
    # Active chain. `:free` OpenRouter slugs and Groq dev usage are effectively $0;
    # list prices shown so the dashboard can estimate "what this WOULD cost".
    # Fable 5 IS billed (funded OpenRouter key) — price is the verified OpenRouter
    # list price (GET /models, 2026-07-01), not an estimate.
    "anthropic/claude-fable-5":                 {"in": 10.00, "out": 50.00},
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
# Part 3a: max concurrent LIVE LLM calls across all agents (cache hits bypass).
# 6 parallel agents sharing one Groq account used to stampede the per-model TPM
# and burn wall time on 429 retries; 3-at-a-time takes turns instead.
LLM_MAX_CONCURRENCY = int(os.environ.get("SOLLIA_LLM_MAX_CONCURRENCY", "3"))
_LLM_SEMAPHORE = threading.Semaphore(LLM_MAX_CONCURRENCY)

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


class LLMBudgetExceeded(LLMError):
    """Raised when a pipeline run exceeds its live-LLM-call budget (abuse guard)."""


class RunCostExceeded(LLMError):
    """Raised when a single pipeline run exceeds the cost budget AND no free
    provider could serve the call. In normal operation the cap does NOT raise:
    paid tiers are silently skipped and the chain falls through to free tiers."""


# ---------------------------------------------------------------------------
# Per-run abuse guards (Part 8d, 2026-07-01 security hardening)
#   * LLM call budget: a single pipeline run may issue at most
#     SOLLIA_MAX_LLM_CALLS_PER_RUN LIVE calls (cache hits are free). Prevents a
#     runaway loop from burning the paid Fable 5 key.
#   * Circuit breaker: 5 consecutive total-chain failures abort the run — if the
#     whole provider ecosystem is down, more calls only add latency and load.
#   * HARD COST CAP (Part 1d): cumulative BILLED cost (providers marked
#     billed:True — real dollars, not list-price estimates for free-tier usage)
#     is tracked per run. Once it exceeds MAX_COST_PER_RUN_USD, paid providers
#     are skipped and every remaining call runs on free tiers only. There is no
#     code path around it: the check lives inside call_llm's provider loop, so
#     a single run can never burn more than the cap (+ one in-flight call)
#     without an explicit SOLLIA_MAX_COST_PER_RUN env override.
#   * Cache-bust rate limit (Part 3b): SOLLIA_LLM_NO_CACHE=1 forces live calls;
#     an attacker who sets it and spams runs bypasses the byte-cache and runs up
#     provider load/cost. reset_run_budget() counts NO_CACHE runs in a rolling
#     hour (persisted to .cache/no_cache_runs.json); past 5/hour the flag is
#     suppressed and the run silently uses the cache again.
# The backend calls reset_run_budget() at the start of each pipeline run.
# ---------------------------------------------------------------------------
# 300 (was 100): Agent 1's rolling 5-year window legitimately issues ~150-200
# classify batches on a fully cold cache; this stays a hard abuse ceiling.
MAX_LLM_CALLS_PER_RUN = int(os.environ.get("SOLLIA_MAX_LLM_CALLS_PER_RUN", "300"))
CIRCUIT_BREAKER_THRESHOLD = int(os.environ.get("SOLLIA_LLM_CIRCUIT_THRESHOLD", "5"))
# $1.00 (was $2.00, 2026-07-03): with Fable 5 reserved for ~1 pinned call/run
# (~$0.037) actual spend is ~$0.04/run — the cap is a safety net, not a budget.
MAX_COST_PER_RUN_USD = float(os.environ.get("SOLLIA_MAX_COST_PER_RUN", "1.00"))
MAX_NO_CACHE_RUNS_PER_HOUR = int(os.environ.get("SOLLIA_MAX_NO_CACHE_RUNS_PER_HOUR", "5"))
_NO_CACHE_STATE = os.path.join(_SOLLIA_WORKING, ".cache", "no_cache_runs.json")
_budget_lock = threading.Lock()
_live_calls_this_run = 0
_consecutive_chain_failures = 0
_run_cost_usd = 0.0
_no_cache_suppressed = False


def _check_no_cache_rate_limit() -> bool:
    """Record a NO_CACHE run and return True when the rolling-hour limit is
    exceeded (meaning the cache-bust flag must be suppressed). Never raises."""
    now = time.time()
    stamps: List[float] = []
    try:
        with open(_NO_CACHE_STATE) as f:
            stamps = [t for t in json.load(f) if isinstance(t, (int, float))]
    except Exception:
        stamps = []
    stamps = [t for t in stamps if now - t < 3600.0]
    over = len(stamps) >= MAX_NO_CACHE_RUNS_PER_HOUR
    if not over:
        stamps.append(now)
    try:
        os.makedirs(os.path.dirname(_NO_CACHE_STATE), exist_ok=True)
        with open(_NO_CACHE_STATE, "w") as f:
            json.dump(stamps, f)
    except Exception:
        pass
    return over


def reset_run_budget() -> None:
    """Reset the per-run LLM budget, cost cap + circuit breaker. Call at
    pipeline start. Also enforces the cache-bust rate limit."""
    global _live_calls_this_run, _consecutive_chain_failures, _run_cost_usd, _no_cache_suppressed
    with _budget_lock:
        _live_calls_this_run = 0
        _consecutive_chain_failures = 0
        _run_cost_usd = 0.0
    if NO_CACHE:
        suppressed = _check_no_cache_rate_limit()
        with _budget_lock:
            _no_cache_suppressed = suppressed
        if suppressed:
            print("[llm_config] WARNING: SOLLIA_LLM_NO_CACHE rate limit reached "
                  f"({MAX_NO_CACHE_RUNS_PER_HOUR} cache-busted runs/hour) — this run "
                  "will use the cache.", flush=True)


def get_run_budget_state() -> Dict[str, Any]:
    with _budget_lock:
        return {"live_calls": _live_calls_this_run,
                "max_calls": MAX_LLM_CALLS_PER_RUN,
                "consecutive_failures": _consecutive_chain_failures,
                "billed_cost_usd": round(_run_cost_usd, 6),
                "max_cost_usd": MAX_COST_PER_RUN_USD,
                "cost_cap_reached": _run_cost_usd >= MAX_COST_PER_RUN_USD}


def _cost_cap_reached() -> bool:
    with _budget_lock:
        return _run_cost_usd >= MAX_COST_PER_RUN_USD


def _record_billed_cost(cost: float) -> None:
    global _run_cost_usd
    if cost:
        with _budget_lock:
            _run_cost_usd += cost


def _budget_precheck() -> None:
    """Raise if the run is over budget or the circuit breaker is open."""
    global _live_calls_this_run
    with _budget_lock:
        if _consecutive_chain_failures >= CIRCUIT_BREAKER_THRESHOLD:
            raise LLMError(
                f"LLM circuit breaker open: {_consecutive_chain_failures} consecutive "
                f"provider-chain failures. Aborting run — check provider status/keys, "
                f"then retry (reset_run_budget() clears the breaker).")
        if _live_calls_this_run >= MAX_LLM_CALLS_PER_RUN:
            raise LLMBudgetExceeded(
                f"Run exceeded its LLM call budget ({MAX_LLM_CALLS_PER_RUN} live calls). "
                f"This is an abuse guard; raise SOLLIA_MAX_LLM_CALLS_PER_RUN if the "
                f"workload is legitimate.")
        _live_calls_this_run += 1


def _budget_record_outcome(success: bool) -> None:
    global _consecutive_chain_failures
    with _budget_lock:
        _consecutive_chain_failures = 0 if success else _consecutive_chain_failures + 1


# ===========================================================================
# Hardened HTTP POST with a WALL-CLOCK ceiling (2026-07-02 hang fix)
# ===========================================================================
# A production hang was observed: an ESTABLISHED connection to OpenRouter's
# Cloudflare front stalled for 13+ minutes even though requests carried a
# timeout. requests' scalar/read timeout means "seconds of inactivity between
# bytes" — a server that dribbles keepalive bytes (or a stuck TLS session)
# defeats it and the pipeline blocks forever. Fix: run the POST on a throwaway
# worker thread and enforce a HARD wall-clock deadline with future.result().
# On breach we abandon the call (the orphaned thread dies on its own socket
# timeout; it never blocks the pipeline) and the chain falls to the next
# provider. Also splits the socket timeout into (connect, read) so a stuck
# handshake is bounded separately.
WALLCLOCK_SLACK = float(os.environ.get("SOLLIA_LLM_WALLCLOCK_SLACK", "15"))
CONNECT_TIMEOUT = float(os.environ.get("SOLLIA_LLM_CONNECT_TIMEOUT", "10"))


class LLMHardTimeout(RuntimeError):
    """The provider did not respond within the hard wall-clock ceiling."""


# ---------------------------------------------------------------------------
# Provider-health memo (network-condition pass, 2026-07-03). A provider that
# just HUNG (wall-clock breach — the 13-minute Cloudflare-stall class) is the
# single worst latency tail in the system: the chain survives it, but every
# retry risks another `timeout+slack` stall. So hard timeouts are remembered
# for HARD_TIMEOUT_MEMO_SECONDS and, while the memo is fresh:
#   * that provider's read timeout is clamped to HUNG_PROVIDER_TIMEOUT so a
#     repeat stall costs ~35s, not 180+15s;
#   * select_best_tier stops PROMOTING >90k-word calls to a hung Fable 5 —
#     Groq-70B (128k ctx) takes them instead. pin_tier callers still pin.
# ---------------------------------------------------------------------------
HARD_TIMEOUT_MEMO_SECONDS = float(os.environ.get("SOLLIA_LLM_HANG_MEMO", "600"))
HUNG_PROVIDER_TIMEOUT = float(os.environ.get("SOLLIA_LLM_HUNG_TIMEOUT", "30"))
_provider_health_lock = threading.Lock()
_provider_last_hang: Dict[str, float] = {}


def _note_hard_timeout(provider_name: str) -> None:
    with _provider_health_lock:
        _provider_last_hang[provider_name] = time.time()


def _recently_hung(provider_name: str) -> bool:
    with _provider_health_lock:
        ts = _provider_last_hang.get(provider_name, 0.0)
    return (time.time() - ts) < HARD_TIMEOUT_MEMO_SECONDS


def _post_with_wallclock(url: str, headers: Dict[str, str], payload: Dict[str, Any],
                         read_timeout: int):
    """POST with BOTH a (connect, read) socket timeout AND a hard wall-clock
    deadline. The request runs on a DAEMON thread so a stalled connection can
    never block the pipeline OR delay interpreter exit — a leaked thread dies
    on its own socket read timeout. Raises on breach so call_llm falls through
    to the next provider."""
    hard_deadline = read_timeout + WALLCLOCK_SLACK
    box: Dict[str, Any] = {}
    done = threading.Event()

    def _do():
        try:
            box["resp"] = requests.post(url, headers=headers, json=payload,
                                        timeout=(CONNECT_TIMEOUT, read_timeout))
        except Exception as exc:  # noqa: BLE001
            box["exc"] = exc
        finally:
            done.set()

    t = threading.Thread(target=_do, daemon=True, name="llm-http")
    t.start()
    if not done.wait(timeout=hard_deadline):
        raise LLMHardTimeout(
            "no response within hard wall-clock deadline %.0fs (provider stalled)"
            % hard_deadline)
    if "exc" in box:
        raise box["exc"]
    return box["resp"]


# ===========================================================================
# Main entry point
# ===========================================================================
def call_llm(prompt: str, max_tokens: int = 400, temperature: float = 0.0,
             provider_idx: int = 0, agent: Optional[str] = None,
             stage: Optional[str] = None, ticker: Optional[str] = None,
             pin_tier: bool = False, requires_strict_json: bool = False,
             requires_long_context: bool = False,
             bypass_cache: bool = False) -> Tuple[str, Dict]:
    """Call the LLM provider chain with hardened retry + full history recording.

    Backwards compatible: returns (response_text, meta). New optional kwargs
    (agent/stage/ticker) attribute the call in .cache/llm_history/; if omitted
    they fall back to the thread-local call_context().

    pin_tier=True bypasses the adaptive word-count router and starts exactly at
    provider_idx — for LOW-VOLUME judgment calls that deliberately use the paid
    tier on a short prompt (e.g. Agent 1's high-severity audit: ~1 call/run).
    The per-run cost cap still applies; pinning cannot bypass it.

    requires_strict_json / requires_long_context declare the TASK's needs (set
    by agent code, never by document content): they let the dynamic router
    promote a long call to Fable 5 per select_best_tier. Flag-less calls keep
    the demote-only behavior, so ordinary prompts can never bill the paid tier.

    bypass_cache=True skips the byte-cache READ (the fresh response still
    overwrites the entry). Agents use it for a single parse-failure retry: a
    garbage response cached during a rate-limit storm would otherwise be
    served forever (the TSLA "unparseable result" loop).
    """
    r_agent, r_stage, r_ticker = _resolve_ctx(agent, stage, ticker)
    call_id = "%s_%s_%s_%s" % (
        r_agent, r_ticker, r_stage,
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f") + "Z",
    )

    cache_key = hashlib.sha256(f"{prompt}_{max_tokens}_{temperature}".encode()).hexdigest()[:16]
    cache_path = os.path.join(LLM_CACHE_DIR, f"{cache_key}.json")

    # ---- byte-identical cache (skipped under NO_CACHE, unless the cache-bust
    #      rate limit suppressed the flag for this run — Part 3b; also skipped
    #      for a caller-driven parse-failure retry, which REWRITES the entry) ----
    if (not bypass_cache) and (not NO_CACHE or _no_cache_suppressed) and os.path.exists(cache_path):
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

    # Live call — enforce the per-run budget + circuit breaker (cache hits above
    # returned already and are exempt).
    _budget_precheck()

    # DYNAMIC ROUTING (Part 1c): the caller's provider_idx (AGENT_START_TIER)
    # is a suggestion. Calls that declare a task requirement (strict JSON /
    # long context — set by agent CODE, not by document content) are routed by
    # select_best_tier and may be PROMOTED to the paid tier; everything else
    # keeps the demote-only rule so ordinary prompts can never bill Fable 5.
    # pin_tier callers opt out entirely; the cost cap below still binds them.
    prompt_wc = len(prompt.split())
    if pin_tier:
        effective_idx = provider_idx
    elif requires_strict_json or requires_long_context:
        effective_idx = select_best_tier(prompt_wc, requires_strict_json,
                                         requires_long_context)
    else:
        effective_idx = select_tier_for_prompt(prompt, provider_idx)

    prev_errors: List[str] = []
    paid_skipped_by_cap = False

    # LLM CONCURRENCY SEMAPHORE (Part 3a): the pipeline runs 6 agents in
    # parallel and they share the free tiers' per-model rate limits. Serializing
    # live calls down to LLM_MAX_CONCURRENCY turns "everyone 429s and retries"
    # into "calls take turns" — same or better wall time, no wasted retries.
    # Cache hits return above WITHOUT touching the semaphore.
    _LLM_SEMAPHORE.acquire()
    try:
        return _call_llm_chain(prompt, max_tokens, temperature, effective_idx,
                               prompt_wc, cache_path, call_id,
                               r_agent, r_stage, r_ticker,
                               prev_errors, paid_skipped_by_cap,
                               requires_strict_json)
    finally:
        _LLM_SEMAPHORE.release()


def _provider_keys(provider: Dict[str, Any]) -> List[str]:
    """All configured API keys for a provider (Part 3b key cycling): key_envs
    when present, else the single key_env. Order-stable, missing keys dropped."""
    envs = provider.get("key_envs") or [provider["key_env"]]
    keys = []
    for e in envs:
        v = os.environ.get(e, "")
        if v and v not in keys:
            keys.append(v)
    return keys


_key_rotation_lock = threading.Lock()
_key_rotation_counters: Dict[str, int] = {}


def _rotated(keys: List[str], provider_name: str) -> List[str]:
    """Round-robin the key list per provider so concurrent calls spread across
    rate-limit buckets instead of all hitting the first key."""
    if len(keys) <= 1:
        return keys
    with _key_rotation_lock:
        n = _key_rotation_counters.get(provider_name, 0)
        _key_rotation_counters[provider_name] = n + 1
    off = n % len(keys)
    return keys[off:] + keys[:off]


def _call_llm_chain(prompt, max_tokens, temperature, effective_idx, prompt_wc,
                    cache_path, call_id, r_agent, r_stage, r_ticker,
                    prev_errors, paid_skipped_by_cap,
                    requires_strict_json: bool = False) -> Tuple[str, Dict]:
    """The provider-chain loop of call_llm (runs holding the LLM semaphore)."""
    consecutive_chain_429 = 0
    total_attempts = 0
    total_attempts_holder = [0]
    chain_pass = 0
    for chain_pass in range(MAX_CHAIN_RETRIES):
        pass_had_429 = False
        pass_retry_after: Optional[float] = None

        for idx in range(effective_idx, len(LLM_PROVIDERS)):
            provider = LLM_PROVIDERS[idx]
            # A model measured incapable of a strict-JSON shape (json_weak) is
            # never given a strict-JSON task — 429 fallthrough included
            # (2026-07-03 Part 2d: Nemotron answered the Agent-4 shape with
            # prose/truncation/empty arrays; skipping beats parsing garbage).
            if requires_strict_json and provider.get("json_weak"):
                continue
            api_keys = _rotated(_provider_keys(provider), provider["name"])
            if not api_keys:
                continue
            # HARD COST CAP (Part 1d): once the run's billed spend crosses
            # MAX_COST_PER_RUN_USD, paid tiers are off the table — fall through
            # to the free tiers below instead.
            if provider.get("billed") and _cost_cap_reached():
                if not paid_skipped_by_cap:
                    prev_errors.append(f"{provider['name']}: skipped (run cost cap "
                                       f"${MAX_COST_PER_RUN_USD:.2f} reached)")
                paid_skipped_by_cap = True
                continue

            payload = {"model": provider["model"],
                       "messages": [{"role": "user", "content": prompt}],
                       "temperature": temperature, "max_tokens": max_tokens}
            # Provider-specific payload knobs (e.g. Gemini reasoning_effort:none).
            if provider.get("payload_extra"):
                payload.update(provider["payload_extra"])
            if provider.get("adaptive_reasoning"):
                # Part 1c: reasoning budget proportional to prompt complexity.
                # Reasoning counts against max_tokens on this endpoint, so the
                # budget is also added as a pad (pad does NOT change the byte-
                # cache key). Clamped to the Anthropic minimum thinking budget.
                rbudget = max(fable5_effort_level(prompt_wc), FABLE5_MIN_REASONING)
                payload["reasoning"] = {"max_tokens": rbudget}
                payload["max_tokens"] = max_tokens + rbudget
            elif provider.get("reasoning_effort"):
                payload["reasoning"] = {"effort": provider["reasoning_effort"]}
                payload["max_tokens"] = max_tokens + int(provider.get("max_tokens_pad", 0))

            resp = None
            # Key cycling (Part 3b, recalibrated 2026-07-03): a 429 on one key
            # tries the provider's NEXT key — but only until _KEY_429_CUTOFF
            # consecutive 429s. Measured: today's keys share ONE bucket per
            # provider (org/account-level limits), so after two keys 429 the
            # rest are guaranteed to as well; cycling through all 11 would burn
            # ~5s of doomed round-trips per chain pass. Two consecutive 429s
            # also stays correct if the keys are later re-issued from separate
            # accounts (independent buckets rarely 429 in the same instant).
            # Auth failures (401/403 revoked key, 402 out of credits) always
            # advance to the next key — one dead key must not kill the tier.
            consecutive_429 = 0
            for k_i, api_key in enumerate(api_keys):
                headers = {"Content-Type": "application/json",
                           "Authorization": f"Bearer {api_key}"}
                if "openrouter.ai" in provider["url"]:
                    headers["HTTP-Referer"] = "https://sollia.app"
                    headers["X-Title"] = "Sollia"
                _rate_gate()
                total_attempts_holder[0] += 1
                t0 = time.time()
                # Reliability-under-stress (network-condition pass): a provider
                # that recently breached the wall-clock guard gets a clamped
                # timeout so a repeat stall costs seconds, not minutes.
                read_timeout = int(provider.get("timeout", REQUEST_TIMEOUT))
                if _recently_hung(provider["name"]):
                    read_timeout = int(min(read_timeout, HUNG_PROVIDER_TIMEOUT))
                try:
                    resp = _post_with_wallclock(provider["url"], headers, payload,
                                                read_timeout)
                except Exception as exc:  # network/timeout/hang -> try next provider
                    if isinstance(exc, LLMHardTimeout):
                        _note_hard_timeout(provider["name"])
                    prev_errors.append(f"{provider['name']}: {type(exc).__name__}: {str(exc)[:120]}")
                    resp = None
                    break
                if resp.status_code == 429 and k_i < len(api_keys) - 1:
                    consecutive_429 += 1
                    if consecutive_429 >= _KEY_429_CUTOFF:
                        prev_errors.append(
                            f"{provider['name']}: {consecutive_429} keys 429 in a row "
                            "— shared rate bucket, skipping remaining keys")
                        break
                    prev_errors.append(f"{provider['name']} key#{k_i + 1}: 429 — rotating key")
                    continue
                if resp.status_code in (401, 402, 403) and k_i < len(api_keys) - 1:
                    consecutive_429 = 0
                    prev_errors.append(f"{provider['name']} key#{k_i + 1}: "
                                       f"HTTP {resp.status_code} — trying next key")
                    continue
                break
            if resp is None:
                continue
            total_attempts = total_attempts_holder[0]

            if resp.status_code == 200:
                data = resp.json()
                try:
                    content = data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError):
                    prev_errors.append(f"{provider['name']}: malformed 200 (no choices/content)")
                    continue
                # content can be None on a REASONING model: a refusal, or the whole
                # token budget spent thinking with nothing emitted (Fable 5 returns
                # content:null here). Treat None exactly like an empty string —
                # AttributeError from None.strip() previously crashed the whole
                # chain instead of falling through (2026-07-01 injection-audit fix).
                result = (content or "").strip()
                if not result:
                    prev_errors.append(f"{provider['name']}: empty 200 response")
                    continue
                usage = data.get("usage")
                total_ms = (time.time() - t0) * 1000.0
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
                _budget_record_outcome(True)
                cost = estimate_cost(provider["model"], usage)
                if provider.get("billed"):
                    # OpenRouter returns the ACTUAL billed cost in usage.cost —
                    # prefer it over the list-price estimate for the run cap.
                    actual = (usage or {}).get("cost")
                    _record_billed_cost(float(actual) if actual is not None else cost)
                return result, {"cached": False, "provider": provider["name"],
                                "model": provider["model"], "retries": chain_pass,
                                "attempts": total_attempts,
                                "cost_usd": cost}

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
    _budget_record_outcome(False)
    rec = _build_record(call_id, r_agent, r_stage, r_ticker, "FAILED", "none",
                        temperature, max_tokens, prompt, "", None, False, 0.0,
                        chain_pass + 1, total_attempts, prev_errors)
    _record_history(rec)
    summary = " | ".join(prev_errors[-4:]) if prev_errors else "no providers had API keys"
    if consecutive_chain_429 >= MAX_CHAIN_RETRIES:
        raise LLMRateLimited(
            f"All LLM providers rate-limited (429) for {MAX_CHAIN_RETRIES} consecutive "
            f"passes. Last errors: {summary}")
    if paid_skipped_by_cap:
        raise RunCostExceeded(
            f"Run cost cap ${MAX_COST_PER_RUN_USD:.2f} reached; the paid tier was "
            f"skipped and no free provider could serve the call. Last errors: {summary}")
    raise LLMError(f"All LLM providers failed. Last errors: {summary}")


def get_primary_model() -> str:
    return LLM_PROVIDERS[0]["model"]


# ---------------------------------------------------------------------------
# API-key hygiene (Part 3b, 2026-07-01). Keys must come from the environment
# (.env or shell) — never hardcoded. check_api_keys() flags keys that look like
# placeholders/examples so a misconfigured deploy fails loudly at startup
# instead of silently skipping providers. Values are NEVER printed.
# ---------------------------------------------------------------------------
_PLACEHOLDER_PAT = re.compile(
    r"(your[-_ ]?key|example|placeholder|changeme|xxx|<|>|^dead$|^test$)", re.IGNORECASE)


def check_api_keys(verbose: bool = False) -> Dict[str, str]:
    """Return {key_env: status} for every provider key. Status is one of
    'ok', 'missing', 'placeholder', 'too_short'. Never logs key values."""
    out: Dict[str, str] = {}
    for env in sorted({p["key_env"] for p in LLM_PROVIDERS}):
        val = os.environ.get(env, "")
        if not val:
            out[env] = "missing"
        elif _PLACEHOLDER_PAT.search(val):
            out[env] = "placeholder"
        elif len(val) < 20:
            out[env] = "too_short"
        else:
            out[env] = "ok"
        if verbose and out[env] != "ok":
            print(f"[llm_config] WARNING: {env} looks {out[env]} — that provider "
                  "tier will be skipped or fail.", flush=True)
    return out


# Warn (once, at import) about suspicious keys so misconfiguration is visible
# in the backend startup log rather than as mysterious provider fallthrough.
for _env, _status in check_api_keys().items():
    if _status in ("placeholder", "too_short"):
        print(f"[llm_config] WARNING: {_env} appears to be a {_status} value — "
              "check sollia_working/.env.", flush=True)


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
