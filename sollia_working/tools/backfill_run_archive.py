"""One-time migration: backfill .test_results/ into the run archive (PART 3c).

Reads every cached agent output (preferring the *_raw.json full-output variant)
for each ticker and writes it through run_archive.save_run, seeding
.cache/runs/{agent}/{TICKER}/{year}/run_NNN.json + index.jsonl. The archived
manifests preserve each run's ORIGINAL output (scores/findings as they were) —
an archive records what happened, it is not re-scored.

Run:  PYTHONPATH=<sollia_working> python3.11 tools/backfill_run_archive.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.utils import run_archive as ra  # noqa: E402

TEST_RESULTS = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", ".test_results"))

AGENT_ID = {
    "agent_1": "corporate_discrepancy",
    "agent_2": "promise_audit",
    "agent_3": "legal_proceedings",
    "agent_4": "risk_factors",
}


def _load(d, name):
    for variant in (name + "_raw", name):           # prefer the full-output _raw
        p = os.path.join(d, variant + ".json")
        if os.path.exists(p):
            try:
                return json.load(open(p))
            except Exception:
                pass
    return None


def main():
    if not os.path.isdir(TEST_RESULTS):
        print("No .test_results/ at", TEST_RESULTS)
        return
    n = 0
    for ticker in sorted(os.listdir(TEST_RESULTS)):
        d = os.path.join(TEST_RESULTS, ticker)
        if not os.path.isdir(d) or ticker == "model_benchmarks":
            continue
        for tag, agent_id in AGENT_ID.items():
            result = _load(d, tag)
            if not result or not isinstance(result, dict):
                continue
            year = result.get("year") or 2025
            rid = ra.save_run(agent_id, ticker, year, result)
            if rid:
                n += 1
                print(f"  archived {agent_id:22} {ticker:5} -> {rid}")
    print(f"\nBackfilled {n} runs into {ra.RUNS_ROOT}")
    print(f"Index now holds {len(ra._read_index())} runs.")


if __name__ == "__main__":
    main()
