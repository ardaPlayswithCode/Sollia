#!/usr/bin/env python3.11
"""Part 1d verification: Agent 4 on TSLA 5 times — parse-failure rate must be 0%.

A parse failure is the "Could not parse the comparison output" path: the LLM
responded but _parse_changes returned None. Legit "no changes" results count
as success. Prints per-run provider/timing and a final verdict; exit 0 = pass.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.config.llm_config import reset_run_budget
from src.utils.fetch_budget import install as install_fb, reset_fetch_budget
from src.agents.risk_factors import RiskFactorAgent

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
TICKER = sys.argv[2] if len(sys.argv) > 2 else "TSLA"

install_fb()
failures = 0
for i in range(1, RUNS + 1):
    reset_run_budget()
    reset_fetch_budget()
    t0 = time.time()
    r = RiskFactorAgent().run(TICKER)
    el = time.time() - t0
    summary = (r.get("summary") or "")
    parse_failed = ("could not parse" in summary.lower()
                    or "unparseable" in summary.lower())
    n_changes = len(r.get("top_changes") or [])
    tq = r.get("ten_q_findings") or {}
    tq_changes = len(tq.get("changes") or [])
    status = "PARSE-FAIL" if parse_failed else "ok"
    if parse_failed:
        failures += 1
    print(f"run {i}/{RUNS}: {status}  {el:5.1f}s  changes={n_changes} "
          f"10q_changes={tq_changes} score={r.get('score')}", flush=True)

rate = 100.0 * failures / RUNS
print(f"\nparse-failure rate: {failures}/{RUNS} = {rate:.0f}%")
sys.exit(0 if failures == 0 else 1)
