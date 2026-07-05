"""
Sollia Agent 5 — Constraint Auditor.

The first market-style agent: it pressure-tests whether a sector's PROJECTED
growth rate is physically possible given raw-material SUPPLY constraints. For each
constraining material it models a supply curve and a two-phase demand curve and
reports the calendar quarter when demand crosses supply — the moment the growth
assumption becomes physically binding.

This is an AUDIT tool, not a forecast. It produces a constraint finding, not a
bullish/bearish score (score is null in the synthesizer — PART 6b).

Provenance contract (PART 9): every number is traceable to a specific sentence in
a government or industry source, with an exact verbatim quote, a link, and a
publication date. No synthetic numbers. When a source is unreachable or its data
can't be cleanly extracted, the material degrades to status="data_unavailable"
with a reason — we never fabricate (PART 7.6).

Determinism (PART 7d): the only LLM call is sector classification, cached
permanently per ticker. Everything else (CAGR extraction, supply/demand parsing,
crossover math) is deterministic. `run_timestamp`/`elapsed_ms` are the only
time-varying fields; strip them to compare two runs byte-for-byte.

Environment note: live USGS PDF parsing needs a table parser (camelot/pdfplumber)
and FRED/EIA/Census need API keys. Where those are absent the material/industry
data degrades gracefully (PART 7.6) and the agent logs exactly what to provision.
The crossover engine itself is pure stdlib math and is fully exercised by
`_selftest_crossover()` (`python -m src.agents.constraint_auditor --selftest`).
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
import math
from datetime import datetime, date, timezone
from typing import Any, Dict, List, Optional, Tuple, Callable

import requests

# Make `from src.xxx import ...` work standalone (file is src/agents/constraint_auditor.py;
# three dirnames up = the sollia_working root that holds the `src` package).
_SOLLIA_WORKING = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SOLLIA_WORKING not in sys.path:
    sys.path.insert(0, _SOLLIA_WORKING)

from src.config.llm_config import call_llm, start_tier  # noqa: E402
from src.data.constraint_database import (  # noqa: E402
    SECTOR_DATABASE, SUPPORTED_SECTORS, get_sector,
)
from src.data.sector_sources import SOURCE_REGISTRY, missing_capabilities  # noqa: E402

try:  # USGS MCS PDF parser (pdfplumber); optional — degrades to data_unavailable
    from src.data.usgs_parser import extract as usgs_extract
except Exception:  # pragma: no cover
    usgs_extract = None  # type: ignore

try:  # FRED client (needs FRED_API_KEY); optional — degrades silently if absent
    from src.data.fred_client import fetch_fred
except Exception:  # pragma: no cover
    fetch_fred = None  # type: ignore

try:  # transcripts + filings (already used by Agents 1-4)
    from src.providers.transcripts.sec_transcript import SECTranscriptProvider
    from src.providers.filings.sec_edgar import SECEdgarProvider
except Exception:  # pragma: no cover - providers always present in the pipeline
    SECTranscriptProvider = None  # type: ignore
    SECEdgarProvider = None  # type: ignore

try:
    from src.utils.run_archive import save_run
except Exception:  # pragma: no cover
    def save_run(*_a, **_k):  # type: ignore
        return None


# ── cache locations (PART 1b classification cache + PART 7.5b market-data cache) ──
_CACHE_ROOT = os.path.join(_SOLLIA_WORKING, ".cache")
SECTOR_CACHE_DIR = os.path.join(_CACHE_ROOT, "sector_classifications")
MARKET_DATA_DIR = os.path.join(_CACHE_ROOT, "market_data")
os.makedirs(SECTOR_CACHE_DIR, exist_ok=True)
os.makedirs(MARKET_DATA_DIR, exist_ok=True)

AGENT_ID = "constraint_auditor"
CROSSOVER_HORIZON_MONTHS = 240  # PART 4c: 20-year window


# ===========================================================================
# Text helpers (deterministic — regex sentence split, no spaCy dependency)
# ===========================================================================
def _norm(s: Optional[str]) -> str:
    """Normalize smart quotes / whitespace / case for robust substring matching."""
    if not s:
        return ""
    s = re.sub(r"[‘’“”\"']", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _quote_in_text(quote: Optional[str], source: Optional[str]) -> bool:
    """True when a quote is actually present in the source (PART 7b #8). Same
    alphanumeric/whitespace-normalized match Agents 1-4 use, so a consumer can
    trust a citation without re-reading the source. A representative prefix is
    enough for long quotes (trailing truncation isn't penalized)."""
    q, src = _norm(quote), _norm(source)
    if not q or not src:
        return False
    if q in src:
        return True
    probe = q[:60]
    return len(probe) >= 20 and probe in src


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9$\"'])")


def _split_sentences(text: str) -> List[str]:
    """Deterministic sentence segmentation. spaCy (en_core_web_sm) is the spec's
    tool; it isn't installed here, so this regex splitter is the dependency-free
    stand-in. Swap in spaCy later behind this same function with no caller change."""
    if not text:
        return []
    # Collapse whitespace first so quotes extracted are clean/verbatim-comparable.
    text = re.sub(r"\s+", " ", text).strip()
    parts = _SENT_SPLIT.split(text)
    return [p.strip() for p in parts if p.strip()]


# ===========================================================================
# PART 7.5b — market-data cache namespace (.cache/market_data/)
# ===========================================================================
def get_market_data(source_type: str, key: str,
                    fetch_fn: Callable[[], Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """Cache-checked fetch for a market/economic source. Staleness rules (PART 7.5b):
      * USGS (annual, January): re-fetch only if we're in a later year and past January.
      * EIA/FRED/USA Trade (monthly): re-fetch if the cache is >30 days old.
      * industry bodies / others: use cache (manual cache-bust to update).
    Returns the cached/fetched dict, or None if the fetch yields nothing.
    Never raises — a cache miss/parse error just falls through to fetch_fn."""
    safe_key = re.sub(r"[^A-Za-z0-9._-]", "_", key)
    cache_path = os.path.join(MARKET_DATA_DIR, source_type, safe_key + ".json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as fh:
                cached = json.load(fh)
            if _cache_still_fresh(source_type, cached):
                cached["_cache_hit"] = True
                return cached
        except Exception:
            pass  # corrupt cache -> re-fetch

    data = None
    try:
        data = fetch_fn()
    except Exception:
        data = None
    if data is None:
        return None
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        data.setdefault("access_date", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        tmp = cache_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, cache_path)
    except Exception:
        pass
    data["_cache_hit"] = False
    return data


def _cache_still_fresh(source_type: str, cached: Dict[str, Any]) -> bool:
    now = datetime.now(timezone.utc)
    pub = cached.get("publication_date")
    if source_type == "usgs":
        # A payload written by an older parser is stale by definition — parser
        # upgrades (e.g. the 2026-07-01 withheld/none/subrow fix) must re-extract,
        # otherwise the old nulls persist forever.
        try:
            from src.data.usgs_parser import PARSER_VERSION
            if cached.get("parser_version") != PARSER_VERSION:
                return False
        except Exception:
            pass
    if source_type == "usgs" and pub:
        try:
            pub_year = int(str(pub)[:4])
            if now.year > pub_year and now.month >= 2:
                return False  # a new January edition should exist -> re-fetch
        except Exception:
            return True
        return True
    if source_type in ("eia", "fred", "usa_trade"):
        try:
            acc = cached.get("access_date")
            age_days = (now - datetime.fromisoformat(str(acc).replace("Z", "+00:00"))).days
            return age_days < 30
        except Exception:
            return True
    return True  # industry bodies / others: trust cache


# ===========================================================================
# PART 4 — the crossover engine (pure math, fully deterministic)
# ===========================================================================
def _add_months(d: date, n: int) -> date:
    """Return d shifted by n whole months (clamped to a valid day-of-month)."""
    total = (d.year * 12 + (d.month - 1)) + n
    y, m = divmod(total, 12)
    day = min(d.day, [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
                      31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m])
    return date(y, m + 1, day)


def supply_at(t_years: float, supply0: float, growth_rate: float) -> float:
    """Supply(t) = supply0 * (1 + g)^t — continuous growth from the supply source
    publication date (PART 4b). g may be negative (flat/declining production)."""
    return supply0 * ((1.0 + growth_rate) ** t_years)


def demand_at(t_years: float, demand0: float, mgmt_offset_years: Optional[float],
              phase1_cagr: Optional[float], phase2_cagr: float) -> float:
    """Two-phase demand (PART 4a).

      Phase 1 (industry CAGR): from the supply source date (t=0) to the management
              statement date (t = mgmt_offset_years).
      Phase 2 (management CAGR): from the management statement date onward.

    If mgmt_offset_years is None or <= 0, or phase1_cagr is None, demand grows at
    phase2_cagr for the whole timeline (single phase) — this also covers the
    'management statement predates the supply report' case (PART 4a)."""
    if mgmt_offset_years is None or phase1_cagr is None or mgmt_offset_years <= 0:
        return demand0 * ((1.0 + phase2_cagr) ** t_years)
    if t_years <= mgmt_offset_years:
        return demand0 * ((1.0 + phase1_cagr) ** t_years)
    base = demand0 * ((1.0 + phase1_cagr) ** mgmt_offset_years)
    return base * ((1.0 + phase2_cagr) ** (t_years - mgmt_offset_years))


def find_crossover_month(supply0: float, supply_growth: float, demand0: float,
                         mgmt_offset_years: Optional[float],
                         phase1_cagr: Optional[float], phase2_cagr: float,
                         horizon_months: int = CROSSOVER_HORIZON_MONTHS) -> Optional[int]:
    """First month offset m in [0, horizon] where demand(m) >= supply(m).
    Returns 0 if already constrained, None if no crossover within the horizon.
    A monthly scan (not bisection) is used because the two-phase demand curve is
    piecewise — the scan finds the FIRST crossing correctly even across the kink."""
    if demand0 >= supply0:
        return 0  # ALREADY CONSTRAINED (PART 7c #5)
    for m in range(0, horizon_months + 1):
        t = m / 12.0
        s = supply_at(t, supply0, supply_growth)
        d = demand_at(t, demand0, mgmt_offset_years, phase1_cagr, phase2_cagr)
        if d >= s:
            return m
    return None


def month_to_quarter(start: date, month_offset: int) -> str:
    """Convert a month offset from `start` to a calendar 'Q# YYYY' (PART 4d).
    e.g. start Jan 2026 + 31 months -> Aug 2028 -> 'Q3 2028'."""
    d = _add_months(start, month_offset)
    return "Q%d %d" % ((d.month - 1) // 3 + 1, d.year)


def compute_crossovers(supply0: float, supply_growth: float, demand0: float,
                       supply_date: date, mgmt_date: Optional[date],
                       industry_cagr: Optional[float],
                       management_cagr: Optional[float]) -> Dict[str, Any]:
    """Compute the management and industry crossover quarters for one material.

    crossover_management : two-phase demand — industry CAGR until the management
        statement date, then management CAGR (the headline finding).
    crossover_industry   : demand at the (more conservative) industry CAGR for the
        whole timeline.

    Fallbacks (PART 4a): if management CAGR is missing, both use industry; if
    industry CAGR is missing, management is single-phase from the supply date and
    the industry crossover is unavailable; if both are missing -> insufficient
    data (caller skips the material)."""
    out: Dict[str, Any] = {
        "crossover_management": None, "crossover_industry": None,
        "model_note": "", "insufficient_data": False,
    }
    # Already constrained today (demand >= supply) — true regardless of any growth
    # rate (PART 7c #5). Report it even when no CAGR is available.
    if demand0 >= supply0:
        out["crossover_management"] = "ALREADY_CONSTRAINED"
        out["crossover_industry"] = "ALREADY_CONSTRAINED"
        out["model_note"] = ("Apparent consumption (%.0f) already meets or exceeds production "
                             "(%.0f) as of %s — the constraint binds today." %
                             (demand0, supply0, _fmt_month(supply_date)))
        return out
    if management_cagr is None and industry_cagr is None:
        out["insufficient_data"] = True
        out["model_note"] = "Insufficient data: neither a management nor an industry CAGR was available."
        return out

    mgmt_offset = None
    if mgmt_date is not None and mgmt_date > supply_date:
        mgmt_offset = (mgmt_date.year - supply_date.year) + (mgmt_date.month - supply_date.month) / 12.0

    # ---- management crossover (two-phase) ----
    if management_cagr is not None:
        phase2 = management_cagr
        phase1 = industry_cagr  # None -> single phase at management CAGR
        m_mgmt = find_crossover_month(supply0, supply_growth, demand0, mgmt_offset, phase1, phase2)
        out["crossover_management"] = _quarter_or_label(m_mgmt, supply_date)
    else:
        # management missing -> use industry as the "management" line too (PART 4a)
        m_mgmt = find_crossover_month(supply0, supply_growth, demand0, None, None, industry_cagr)  # type: ignore[arg-type]
        out["crossover_management"] = _quarter_or_label(m_mgmt, supply_date)

    # ---- industry crossover (single phase at industry CAGR) ----
    if industry_cagr is not None:
        m_ind = find_crossover_month(supply0, supply_growth, demand0, None, None, industry_cagr)
        out["crossover_industry"] = _quarter_or_label(m_ind, supply_date)
    else:
        out["crossover_industry"] = "unavailable (no industry CAGR)"

    out["model_note"] = _model_note(supply_date, mgmt_date, mgmt_offset,
                                    industry_cagr, management_cagr)
    return out


def _quarter_or_label(month_offset: Optional[int], start: date) -> str:
    if month_offset is None:
        return "no crossover within 20-year window"
    if month_offset == 0:
        return "ALREADY_CONSTRAINED"
    return month_to_quarter(start, month_offset)


def _fmt_month(d: Optional[date]) -> str:
    return d.strftime("%b %Y") if d else "n/a"


def _model_note(supply_date: date, mgmt_date: Optional[date], mgmt_offset: Optional[float],
                industry_cagr: Optional[float], management_cagr: Optional[float]) -> str:
    def pct(x):
        return "%.1f%%" % (x * 100.0) if x is not None else "n/a"
    if management_cagr is None:
        return ("Demand modeled at industry CAGR (%s) from %s onward (no management "
                "CAGR available)." % (pct(industry_cagr), _fmt_month(supply_date)))
    if industry_cagr is None or mgmt_offset is None:
        return ("Demand modeled at management CAGR (%s) from %s onward (single phase; "
                "management statement on/at-or-before the supply report date)."
                % (pct(management_cagr), _fmt_month(supply_date)))
    return ("Demand modeled at industry CAGR (%s) from %s to %s, then management CAGR "
            "(%s) from %s onward." % (pct(industry_cagr), _fmt_month(supply_date),
                                      _fmt_month(mgmt_date), pct(management_cagr),
                                      _fmt_month(mgmt_date)))


# ===========================================================================
# PART 2 — CAGR extraction (deterministic: sentence filter + regex)
# ===========================================================================
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_YEAR_RANGE_RE = re.compile(r"(?:through|by|to|until)\s+(\d{4})", re.IGNORECASE)
_GROWTH_WORDS = ("grow", "growth", "cagr", "expand", "increase", "project",
                 "forecast", "expect", "annual", "compound")
_GENERIC_SECTOR_WORDS = ("market", "industry", "sector", "tam", "demand", "addressable")

# ── PART 2 fix (2026-07-05): "sector word + % + growth word" was too loose —
# OKLO's "Used nuclear fuel still contains approximately 95% of its energy
# content … expected electrical needs …" qualified ("expected" counted as the
# growth word) and highest-percentage selection then PREFERRED the irrelevant
# 95. A percentage is only a sector CAGR when the sentence carries a forward
# TIME HORIZON, the growth language sits AROUND the percentage itself, it is
# not "X% of <something>", not litigation/safe-harbor boilerplate (AMD's
# 2.125%), and not a past-tense fact. Selection prefers the percentage
# anchored to "CAGR/compound", never the largest number.
_HORIZON_RE = re.compile(
    r"(?:through|by|until|into)\s+(?:fiscal\s+)?20\d\d"
    r"|20\d\d\s*(?:through|to|-|–|until)\s*20\d\d"
    r"|over\s+the\s+next|next\s+(?:several|few|\d+|two|three|four|five|six|"
    r"seven|eight|nine|ten)\s+(?:years|decades)"
    r"|annually|per\s+annum|per\s+year|long[- ]term"
    r"|(?:five|ten|\d+)[- ]year\s+(?:period|horizon|outlook|forecast|plan)"
    r"|fiscal\s+(?:year\s+)?20\d\d|coming\s+(?:years|decade)", re.IGNORECASE)
_LEGAL_BOILERPLATE_RE = re.compile(
    r"forward-?looking\s+statements|safe\s+harbor|risks?\s+and\s+uncertaint"
    r"|securities\s+litigation|undue\s+reliance|except\s+as\s+required\s+by\s+law",
    re.IGNORECASE)
_PAST_FACT_RE = re.compile(
    r"\b(?:grew|increased|rose|was|were|gained|declined|decreased|had|"
    r"resulted|represented|accounted)\b", re.IGNORECASE)
_FORWARD_RE = re.compile(
    r"\b(?:expect(?:ed|s)?|project(?:ed|s|ion|ions)?|forecast(?:ed|s)?"
    r"|anticipat\w+|will|estimat\w+|target(?:ed|s|ing)?|guidance|outlook)\b",
    re.IGNORECASE)
_GROWTH_CONTEXT_RE = re.compile(
    r"cagr|compound\s+annual|grow(?:th|ing|s)?\b|expand(?:ing|ed|s)?\s+(?:at|by)"
    r"|increas(?:e|ing)\s+(?:at|by)|annual(?:ized)?\s+(?:growth\s+)?rate"
    r"|rate\s+of\s+growth", re.IGNORECASE)


def _sector_keywords(sector_name: str) -> List[str]:
    """Sector-specific keywords (lowercased significant words) + generic market words."""
    words = re.findall(r"[a-zA-Z]{4,}", sector_name.lower())
    stop = {"manufacturing", "production", "advanced", "infrastructure", "energy",
            "equipment", "inputs", "base", "materials", "amp"}
    kws = [w for w in words if w not in stop]
    return list(dict.fromkeys(kws + list(_GENERIC_SECTOR_WORDS)))


def _extract_cagr_from_text(text: str, sector_name: str) -> Optional[Dict[str, Any]]:
    """Find the best sector-growth CAGR sentence in a block of text.

    A qualifying sentence must have: a sector keyword, a growth word, a
    forward TIME HORIZON, no safe-harbor/litigation boilerplate, and no
    past-tense-only framing. A qualifying PERCENTAGE must additionally sit in
    growth context and not be an "X% of <something>" share/content figure.
    Returns None when nothing qualifies — never a random percentage."""
    sector_kws = _sector_keywords(sector_name)
    # (anchor_rank, sentence_order, value, year, sentence)
    candidates: List[Tuple[int, int, float, Optional[str], str]] = []
    for i, sent in enumerate(_split_sentences(text)):
        low = sent.lower()
        if "%" not in sent:
            continue
        if not any(k in low for k in sector_kws):
            continue
        if not any(g in low for g in _GROWTH_WORDS):
            continue
        if _LEGAL_BOILERPLATE_RE.search(sent):
            continue
        if not _HORIZON_RE.search(sent):
            continue
        if _PAST_FACT_RE.search(sent) and not _FORWARD_RE.search(sent):
            continue  # a historical fact, not a projection
        for m in _PCT_RE.finditer(sent):
            val = float(m.group(1))
            if not (0.0 < val <= 100.0):  # implausible-CAGR guard (PART 2a #5)
                continue
            if re.match(r"\s*of\b", sent[m.end():]):
                continue  # "95% of its energy content" — a share, not a rate
            around = low[max(0, m.start() - 80):min(len(sent), m.end() + 40)]
            if not _GROWTH_CONTEXT_RE.search(around):
                continue  # the % itself isn't growth-framed
            anchored = 0 if re.search(r"cagr|compound", around) else 1
            yr = _YEAR_RANGE_RE.search(sent)
            candidates.append((anchored, i, val, yr.group(1) if yr else None,
                               sent.strip()))
    if not candidates:
        return None
    # explicit CAGR-anchored percentages first, then earliest sentence
    candidates.sort(key=lambda c: (c[0], c[1]))
    _, _, val, year, sent = candidates[0]
    return {"cagr_value": val, "cagr_decimal": val / 100.0,
            "year_horizon": int(year) if year else None, "sentence": sent}


def _tr_text(tr: Dict[str, Any]) -> str:
    """Transcript text, tolerant of both provider schemas in this repo: the
    SECTranscriptProvider writes `text`; the older richer cache writes `content`."""
    return tr.get("text") or tr.get("content") or ""


def _tr_url(tr: Dict[str, Any]) -> str:
    # No fabricated pseudo-URL fallback (Part 7.2, 2026-07-05): "SEC 8-K
    # EX-99.1" rendered as a dead "Source" link in the UI. Real URL or none.
    return tr.get("url") or tr.get("source_url") or ""


def _tr_quarter(tr: Dict[str, Any]) -> str:
    return tr.get("quarter") or tr.get("fiscal_period") or ""


def _parse_iso(d: Optional[str]) -> Optional[date]:
    if not d:
        return None
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _extract_management_cagr(ticker: str, sector_name: str,
                             log: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """Management CAGR from the most recent earnings call, falling back to the
    previous call, then 10-K MD&A (PART 2a #6, PART 7.6b). Returns a citation dict
    with verbatim quote, date (SEC filing_date = clock anchor), url, and a
    verified flag — or None if nothing qualifies."""
    if SECTranscriptProvider is None:
        return None
    # 1) earnings call transcripts (most recent first)
    try:
        transcripts = SECTranscriptProvider().get_transcripts(ticker) or []
    except Exception:
        transcripts = []
    transcripts = sorted(transcripts, key=lambda t: t.get("date", ""), reverse=True)
    # Scan most-recent-first; companies state an explicit market CAGR only
    # occasionally, so look back over several calls (PART 2a #1, PART 7.6b #1-2).
    for tr in transcripts[:8]:
        text = _tr_text(tr)
        found = _extract_cagr_from_text(text, sector_name)
        if found:
            verified = _quote_in_text(found["sentence"], text)
            log.append({"step": "CAGR_MGMT",
                        "detail": "Extracted management CAGR %.1f%% from %s earnings call (%s)"
                                  % (found["cagr_value"], _tr_quarter(tr) or "?", tr.get("date", "?"))})
            return {
                "value": found["cagr_value"], "decimal": found["cagr_decimal"],
                "source": "%s %s Earnings Call" % (ticker.upper(), _tr_quarter(tr)),
                "date": tr.get("date", ""), "quote": found["sentence"],
                "url": _tr_url(tr), "filing_type": "8-K EX-99.1",
                "quote_verified": verified, "year_horizon": found["year_horizon"],
            }
    # 2) fall back to 10-K MD&A (Item 7)
    if SECEdgarProvider is not None:
        try:
            prov = SECEdgarProvider()
            tenks = prov.get_10k_filings(ticker)
            if tenks:
                fr = prov.get_filing_result(ticker, tenks[0])
                found = _extract_cagr_from_text(fr.mda_text or "", sector_name)
                if found:
                    verified = _quote_in_text(found["sentence"], fr.mda_text or "")
                    log.append({"step": "CAGR_MGMT",
                                "detail": "Management CAGR %.1f%% from 10-K MD&A fallback (%s)"
                                          % (found["cagr_value"], fr.filing_date)})
                    return {
                        "value": found["cagr_value"], "decimal": found["cagr_decimal"],
                        "source": "%s 10-K MD&A (Item 7)" % ticker.upper(),
                        "date": fr.filing_date, "quote": found["sentence"],
                        "url": fr.htm_url, "filing_type": "10-K",
                        "quote_verified": verified, "year_horizon": found["year_horizon"],
                    }
        except Exception:
            pass
    log.append({"step": "CAGR_MGMT", "detail": "No management CAGR found in transcripts or 10-K MD&A"})
    return None


def _extract_industry_cagr(sector_name: str, log: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """Industry-body CAGR for the sector (PART 2b). The industry sources (WSTS, IEA,
    USDA, EIA AEO, ACC, PCA, AIA, EvaluatePharma) are free-text/PDF; reliable live
    extraction needs the source-specific fetcher + (for PDFs) a table parser. Until
    those are wired/installed this returns None and the agent flags the industry
    CAGR as 'unavailable' (PART 7.6b #3) — management CAGR is then used alone."""
    src = get_sector(sector_name)
    body = (src or {}).get("cagr_sources", {})
    log.append({"step": "CAGR_IND",
                "detail": "Industry CAGR source = %s (%s). Live extraction pending "
                          "source fetcher/parser; flagged unavailable."
                          % (body.get("industry_body", "?"), body.get("industry_body_frequency", "?"))})
    return None


# ===========================================================================
# PART 3 — material supply/demand (graceful-degradation fetch layer)
# ===========================================================================
def _analyze_material(material: Dict[str, Any], mgmt: Optional[Dict[str, Any]],
                      industry: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve supply + demand for one material and compute its crossovers.

    The supply/demand fetch routes through get_market_data() to the registry source
    named on the material (USGS / USA Trade / EIA / etc.). Missing metrics degrade
    PER-METRIC (2026-07-01 Part 1 fix):
      * both present            -> status='success', full crossover math
      * demand only (or supply
        only)                   -> status='partial_data' — the available side is
                                   shown with its citation; no crossover is modeled
      * neither                 -> status='data_unavailable'
    Every degraded material carries a PRECISE reason (withheld vs none vs not
    published vs needs-API-key vs fetch failure) and, when USGS publishes it, the
    WORLD production figure as context. Never a fabricated number."""
    name = material["name"]
    route = material.get("supply_source", "usgs")
    commodity = material.get("usgs_commodity")
    ext = _get_usgs_extract(commodity) if commodity else None
    supply = (ext or {}).get("supply") if route == "usgs" else _fetch_supply(material)
    demand = (ext or {}).get("demand")
    world = (ext or {}).get("world_production")
    # FRED price/index signal — attached whether or not USGS tonnage is available,
    # since a price trend is itself constraint context (a cited indicator, not a
    # crossover input: FRED indices/prices don't share units with tonnage).
    indicator = _get_fred(material.get("fred_series")) if material.get("fred_series") else None

    if not supply or not demand:
        reason = _degradation_reason(material, ext, supply, demand, world)
        out = {"material": name,
               "status": "partial_data" if (supply or demand) else "data_unavailable",
               "reason": reason,
               "crossover_management": None, "crossover_industry": None}
        if supply:
            out["supply"] = supply
        if demand:
            out["demand"] = demand
        if world:
            out["world_production"] = world
        if indicator:
            out["indicator"] = _indicator_block(indicator)
        return out

    supply_date = _parse_iso(supply.get("date")) or date.today()
    mgmt_date = _parse_iso(mgmt.get("date")) if mgmt else None
    cross = compute_crossovers(
        supply0=float(supply["value"]), supply_growth=float(supply.get("growth_rate_pct", 0.0)) / 100.0,
        demand0=float(demand["value"]), supply_date=supply_date, mgmt_date=mgmt_date,
        industry_cagr=(industry or {}).get("decimal"),
        management_cagr=(mgmt or {}).get("decimal"),
    )
    if cross["insufficient_data"]:
        # The DATA is present (cited below) — only the growth-rate model input is
        # missing, so say that and still show the numbers we extracted.
        out = {"material": name, "status": "insufficient_data", "reason": cross["model_note"],
               "crossover_management": None, "crossover_industry": None,
               "supply": supply, "demand": demand}
        if world:
            out["world_production"] = world
        if indicator:
            out["indicator"] = _indicator_block(indicator)
        return out
    out = {
        "material": name, "status": "success",
        "crossover_management": cross["crossover_management"],
        "crossover_industry": cross["crossover_industry"],
        "supply": supply, "demand": demand, "model_note": cross["model_note"],
    }
    if world:
        out["world_production"] = world  # cited world context beside the US figures
    if indicator:
        ib = _indicator_block(indicator)
        # PART 7.6e: flag when the FRED reading and the USGS supply data are from
        # materially different periods (FRED is monthly, USGS annual) so the analyst
        # knows the price/index signal is fresher than the tonnage it sits beside.
        fnote = _date_mismatch_note(indicator.get("date"), supply.get("date"),
                                    label_a="FRED reading", label_b="USGS data")
        if fnote:
            ib["as_of_note"] = fnote
        out["indicator"] = ib
    # Date-consistency check (PART 7.6e): flag if supply & demand periods diverge.
    dnote = _date_mismatch_note(supply.get("date"), demand.get("date"))
    if dnote:
        out["date_note"] = dnote
    return out


def _indicator_block(ind: Dict[str, Any]) -> Dict[str, Any]:
    """The cited FRED signal exposed in output (no API key, public URL)."""
    return {
        "type": "FRED index/price", "series_id": ind.get("series_id"),
        "value": ind.get("value"), "units": ind.get("units"), "date": ind.get("date"),
        "growth_1y_pct": ind.get("growth_1y_pct"),
        "growth_5y_cagr_pct": ind.get("growth_5y_cagr_pct"),
        "source": ind.get("source"), "url": ind.get("url"),
        "quote": ind.get("quote"), "reading": ind.get("reading"),
        "quote_verified": ind.get("quote_verified", False),
    }


def _date_mismatch_note(d1: Optional[str], d2: Optional[str],
                        label_a: str = "Supply", label_b: str = "demand") -> str:
    a, b = _parse_iso(d1), _parse_iso(d2)
    if not a or not b:
        return ""
    days = abs((a - b).days)
    if days > 30:
        return ("%s (%s) and %s (%s) are %d days apart — the figures describe "
                "different periods; interpret the comparison accordingly."
                % (label_a, d1, label_b, d2, days))
    return ""


def _get_usgs_extract(commodity: str) -> Optional[Dict[str, Any]]:
    """Cached full MCS extract (supply + demand together, one PDF fetch) for a USGS
    commodity. The second of the supply/demand pair is a cache hit, so the PDF is
    fetched/parsed once per commodity per year (PART 7d #6).

    CACHE-FIRST even when the parser import is unavailable: a valid cached extract
    must still be served if pdfplumber is missing in this environment (the old
    pre-check returned None before ever consulting the cache — 2026-07-01 fix)."""
    if not commodity:
        return None
    yr = datetime.now().year
    return get_market_data("usgs", "%s_%d" % (_slug(commodity), yr),
                           lambda: usgs_extract(commodity, yr) if usgs_extract else None)


def _get_fred(series_id: str) -> Optional[Dict[str, Any]]:
    """Cached FRED indicator fetch (PART 7.5b; 30-day staleness via get_market_data).
    Returns a cited index/price datum, or None if FRED is unavailable (no key /
    invalid series / network) — the caller simply omits the indicator. Cache-first
    even if the fred_client import failed (same fix as _get_usgs_extract)."""
    if not series_id:
        return None
    return get_market_data("fred", _slug(series_id),
                           lambda: fetch_fred(series_id) if fetch_fred else None)


def _fetch_supply(material: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Supply value + 5-yr growth for a material from its registry source.
    USGS is live (pdfplumber); USA Trade / EIA need API keys (return None ->
    data_unavailable)."""
    route = material.get("supply_source", "usgs")
    commodity = material.get("usgs_commodity")
    if route == "usgs" and commodity:
        ext = _get_usgs_extract(commodity)
        return ext.get("supply") if ext else None
    if route == "usa_trade" and material.get("hs_code"):
        return get_market_data("usa_trade", "HS%s_%s" % (str(material["hs_code"]).replace(".", ""),
                                                         datetime.now().strftime("%Y-%m")),
                               lambda: _fetch_usa_trade(material))
    if route == "eia" and material.get("eia_series"):
        return get_market_data("eia", "%s_%s" % (_slug(material["eia_series"]),
                                                 datetime.now().strftime("%Y-%m")),
                               lambda: _fetch_eia(material))
    return None


# -- non-USGS live fetchers: structured + cited, but require API keys (None here) --
def _fetch_usa_trade(material: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """USA Trade Online / Census intltrade net-imports proxy. Requires CENSUS_API_KEY
    (or the bulk CSV path) — returns None until provisioned."""
    return None


def _fetch_eia(material: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """EIA series fetch. Requires EIA_API_KEY — returns None until provisioned."""
    return None


def _fmt_qty(blk: Optional[Dict[str, Any]]) -> str:
    """'19,000 kilograms, gallium content (2025)' from a supply/demand/world block."""
    if not blk:
        return ""
    val = blk.get("value")
    unit = blk.get("unit") or ""
    yr = blk.get("data_year") or blk.get("year")
    txt = ("{:,.0f}".format(val) if isinstance(val, (int, float)) else str(val))
    if unit:
        txt += " " + unit
    if yr:
        txt += " (%s)" % yr
    return txt


_DIAG_TEXT = {
    "withheld": "USGS withholds the US figure (\"W\" — company-proprietary data)",
    "na": "USGS reports the US figure as not available (NA) for recent years",
    "not_found": "the MCS sheet's salient statistics carry no such row",
    "not_published": "USGS does not publish this metric",
}


def _degradation_reason(material: Dict[str, Any], ext: Optional[Dict[str, Any]],
                        supply, demand, world) -> str:
    """Material-specific degradation detail (Part 1c, 2026-07-01): say exactly WHY
    each missing metric is missing, show the side we DO have, and cite world
    production when USGS publishes it despite withholding the US figure."""
    name = material["name"]
    route = material.get("supply_source", "usgs")

    # Non-USGS supply routes: the gate is a missing API key, not USGS policy.
    if route == "usa_trade":
        hs = material.get("hs_code", "?")
        base = ("Supply data requires CENSUS_API_KEY for USA Trade Online HS %s imports. "
                "Until provisioned, %s." % (
                    hs, "only demand-side data is available"
                    if demand else "no supply or demand data is available for %s" % name))
        return base
    if route == "eia":
        return ("Supply data requires EIA_API_KEY for the EIA series %s. Until "
                "provisioned, this material cannot be evaluated." % material.get("eia_series", "?"))

    # USGS route.
    if ext is None:
        return ("Unable to retrieve supply or demand data for %s: the USGS MCS sheet "
                "could not be fetched or parsed. This material cannot be evaluated "
                "until the data source is available." % name)

    diags = ext.get("diagnostics", {})
    if diags.get("production") == "not_published":
        detail = diags.get("detail") or _DIAG_TEXT["not_published"]
        return "%s. See the MCS sheet: %s" % (detail[0].upper() + detail[1:], ext.get("url", ""))

    parts: List[str] = []
    if not supply:
        parts.append("US production: %s" % _DIAG_TEXT.get(diags.get("production"),
                                                          "no value could be extracted"))
    if not demand:
        parts.append("US apparent consumption: %s" % _DIAG_TEXT.get(diags.get("consumption"),
                                                                    "no value could be extracted"))
    if demand and not supply:
        parts.append("US apparent consumption is available: %s" % _fmt_qty(demand))
    if supply and not demand:
        parts.append("US production is available: %s" % _fmt_qty(supply))
    if world:
        parts.append("World production: %s" % _fmt_qty(world))
    parts.append("Source: %s" % ext.get("url", ""))
    return "%s — %s." % (name, "; ".join(parts))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


# ===========================================================================
# PART 1b — sector classification (one cached LLM call)
# ===========================================================================
_CLASSIFY_PROMPT = (
    "Map the company {company} (ticker: {ticker}) to the CLOSEST sector from this "
    "list. Choose based on the company's PRIMARY business — specifically, what it "
    "primarily MAKES OR SELLS.\n\nSECTORS:\n"
    + "\n".join("* " + s for s in SUPPORTED_SECTORS)
    + "\n\nRules:\n"
      "- The company's main PRODUCT must BE one of these sectors' outputs (e.g. a "
      "chipmaker that sells semiconductors -> Advanced Semiconductor Manufacturing).\n"
      "- A company that merely USES or DESIGNS-AROUND these outputs is NOT a match: "
      "consumer electronics/devices, software, SaaS, internet, retail, banking, "
      "healthcare services, and media companies do NOT fit any sector here.\n"
      "- Designing or fabricating chips/batteries for your OWN finished products does "
      "NOT make you a match. If a company's primary product is a finished consumer "
      "device (e.g. a smartphone/PC/wearables maker), return UNSUPPORTED even if it "
      "designs its own silicon. (A company that SELLS chips as its product IS a match.)\n"
      "- If the company does not clearly fit one of the listed sectors, return: UNSUPPORTED\n\n"
      "Return ONLY the sector name (or UNSUPPORTED), nothing else."
)


def _match_sector(raw: str) -> Optional[str]:
    """Map a (possibly noisy) LLM response to an exact supported sector or
    UNSUPPORTED. Tolerant of an en-dash vs hyphen and surrounding chatter."""
    if not raw:
        return None
    cleaned = raw.strip().strip(".").strip()
    if "unsupported" in cleaned.lower():
        return "UNSUPPORTED"
    norm = _norm(cleaned).replace("-", " ").replace("—", " ")
    for s in SUPPORTED_SECTORS:
        if _norm(s).replace("-", " ").replace("—", " ") == norm:
            return s
    for s in SUPPORTED_SECTORS:  # substring tolerance ("...: Steel Manufacturing")
        if _norm(s).replace("-", " ").replace("—", " ") in norm:
            return s
    return None


def _classify_sector(ticker: str, company: str,
                     log: List[Dict[str, str]]) -> Tuple[Optional[str], bool]:
    """Return (sector_or_None, from_cache). 'UNSUPPORTED' is a valid sector string.
    Cached permanently per ticker at .cache/sector_classifications/{TICKER}.json
    (a company's sector rarely changes) — PART 1b / PART 7d."""
    cache_path = os.path.join(SECTOR_CACHE_DIR, "%s.json" % ticker.upper())
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as fh:
                cached = json.load(fh)
            sector = cached.get("sector")
            if sector:
                log.append({"step": "CLASSIFY", "detail": "Sector (cached): %s" % sector})
                return sector, True
        except Exception:
            pass

    prompt = _CLASSIFY_PROMPT.format(company=company or ticker, ticker=ticker.upper())
    try:
        text, _meta = call_llm(prompt, max_tokens=50, temperature=0.0,
                               provider_idx=start_tier(AGENT_ID),
                               agent=AGENT_ID, stage="classify", ticker=ticker.upper())
    except Exception as exc:
        log.append({"step": "CLASSIFY", "detail": "Classification LLM call failed: %s" % str(exc)[:120]})
        return None, False

    sector = _match_sector(text)
    if sector is None:
        log.append({"step": "CLASSIFY",
                    "detail": "LLM returned an unrecognized sector %r; treating as UNSUPPORTED" % (text or "")[:60]})
        sector = "UNSUPPORTED"
    try:
        with open(cache_path, "w") as fh:
            json.dump({"ticker": ticker.upper(), "company": company, "sector": sector,
                       "classified_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
                      fh, indent=2)
    except Exception:
        pass
    log.append({"step": "CLASSIFY", "detail": "Classified %s as %s" % (ticker.upper(), sector)})
    return sector, False


# ===========================================================================
# Severity (PART 6c) + output assembly (PART 5)
# ===========================================================================
def _severity(crossover_label: Optional[str], start_year: int) -> str:
    """Badge color by urgency (production-polish Part 4c): red = binding now,
    orange <2yr, yellow <5yr, green beyond 5yr, gray = no crossover/unknown."""
    urgency = _urgency(crossover_label, start_year)
    return {"immediate": "red", "high": "orange", "medium": "yellow",
            "low": "green", "none": "gray"}[urgency]


def _urgency(crossover_label: Optional[str], start_year: int) -> str:
    """Analyst urgency for a crossover: immediate (binding today), high (<2yr),
    medium (<5yr), low (beyond 5yr), none (no crossover/unknown)."""
    if not crossover_label:
        return "none"
    if crossover_label == "ALREADY_CONSTRAINED":
        return "immediate"
    m = re.search(r"Q(\d)\s+(\d{4})", crossover_label)
    if not m:
        return "none"
    yrs = int(m.group(2)) + (int(m.group(1)) - 1) * 0.25 - start_year
    if yrs < 2:
        return "high"
    if yrs < 5:
        return "medium"
    return "low"


def _urgency_badge(crossover_label: Optional[str], urgency: str) -> str:
    """Short badge text the UI renders next to the material name."""
    if urgency == "immediate":
        return "BINDING NOW"
    if urgency == "none":
        return "No constraint"
    if urgency == "low":
        m = re.search(r"Q\d\s+(\d{4})", crossover_label or "")
        return "Beyond %s" % m.group(1) if m else "Beyond horizon"
    return crossover_label or ""  # "Q3 2027" style


def _crossover_detail(c: Dict[str, Any]) -> str:
    """One analyst-ready sentence saying WHAT the crossover means, with the real
    numbers — replaces the bare 'ALREADY CONSTRAINED' phrasing that read like a
    model failure instead of a binding constraint found today."""
    s, d = c.get("supply") or {}, c.get("demand") or {}
    unit = s.get("unit") or d.get("unit") or ""
    sv, dv = s.get("value"), d.get("value")
    asof = d.get("date") or s.get("date") or ""
    label = c.get("crossover_management")
    if label == "ALREADY_CONSTRAINED":
        return ("US apparent consumption (%s %s) already meets or exceeds domestic "
                "production (%s %s) as of %s. The constraint is binding today — any "
                "sector growth requires increased imports or new domestic capacity."
                % (_fmt_num(dv), unit, _fmt_num(sv), unit, asof or "the latest USGS data"))
    if label and label.startswith("Q"):
        return ("US demand (%s %s) is projected to exceed domestic production (%s %s) "
                "in %s at the modeled growth rates. Watch import dependence as the "
                "crossover approaches." % (_fmt_num(dv), unit, _fmt_num(sv), unit, label))
    if label and "no crossover" in label:
        return ("Domestic production (%s %s) stays ahead of demand (%s %s) at the "
                "modeled growth rates — no supply constraint within the 20-year window."
                % (_fmt_num(sv), unit, _fmt_num(dv), unit))
    return ""


def _fmt_num(v) -> str:
    try:
        return "{:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return str(v)


def _constraint_sort_key(c: Dict[str, Any]) -> Tuple[int, float]:
    """Earliest management crossover first; then partial data; unavailable last
    (PART 7c #6)."""
    label = c.get("crossover_management")
    if not label:
        # materials WITH data but no crossover model sort above true unavailables
        return (3, 0.0) if c.get("status") in ("partial_data", "insufficient_data") else (4, 0.0)
    if label == "ALREADY_CONSTRAINED":
        return (0, 0.0)
    m = re.search(r"Q(\d)\s+(\d{4})", label)
    if m:
        return (1, int(m.group(2)) + (int(m.group(1)) - 1) * 0.25)
    return (2, 0.0)  # "no crossover within window"


class ConstraintAuditor:
    """Agent 5 entry point. `.run(ticker, progress=None)` mirrors Agents 1-4."""

    def run(self, ticker: str, progress: Optional[Callable] = None, year: Optional[int] = None) -> Dict[str, Any]:
        t0 = time.time()
        ticker = (ticker or "").strip().upper()
        log: List[Dict[str, str]] = []

        def emit(msg: str, kind: str = "info") -> None:
            if progress:
                try:
                    progress(msg, kind)
                except Exception:
                    pass

        # company name (best-effort; classification tolerates ticker-only)
        company = ticker
        if SECEdgarProvider is not None:
            try:
                company = SECEdgarProvider().get_company_name(ticker) or ticker
            except Exception:
                company = ticker

        emit("Classifying %s into a constraint sector..." % ticker)
        sector, _cached = _classify_sector(ticker, company, log)

        if sector is None:
            return self._error_result(ticker, company, log, t0,
                                       "Sector classification failed (LLM unavailable).")
        if sector == "UNSUPPORTED":
            emit("%s is not in the constraint library." % ticker, "flag")
            result = {
                "agent": "Constraint Auditor", "agent_id": AGENT_ID,
                "ticker": ticker, "company": company, "sector": None,
                "status": "unsupported",
                "message": ("Sector not yet supported. Sollia's constraint library covers 14 "
                            "resource-intensive sectors. This ticker does not map to any of them."),
                "score": None, "signal": None,
                "agent_log": log, "elapsed_ms": round((time.time() - t0) * 1000, 1),
            }
            self._archive(ticker, year, result, t0)
            return result

        emit("Sector: %s" % sector)
        # CAGRs
        mgmt = _extract_management_cagr(ticker, sector, log)
        industry = _extract_industry_cagr(sector, log)

        # Sector-level FRED production-trend indicator (the IP index), cited.
        sector_indicators = self._sector_indicators(sector, log)

        # materials
        materials = SECTOR_DATABASE[sector]["materials"]
        constraints: List[Dict[str, Any]] = []
        for material in materials:
            constraints.append(_analyze_material(material, mgmt, industry))
        n_eval = sum(1 for c in constraints if c.get("status") == "success")
        n_partial = sum(1 for c in constraints if c.get("status") == "partial_data")
        constraints.sort(key=_constraint_sort_key)
        start_year = datetime.now().year
        for c in constraints:
            c["severity"] = _severity(c.get("crossover_management"), start_year)
            c["urgency"] = _urgency(c.get("crossover_management"), start_year)
            c["urgency_badge"] = _urgency_badge(c.get("crossover_management"), c["urgency"])
            detail = _crossover_detail(c)
            if detail:
                c["crossover_detail"] = detail
        log.append({"step": "MATERIALS",
                    "detail": "Analyzed %d materials: %d with full supply/demand, "
                              "%d with partial data" % (len(materials), n_eval, n_partial)})
        log.append({"step": "OUTPUT", "detail": "Constraint audit complete"})
        emit("Constraint audit complete (%d/%d materials fully evaluated, %d partial)."
             % (n_eval, len(materials), n_partial), "done")

        caps = missing_capabilities()
        result = {
            "agent": "Constraint Auditor", "agent_id": AGENT_ID,
            "ticker": ticker, "company": company, "sector": sector,
            "run_timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": "success",
            "score": None, "signal": None,  # PART 6b — audit, not a bull/bear score
            "projected_cagr": {
                "management": _cagr_block(mgmt),
                "industry": _cagr_block(industry),
            },
            "sector_indicators": sector_indicators,
            "constraints": constraints,
            "data_provisioning": {  # honest provenance: what must be added to go fully live
                "missing_keys": caps["missing_keys"],
                "needs_pdf_parser": caps["needs_pdf_parser"],
                "note": ("LIVE: sector classification, USGS supply/demand, FRED indices/prices, "
                         "management CAGR. Gated on the above: EIA/USA-Trade materials and "
                         "industry-CAGR (WSTS/IEA/USDA) extraction."),
            },
            "agent_log": log,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
        }
        result["human_readable"] = render_human_readable(result)
        self._archive(ticker, year, result, t0)
        return result

    def _sector_indicators(self, sector: str, log: List[Dict[str, str]]) -> Dict[str, Any]:
        """The sector's FRED Industrial Production index (production-trend signal),
        cited. Empty if the sector has no index or FRED is unavailable."""
        sid = (SECTOR_DATABASE.get(sector) or {}).get("fred_sector_index")
        if not sid:
            return {}
        ind = _get_fred(sid)
        if not ind:
            log.append({"step": "FRED", "detail": "Sector IP index %s unavailable" % sid})
            return {}
        log.append({"step": "FRED",
                    "detail": "Sector IP index %s = %.1f %s (%s; 1y %s%%, 5y CAGR %s%%)"
                              % (sid, ind["value"], ind["units"], ind["date"],
                                 ind.get("growth_1y_pct"), ind.get("growth_5y_cagr_pct"))})
        return {"production_index": _indicator_block(ind)}

    def _error_result(self, ticker, company, log, t0, msg) -> Dict[str, Any]:
        return {
            "agent": "Constraint Auditor", "agent_id": AGENT_ID, "ticker": ticker,
            "company": company, "sector": None, "status": "error", "message": msg,
            "score": None, "signal": None, "agent_log": log,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
        }

    def _archive(self, ticker, year, result, t0) -> None:
        try:
            save_run(AGENT_ID, ticker, year or datetime.now().year, result,
                     duration_ms=(time.time() - t0) * 1000.0)
        except Exception:
            pass  # archiving must never break a run (PART 7.5c)


def _cagr_block(c: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not c:
        return {"status": "unavailable"}
    return {
        "value": c["value"], "source": c["source"], "date": c.get("date", ""),
        "quote": c["quote"], "url": c.get("url", ""),
        "quote_verified": c.get("quote_verified", False),
    }


# ===========================================================================
# PART 5b — human-readable dashboard output
# ===========================================================================
def render_human_readable(result: Dict[str, Any]) -> str:
    if result.get("status") == "unsupported":
        return "UNSUPPORTED: %s" % result.get("message", "")
    lines: List[str] = []
    lines.append("SECTOR: %s" % result.get("sector", "?"))
    lines.append("")
    lines.append("PROJECTED CAGR:")
    pc = result.get("projected_cagr", {})
    mgmt = pc.get("management") or {}
    ind = pc.get("industry") or {}
    if mgmt.get("value") is not None:
        lines.append("  Management (%s, %s):" % (mgmt.get("source", "?"), mgmt.get("date", "?")))
        lines.append("    \"%s\" [%s]" % (mgmt.get("quote", ""), mgmt.get("url", "")))
    else:
        lines.append("  Management: unavailable")
    if ind.get("value") is not None:
        lines.append("  Industry (%s, %s):" % (ind.get("source", "?"), ind.get("date", "?")))
        lines.append("    \"%s\" [%s]" % (ind.get("quote", ""), ind.get("url", "")))
    else:
        lines.append("  Industry: unavailable")

    pix = (result.get("sector_indicators") or {}).get("production_index")
    if pix:
        lines.append("")
        lines.append("SECTOR PRODUCTION INDEX (FRED, %s):" % pix.get("date", "?"))
        lines.append("  %s = %.1f %s  (1y %s%%, 5y CAGR %s%%)" % (
            pix.get("quote", ""), pix.get("value", 0), pix.get("units", ""),
            pix.get("growth_1y_pct"), pix.get("growth_5y_cagr_pct")))
        lines.append("    [%s]" % pix.get("url", ""))

    for c in result.get("constraints", []):
        lines.append("")
        lines.append("───")
        if c.get("status") != "success":
            lines.append("MATERIAL: %s — %s" % (c.get("material", "?"), c.get("status", "")))
            if c.get("reason"):
                lines.append("  %s" % c["reason"])
            for side, label in (("supply", "US production"), ("demand", "US apparent consumption")):
                blk = c.get(side)
                if blk:
                    lines.append("  %s: %s" % (label, _fmt_qty(blk)))
                    lines.append("    Quote: \"%s\" [%s]" % (blk.get("quote", ""), blk.get("url", "")))
            wp = c.get("world_production")
            if wp:
                lines.append("  World production: %s" % _fmt_qty(wp))
                lines.append("    Quote: \"%s\" [%s]" % (wp.get("quote", ""), wp.get("url", "")))
            _render_indicator(lines, c.get("indicator"))
            continue
        lines.append("BINDING CONSTRAINT: %s%s" % (
            c.get("material", "?"),
            "  [%s]" % c["urgency_badge"] if c.get("urgency_badge") else ""))
        if c.get("crossover_detail"):
            prefix = "⚠️  " if c.get("urgency") == "immediate" else "  "
            lines.append("%s%s" % (prefix, c["crossover_detail"]))
            src = (c.get("demand") or {}).get("source") or (c.get("supply") or {}).get("source")
            if src:
                lines.append("     Source: %s" % src)
        if c.get("crossover_management") != "ALREADY_CONSTRAINED":
            lines.append("  Management CAGR: demand exceeds supply in %s" % c.get("crossover_management"))
            lines.append("  Industry CAGR:   demand exceeds supply in %s" % c.get("crossover_industry"))
        s = c.get("supply", {})
        d = c.get("demand", {})
        lines.append("  Supply: %s %s growing at %s%%" % (
            s.get("value"), s.get("unit", ""), s.get("growth_rate_pct", "?")))
        lines.append("    Source: %s, %s" % (s.get("source", "?"), s.get("date", "?")))
        lines.append("    Quote: \"%s\" [%s]" % (s.get("quote", ""), s.get("url", "")))
        lines.append("  Demand: %s %s" % (d.get("value"), d.get("unit", "")))
        lines.append("    Source: %s, %s" % (d.get("source", "?"), d.get("date", "?")))
        lines.append("    Quote: \"%s\" [%s]" % (d.get("quote", ""), d.get("url", "")))
        wp = c.get("world_production")
        if wp:
            lines.append("  World production (context): %s" % _fmt_qty(wp))
            lines.append("    Quote: \"%s\" [%s]" % (wp.get("quote", ""), wp.get("url", "")))
        if c.get("model_note"):
            lines.append("  Note: %s" % c["model_note"])
        if c.get("date_note"):
            lines.append("  ⚠ %s" % c["date_note"])
        _render_indicator(lines, c.get("indicator"))
    return "\n".join(lines)


def _render_indicator(lines: List[str], ind: Optional[Dict[str, Any]]) -> None:
    if not ind:
        return
    lines.append("  Price/Index (FRED): %s = %s %s (%s; 5y CAGR %s%%)" % (
        ind.get("quote", ""), ind.get("value"), ind.get("units", ""),
        ind.get("date", ""), ind.get("growth_5y_cagr_pct")))
    lines.append("    [%s]" % ind.get("url", ""))


def run_constraint_auditor(ticker: str, progress: Optional[Callable] = None,
                           year: Optional[int] = None) -> Dict[str, Any]:
    """Module-level entry point (mirrors run_risk_factors)."""
    return ConstraintAuditor().run(ticker, progress=progress, year=year)


# ===========================================================================
# Crossover engine self-test (hand-verified math — PART 7b #4, PART 8)
# ===========================================================================
def _selftest_crossover() -> None:
    """Hand-checked crossover cases. No network, no LLM — proves PART 4 math."""
    print("== crossover engine self-test ==")

    # Case A: clean single-phase. supply 1000 @ 0%, demand 1000 @ ~7.18%/yr.
    # demand doubles in ~10 yr; but we want first month demand>=supply with demand0<supply0.
    # Use demand0 = 800, supply0 = 1000, supply flat, demand 10%/yr.
    # crossover when 800*1.1^t >= 1000 -> 1.1^t >= 1.25 -> t = ln1.25/ln1.1 = 2.342 yr ~ 28.1 mo -> month 29.
    m = find_crossover_month(1000, 0.0, 800, None, None, 0.10)
    start = date(2026, 1, 1)
    assert m == 29, "Case A expected month 29, got %s" % m
    q = month_to_quarter(start, m)
    assert q == "Q2 2028", "Case A expected Q2 2028, got %s" % q
    print("  A single-phase: month %d -> %s  OK" % (m, q))

    # Case B: already constrained (demand0 >= supply0).
    m = find_crossover_month(1000, 0.05, 1000, None, None, 0.20)
    assert m == 0, "Case B expected 0 (already constrained), got %s" % m
    print("  B already-constrained: %s  OK" % _quarter_or_label(m, start))

    # Case C: no crossover (demand grows slower than supply, starts below).
    m = find_crossover_month(1000, 0.10, 900, None, None, 0.05)
    assert m is None, "Case C expected None, got %s" % m
    print("  C no-crossover: %s  OK" % _quarter_or_label(m, start))

    # Case D: two-phase. supply 30000 @2%, demand0 30000? would be already-constrained.
    # Use demand0 = 25000. Phase1 industry 18.4% for first 8 months, then mgmt 25%.
    # Quick sanity: management (two-phase) must cross NO LATER than industry-only(18.4%).
    s0, sg, d0 = 30000.0, 0.02, 25000.0
    mgmt_off = 8 / 12.0
    m_two = find_crossover_month(s0, sg, d0, mgmt_off, 0.184, 0.25)
    m_ind = find_crossover_month(s0, sg, d0, None, None, 0.184)
    assert m_two is not None and m_ind is not None
    assert m_two <= m_ind, "two-phase(mgmt) should bind on/before industry-only (%s vs %s)" % (m_two, m_ind)
    print("  D two-phase: mgmt %s (%s) <= industry %s (%s)  OK"
          % (m_two, month_to_quarter(start, m_two), m_ind, month_to_quarter(start, m_ind)))

    # Case E: compute_crossovers fallback wiring (mgmt date before supply date -> single phase).
    res = compute_crossovers(30000, 0.02, 25000, date(2026, 1, 1), date(2025, 8, 28), 0.184, 0.25)
    assert res["crossover_management"].startswith("Q"), res
    assert res["crossover_industry"].startswith("Q"), res
    assert "single phase" in res["model_note"], res["model_note"]
    print("  E mgmt-predates-supply single phase: mgmt=%s industry=%s"
          % (res["crossover_management"], res["crossover_industry"]))
    print("  model_note: %s" % res["model_note"])

    # Case F: month_to_quarter calendar mapping (spec example Aug 2028 -> Q3 2028).
    assert month_to_quarter(date(2026, 1, 1), 31) == "Q3 2028", month_to_quarter(date(2026, 1, 1), 31)
    print("  F Aug-2028 -> %s  OK" % month_to_quarter(date(2026, 1, 1), 31))
    print("ALL CROSSOVER SELF-TESTS PASSED")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest_crossover()
    else:
        tk = next((a for a in sys.argv[1:] if not a.startswith("-")), "NVDA")
        out = run_constraint_auditor(tk, progress=lambda m, k="info": print("  [%s] %s" % (k, m)))
        print(json.dumps({k: v for k, v in out.items() if k != "human_readable"}, indent=2, default=str)[:4000])
        if out.get("human_readable"):
            print("\n--- HUMAN READABLE ---\n" + out["human_readable"])
