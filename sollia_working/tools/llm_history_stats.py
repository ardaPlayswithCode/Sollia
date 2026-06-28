#!/usr/bin/env python3.11
"""LLM call-history dashboard.

Reads .cache/llm_history/ (via index.jsonl, or --rebuild to scan the per-call
files which are the source of truth) and prints summary statistics for the
training corpus we are accumulating.

Usage:
    python3.11 tools/llm_history_stats.py [--rebuild]
"""
import os
import sys
import json
import glob
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
WORKING = os.path.dirname(HERE)
sys.path.insert(0, WORKING)
from src.config.llm_config import LLM_HISTORY_DIR, PRICING  # noqa: E402


def _row_from_record(r, path):
    return {
        "call_id": r["call_id"], "timestamp": r["timestamp"], "agent": r["agent"],
        "stage": r["stage"], "ticker": r["ticker"], "provider": r["provider"],
        "model": r["model"], "cached": r["response"].get("cached", False),
        "prompt_tokens": r["prompt"]["token_count"],
        "response_tokens": r["response"]["token_count"],
        "total_time_ms": r["performance"]["total_time_ms"],
        "attempts": r["retry_info"]["total_attempts"],
        "parse_success": r["response"]["parse_success"],
        "path": os.path.relpath(path, LLM_HISTORY_DIR),
    }


def load_rows(rebuild=False):
    """Load history rows. The per-call JSON files are the source of truth; the
    append-only index.jsonl is a fast path that can drift (deleted files,
    re-run backfills). We de-dup by call_id; --rebuild rescans the files and
    self-heals the index."""
    idx_path = os.path.join(LLM_HISTORY_DIR, "index.jsonl")
    rows = []
    if not rebuild and os.path.exists(idx_path):
        for line in open(idx_path):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    if rebuild or not rows:
        rows = []
        for p in glob.glob(os.path.join(LLM_HISTORY_DIR, "*", "*", "*.json")):
            try:
                rows.append(_row_from_record(json.load(open(p)), p))
            except Exception:
                pass
    # de-dup by call_id (last wins)
    dedup = {r["call_id"]: r for r in rows if r.get("call_id")}
    rows = list(dedup.values())
    if rebuild:  # self-heal the index from the authoritative file scan
        try:
            with open(idx_path, "w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
        except Exception:
            pass
    return rows


def cost_for(model, pin, pout):
    pr = PRICING.get(model)
    return (pin / 1e6 * pr["in"] + pout / 1e6 * pr["out"]) if pr else 0.0


def bar(n, total, width=24):
    filled = int(round(width * n / total)) if total else 0
    return "█" * filled + "·" * (width - filled)


def main():
    rebuild = "--rebuild" in sys.argv
    rows = load_rows(rebuild)
    n = len(rows)
    print("=" * 64)
    print(" SOLLIA — LLM CALL HISTORY DASHBOARD")
    print(" dir:", LLM_HISTORY_DIR)
    print("=" * 64)
    if not n:
        print("No history records yet.")
        return

    by_agent = Counter(r["agent"] for r in rows)
    by_provider = Counter(r["provider"] for r in rows)
    by_model = Counter(r["model"] for r in rows)
    by_ticker = Counter(r["ticker"] for r in rows)
    cached = sum(1 for r in rows if r.get("cached"))
    live = n - cached
    tin = sum(r.get("prompt_tokens", 0) for r in rows)
    tout = sum(r.get("response_tokens", 0) for r in rows)
    cost = sum(cost_for(r["model"], r.get("prompt_tokens", 0), r.get("response_tokens", 0))
               for r in rows if not r.get("cached") and r["provider"] not in ("backfilled", "cache", "FAILED", "none"))
    live_lat = [r["total_time_ms"] for r in rows if not r.get("cached") and r.get("total_time_ms")]
    avg_lat = sum(live_lat) / len(live_lat) if live_lat else 0
    retried = sum(1 for r in rows if (r.get("attempts") or 0) > 1)
    parse_ok = sum(1 for r in rows if r.get("parse_success"))
    failed = [r for r in rows if r["provider"] in ("FAILED", "none")]

    print(f"\nTotal LLM calls recorded : {n}")
    print(f"  live / cached          : {live} / {cached}  ({100*cached/n:.0f}% cache)")
    print(f"  total tokens in / out  : {tin:,} / {tout:,}")
    print(f"  est. cost (list price) : ${cost:.4f}   (Groq/OpenRouter dev = $0 actual)")
    print(f"  avg live latency       : {avg_lat:.0f} ms")
    print(f"  parse-success rate     : {100*parse_ok/n:.1f}%")
    print(f"  retry rate (>1 attempt): {100*retried/n:.1f}%")
    print(f"  failed (chain exhausted): {len(failed)}")

    print("\nBy agent:")
    for k, v in by_agent.most_common():
        print(f"  {k:24s} {v:5d}  {bar(v, n)}")
    print("\nBy provider:")
    for k, v in by_provider.most_common():
        print(f"  {k:24s} {v:5d}  {bar(v, n)}")
    print("\nBy model:")
    for k, v in by_model.most_common():
        print(f"  {k:34s} {v:5d}")
    print("\nBy ticker (top 12):")
    for k, v in by_ticker.most_common(12):
        print(f"  {k:24s} {v:5d}")

    if failed:
        print("\nMost common failure errors:")
        errs = Counter()
        for r in failed:
            try:
                rec = json.load(open(os.path.join(LLM_HISTORY_DIR, r["path"])))
                for e in rec["retry_info"]["previous_errors"]:
                    errs[e.split(":")[0] + ": " + (e.split(":", 1)[1].strip()[:30] if ":" in e else "")] += 1
            except Exception:
                pass
        for k, v in errs.most_common(5):
            print(f"  {v:4d}  {k}")
    print()


if __name__ == "__main__":
    main()
