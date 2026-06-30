"""WS5 + WS6 — run registry, live per-agent log emission, parallel pipeline
execution, and a clean analyst-facing JSON export schema.

The export schema is intentionally generic: top-level identity + signal + score,
plus an ``agents`` map. Each agent entry has the same shape
(``name``/``score``/``signal``/``top_findings[]``). Each finding carries
``type``/``severity``/``summary``/``citations[]`` plus the context a downstream
risk system needs to act without opening the UI: ``severity_reason`` (why a
high/medium finding matters) and one agent-specific qualifier — ``pair`` (which
two documents were compared), ``evidence_quality`` (how well evidence backs a
promise), or ``change_direction`` (whether a legal change improves or worsens the
picture). Each citation carries ``role`` (which side of the comparison it is),
``document``/``excerpt``/``url``, and ``verified`` (whether the excerpt was
confirmed present in the cited filing).

Adding more agents just adds keys under ``agents`` — no schema change, and no
internal fields (weights, similarity, embeddings, chunk ids, score components,
cache keys, model names, thresholds) ever leak out.
"""
from __future__ import annotations

import copy
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

AGENT_NAMES = {
    "corporate_discrepancy": "Corporate Discrepancy",
    "promise_audit": "Promise Audit",
    "legal_proceedings": "Legal Proceedings",
    "risk_factors": "Risk Factors",
    "constraint_auditor": "Constraint Auditor",
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

def _agent_signal(aid: str, score) -> str:
    """Map any agent's score to a signal on the unified BULLISHNESS scale.

    AUDIT agents report score=None (they pressure-test assumptions rather than
    casting a bull/bear vote); their badge is "AUDIT", not a directional signal.

    Every agent now reports the same way — higher = better prospects, 50 = "nothing
    material changed" (Agent 1's discrepancy severity is inverted at its source,
    Agent 4's risk delta already pushes escalations DOWN). So one shared threshold
    serves them all and the UI badge always agrees with this export: above 65
    BULLISH, below 35 BEARISH, otherwise NEUTRAL. Mirrors scoring.classify_signal
    (>=65 / <=35) so the export badge always agrees with each agent's own signal.
    """
    if score is None:
        return "AUDIT"
    if score >= 65:
        return "BULLISH"
    if score <= 35:
        return "BEARISH"
    return "NEUTRAL"


def _sev_label(sev) -> str:
    if isinstance(sev, bool):
        return "medium"
    if isinstance(sev, (int, float)):
        return "high" if sev >= 7 else "medium" if sev >= 4 else "low"
    s = str(sev or "").lower()
    return s if s in ("high", "medium", "low") else "medium"


def _sig_words(text: str) -> set:
    """Significant (4+ char, non-stopword) tokens — used to gauge how strongly a
    piece of evidence overlaps the claim it is meant to support."""
    stop = {"that", "this", "with", "from", "have", "will", "were", "which",
            "their", "would", "there", "about", "than", "these", "those",
            "been", "into", "such", "also", "they", "them", "your", "our"}
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 3 and w not in stop}


def _corporate_severity_reason(dtype: str, severity: str) -> str:
    """One sentence on why a cross-filing discrepancy warrants attention, keyed to
    the kind of contradiction (no fabricated magnitudes — only what we know)."""
    reasons = {
        "numerical_contradiction": "Reported financial figures conflict between the two filings.",
        "narrative_contradiction": "Management's account of the same matter conflicts between filings.",
        "omission_discrepancy": "Material information disclosed in one filing is absent from the other.",
        "qualitative_tension": "The characterization of the same matter diverges between filings.",
    }
    return reasons.get(dtype, "Cross-filing inconsistency material enough to warrant review.")


def _promise_severity_reason(category: str, status: str) -> str:
    """One sentence on why a tracked promise matters — its domain plus its outcome."""
    domain = {
        "revenue": "revenue commitment", "product": "product roadmap commitment",
        "margin": "margin commitment", "rd": "R&D commitment",
        "market": "market-expansion commitment",
    }.get(str(category or "").lower(), "strategic commitment")
    s = str(status or "").upper()
    if s == "KEPT":
        return "Material %s with confirmed follow-through in a later filing." % domain
    if s == "BROKEN":
        return "Material %s with no or contradicting follow-through." % domain
    if s == "PARTIAL":
        return "Material %s only partially fulfilled in later filings." % domain
    if s in ("PENDING", "STALE"):
        return "Material %s still open with no confirming evidence yet." % domain
    return "Material %s with no located follow-through evidence." % domain


