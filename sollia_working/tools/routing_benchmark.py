#!/usr/bin/env python3.11
"""Empirical routing benchmark — all 8 chain models × 3 REAL pipeline prompts.

Feeds each model in LLM_PROVIDERS the exact prompts the pipeline recorded in
.cache/llm_history (not synthetic approximations):

  A  Agent 4 risk-factor comparison   (strict JSON array, longest prompt)
  B  Agent 1 discrepancy classify     (strict JSON, exact sentence indices)
  C  Agent 2 Stage-1 promise extract  (simple classification, category allowlist)

Measures per model×test: clean-JSON rate (strict json.loads of the raw reply),
instruction following (reply is ONLY JSON), tolerant-recoverable rate
(extract_json), test-specific validity (schema keys / index bounds / category
allowlist), latency, and determinism (first 3 successes byte-identical).

Run counts respect provider economics: paid Fable 5 runs 3× per test, Groq +
Gemini 10×, OpenRouter :free tiers 5× (shared upstream 429 pools). 429s honor
Retry-After (capped) and don't count against quality metrics.

Usage:
  python3.11 tools/routing_benchmark.py                # full matrix
  python3.11 tools/routing_benchmark.py --models 1,2   # subset by tier index
  python3.11 tools/routing_benchmark.py --runs 2       # override run count
Results: .test_results/routing_benchmark/results.json + printed table.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
_SW = os.path.dirname(_HERE)
sys.path.insert(0, _SW)

from src.config.llm_config import (  # noqa: E402
    LLM_PROVIDERS, FABLE5_MIN_REASONING, fable5_effort_level,
)
from src.utils.json_extractor import extract_json  # noqa: E402

HISTORY = os.path.join(_SW, ".cache", "llm_history")
RESULTS_DIR = os.path.join(os.path.dirname(_SW), ".test_results", "routing_benchmark")

# The exact recorded pipeline calls used as fixtures (largest real instance of
# each task shape; see PART1 report for provenance).
FIXTURES = {
    "A_agent4_risk": {
        "path": "agent_4/TSLA/agent_4_TSLA_run_2026-06-25T19-17-21-998009Z.json",
        "max_tokens": 2500,
        "kind": "agent4",
    },
    "B_agent1_classify": {
        "path": "unknown/unknown/unknown_unknown_call_2026-06-25T19-11-25-860639Z.json",
        "max_tokens": 800,
        "kind": "agent1",
    },
    "C_agent2_stage1": {
        "path": "unknown/unknown/unknown_unknown_call_2026-06-27T08-02-04-659679Z.json",
        "max_tokens": 2000,
        "kind": "agent2s1",
    },
}

RUNS_BY_TIER = {0: 3}  # paid Fable 5
RUNS_DEFAULT_GROQ_GEMINI = 10
RUNS_DEFAULT_OR_FREE = 5

VALID_STAGE1_CATEGORIES = {"revenue", "product", "margin", "rd", "market", "other"}


def load_fixture_text(rec_path: str) -> str:
    d = json.load(open(os.path.join(HISTORY, rec_path)))
    pr = d.get("prompt", "")
    if isinstance(pr, dict):
        return pr.get("full_text", "")
    if isinstance(pr, str) and pr.startswith("{"):
        try:
            return ast.literal_eval(pr).get("full_text", "")
        except Exception:
            return pr
    return pr


def runs_for_tier(idx: int, prov: dict, override: int | None) -> int:
    if override:
        return override
    if idx in RUNS_BY_TIER:
        return RUNS_BY_TIER[idx]
    if ":free" in prov.get("model", ""):
        return RUNS_DEFAULT_OR_FREE
    return RUNS_DEFAULT_GROQ_GEMINI


def call_provider(prov: dict, prompt: str, max_tokens: int) -> dict:
    """One raw POST to the provider (OpenAI-compatible), no cache, temp 0.
    Returns {ok, text, ms, status, rate_limited, error}."""
    key = os.environ.get(prov["key_env"], "")
    if not key:
        return {"ok": False, "error": f"no key in {prov['key_env']}", "rate_limited": False}
    payload = {
        "model": prov["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    if prov.get("adaptive_reasoning"):  # Fable 5: reasoning is mandatory & billed
        budget = max(fable5_effort_level(len(prompt.split())), FABLE5_MIN_REASONING)
        payload["reasoning"] = {"max_tokens": budget}
        payload["max_tokens"] = max_tokens + budget
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    t0 = time.time()
    try:
        resp = requests.post(prov["url"], json=payload, headers=headers,
                             timeout=(10, prov.get("timeout", 120)))
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200], "ms": (time.time() - t0) * 1000,
                "rate_limited": False}
    ms = (time.time() - t0) * 1000
    if resp.status_code == 429:
        ra = resp.headers.get("retry-after")
        return {"ok": False, "rate_limited": True, "retry_after": ra, "ms": ms,
                "status": 429, "error": "429"}
    if resp.status_code != 200:
        return {"ok": False, "rate_limited": False, "status": resp.status_code,
                "ms": ms, "error": resp.text[:200]}
    try:
        content = resp.json()["choices"][0]["message"].get("content")
    except Exception as exc:
        return {"ok": False, "rate_limited": False, "ms": ms,
                "error": f"bad body: {exc}"}
    return {"ok": True, "text": (content or ""), "ms": ms, "status": 200,
            "rate_limited": False}


def index_bounds_from_prompt(prompt: str) -> int:
    """Highest [N] sentence label in an Agent-1 classify prompt (index bound)."""
    labels = [int(m) for m in re.findall(r"^\s*\[(\d+)\]", prompt, re.MULTILINE)]
    return max(labels) if labels else -1


def validate(kind: str, prompt: str, text: str) -> dict:
    """Test-specific quality checks on one response."""
    out = {"clean_json": False, "instruction_following": False,
           "recoverable": False, "valid": False}
    stripped = (text or "").strip()
    out["instruction_following"] = bool(stripped) and stripped[0] in "[{" and stripped[-1] in "]}"
    try:
        parsed = json.loads(stripped)
        out["clean_json"] = isinstance(parsed, (list, dict))
    except Exception:
        parsed = None
    if parsed is None:
        try:
            parsed = extract_json(stripped)
            out["recoverable"] = True
        except ValueError:
            return out
    else:
        out["recoverable"] = True

    data = parsed if isinstance(parsed, list) else [parsed]
    dicts = [d for d in data if isinstance(d, dict)]
    if kind == "agent4":
        cats = {"ESCALATED", "NEW", "REMOVED", "DE-ESCALATED", "REWORDED"}
        out["valid"] = all(
            str(d.get("category", "")).upper() in cats and
            (d.get("old_quote") or d.get("new_quote"))
            for d in dicts) and isinstance(parsed, list)
    elif kind == "agent1":
        bound = index_bounds_from_prompt(prompt)
        def _ok(d):
            a, b = d.get("sentence_index_a"), d.get("sentence_index_b")
            return (isinstance(a, int) and isinstance(b, int)
                    and 0 <= a <= bound and 0 <= b <= bound)
        out["valid"] = isinstance(parsed, list) and all(_ok(d) for d in dicts) and (
            bool(dicts) or parsed == [])
    elif kind == "agent2s1":
        def _ok(d):
            if not d.get("is_promise"):
                return True
            return d.get("category") in VALID_STAGE1_CATEGORIES
        out["valid"] = isinstance(parsed, list) and all(_ok(d) for d in dicts)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="", help="comma tier indices, e.g. 0,1,2")
    ap.add_argument("--runs", type=int, default=0, help="override run count")
    ap.add_argument("--tests", default="", help="comma fixture keys (A_,B_,C_ prefixes ok)")
    args = ap.parse_args()

    tiers = [int(x) for x in args.models.split(",") if x.strip() != ""] or list(range(len(LLM_PROVIDERS)))
    fixtures = {}
    for name, spec in FIXTURES.items():
        if args.tests and not any(name.startswith(t) for t in args.tests.split(",")):
            continue
        text = load_fixture_text(spec["path"])
        if not text:
            print(f"fixture {name}: EMPTY prompt at {spec['path']}", file=sys.stderr)
            return 1
        fixtures[name] = {**spec, "prompt": text, "words": len(text.split())}
        print(f"fixture {name}: {fixtures[name]['words']} words")

    os.makedirs(RESULTS_DIR, exist_ok=True)
    results = {"started": datetime.now(timezone.utc).isoformat(), "models": {}}

    for idx in tiers:
        prov = LLM_PROVIDERS[idx]
        n_runs_base = runs_for_tier(idx, prov, args.runs)
        mres = {"name": prov["name"], "model": prov["model"], "tier": idx, "tests": {}}
        print(f"\n=== tier {idx}: {prov['name']} ({prov['model']}) — {n_runs_base} runs/test ===")
        for fname, fx in fixtures.items():
            runs, texts, lat = [], [], []
            rate_limited = errors = 0
            attempts = 0
            max_attempts = n_runs_base * 3  # 429 retries don't eat run slots
            while len(runs) < n_runs_base and attempts < max_attempts:
                attempts += 1
                r = call_provider(prov, fx["prompt"], fx["max_tokens"])
                if r.get("rate_limited"):
                    rate_limited += 1
                    ra = r.get("retry_after")
                    wait = min(float(ra), 45.0) if ra and str(ra).replace(".", "").isdigit() else 15.0
                    print(f"  {fname}: 429, sleeping {wait:.0f}s")
                    time.sleep(wait)
                    continue
                if not r.get("ok"):
                    errors += 1
                    print(f"  {fname}: error {r.get('status','')} {r.get('error','')[:80]}")
                    time.sleep(3)
                    continue
                v = validate(fx["kind"], fx["prompt"], r["text"])
                runs.append({**v, "ms": r["ms"], "chars": len(r["text"]),
                             "head": r["text"][:200]})
                texts.append(r["text"])
                lat.append(r["ms"])
                # Pace by the provider's real TPM: Groq-70B is 12k tokens/min,
                # so a jumbo test-A call owns a whole minute; smaller tests and
                # other providers get lighter spacing.
                jumbo = fx["words"] > 1800
                time.sleep(65.0 if (idx in (1, 2) and jumbo) else
                           (8.0 if idx in (1, 2) else 15.0))
            n = len(runs)
            det = len(set(texts[:3])) == 1 if len(texts) >= 3 else None
            summary = {
                "n": n,
                "clean_json_rate": round(sum(r["clean_json"] for r in runs) / n, 3) if n else None,
                "instruction_following": round(sum(r["instruction_following"] for r in runs) / n, 3) if n else None,
                "recoverable_rate": round(sum(r["recoverable"] for r in runs) / n, 3) if n else None,
                "valid_rate": round(sum(r["valid"] for r in runs) / n, 3) if n else None,
                "avg_latency_ms": round(sum(lat) / n) if n else None,
                "p95_latency_ms": round(sorted(lat)[max(0, int(n * 0.95) - 1)]) if n else None,
                "deterministic": det,
                "rate_limited_429s": rate_limited,
                "hard_errors": errors,
            }
            mres["tests"][fname] = {"summary": summary, "runs": runs}
            print(f"  {fname}: n={n} clean={summary['clean_json_rate']} follow="
                  f"{summary['instruction_following']} valid={summary['valid_rate']} "
                  f"lat={summary['avg_latency_ms']}ms det={det} 429s={rate_limited}")
        results["models"][str(idx)] = mres
        with open(os.path.join(RESULTS_DIR, "results.json"), "w") as f:
            json.dump(results, f, indent=1)

    print(f"\nresults -> {os.path.join(RESULTS_DIR, 'results.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
