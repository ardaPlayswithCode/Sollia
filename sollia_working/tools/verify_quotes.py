#!/usr/bin/env python3.11
"""Independent quote verification (Part 3 of the audit).

For every quoted phrase an agent emitted, RE-DOWNLOAD the cited EDGAR filing,
extract its text ourselves, and confirm the quote is present verbatim
(normalized). Then compare our independent result against the agent's own
`*_verified` flag to catch any flag that lies (rubber-stamp / hallucination).

Writes .test_results/{ticker}/quote_verification.json and prints a summary.

Usage:
    python3.11 tools/verify_quotes.py TICKER
"""
import sys
import os
import re
import json
import time
import hashlib

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
WORKING = os.path.dirname(HERE)
ROOT = os.path.dirname(WORKING)
RESULTS_DIR = os.path.join(ROOT, ".test_results")
DLCACHE = os.path.join(WORKING, ".cache", "_verify_downloads")
os.makedirs(DLCACHE, exist_ok=True)

UA = {"User-Agent": "Sollia Audit contact@sollia.app"}

# Agent 5 citations are USGS PDFs and FRED series (not EDGAR HTML). Independent
# verification re-fetches the PDF (pdfplumber) or the FRED metadata (title) itself.
sys.path.insert(0, WORKING)
try:
    import pdfplumber  # type: ignore
except Exception:
    pdfplumber = None
try:
    from src.data.fred_client import verify_title as _fred_verify_title  # type: ignore
except Exception:
    _fred_verify_title = None