def _legal_severity_reason(status: str) -> str:
    """One sentence on why a legal disclosure warrants attention. Covers both
    changes (new/escalated/settled/…) and material proceedings carried forward
    unchanged, so the wording never contradicts the status."""
    return {
        "NEW": "New legal proceeding or material agreement disclosed.",
        "ESCALATED": "Previously disclosed proceeding escalated in scope or exposure.",
        "SETTLED": "Legal proceeding reported as settled.",
        "DISMISSED": "Legal proceeding reported as dismissed.",
        "QUIETLY_REMOVED": "Previously disclosed proceeding no longer appears, without explanation.",
        "AMENDED": "Material agreement amended between filings.",
        "TERMINATED": "Material agreement terminated between filings.",
        "UNCHANGED": "Material legal proceeding remains open and unresolved between filings.",
        "NO_UPDATE": "Material legal proceeding remains open and unresolved between filings.",
    }.get(str(status or "").upper(), "Material legal matter significant enough to warrant review.")


# settled/dismissed read as improving; new/escalated/quietly-removed as worsening;
# everything else neutral. Mirrors Agent 3's own scoring polarity.
_LEGAL_DIRECTION = {
    "SETTLED": "positive", "DISMISSED": "positive",
    "NEW": "negative", "ESCALATED": "negative", "QUIETLY_REMOVED": "negative",
}


def _change_direction(status: str) -> str:
    return _LEGAL_DIRECTION.get(str(status or "").upper(), "neutral")


def _evidence_quality(promise: dict) -> str:
    """How strongly the located evidence backs the promise: significant-word overlap
    with the matcher's own confidence as a tie-break. 'strong' clearly confirms,
    'partial' shares some terms, 'weak' has minimal or no overlap / no evidence."""
    evidence = (promise.get("evidence_text") or "").strip()
    if not evidence:
        return "weak"
    overlap = len(_sig_words(promise.get("text", "")) & _sig_words(evidence))
    confidence = str(promise.get("confidence", "")).lower()
    if overlap >= 5 or confidence == "high":
        return "strong"
    if overlap >= 3 or confidence == "medium":
        return "partial"
    return "weak"


def _findings_corporate(result: dict):
    out = []
    for d in result.get("top_discrepancies", []):
        cits = []
        for role, s in (("source_a", d.get("source_a")), ("source_b", d.get("source_b"))):
            if s and (s.get("sentence_text") or s.get("edgar_url")):
                excerpt = s.get("sentence_text", "")
                cits.append({
                    "role": role,
                    "document": s.get("document_type", ""),
                    "excerpt": excerpt,
                    "url": s.get("edgar_url", ""),
                    # sentence_text is pulled verbatim from the parsed filing chunk,
                    # so a non-empty excerpt is confirmed present in the document.
                    "verified": bool(excerpt.strip()),
                })
        severity = _sev_label(d.get("severity"))
        dtype = d.get("discrepancy_type", "discrepancy")
        finding = {"type": dtype, "severity": severity}
        if severity in ("high", "medium"):
            finding["severity_reason"] = _corporate_severity_reason(dtype, severity)
        finding["pair"] = d.get("pair_display") or d.get("pair", "").replace("_vs_", " vs ")
        finding["summary"] = d.get("reasoning", "")
        finding["citations"] = cits
        out.append(finding)
    return out


def _findings_promise(result: dict):
    out = []
    for p in result.get("top_promises", []):
        cits = []
        if p.get("text"):
            # The extractor only keeps a promise whose text matches a real filing
            # sentence (verbatim or fuzzy-replaced), so the excerpt is document-backed.
            cits.append({
                "role": "promise",
                "document": p.get("promise_source") or "Promise",
                "excerpt": p.get("text", ""),
                "url": p.get("promise_url", ""),
                "verified": bool((p.get("text") or "").strip()),
            })
        if p.get("evidence_text"):
            cits.append({
                "role": "evidence",
                "document": p.get("evidence_filing") or "Evidence",
                "excerpt": p.get("evidence_text", ""),
                "url": p.get("evidence_url", ""),
                "verified": bool((p.get("evidence_text") or "").strip()),
            })
        severity = _sev_label(p.get("severity"))
        status = str(p.get("status", "unresolved"))
        finding = {"type": "promise_" + status.lower(), "severity": severity}
        if severity in ("high", "medium"):
            finding["severity_reason"] = _promise_severity_reason(p.get("category"), status)
        finding["evidence_quality"] = _evidence_quality(p)
        finding["summary"] = p.get("reasoning") or p.get("text", "")
        finding["citations"] = cits
        out.append(finding)
    return out


