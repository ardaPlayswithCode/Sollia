"""Fiscal year alignment utilities — shared across all Sollia agents.

Assigns SEC filings and earnings call transcripts to fiscal year buckets
using 10-K period_of_report as the anchor. Handles 52-53 week fiscal years,
transition reports (10-QT), and merger proxies (DEFM14A).

Architecture:
  1. 10-K period_of_report defines FY boundaries
  2. 10-Q period_of_report assigns to Q1/Q2/Q3 (no Q4 10-Q — 10-K covers it)
  3. Transcripts: map to the most recent quarter end (reporting quarter)
  4. DEF 14A: lookback 0-150 days after FY end
  5. 8-K: filing_date within FY range
"""
from __future__ import annotations
import re
from datetime import date, timedelta
from typing import List, Dict, Optional, Tuple


class FiscalYear:
    """Represents one fiscal year bucket."""
    def __init__(self, year: int, start: date, end: date,
                 q1_end: date, q2_end: date, q3_end: date,
                 ten_k_accession: str = "", ten_k_filing_date: str = ""):
        self.year = year
        self.start = start
        self.end = end
        self.q1_end = q1_end
        self.q2_end = q2_end
        self.q3_end = q3_end
        self.ten_k_accession = ten_k_accession
        self.ten_k_filing_date = ten_k_filing_date

    def get_quarter(self, d: date) -> str:
        if d <= self.q1_end: return "Q1"
        if d <= self.q2_end: return "Q2"
        if d <= self.q3_end: return "Q3"
        return "Q4"

    def __repr__(self):
        return f"FY{self.year}({self.start}..{self.end})"


