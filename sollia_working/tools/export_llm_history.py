#!/usr/bin/env python3.11
"""Export the LLM call history to CSV for offline analysis / fine-tuning prep.

One row per call. Full prompt/response text stays in the per-call JSON (the
`path` column points to it); the CSV carries the metadata + token/latency/cost.

Usage:
    python3.11 tools/export_llm_history.py [out.csv] [--rebuild]
"""
import os
import sys
import csv

HERE = os.path.dirname(os.path.abspath(__file__))
WORKING = os.path.dirname(HERE)
sys.path.insert(0, WORKING)
sys.path.insert(0, HERE)
from llm_history_stats import load_rows, cost_for  # noqa: E402  (sibling module)

COLS = ["call_id", "timestamp", "agent", "ticker", "stage", "provider", "model",
        "cached", "prompt_tokens", "response_tokens", "total_time_ms", "attempts",
        "parse_success", "est_cost_usd", "path"]


def main():
    args = [a for a in sys.argv[1:] if a != "--rebuild"]
    rebuild = "--rebuild" in sys.argv
    out = args[0] if args else os.path.join(WORKING, ".cache", "llm_history", "history_export.csv")
    rows = load_rows(rebuild)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = dict(r)
            r["est_cost_usd"] = round(
                0.0 if r.get("cached") else
                cost_for(r["model"], r.get("prompt_tokens", 0), r.get("response_tokens", 0)), 6)
            w.writerow(r)
    print(f"Wrote {len(rows)} rows -> {out}")


if __name__ == "__main__":
    main()