def _findings_legal(result: dict):
    out = []
    for f in result.get("findings", []):
        dt = f.get("doc_type", "")
        if f.get("empty") or not f.get("proceedings"):
            out.append({
                "type": "no_change", "severity": "low",
                "change_direction": "neutral",
                "summary": f.get("summary") or ("No legal proceedings in %s" % dt),
                "citations": [],
            })
            continue
        for p in f.get("proceedings", []):
            cits = []
            if p.get("prior_quote"):
                cits.append({
                    "role": "prior",
                    "document": "%s (prior %s)" % (dt, f.get("prior_date", "")),
                    "excerpt": p.get("prior_quote", ""),
                    "url": p.get("prior_edgar_url", ""),
                    "verified": bool(p.get("prior_quote_verified")),
                })
            if p.get("current_quote"):
                cits.append({
                    "role": "current",
                    "document": "%s (current %s)" % (dt, f.get("current_date", "")),
                    "excerpt": p.get("current_quote", ""),
                    "url": p.get("edgar_url", ""),
                    "verified": bool(p.get("current_quote_verified")),
                })
            status = p.get("status") or p.get("change_type") or "update"
            severity = _sev_label(p.get("priority"))
            finding = {"type": str(status).lower(), "severity": severity}
            if severity in ("high", "medium"):
                finding["severity_reason"] = _legal_severity_reason(status)
            finding["change_direction"] = _change_direction(status)
            finding["summary"] = p.get("case_name") or p.get("description") or f.get("summary", "")
            finding["citations"] = cits
            out.append(finding)
    return out


# A risk ESCALATING or appearing reads as worsening; softening or disappearing as
# improving; a pure reword as neutral. Mirrors Agent 4's own scoring polarity,
# where REMOVED/DE-ESCALATED push the score UP (bullish) and ESCALATED/NEW push it
# DOWN (bearish) — so a removed/receded risk is a "positive" change_direction.
_RISK_DIRECTION = {
    "ESCALATED": "negative", "NEW": "negative",
    "REMOVED": "positive", "DE-ESCALATED": "positive", "REWORDED": "neutral",
}

_RISK_SEVERITY = {
    "ESCALATED": "high", "NEW": "high", "REMOVED": "medium",
    "DE-ESCALATED": "low", "REWORDED": "low",
}


def _risk_severity_reason(category: str) -> str:
    """One sentence on why a change in Item 1A warrants attention, keyed to how
    the risk language moved between the two annual filings."""
    return {
        "ESCALATED": "A previously disclosed risk's language strengthened, signaling a worsening situation.",
        "NEW": "A new risk factor appeared that was absent from the prior 10-K.",
        "REMOVED": "A risk factor from the prior 10-K no longer appears in the current filing.",
        "DE-ESCALATED": "A previously disclosed risk's language softened, signaling a receding concern.",
        "REWORDED": "A risk factor was restructured or reworded without a clear change in intensity.",
    }.get(str(category or "").upper(), "Material change in Item 1A risk factors between annual filings.")


def _risk_finding(c: dict, source: str, old_doc: str, new_doc: str) -> dict:
    """Build one analyst-ready finding from a risk-change row. `source` is the
    filing the comparison came from ("10-K" or "10-Q"); old_doc/new_doc are the
    citation document labels for the OLD and NEW quotes."""
    cits = []
    if c.get("old_quote"):
        cits.append({
            "role": "old",
            "document": old_doc,
            "excerpt": c.get("old_quote", ""),
            "url": c.get("old_url", ""),
            "verified": bool(c.get("old_quote_verified")),
        })
    if c.get("new_quote"):
        cits.append({
            "role": "new",
            "document": new_doc,
            "excerpt": c.get("new_quote", ""),
            "url": c.get("new_url", ""),
            "verified": bool(c.get("new_quote_verified")),
        })
    category = str(c.get("category", "")).upper()
    severity = _RISK_SEVERITY.get(category, "medium")
    finding = {"type": "risk_" + category.lower().replace("-", "_"), "severity": severity, "source": source}
    if severity in ("high", "medium"):
        finding["severity_reason"] = _risk_severity_reason(category)
    finding["change_direction"] = _RISK_DIRECTION.get(category, "neutral")
    finding["summary"] = c.get("summary") or c.get("risk_title", "")
    finding["citations"] = cits
    return finding