def build_fiscal_boundaries(ten_ks: List[dict]) -> List[FiscalYear]:
    """Build FiscalYear list from 10-K filings sorted by period_of_report."""
    seen_fy = set()
    unique = []
    for k in ten_ks:
        if not k.get("period_of_report"):
            continue
        fy_year = date.fromisoformat(k["period_of_report"]).year
        if fy_year in seen_fy:
            for i, u in enumerate(unique):
                if date.fromisoformat(u["period_of_report"]).year == fy_year:
                    if k.get("filing_date", "") > u.get("filing_date", ""):
                        unique[i] = k
                    break
        else:
            seen_fy.add(fy_year)
            unique.append(k)
    unique.sort(key=lambda x: x["period_of_report"])
    
    boundaries = []
    for i, k in enumerate(unique):
        pe = date.fromisoformat(k["period_of_report"])
        ps = date.fromisoformat(unique[i-1]["period_of_report"]) + timedelta(days=1) if i > 0 else pe - timedelta(days=365)
        total_days = (pe - ps).days
        boundaries.append(FiscalYear(
            year=pe.year, start=ps, end=pe,
            q1_end=ps + timedelta(days=total_days // 4),
            q2_end=ps + timedelta(days=total_days // 2),
            q3_end=ps + timedelta(days=3 * total_days // 4),
            ten_k_accession=k.get("accession_number", ""),
            ten_k_filing_date=k.get("filing_date", ""),
        ))
    return boundaries


def refine_boundaries_with_10qs(boundaries: List[FiscalYear], ten_qs: List[dict]) -> List[FiscalYear]:
    """Use actual 10-Q period_of_report dates to refine quarter boundaries."""
    for fy in boundaries:
        fy_qs = []
        for q in ten_qs:
            if not q.get("period_of_report"):
                continue
            q_date = date.fromisoformat(q["period_of_report"])
            if fy.start <= q_date <= fy.end:
                fy_qs.append(q_date)
        fy_qs.sort()
        if len(fy_qs) >= 1: fy.q1_end = fy_qs[0]
        if len(fy_qs) >= 2: fy.q2_end = fy_qs[1]
        if len(fy_qs) >= 3: fy.q3_end = fy_qs[2]
    return boundaries


def assign_fy(doc_date: date, boundaries: List[FiscalYear], lookback_days: int = 0) -> Optional[FiscalYear]:
    """Assign a document date to a fiscal year."""
    for fy in boundaries:
        if fy.start <= doc_date <= fy.end + timedelta(days=lookback_days):
            return fy
    if boundaries and doc_date < boundaries[0].start:
        return boundaries[0]
    if boundaries and doc_date > boundaries[-1].end + timedelta(days=max(lookback_days, 0)):
        return boundaries[-1]
    return None


def assign_transcript(t_date: date, boundaries: List[FiscalYear], t_content: str = "") -> Tuple[Optional[FiscalYear], Optional[str]]:
    """Assign transcript to FY + quarter. Maps to the most recent quarter end."""
    fy = assign_fy(t_date, boundaries, lookback_days=90)
    if fy:
        candidates = [("Q1", fy.q1_end), ("Q2", fy.q2_end), ("Q3", fy.q3_end), ("Q4", fy.end)]
        best_q, best_dist = None, 999
        for q_name, q_end in candidates:
            dist = (t_date - q_end).days
            if 0 <= dist <= 60 and dist < best_dist:
                best_dist, best_q = dist, q_name
        if best_q:
            return fy, best_q
        return fy, fy.get_quarter(t_date)
    if t_content:
        q_text = extract_quarter_from_text(t_content)
        if q_text:
            for fy in reversed(boundaries):
                if fy.end < t_date:
                    return fy, q_text
            if boundaries:
                return boundaries[-1], q_text
    return None, None


def extract_quarter_from_text(text: str) -> Optional[str]:
    """Parse quarter reference from transcript text."""
    if not text:
        return None
    preview = text[:3000]
    m = re.search(r"(?<![A-Za-z])Q([1-4])\b", preview)
    if m:
        return f"Q{m.group(1)}"
    ordinal_map = {"first": "Q1", "second": "Q2", "third": "Q3", "fourth": "Q4"}
    m = re.search(r"(first|second|third|fourth)\s+quarter", preview, re.IGNORECASE)
    if m:
        return ordinal_map[m.group(1).lower()]
    return None


def classify_filing(filing: dict, boundaries: List[FiscalYear]) -> Tuple[Optional[FiscalYear], Optional[str]]:
    """Classify any filing into (FiscalYear, quarter)."""
    form = filing.get("form", "")
    period = filing.get("period_of_report", "")
    filing_date_str = filing.get("filing_date", "")
    doc_date = None
    if period:
        try: doc_date = date.fromisoformat(period)
        except ValueError: pass
    if not doc_date and filing_date_str:
        try: doc_date = date.fromisoformat(filing_date_str)
        except ValueError: pass
    if not doc_date:
        return None, None
    if form in ("10-K", "10-K/A"):
        for fy in boundaries:
            if fy.year == doc_date.year:
                return fy, "Q4"
    if form in ("10-Q", "10-Q/A", "10-QT"):
        fy = assign_fy(doc_date, boundaries)
        return (fy, fy.get_quarter(doc_date)) if fy else (None, None)
    if "14A" in form:
        fy = assign_fy(doc_date, boundaries, lookback_days=150)
        return fy, None
    if form in ("8-K", "8-K/A"):
        fy = assign_fy(doc_date, boundaries)
        return (fy, fy.get_quarter(doc_date)) if fy else (None, None)
    fy = assign_fy(doc_date, boundaries)
    return (fy, fy.get_quarter(doc_date)) if fy else (None, None)


def get_fy_bundle(ticker: str, all_filings: List[dict], all_transcripts: List) -> Dict[int, dict]:
    """Build complete FY bundles with all documents assigned."""
    ten_ks = sorted(
        [f for f in all_filings if f.get("form") in ("10-K", "10-K/A") and f.get("period_of_report")],
        key=lambda x: x["period_of_report"]
    )
    ten_qs = sorted(
        [f for f in all_filings if f.get("form") in ("10-Q", "10-Q/A") and f.get("period_of_report")],
        key=lambda x: x["period_of_report"]
    )
    boundaries = build_fiscal_boundaries(ten_ks)
    boundaries = refine_boundaries_with_10qs(boundaries, ten_qs)
    
    bundles = {}
    for fy in boundaries:
        bundles[fy.year] = {
            "fy": fy,
            "ten_k": fy.ten_k_accession,
            "def_14a": None,
            "quarters": {
                q: {"ten_q": None, "transcript": None, "eight_ks": []}
                for q in ["Q1", "Q2", "Q3", "Q4"]
            }
        }
    
    for q in ten_qs:
        fy, quarter = classify_filing(q, boundaries)
        if fy and quarter and fy.year in bundles and quarter in ("Q1", "Q2", "Q3"):
            bundles[fy.year]["quarters"][quarter]["ten_q"] = q.get("accession_number", "")
    
    for ek in all_filings:
        if ek.get("form") not in ("8-K", "8-K/A"):
            continue
        fy, quarter = classify_filing(ek, boundaries)
        if fy and quarter and fy.year in bundles:
            bundles[fy.year]["quarters"][quarter]["eight_ks"].append(ek.get("accession_number", ""))
    
    for d14 in all_filings:
        if "14A" not in d14.get("form", ""):
            continue
        fy, _ = classify_filing(d14, boundaries)
        if fy and fy.year in bundles:
            bundles[fy.year]["def_14a"] = d14.get("accession_number", "")
    
    for t in all_transcripts:
        t_date = None
        try: t_date = date.fromisoformat(str(t.date))
        except (ValueError, AttributeError): continue
        content = getattr(t, "content", "") or ""
        fy, quarter = assign_transcript(t_date, boundaries, content)
        if fy and quarter and fy.year in bundles:
            bundles[fy.year]["quarters"][quarter]["transcript"] = str(t.date)
    
    return bundles


def get_fy_bundles_in_window(
    ticker: str,
    all_filings: List[dict],
    all_transcripts: List,
    max_years: int = 10,
) -> Dict[int, dict]:
    """Get FY bundles within a rolling window of max_years from today.
    
    Always includes the current (possibly incomplete) fiscal year.
    For a new company with < max_years of history, returns all available FYs.
    """
    from datetime import date as _date, timedelta as _td
    cutoff = _date.today() - _td(days=365.25 * max_years)
    
    bundles = get_fy_bundle(ticker, all_filings, all_transcripts)
    
    # Keep FYs where the FY end date is >= cutoff
    # Always include the most recent FY (current, possibly incomplete)
    filtered = {}
    sorted_fys = sorted(bundles.keys())
    
    for fy_year in sorted_fys:
        b = bundles[fy_year]
        fy_end = b['fy'].end
        if fy_end >= cutoff or fy_year == sorted_fys[-1]:
            filtered[fy_year] = b
    
    return filtered
