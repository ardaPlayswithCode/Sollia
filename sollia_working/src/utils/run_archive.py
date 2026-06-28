"""Comprehensive run archive (PART 3) — a self-contained, queryable record of
every agent run, designed for backtesting months later.

DESIGN — why Option B (Hybrid)
------------------------------
Three layouts were weighed:

  A. One big human-readable JSON per run (everything inline, incl. embeddings).
     + trivially inspectable, git-diffable.
     - 2-10 MB/run for Agent 1; embeddings as float arrays are unreadable noise;
       slow to read/write.
  C. A SQLite database.
     + compact, natively queryable.
     - opaque without tools; painful to eyeball a single run while debugging.
  B. HYBRID  ← chosen.
     * Human-readable JSON MANIFEST per run with all metadata, scores, citations,
       findings and a performance block — everything an analyst would read.
     * Large binary data (FinBERT embeddings) is NOT copied: it already lives in
       `.cache/embeddings/` as SHA-named, content-addressed files. The manifest
       references those hashes — the SHA *is* the pointer (Option B's "binary
       stored separately"), with automatic dedup across runs.
     * Full LLM prompts/responses already live in `.cache/llm_history/` (its own
       index.jsonl). The manifest references that store rather than duplicating
       multi-KB prompts into every run.
     * A master `index.jsonl` — one line per run — gives O(runs) lookup and
       backtest filtering without opening any manifest.

Layout::

    .cache/runs/
      index.jsonl                         one line per run (summary + path)
      agent_2/NVDA/2025/run_001.json      human-readable manifest
      agent_1/NVDA/2025/run_001_embeddings/   (only if caller passes raw vectors)

All writes are atomic (tmp + os.replace) and exception-safe at the call site:
archiving must NEVER break an agent run.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

_CACHE_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".cache"))
RUNS_ROOT = os.path.join(_CACHE_ROOT, "runs")
INDEX_PATH = os.path.join(RUNS_ROOT, "index.jsonl")

_index_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe(s: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(s))


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


# ── extraction helpers — turn a raw agent result into manifest sub-blocks ──────

def _extract_documents(agent_id: str, result: Dict) -> List[Dict]:
    """Best-effort list of the filings a run actually used (accession/url/date),
    pulled from wherever each agent records them."""
    docs: List[Dict] = []
    seen = set()

    def add(acc="", url="", date="", role=""):
        key = (acc, url)
        if (acc or url) and key not in seen:
            seen.add(key)
            docs.append({"accession": acc, "url": url, "date": date, "role": role})

    if agent_id == "legal_proceedings":
        for f in result.get("findings", []) or []:
            add(f.get("current_accession", ""), f.get("current_edgar_url", ""),
                f.get("current_date", ""), f"{f.get('doc_type','')} current")
            add(f.get("prior_accession", ""), f.get("prior_edgar_url", ""),
                f.get("prior_date", ""), f"{f.get('doc_type','')} prior")
    elif agent_id == "risk_factors":
        add(url=result.get("old_url", ""), date=result.get("old_filing_date", ""), role="10-K old")
        add(url=result.get("new_url", ""), date=result.get("new_filing_date", ""), role="10-K new")
    elif agent_id == "promise_audit":
        for p in result.get("top_promises", []) or []:
            src = p.get("source", {}) if isinstance(p, dict) else {}
            add(src.get("accession", ""), src.get("htm_url", ""), src.get("filing_date", ""), "promise source")
    elif agent_id == "corporate_discrepancy":
        for d in result.get("top_discrepancies", []) or []:
            for c in (d.get("citations", []) or []):
                add(c.get("accession", ""), c.get("url", ""), c.get("date", ""), c.get("role", ""))
    return docs


def _extract_findings(agent_id: str, result: Dict) -> List[Dict]:
    """The agent's primary output list, under one common key for backtesting."""
    if agent_id == "corporate_discrepancy":
        return result.get("top_discrepancies", []) or []
    if agent_id == "promise_audit":
        return result.get("top_promises", []) or []
    if agent_id == "legal_proceedings":
        out = []
        for f in result.get("findings", []) or []:
            out.extend(f.get("proceedings", []) or [])
        return out
    if agent_id == "risk_factors":
        return result.get("top_changes", []) or []
    return []