def _norm(s):
    s = re.sub(r"[‘’“”\"'`]", "", s or "")
    s = s.replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def _alnum(s):
    """Lowercase, alphanumeric-only. Eliminates whitespace/punctuation/apostrophe
    differences between HTML extractors — the #1 cause of false-negative quote
    matches (e.g. the agent's "Company s" vs our extraction "Company's"). A long
    alphanumeric run cannot coincidentally match, so this stays strict."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def quote_present(quote, text_alnum):
    """Alphanumeric substring match with an 80%-prefix fallback for trailing
    truncation/ellipsis."""
    q = _alnum((quote or "").rstrip("…"))
    if not q or not text_alnum:
        return False
    if q in text_alnum:
        return True
    probe = q[:max(20, int(len(q) * 0.8))]
    return len(probe) >= 20 and probe in text_alnum


def download_text(url):
    if not url:
        return ""
    key = hashlib.sha256(url.encode()).hexdigest()[:16]
    path = os.path.join(DLCACHE, key + ".txt")
    if os.path.exists(path):
        return open(path, encoding="utf-8", errors="ignore").read()
    try:
        time.sleep(0.2)  # be polite to EDGAR
        r = requests.get(url, headers=UA, timeout=60)
        if r.status_code != 200:
            return ""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        for t in soup(["script", "style"]):
            t.extract()
        text = _norm(soup.get_text(" "))
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return text
    except Exception:
        return ""


def download_pdf_text(url):
    """Independently re-download a USGS MCS PDF and extract its text (pdfplumber),
    so an Agent-5 supply/demand table-row quote can be confirmed verbatim."""
    if not url or pdfplumber is None:
        return ""
    key = hashlib.sha256(("pdf:" + url).encode()).hexdigest()[:16]
    path = os.path.join(DLCACHE, key + ".txt")
    if os.path.exists(path):
        return open(path, encoding="utf-8", errors="ignore").read()
    try:
        import io
        time.sleep(0.2)
        r = requests.get(url, headers=UA, timeout=60)
        if r.status_code != 200 or r.content[:4] != b"%PDF":
            return ""
        parts = []
        with pdfplumber.open(io.BytesIO(r.content)) as pdf:
            for pg in pdf.pages[:2]:
                parts.append(pg.extract_text() or "")
        text = _norm(" ".join(parts))
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return text
    except Exception:
        return ""


def verify_fred(url, quote):
    """Independently confirm a FRED citation: re-fetch the series metadata and check
    the cited title matches verbatim. Returns True/False, or None if unreachable."""
    if _fred_verify_title is None:
        return None
    m = re.search(r"/series/([A-Za-z0-9_.-]+)", url or "")
    if not m:
        return None
    try:
        return _fred_verify_title(m.group(1), quote)
    except Exception:
        return None


def collect_quotes(ticker):
    """Yield dicts: {agent, fid, side, quote, url, agent_flag[, source_type]}."""
    tdir = os.path.join(RESULTS_DIR, ticker)
    out = []

    def load(a):
        p = os.path.join(tdir, f"{a}_raw.json")
        return json.load(open(p)) if os.path.exists(p) else {}

    # Agent 1 — discrepancies (quoted_phrase_a/b cited via source_a/source_b)
    d1 = load("agent_1")
    for i, d in enumerate(d1.get("top_discrepancies", []) or []):
        sa, sb = d.get("source_a") or {}, d.get("source_b") or {}
        url_a = sa.get("edgar_url") or sa.get("htm_link") or ""
        url_b = sb.get("edgar_url") or sb.get("htm_link") or ""
        if d.get("quoted_phrase_a"):
            out.append(dict(agent="agent_1", fid=f"disc{i}", side="a",
                            quote=d["quoted_phrase_a"], url=url_a, agent_flag=None,
                            same_filing=d.get("same_filing")))
        if d.get("quoted_phrase_b"):
            out.append(dict(agent="agent_1", fid=f"disc{i}", side="b",
                            quote=d["quoted_phrase_b"], url=url_b, agent_flag=None,
                            same_filing=d.get("same_filing")))

    # Agent 2 — promises (text@promise_url, evidence_text@evidence_url)
    d2 = load("agent_2")
    for i, p in enumerate(d2.get("top_promises", []) or []):
        if p.get("text"):
            out.append(dict(agent="agent_2", fid=p.get("promise_id", f"prom{i}"), side="promise",
                            quote=p["text"], url=p.get("promise_url", ""), agent_flag=None))
        if p.get("evidence_text") and p.get("evidence_url"):
            out.append(dict(agent="agent_2", fid=p.get("promise_id", f"prom{i}"), side="evidence",
                            quote=p["evidence_text"], url=p.get("evidence_url", ""), agent_flag=None))

    # Agent 3 — legal proceedings (current/prior quote@edgar_url, with verified flags)
    d3 = load("agent_3")
    for fi, f in enumerate(d3.get("findings", []) or []):
        for pi, p in enumerate(f.get("proceedings", []) or []):
            cq = p.get("current_quote") or p.get("current_text") or p.get("quote")
            pq = p.get("prior_quote") or p.get("prior_text")
            if cq:
                out.append(dict(agent="agent_3", fid=f"f{fi}p{pi}", side="current",
                                quote=cq, url=p.get("edgar_url") or f.get("current_edgar_url", ""),
                                agent_flag=p.get("current_quote_verified")))
            if pq:
                out.append(dict(agent="agent_3", fid=f"f{fi}p{pi}", side="prior",
                                quote=pq, url=p.get("prior_edgar_url") or f.get("prior_edgar_url", ""),
                                agent_flag=p.get("prior_quote_verified")))

    # Agent 4 — risk changes (old/new quote@old/new_url, with verified flags)
    d4 = load("agent_4")
    for i, c in enumerate(d4.get("top_changes", []) or []):
        if c.get("old_quote"):
            out.append(dict(agent="agent_4", fid=f"chg{i}", side="old",
                            quote=c["old_quote"], url=c.get("old_url", ""),
                            agent_flag=c.get("old_quote_verified")))
        if c.get("new_quote"):
            out.append(dict(agent="agent_4", fid=f"chg{i}", side="new",
                            quote=c["new_quote"], url=c.get("new_url", ""),
                            agent_flag=c.get("new_quote_verified")))

    # Agent 5 — constraint auditor. Citations are USGS PDF table rows and FRED
    # series titles (+ optional management-CAGR from EDGAR). Tag each with a
    # source_type so main() picks the right independent re-fetch.
    def _src(url):
        if "pubs.usgs.gov" in (url or ""):
            return "usgs"
        if "fred.stlouisfed.org" in (url or ""):
            return "fred"
        return "edgar"

    d5 = load("agent_5")
    si = (d5.get("sector_indicators") or {}).get("production_index")
    if si and si.get("quote"):
        out.append(dict(agent="agent_5", fid="sector_index", side="fred_title",
                        quote=si["quote"], url=si.get("url", ""),
                        agent_flag=si.get("quote_verified"), source_type="fred"))
    for ci, c in enumerate(d5.get("constraints", []) or []):
        for side in ("supply", "demand"):
            blk = c.get(side) or {}
            if blk.get("quote"):
                out.append(dict(agent="agent_5", fid=f"c{ci}", side=side,
                                quote=blk["quote"], url=blk.get("url", ""),
                                agent_flag=blk.get("quote_verified"),
                                source_type=_src(blk.get("url", ""))))
        ind = c.get("indicator") or {}
        if ind.get("quote"):
            out.append(dict(agent="agent_5", fid=f"c{ci}", side="indicator",
                            quote=ind["quote"], url=ind.get("url", ""),
                            agent_flag=ind.get("quote_verified"), source_type="fred"))
    mgmt = (d5.get("projected_cagr") or {}).get("management") or {}
    if mgmt.get("quote") and str(mgmt.get("url", "")).startswith("http"):
        out.append(dict(agent="agent_5", fid="cagr_mgmt", side="management",
                        quote=mgmt["quote"], url=mgmt.get("url", ""),
                        agent_flag=mgmt.get("quote_verified"), source_type="edgar"))
    return out


def main():
    ticker = sys.argv[1].upper()
    quotes = collect_quotes(ticker)
    print(f"=== Quote verification: {ticker} ({len(quotes)} quotes) ===")
    results = []
    per_agent = {}
    alnum_cache = {}
    for q in quotes:
        st = q.get("source_type", "edgar")
        downloaded = True
        if st == "fred":
            # FRED: independently re-fetch series metadata; the title is the quote.
            present = verify_fred(q["url"], q["quote"])
            downloaded = present is not None
        else:
            if q["url"] not in alnum_cache:
                text = download_pdf_text(q["url"]) if st == "usgs" else download_text(q["url"])
                alnum_cache[q["url"]] = _alnum(text)
                q["_dl"] = bool(text)
            ta = alnum_cache[q["url"]]
            present = quote_present(q["quote"], ta) if ta else None
            downloaded = bool(ta)
        rec = dict(q, downloaded=downloaded, independently_present=present)
        # disagreement = the agent claimed verified True but we could not find it
        rec["agreement"] = (None if (q["agent_flag"] is None or present is None)
                            else (bool(q["agent_flag"]) == bool(present)))
        results.append(rec)
        a = per_agent.setdefault(q["agent"], {"n": 0, "present": 0, "absent": 0, "no_dl": 0,
                                              "flag_true": 0, "disagree": 0})
        a["n"] += 1
        if present is None:
            a["no_dl"] += 1
        elif present:
            a["present"] += 1
        else:
            a["absent"] += 1
        if q["agent_flag"] in (True, "True"):
            a["flag_true"] += 1
        if rec["agreement"] is False:
            a["disagree"] += 1

    tdir = os.path.join(RESULTS_DIR, ticker)
    os.makedirs(tdir, exist_ok=True)
    with open(os.path.join(tdir, "quote_verification.json"), "w") as f:
        json.dump({"ticker": ticker, "total": len(quotes), "per_agent": per_agent,
                   "results": results}, f, indent=2)

    for a, s in sorted(per_agent.items()):
        checkable = s["present"] + s["absent"]
        rate = f"{100*s['present']/checkable:.0f}%" if checkable else "n/a"
        print(f"  {a}: {s['n']:3d} quotes | verified {s['present']}/{checkable} ({rate}) | "
              f"absent {s['absent']} | not-downloaded {s['no_dl']} | "
              f"agent-flag-True {s['flag_true']} | disagreements {s['disagree']}")
    # surface any absent quotes (potential hallucinations / wrong URL)
    absents = [r for r in results if r["independently_present"] is False]
    if absents:
        print(f"\n  {len(absents)} quote(s) NOT found in source — investigate:")
        for r in absents[:8]:
            print(f"   [{r['agent']} {r['fid']} {r['side']}] flag={r['agent_flag']} "
                  f"q={r['quote'][:70]!r}")
    print("\nDONE")


if __name__ == "__main__":
    main()
