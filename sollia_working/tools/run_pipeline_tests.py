#!/usr/bin/env python3.11
"""Run the full 4-agent pipeline for one ticker and record metrics + raw output.

Writes:
  .test_results/{ticker}/{agent}.json        — metrics summary (Part 2 schema)
  .test_results/{ticker}/{agent}_raw.json     — full agent output (for Part 3 quote verification)
  .test_results/{ticker}/_summary.json        — combined per-ticker summary

Agents run SEQUENTIALLY (per project note: concurrent Groq calls trip the
per-minute limit). Every LLM call is attributed to (agent, ticker) via the
llm_config call-context so it lands in .cache/llm_history/.

Usage:
    python3.11 tools/run_pipeline_tests.py TICKER [YEAR]
"""
import sys
import os
import time
import json
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
WORKING = os.path.dirname(HERE)
ROOT = os.path.dirname(WORKING)
sys.path.insert(0, WORKING)

from src.config import llm_config as L                       # noqa: E402
from src.agents.corporate_discrepancy import run_discrepancy_agent  # noqa: E402
from src.agents.promise_audit import PromiseAuditAgent       # noqa: E402
from src.agents.legal_proceedings import track_legal         # noqa: E402
from src.agents.risk_factors import RiskFactorAgent          # noqa: E402

RESULTS_DIR = os.path.join(ROOT, ".test_results")


def _hist_count(ticker):
    """How many history records exist for this ticker right now (completeness check)."""
    import glob
    return len(glob.glob(os.path.join(L.LLM_HISTORY_DIR, "*", ticker, "*.json")))


def summarize(agent_id, result, runtime, hist_delta):
    """Best-effort metric extraction; tolerant of schema variation across agents."""
    s = {"status": "error" if (not isinstance(result, dict) or "error" in result) else "success",
         "runtime_seconds": round(runtime, 1),
         "llm_calls_recorded": hist_delta,
         "error": (result.get("error") if isinstance(result, dict) else str(result)[:300])
                  if (not isinstance(result, dict) or "error" in result) else None,
         "top_level_keys": sorted(result.keys()) if isinstance(result, dict) else []}
    if not isinstance(result, dict):
        return s
    from collections import Counter
    g = result.get
    if agent_id == "agent_1":
        td = g("top_discrepancies") or []
        s.update(documents_analyzed=g("documents_analyzed"),
                 total_aligned_pairs=g("total_aligned_pairs"),
                 discrepancies_found=g("total_discrepancies_found", len(td)),
                 top_score=(td[0].get("weight") or td[0].get("score")) if td else None,
                 score=g("score"))
    elif agent_id == "agent_2":
        tp = g("top_promises") or []
        dist = {k.upper(): g("total_" + k, 0) for k in
                ["kept", "broken", "partial", "pending", "stale", "unresolved"]}
        s.update(promises_extracted=g("total_promises_extracted", len(tp)),
                 filings_analyzed=g("total_filings_analyzed"),
                 score=g("score"), status_distribution={k: v for k, v in dist.items() if v})
    elif agent_id == "agent_3":
        finds = g("findings") or []
        procs = [p for f in finds for p in (f.get("proceedings") or [])]
        qr = sum(1 for p in procs if "REMOV" in str(p.get("change_type", "")).upper())
        s.update(findings_count=len(finds), proceedings_count=len(procs),
                 quietly_removed_count=qr,
                 score=(finds[0].get("agent3_score") if finds else None))
    elif agent_id == "agent_4":
        ch = g("top_changes") or []
        s.update(changes_found=g("changes_found", len(ch)),
                 distribution=dict(Counter(c.get("category") for c in ch)),
                 verified_count=sum(1 for c in ch if c.get("verified") in (True, "True")),
                 score=g("score"), signal=g("signal"))
    return s


def run_agent(agent_id, ticker, year):
    L.set_call_context(agent=agent_id, ticker=ticker, stage="run")
    before = _hist_count(ticker)
    t0 = time.time()
    try:
        if agent_id == "agent_1":
            result = run_discrepancy_agent(ticker, year)
        elif agent_id == "agent_2":
            result = PromiseAuditAgent().run(ticker)
        elif agent_id == "agent_3":
            result = track_legal(ticker)
        elif agent_id == "agent_4":
            result = RiskFactorAgent().run(ticker)
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()[-1500:]}
    runtime = time.time() - t0
    hist_delta = _hist_count(ticker) - before
    summary = summarize(agent_id, result, runtime, hist_delta)
    L.clear_call_context()

    tdir = os.path.join(RESULTS_DIR, ticker)
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, f"{agent_id}.json"), "w") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(tdir, f"{agent_id}_raw.json"), "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"  {agent_id}: {summary['status']:7s} {runtime:6.1f}s  llm_calls={hist_delta}  "
          f"{ {k: v for k, v in summary.items() if k not in ('status','runtime_seconds','error','top_level_keys','llm_calls_recorded')} }")
    if summary["status"] == "error":
        print(f"     ERROR: {summary['error']}")
    return summary


def main():
    if len(sys.argv) < 2:
        print("usage: run_pipeline_tests.py TICKER [YEAR]"); sys.exit(1)
    ticker = sys.argv[1].upper()
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    resummarize = "--resummarize" in sys.argv
    year = int(args[0]) if args else 2024
    only = args[1].split(",") if len(args) > 1 else ["agent_1", "agent_2", "agent_3", "agent_4"]
    tdir = os.path.join(RESULTS_DIR, ticker)
    overall = {"ticker": ticker, "year": year, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "agents": {}}

    if resummarize:  # re-derive metrics from saved *_raw.json without re-running agents
        print(f"=== RESUMMARIZE: {ticker} ===")
        for agent_id in only:
            raw_path = os.path.join(tdir, f"{agent_id}_raw.json")
            if not os.path.exists(raw_path):
                continue
            raw = json.load(open(raw_path))
            try:
                old = json.load(open(os.path.join(tdir, f"{agent_id}.json")))
                rt, hd = old.get("runtime_seconds", 0), old.get("llm_calls_recorded", 0)
            except Exception:
                rt, hd = 0, 0
            summary = summarize(agent_id, raw, rt, hd)
            with open(os.path.join(tdir, f"{agent_id}.json"), "w") as f:
                json.dump(summary, f, indent=2)
            overall["agents"][agent_id] = summary
            print(f"  {agent_id}: { {k: v for k, v in summary.items() if k not in ('top_level_keys','error')} }")
        with open(os.path.join(tdir, "_summary.json"), "w") as f:
            json.dump(overall, f, indent=2)
        return

    print(f"=== PIPELINE TEST: {ticker} (year={year}) agents={only} ===")
    for agent_id in only:
        overall["agents"][agent_id] = run_agent(agent_id, ticker, year)
    tdir = os.path.join(RESULTS_DIR, ticker)
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "_summary.json"), "w") as f:
        json.dump(overall, f, indent=2)
    ok = sum(1 for a in overall["agents"].values() if a["status"] == "success")
    print(f"=== {ticker}: {ok}/{len(only)} agents succeeded ===")


if __name__ == "__main__":
    main()