def _findings_risk(result: dict):
    # Annual 10-K vs 10-K delta (the baseline comparison).
    out = [
        _risk_finding(
            c, "10-K",
            "10-K (prior %s)" % result.get("old_filing_date", ""),
            "10-K (current %s)" % result.get("new_filing_date", ""))
        for c in result.get("top_changes", [])
    ]
    # 10-Q update findings (more recent than the annual delta), when present. The
    # OLD label reflects the actual baseline (10-K or previous 10-Q); the NEW
    # label is the 10-Q. Each carries source "10-Q".
    tqf = result.get("ten_q_findings")
    if tqf and tqf.get("changes"):
        old_doc = "%s (%s)" % (tqf.get("compared_against", "10-K"), tqf.get("compared_date", ""))
        new_doc = "10-Q (%s)" % tqf.get("filing_date", "")
        out.extend(_risk_finding(c, "10-Q", old_doc, new_doc) for c in tqf.get("changes", []))
    return out


_CONSTRAINT_SEVERITY = {"red": "high", "orange": "medium", "yellow": "low",
                        "green": "low", "gray": "info"}


def _findings_constraint(result: dict):
    """One finding per constraining material: the crossover quarters + supply/demand
    citations. Materials whose data couldn't be retrieved are surfaced too (so the
    analyst sees what was and wasn't evaluable), ordered earliest-crossover first."""
    out = []
    for c in result.get("constraints", []) or []:
        cits = []
        for which, role in (("supply", "supply"), ("demand", "demand")):
            blk = c.get(which) or {}
            if blk.get("quote"):
                cits.append({"role": role,
                             "document": "%s (%s)" % (blk.get("source", ""), blk.get("date", "")),
                             "excerpt": blk.get("quote", ""), "url": blk.get("url", ""),
                             "verified": bool(blk.get("quote_verified", True))})
        status = c.get("status")
        if status == "success":
            summary = "%s — demand crosses supply: management %s, industry %s" % (
                c.get("material", "?"), c.get("crossover_management"), c.get("crossover_industry"))
        else:
            summary = "%s — %s: %s" % (c.get("material", "?"), status, c.get("reason", ""))
        out.append({
            "type": "constraint",
            "severity": _CONSTRAINT_SEVERITY.get(c.get("severity", "gray"), "info"),
            "source": "Market & Economic Data",
            "summary": summary,
            "crossover_management": c.get("crossover_management"),
            "crossover_industry": c.get("crossover_industry"),
            "model_note": c.get("model_note", ""),
            "citations": cits,
        })
    return out


_FINDING_BUILDERS = {
    "corporate_discrepancy": _findings_corporate,
    "promise_audit": _findings_promise,
    "legal_proceedings": _findings_legal,
    "risk_factors": _findings_risk,
    "constraint_auditor": _findings_constraint,
}


def _agent_score(aid: str, result: dict):
    # AUDIT agents (Constraint Auditor) carry score=None — they don't vote on the
    # composite (PART 6b). Preserve the null so the export badge reads "AUDIT".
    raw = result.get("score", 50)
    if raw is None:
        return None
    # Agent 3 now emits an agent-level `score` (Part 2); fall back to the legacy
    # agent3_score for any older cached result that predates the redesign.
    if aid == "legal_proceedings":
        return round(float(result.get("score", result.get("agent3_score", 50))), 1)
    return round(float(raw), 1)


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
        "risk_factors": _strip_internal(results.get("risk_factors")),
        "constraint_auditor": _strip_internal(results.get("constraint_auditor")),
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

        # Agent 3 now computes a single agent-level legal-trend score over ALL
        # proceedings (Part 2) and sets both `score` and `agent3_score`. The old
        # code here averaged per-finding scores (which double-counted empty
        # findings at 50); just trust the agent's value, defaulting defensively.
        lp = results.get("legal_proceedings")
        if lp and "score" in lp:
            lp["agent3_score"] = lp["score"]
        elif lp:
            lp["agent3_score"] = lp.get("agent3_score", 50)

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