def build_manifest(agent_id: str, ticker: str, year: int, result: Dict,
                   *, run_id: str, duration_ms: Optional[float] = None,
                   llm_calls: Optional[List[Dict]] = None,
                   embedding_refs: Optional[List[str]] = None) -> Dict:
    """Assemble the human-readable manifest from an agent's result dict."""
    findings = _extract_findings(agent_id, result)
    perf = {
        "total_duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
        "llm_calls": len(llm_calls) if llm_calls else None,
        "llm_history_ref": ".cache/llm_history/ (full prompts/responses, indexed)",
        "embedding_store_ref": ".cache/embeddings/ (SHA-addressed FinBERT vectors)",
        "embedding_refs": embedding_refs or [],
    }
    return {
        "run_id": run_id,
        "schema_version": 1,
        "agent": agent_id,
        "ticker": ticker,
        "year": year,
        "timestamp": _now_iso(),
        "score": result.get("score"),
        "signal": result.get("signal"),
        "score_rationale": result.get("score_rationale", ""),
        "documents_used": _extract_documents(agent_id, result),
        "n_findings": len(findings),
        "findings": findings,
        "llm_calls": llm_calls or [],
        "performance": perf,
        "result": result,  # the complete, verbatim agent output
    }


def _next_run_path(agent_id: str, ticker: str, year: int) -> tuple:
    d = os.path.join(RUNS_ROOT, _safe(agent_id), _safe(ticker), _safe(year))
    os.makedirs(d, exist_ok=True)
    n = 1
    existing = [x for x in os.listdir(d) if re.match(r"run_\d+\.json$", x)]
    if existing:
        n = max(int(re.findall(r"\d+", x)[0]) for x in existing) + 1
    return d, n, os.path.join(d, "run_%03d.json" % n)


def _strip_embeddings(obj):
    """Drop raw embedding arrays from the manifest copy of the result — they are
    content-addressed in .cache/embeddings and only bloat the human-readable file."""
    if isinstance(obj, dict):
        return {k: _strip_embeddings(v) for k, v in obj.items()
                if k not in ("embedding", "_emb_c", "embeddings", "vector")}
    if isinstance(obj, list):
        return [_strip_embeddings(i) for i in obj]
    return obj


