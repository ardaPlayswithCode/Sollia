"""WS5 + WS6 — run registry, live per-agent log emission, parallel pipeline
execution, and a clean analyst-facing JSON export schema.

The export schema is intentionally generic: top-level identity + signal + score,
plus an ``agents`` map. Each agent entry has the same shape
(``name``/``score``/``signal``/``top_findings[]`` where each finding is
``type``/``severity``/``summary``/``citations[]`` and each citation is
``document``/``excerpt``/``url``). Adding more agents just adds keys under
``agents`` — no schema change, and no internal fields (weights, similarity,
embeddings, chunk ids, cache keys) ever leak out.
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

AGENT_NAMES = {
    "corporate_discrepancy": "Corporate Discrepancy",
    "promise_audit": "Promise Audit",
    "legal_proceedings": "Legal Proceedings",
}

_RUNS: Dict[str, dict] = {}
_LOCK = threading.Lock()
_MAX_RUNS = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── registry ────────────────────────────────────────────────────────────────

def new_run(ticker: str, agents, year: int) -> str:
    run_id = uuid.uuid4().hex[:12]
    with _LOCK:
        if len(_RUNS) >= _MAX_RUNS:
            for rid in sorted(_RUNS, key=lambda r: _RUNS[r]["created"])[:10]:
                _RUNS.pop(rid, None)
        _RUNS[run_id] = {
            "run_id": run_id,
            "ticker": ticker,
            "year": year,
            "status": "running",  # running | complete | error
            "created": time.time(),
            "agents": {aid: {"id": aid, "name": AGENT_NAMES.get(aid, aid),
                             "status": "queued", "logs": []} for aid in agents},
            "result": None,
            "export": None,
            "error": None,
            "seq": 0,
        }
    return run_id


def emit(run_id: str, agent_id: str, message: str, kind: str = "info") -> None:
    """Append a live, human-readable milestone for an agent. Thread-safe."""
    with _LOCK:
        run = _RUNS.get(run_id)
        if not run:
            return
        agent = run["agents"].get(agent_id)
        if agent is None:
            agent = run["agents"][agent_id] = {
                "id": agent_id, "name": AGENT_NAMES.get(agent_id, agent_id),
                "status": "running", "logs": []}
        if agent["status"] == "queued":
            agent["status"] = "running"
        run["seq"] += 1
        agent["logs"].append({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "message": message,
            "kind": kind,
            "seq": run["seq"],
        })


def finalize(run_id: str, result: dict, export: dict) -> None:
    with _LOCK:
        run = _RUNS.get(run_id)
        if not run:
            return
        run["status"] = "complete"
        run["result"] = result
        run["export"] = export
        for a in run["agents"].values():
            if a["status"] in ("queued", "running"):
                a["status"] = "complete"


def fail(run_id: str, message: str, result: dict = None, export: dict = None) -> None:
    with _LOCK:
        run = _RUNS.get(run_id)
        if not run:
            return
        run["status"] = "error"
        run["error"] = message
        if result is not None:
            run["result"] = result
        if export is not None:
            run["export"] = export


def get(run_id: str) -> Optional[dict]:
    with _LOCK:
        run = _RUNS.get(run_id)
        return copy.deepcopy(run) if run else None


def snapshot_since(run_id: str, since_seq: int) -> Optional[dict]:
    """Snapshot containing only log lines with seq > since_seq (for streaming)."""
    with _LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            return None
        agents_out = {}
        max_seq = since_seq
        for aid, a in run["agents"].items():
            new_logs = [l for l in a["logs"] if l["seq"] > since_seq]
            for l in new_logs:
                if l["seq"] > max_seq:
                    max_seq = l["seq"]
            agents_out[aid] = {"id": aid, "name": a["name"], "status": a["status"],
                               "new_logs": new_logs}
        out = {"status": run["status"], "error": run["error"],
               "agents": agents_out, "max_seq": max_seq}
        if run["status"] in ("complete", "error"):
            out["result"] = run.get("result")
            out["export"] = run.get("export")
        return out


# ── clean export schema ───────────────────────────────────────────────────────

def _agent_signal(aid: str, score: float) -> str:
    if aid == "promise_audit":
        return "BULLISH" if score >= 62 else "BEARISH" if score < 39 else "NEUTRAL"
    if aid == "legal_proceedings":
        return "BULLISH" if score >= 60 else "BEARISH" if score <= 40 else "NEUTRAL"
    # corporate_discrepancy: a higher score means MORE discrepancies => more bearish
    return "BEARISH" if score >= 62 else "BULLISH" if score < 39 else "NEUTRAL"


def _sev_label(sev) -> str:
    if isinstance(sev, bool):
        return "medium"
    if isinstance(sev, (int, float)):
        return "high" if sev >= 7 else "medium" if sev >= 4 else "low"
    s = str(sev or "").lower()
    return s if s in ("high", "medium", "low") else "medium"


def _findings_corporate(result: dict):
    out = []
    for d in result.get("top_discrepancies", []):
        cits = []
        for s in (d.get("source_a"), d.get("source_b")):
            if s and (s.get("sentence_text") or s.get("edgar_url")):
                cits.append({"document": s.get("document_type", ""),
                             "excerpt": s.get("sentence_text", ""),
                             "url": s.get("edgar_url", "")})
        out.append({
            "type": d.get("discrepancy_type", "discrepancy"),
            "severity": _sev_label(d.get("severity")),
            "summary": d.get("reasoning", ""),
            "citations": cits,
        })
    return out


def _findings_promise(result: dict):
    out = []
    for p in result.get("top_promises", []):
        cits = []
        if p.get("text"):
            cits.append({"document": p.get("promise_source") or "Promise",
                         "excerpt": p.get("text", ""), "url": p.get("promise_url", "")})
        if p.get("evidence_text"):
            cits.append({"document": p.get("evidence_filing") or "Evidence",
                         "excerpt": p.get("evidence_text", ""), "url": p.get("evidence_url", "")})
        out.append({
            "type": "promise_" + str(p.get("status", "unresolved")).lower(),
            "severity": _sev_label(p.get("severity")),
            "summary": p.get("reasoning") or p.get("text", ""),
            "citations": cits,
        })
    return out


def _findings_legal(result: dict):
    out = []
    for f in result.get("findings", []):
        dt = f.get("doc_type", "")
        if f.get("empty") or not f.get("proceedings"):
            out.append({"type": "no_change", "severity": "low",
                        "summary": f.get("summary") or ("No legal proceedings in %s" % dt),
                        "citations": []})
            continue
        for p in f.get("proceedings", []):
            cits = []
            if p.get("prior_quote"):
                cits.append({"document": "%s (prior %s)" % (dt, f.get("prior_date", "")),
                             "excerpt": p.get("prior_quote", ""), "url": p.get("prior_edgar_url", "")})
            if p.get("current_quote"):
                cits.append({"document": "%s (current %s)" % (dt, f.get("current_date", "")),
                             "excerpt": p.get("current_quote", ""), "url": p.get("edgar_url", "")})
            out.append({
                "type": str(p.get("status") or p.get("change_type") or "update").lower(),
                "severity": _sev_label(p.get("priority")),
                "summary": p.get("case_name") or p.get("description") or f.get("summary", ""),
                "citations": cits,
            })
    return out


_FINDING_BUILDERS = {
    "corporate_discrepancy": _findings_corporate,
    "promise_audit": _findings_promise,
    "legal_proceedings": _findings_legal,
}


def _agent_score(aid: str, result: dict) -> float:
    if aid == "legal_proceedings":
        return round(float(result.get("agent3_score", 50)), 1)
    return round(float(result.get("score", 50)), 1)


def build_clean_export(ticker: str, year: int, merged: dict, results: dict) -> dict:
    agents = {}
    for aid, result in results.items():
        if not result:
            continue
        score = _agent_score(aid, result)
        builder = _FINDING_BUILDERS.get(aid)
        agents[aid] = {
            "name": AGENT_NAMES.get(aid, aid),
            "score": score,
            "signal": _agent_signal(aid, score),
            "top_findings": builder(result) if builder else [],
        }
    return {
        "ticker": ticker,
        "year": year,
        "timestamp": _now_iso(),
        "signal": merged.get("signal", "HOLD"),
        "score": merged.get("score", 50),
        "agents": agents,
    }


# ── rich result (for the UI) ──────────────────────────────────────────────────

def _strip_internal(obj):
    """Drop embedding arrays before serializing the rich result to the UI."""
    if isinstance(obj, dict):
        return {k: _strip_internal(v) for k, v in obj.items() if k not in ("embedding", "_emb_c")}
    if isinstance(obj, list):
        return [_strip_internal(i) for i in obj]
    return obj


def _build_rich_result(ticker: str, merged: dict, results: dict) -> dict:
    return {
        "ticker": ticker,
        "company": ticker,
        "score": merged.get("score", 50),
        "signal": merged.get("signal", "HOLD"),
        "signal_color": merged.get("signal_color", "#F59E0B"),
        "summary": merged.get("summary", ""),
        "agents_run": merged.get("agents_run", list(results.keys())),
        "similarity": merged.get("similarity", 0),
        "agent_weights": merged.get("agent_weights", {}),
        "agent_scores": merged.get("agent_scores", {}),
        "corporate_discrepancy": _strip_internal(results.get("corporate_discrepancy")),
        "promise_audit": _strip_internal(results.get("promise_audit")),
        "legal_proceedings": _strip_internal(results.get("legal_proceedings")),
    }


# ── pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline(run_id: str, ticker: str, selected, year: int):
    """Run the full pipeline for a run: parallel agents -> synthesis -> rich result
    (UI) + clean export (download). Updates the run store live; never raises."""
    from backend.agents import run_selected_agents
    from src.agents.synthesizer import Synthesizer

    def _emit(agent_id, message, kind="info"):
        emit(run_id, agent_id, message, kind)

    try:
        results, weights, _logs = run_selected_agents(ticker, selected, year, emit=_emit)

        if not results:
            merged = {"score": 50, "signal": "HOLD", "signal_color": "#F59E0B",
                      "summary": "No agents produced results.", "agents_run": []}
        else:
            try:
                merged = Synthesizer().run(results, weights, ticker, ticker)
            except Exception as e:
                merged = {"score": 50, "signal": "HOLD", "signal_color": "#F59E0B",
                          "summary": "Synthesis failed: %s" % str(e)[:120],
                          "agents_run": list(results.keys())}

        lp = results.get("legal_proceedings")
        if lp and lp.get("findings"):
            scs = [f.get("agent3_score", 50) for f in lp["findings"]]
            lp["agent3_score"] = sum(scs) / len(scs) if scs else 50
        elif lp:
            lp["agent3_score"] = 50

        rich = _build_rich_result(ticker, merged, results)
        export = build_clean_export(ticker, year, merged, results)
        finalize(run_id, rich, export)
        return rich, export
    except Exception as e:
        err = "Analysis failed: %s" % str(e)[:200]
        rich = {"ticker": ticker, "company": ticker, "score": 50, "signal": "ERROR",
                "signal_color": "#EF4444", "summary": err, "agents_run": [],
                "corporate_discrepancy": None, "promise_audit": None,
                "legal_proceedings": None}
        export = {"ticker": ticker, "year": year, "timestamp": _now_iso(),
                  "signal": "ERROR", "score": 50, "agents": {}, "error": err}
        fail(run_id, err, rich, export)
        return rich, export
