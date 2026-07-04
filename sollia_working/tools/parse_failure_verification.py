"""Part 2d — parse-failure verification on the three highest-volume JSON shapes.

Replays each agent's REAL recorded prompt (benchmark fixtures) 10x through the
PRODUCTION chain (call_llm with bypass_cache=True, so every sample is a live
response, exactly what an agent would receive) and runs each raw response
through the shared extractor + the task's schema check. Required: 0% failures.

Why fixture-replay rather than 10 full pipeline runs: with the byte-cache on,
10 identical pipeline runs would test ONE response 10 times; with the cache
bypassed they'd trip the NO_CACHE abuse guard (5/hour) and burn ~30 min of
org-wide Groq TPM. This tests the same code path (call_llm -> raw response ->
extract_json) on 10 independent live samples per shape.

    python3.11 tools/parse_failure_verification.py [--n 10]
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.config.llm_config import call_llm, start_tier, reset_run_budget  # noqa: E402
from src.utils.json_extractor import extract_json  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, ".cache", "benchmark_fixtures")

SHAPES = [
    {
        "name": "Agent 4 risk delta (TSLA-class strict JSON)",
        "fixture": "test4_agent4_risk.json",
        "call": lambda fx: call_llm(fx["prompt"], max_tokens=fx.get("max_tokens", 2500),
                                    provider_idx=start_tier("risk_factors"),
                                    requires_strict_json=True, bypass_cache=True,
                                    agent="verify2d", stage="agent4_shape", ticker="TSLA"),
        "check": lambda v, fx: isinstance(v, list) and all(
            isinstance(c, dict) and str(c.get("category", "")).upper()
            in set(fx.get("valid_categories") or []) for c in v),
    },
    {
        "name": "Agent 1 discrepancy classify (NVDA-class sentence indices)",
        "fixture": "test2_agent1_discrepancy.json",
        "call": lambda fx: call_llm(fx["prompt"], max_tokens=fx.get("max_tokens", 1500),
                                    provider_idx=start_tier("corporate_discrepancy"),
                                    requires_strict_json=True, bypass_cache=True,
                                    agent="verify2d", stage="agent1_shape", ticker="NVDA"),
        "check": lambda v, fx: isinstance(v, (list, dict)),
    },
    {
        "name": "Agent 2 Stage-1 promise extraction (AMD-class)",
        "fixture": "test5_agent2_stage1.json",
        "call": lambda fx: call_llm(fx["prompt"], max_tokens=fx.get("max_tokens", 2000),
                                    provider_idx=start_tier("promise_audit:stage1"),
                                    bypass_cache=True,
                                    agent="verify2d", stage="agent2_shape", ticker="AMD"),
        "check": lambda v, fx: isinstance(v, (list, dict)),
    },
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--shape", default="", help="substring filter, e.g. 'Agent 4'")
    args = ap.parse_args()
    reset_run_budget()
    report = {}
    for shape in SHAPES:
        if args.shape and args.shape.lower() not in shape["name"].lower():
            continue
        path = os.path.join(FIX, shape["fixture"])
        if not os.path.exists(path):
            print(f"SKIP {shape['name']}: fixture missing ({shape['fixture']})")
            continue
        with open(path) as f:
            fx = json.load(f)
        ok = parse_fail = schema_fail = provider_fail = 0
        providers = {}
        print(f"\n=== {shape['name']} — {args.n} live samples ===")
        for i in range(args.n):
            t0 = time.time()
            try:
                raw, meta = shape["call"](fx)
            except Exception as e:
                provider_fail += 1
                print(f"  #{i + 1}: PROVIDER-FAIL {type(e).__name__}: {str(e)[:90]}")
                continue
            prov = meta.get("provider", "?")
            providers[prov] = providers.get(prov, 0) + 1
            try:
                val = extract_json(raw)
            except ValueError as e:
                parse_fail += 1
                print(f"  #{i + 1}: PARSE-FAIL via {prov}: {str(e)[:90]}")
                continue
            if shape["check"](val, fx):
                ok += 1
                n = len(val) if isinstance(val, list) else "dict"
                print(f"  #{i + 1}: ok via {prov} ({n} items, {time.time() - t0:.1f}s)")
            else:
                schema_fail += 1
                print(f"  #{i + 1}: SCHEMA-FAIL via {prov}: {str(val)[:90]}")
        n_resp = ok + parse_fail + schema_fail
        rate = (parse_fail / n_resp * 100) if n_resp else 0.0
        report[shape["name"]] = {
            "samples": args.n, "responses": n_resp, "parsed_ok": ok,
            "parse_failures": parse_fail, "schema_failures": schema_fail,
            "provider_failures": provider_fail,
            "parse_failure_rate_pct": round(rate, 1), "providers": providers,
        }
        print(f"  -> parse-failure rate: {rate:.1f}% "
              f"({parse_fail}/{n_resp} responses; {provider_fail} provider errors)")
    out = os.path.join(ROOT, ".test_results", "parse_failure_verification.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nsaved -> {out}")
    bad = any(v["parse_failure_rate_pct"] > 0 for v in report.values())
    print("RESULT:", "FAIL (parse failures present)" if bad else "PASS (0% parse failures)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
