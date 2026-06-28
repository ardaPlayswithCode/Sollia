#!/usr/bin/env python3.11
"""Backfill historical LLM calls into the new .cache/llm_history/ structure.

The legacy .cache/llm/ cache stored the full PROMPT alongside the response, so
those calls can be reconstructed (agent is inferred heuristically from prompt
text; ticker is unknown). The .llm_cache/ store keeps only response-by-hash
(no prompt) so it cannot be reconstructed and is skipped.

Records are marked {"backfilled": true}. Idempotent: re-running overwrites the
same per-call files (call_id is derived from the source filename).

Usage:
    python3.11 tools/backfill_llm_history.py
"""
import os
import sys
import json
import glob
import hashlib
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WORKING = os.path.dirname(HERE)
sys.path.insert(0, WORKING)
from src.config import llm_config as L  # noqa: E402

LEGACY_DIR = os.path.join(WORKING, ".cache", "llm")


def infer_agent(prompt: str) -> str:
    p = (prompt or "").lower()
    if "promise" in p:
        return "agent_2_promise_audit"
    if "discrepanc" in p or "contradiction" in p:
        return "agent_1_corporate_discrepancy"
    if "legal" in p or "proceeding" in p or "litigation" in p:
        return "agent_3_legal_proceedings"
    if "risk factor" in p:
        return "agent_4_risk_factors"
    return "backfilled_unknown"


def extract_response(blob) -> str:
    for k in ("response", "completion", "output", "result", "text", "content"):
        v = blob.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def main():
    files = sorted(glob.glob(os.path.join(LEGACY_DIR, "*.json")))
    print(f"Scanning {len(files)} legacy records in {LEGACY_DIR}")
    migrated = skipped = 0
    by_agent = {}
    for path in files:
        try:
            blob = json.load(open(path))
        except Exception:
            skipped += 1
            continue
        prompt = blob.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            skipped += 1
            continue
        response = extract_response(blob)
        agent = infer_agent(prompt)
        model = blob.get("model", "unknown")
        provider = blob.get("provider", "backfilled")
        mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        stem = hashlib.sha256(path.encode()).hexdigest()[:12]
        call_id = f"{agent}_backfill_legacy_{stem}"

        rec = L._build_record(call_id, agent, "backfilled", "unknown", provider, model,
                              0.0, 0, prompt, response, None, False, 0.0, 0, 0, [])
        rec["timestamp"] = mtime.isoformat().replace("+00:00", "Z")
        rec["backfilled"] = True
        rec["source_file"] = os.path.relpath(path, WORKING)
        L._record_history(rec)
        migrated += 1
        by_agent[agent] = by_agent.get(agent, 0) + 1

    print(f"Migrated {migrated}, skipped {skipped} (no usable prompt).")
    for a, c in sorted(by_agent.items(), key=lambda kv: -kv[1]):
        print(f"  {a:34s} {c}")
    print("Note: .llm_cache/ (response-only, no prompt) is NOT reconstructable and was not migrated.")


if __name__ == "__main__":
    main()