def save_run(agent_id: str, ticker: str, year: int, result: Dict,
             *, duration_ms: Optional[float] = None,
             llm_calls: Optional[List[Dict]] = None,
             embeddings: Optional[Dict[str, List[float]]] = None) -> Optional[str]:
    """Archive one agent run. Returns the run_id, or None on any failure
    (archiving must never break a run — callers should still wrap in try/except).

    `embeddings` (optional) maps a label -> vector; only pass it when you want the
    raw vectors copied into run_NNN_embeddings/. By default embeddings are NOT
    copied (they already live, deduplicated, in .cache/embeddings).

    Set SOLLIA_ARCHIVE=0 to disable archiving entirely (e.g. throwaway tests)."""
    if os.environ.get("SOLLIA_ARCHIVE", "1") == "0":
        return None
    try:
        ticker = (ticker or "").upper()
        d, n, manifest_path = _next_run_path(agent_id, ticker, year)
        run_id = "%s_%s_%s_%s" % (agent_id, _safe(ticker), _safe(year),
                                  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ"))

        embedding_refs = []
        if embeddings:
            edir = os.path.join(d, "run_%03d_embeddings" % n)
            os.makedirs(edir, exist_ok=True)
            for label, vec in embeddings.items():
                _atomic_write(os.path.join(edir, _safe(label) + ".json"),
                              json.dumps(vec))
                embedding_refs.append("run_%03d_embeddings/%s.json" % (n, _safe(label)))

        result_clean = _strip_embeddings(result)
        manifest = build_manifest(agent_id, ticker, year, result_clean, run_id=run_id,
                                  duration_ms=duration_ms, llm_calls=llm_calls,
                                  embedding_refs=embedding_refs)
        _atomic_write(manifest_path, json.dumps(manifest, indent=2, default=str))

        rel = os.path.relpath(manifest_path, RUNS_ROOT)
        index_line = {
            "run_id": run_id, "agent": agent_id, "ticker": ticker, "year": year,
            "timestamp": manifest["timestamp"], "score": manifest["score"],
            "signal": manifest["signal"], "n_findings": manifest["n_findings"],
            "duration_ms": manifest["performance"]["total_duration_ms"],
            "manifest": rel,
        }
        with _index_lock:
            os.makedirs(RUNS_ROOT, exist_ok=True)
            with open(INDEX_PATH, "a") as f:
                f.write(json.dumps(index_line, default=str) + "\n")
        return run_id
    except Exception:
        return None


# ── readers / queries ─────────────────────────────────────────────────────────

def _read_index() -> List[Dict]:
    if not os.path.exists(INDEX_PATH):
        return []
    out = []
    with open(INDEX_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def load_run(run_id: str) -> Optional[Dict]:
    """Load a full manifest by run_id (via the index)."""
    for e in _read_index():
        if e.get("run_id") == run_id:
            p = os.path.join(RUNS_ROOT, e["manifest"])
            if os.path.exists(p):
                return json.load(open(p))
    return None


def list_runs(ticker: Optional[str] = None, agent: Optional[str] = None,
              year: Optional[int] = None) -> List[Dict]:
    """Return index entries (newest first) filtered by ticker/agent/year."""
    rows = _read_index()
    if ticker:
        rows = [r for r in rows if r.get("ticker") == ticker.upper()]
    if agent:
        rows = [r for r in rows if r.get("agent") == agent]
    if year is not None:
        rows = [r for r in rows if str(r.get("year")) == str(year)]
    return sorted(rows, key=lambda r: r.get("timestamp", ""), reverse=True)


def backtest_query(*, agent: Optional[str] = None, ticker: Optional[str] = None,
                   year: Optional[int] = None, signal: Optional[str] = None,
                   min_score: Optional[float] = None, max_score: Optional[float] = None,
                   finding_contains: Optional[str] = None) -> List[Dict]:
    """Find runs matching criteria. Index-level filters (agent/ticker/year/signal/
    score band) are evaluated cheaply on index.jsonl; `finding_contains` does a
    substring scan of each candidate's manifest findings (case-insensitive) so you
    can ask e.g. backtest_query(agent='promise_audit', signal='BEARISH',
    finding_contains='gross margin'). Returns a list of
    {index entry + 'matched_findings'}."""
    rows = list_runs(ticker=ticker, agent=agent, year=year)
    if signal:
        rows = [r for r in rows if str(r.get("signal", "")).upper() == signal.upper()]
    if min_score is not None:
        rows = [r for r in rows if isinstance(r.get("score"), (int, float)) and r["score"] >= min_score]
    if max_score is not None:
        rows = [r for r in rows if isinstance(r.get("score"), (int, float)) and r["score"] <= max_score]

    results = []
    needle = (finding_contains or "").lower()
    for r in rows:
        matched = None
        if needle:
            p = os.path.join(RUNS_ROOT, r.get("manifest", ""))
            if not os.path.exists(p):
                continue
            try:
                man = json.load(open(p))
            except Exception:
                continue
            matched = [f for f in man.get("findings", [])
                       if needle in json.dumps(f, default=str).lower()]
            if not matched:
                continue
        entry = dict(r)
        if matched is not None:
            entry["matched_findings"] = matched
        results.append(entry)
    return results
