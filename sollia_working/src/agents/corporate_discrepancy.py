"""
Agent 1 — Corporate Discrepancy: Cross-document contradiction finder across 5 SEC filings.
Layer 0: Fiscal year alignment + data fetching with 3-tier fallback per document type.
"""
from __future__ import annotations
import os

from src.config.llm_config import LLM_PROVIDERS, RATE_LIMIT, call_llm, get_primary_model, start_tier
from src.utils.scoring import score_corporate_discrepancy
from src.utils.prompt_safety import neutralize_injections

# Cache integration — check before downloading
from src.providers.filings.sec_edgar import get_cached_text, cache_text, get_cached_filings, cache_filings
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.cache')
os.makedirs(os.path.join(CACHE_DIR, 'filings'), exist_ok=True)
os.makedirs(os.path.join(CACHE_DIR, 'texts'), exist_ok=True)

import logging
import re
import time
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Sollia contact@sollia.app"}
DATA_HEADERS = {"User-Agent": "Sollia contact@sollia.app", "Host": "data.sec.gov"}
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
SEC_BASE = "https://data.sec.gov"
EDGAR_BASE = "https://www.sec.gov"
MAX_YEARS_BACK = 10


_CIK_CACHE: Dict[str, str] = {}

def get_cik(ticker: str) -> Optional[str]:
    key = ticker.strip().upper().replace(".", "-")  # " aapl " -> AAPL; BRK.A -> BRK-A (SEC form)
    if key in _CIK_CACHE:
        return _CIK_CACHE[key]
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=HEADERS, timeout=15)
        r.raise_for_status()
        for item in r.json().values():
            if str(item.get("ticker", "")).upper() == key:
                cik = str(item["cik_str"]).zfill(10)
                _CIK_CACHE[key] = cik
                return cik
    except Exception as e:
        logger.error("CIK lookup failed for %s: %s", ticker, e)
    return None


def _strip_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"<\w+:?\w+[^>]*>", "", text)
    # Remove XBRL metadata lines that survive HTML stripping
    text = re.sub(r'^\d{10}\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\d{4}-\d{2}-\d{2}\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^(false|true)\s*$', '', text, flags=re.MULTILINE | re.IGNORECASE)
    text = re.sub(r'^iso4217:\S+\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^xbrli:\S+\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^us-gaap:\S+\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" +", " ", text)
    # Defang prompt-injection directives in this UNTRUSTED filing text before it
    # can reach any prompt or be surfaced as a quote (Part 3a, 2026-07-02).
    # Shared by Agents 1/3/6 which all clean HTML through here.
    text = neutralize_injections(text)
    return text.strip()


class SECFilingFetcher:
    def __init__(self):
        self._last_request = 0.0
        # Set by align_fiscal_year()/fetch_all_texts() so _get_all_filings() and
        # _fetch_htm() can hit the shared .cache/ instead of re-downloading from SEC.
        # Previously this was only ever READ (getattr default None) and NEVER set,
        # so every run bypassed the cache → 60s SEC download phase + timeouts.
        self._current_ticker = None

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < RATE_LIMIT:
            time.sleep(RATE_LIMIT - elapsed)
        self._last_request = time.time()

    def _get_json(self, cik: str) -> dict:
        self._rate_limit()
        r = requests.get(f"{SEC_BASE}/submissions/CIK{cik}.json", headers=DATA_HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()

    def _get_all_filings(self, cik: str) -> List[dict]:
        # CHECK CACHE FIRST
        ticker = getattr(self, '_current_ticker', None)
        if ticker:
            cached = get_cached_filings(ticker)
            if cached:
                recent = cached.get("filings", {}).get("recent", {})
                all_forms = list(recent.get("form", []))
                n = len(all_forms)
                all_periods = list(recent.get("reportDate", [""] * n))
                all_filing_dates = list(recent.get("filingDate", [""] * n))
                all_items = list(recent.get("items", [""] * n))
                all_accessions = list(recent.get("accessionNumber", [""] * n))
                all_docs = list(recent.get("primaryDocument", [""] * n))
                # Also merge historical archives from cache
                for file_info in cached.get("filings", {}).get("files", []):
                    hist_cached = get_cached_filings(f"{ticker}_hist_{file_info['name']}")
                    if hist_cached:
                        m = len(hist_cached.get("form", []))
                        all_forms.extend(hist_cached.get("form", []))
                        all_periods.extend(hist_cached.get("reportDate", [""] * m))
                        all_filing_dates.extend(hist_cached.get("filingDate", [""] * m))
                        all_items.extend(hist_cached.get("items", [""] * m))
                        all_accessions.extend(hist_cached.get("accessionNumber", [""] * m))
                        all_docs.extend(hist_cached.get("primaryDocument", [""] * m))
            else:
                # Fetch fresh and cache
                return self._fetch_filings_from_sec(cik, ticker)
        else:
            return self._fetch_filings_from_sec(cik, None)
        
        filings = []
        for i in range(len(all_forms)):
            filings.append({
                "form": all_forms[i],
                "period_of_report": all_periods[i] if i < len(all_periods) else "",
                "filing_date": all_filing_dates[i] if i < len(all_filing_dates) else "",
                "items": all_items[i] if i < len(all_items) else "",
                "accession_number": all_accessions[i] if i < len(all_accessions) else "",
                "primary_document": all_docs[i] if i < len(all_docs) else "",
            })
        return filings

    def _fetch_filings_from_sec(self, cik: str, ticker: str = None) -> List[dict]:
        """Fetch all filings from SEC (fresh, no cache)."""
        data = self._get_json(cik)
        if ticker:
            import json as _json
            cache_file = os.path.join(CACHE_DIR, "filings", f"{ticker}_filings.json")
            with open(cache_file, "w") as _f:
                _json.dump(data, _f)
        recent = data["filings"]["recent"]
        all_forms = list(recent.get("form", []))
        n = len(all_forms)
        all_periods = list(recent.get("reportDate", [""] * n))
        all_filing_dates = list(recent.get("filingDate", [""] * n))
        all_items = list(recent.get("items", [""] * n))
        all_accessions = list(recent.get("accessionNumber", [""] * n))
        all_docs = list(recent.get("primaryDocument", [""] * n))
        for file_info in data.get("filings", {}).get("files", []):
            try:
                self._rate_limit()
                r = requests.get(f"{SEC_BASE}/submissions/{file_info['name']}", headers=DATA_HEADERS, timeout=30)
                if r.status_code == 200:
                    old = r.json()
                    # Cache historical data too
                    if ticker:
                        cache_filings(f"{ticker}_hist_{file_info['name']}", old)
                    m = len(old.get("form", []))
                    all_forms.extend(old.get("form", []))
                    all_periods.extend(old.get("reportDate", [""] * m))
                    all_filing_dates.extend(old.get("filingDate", [""] * m))
                    all_items.extend(old.get("items", [""] * m))
                    all_accessions.extend(old.get("accessionNumber", [""] * m))
                    all_docs.extend(old.get("primaryDocument", [""] * m))
            except Exception as e:
                logger.debug("Archive failed: %s", e)
        filings = []
        for i in range(len(all_forms)):
            filings.append({
                "form": all_forms[i],
                "period_of_report": all_periods[i] if i < len(all_periods) else "",
                "filing_date": all_filing_dates[i] if i < len(all_filing_dates) else "",
                "items": all_items[i] if i < len(all_items) else "",
                "accession_number": all_accessions[i] if i < len(all_accessions) else "",
                "primary_document": all_docs[i] if i < len(all_docs) else "",
            })
        return filings
    def _fetch_htm(self, cik: str, acc: str, doc: str) -> str:
        # CHECK CACHE FIRST
        ticker = getattr(self, '_current_ticker', None)
        if ticker:
            cached_text = get_cached_text(ticker, acc)
            if cached_text:
                return cached_text
        
        acc_clean = acc.replace("-", "")
        url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_clean}/{doc}"
        self._rate_limit()
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200 and len(r.text) > 5000:
            text = r.text
            # Cache the fetched text
            if ticker:
                cache_text(ticker, acc, text)
            return text
        return ""

    def align_fiscal_year(self, ticker: str, year: int) -> Tuple[Optional[dict], Optional[str]]:
        """Back-compat single-year alignment (strict). New code should use
        build_rolling_window(); this remains for direct callers/tests."""
        self._current_ticker = ticker
        cik = get_cik(ticker)
        if not cik:
            return None, f"CIK not found for {ticker}"
        filings = self._get_all_filings(cik)
        ten_ks = [f for f in filings if f["form"] == "10-K" and f.get("period_of_report", "") and f.get("period_of_report", "")[:4] == str(year)]
        if not ten_ks:
            return None, f"No 10-K found for {ticker} in {year}"
        ten_k = max(ten_ks, key=lambda f: f.get("period_of_report", ""))
        return self._assemble_year_set(ticker, cik, filings, ten_k, relaxed=False)

    def _assemble_year_set(self, ticker: str, cik: str, filings: List[dict], ten_k: dict,
                           relaxed: bool = False, used_transcript_dates: Optional[set] = None,
                           ) -> Tuple[Optional[dict], Optional[str]]:
        """Assemble the document set anchored on ONE 10-K: its 10-Qs (period
        within the trailing year), nearest DEF 14A after fiscal end, latest
        material 8-K before it, and PROXIMITY-assigned earnings transcripts.
        strict mode (relaxed=False) requires the full set — that is the
        definition of a COMPLETE fiscal year for the rolling window; relaxed
        mode returns whatever exists (used for older window years and the
        current partial year)."""
        F = date.fromisoformat(ten_k["period_of_report"])
        year = int(ten_k["period_of_report"][:4])
        F_minus_1y = F.replace(year=F.year - 1)
        ten_qs = [f for f in filings if f["form"] == "10-Q" and f.get("period_of_report", "") and F_minus_1y < date.fromisoformat(f.get("period_of_report", "")) <= F]
        def14as = [f for f in filings if "14A" in f["form"] and f["filing_date"] and date.fromisoformat(f["filing_date"]) > F]
        def14a = min(def14as, key=lambda f: abs((date.fromisoformat(f["filing_date"]) - F).days)) if def14as else None
        eight_ks = [f for f in filings if f["form"] == "8-K" and f["filing_date"] and date.fromisoformat(f["filing_date"]) <= F and any(code in (f.get("items") or "") for code in ["2.02", "1.01", "5.02"])]
        eight_k = max(eight_ks, key=lambda f: f["filing_date"]) if eight_ks else None
        transcripts = self._fetch_transcripts(
            ticker, cik, ten_qs, F, filings,
            date_from=F_minus_1y, used_dates=used_transcript_dates)
        missing = []
        if not ten_qs: missing.append("10-Q")
        if not transcripts or len(transcripts) < 4: missing.append("EARNINGS_CALL")
        if not def14a: missing.append("DEF14A")
        if not eight_k: missing.append("8-K")
        if missing and not relaxed:
            return None, f"Insufficient filings for {ticker} in {year}. Missing: {', '.join(missing)}."
        return {"ticker": ticker, "year": year, "cik": cik, "fiscal_end": F.isoformat(),
                "ten_k": ten_k, "ten_qs": ten_qs, "def14a": def14a, "eight_k": eight_k,
                "transcripts": transcripts, "complete": not missing}, None

    def _assemble_partial_year(self, ticker: str, cik: str, filings: List[dict], ten_k: dict,
                               used_transcript_dates: Optional[set] = None,
                               ) -> Optional[dict]:
        """Assemble the CURRENT PARTIAL fiscal year: the latest 10-K plus every
        10-Q/transcript dated after its fiscal end. Returns None when nothing
        has been filed since the 10-K (the window then ends at the anchor)."""
        F = date.fromisoformat(ten_k["period_of_report"])
        ten_qs = [f for f in filings if f["form"] == "10-Q" and f.get("period_of_report", "")
                  and date.fromisoformat(f["period_of_report"]) > F]
        # include_after_f=False: the first call after the 10-K's fiscal end is the
        # PRIOR year's Q4/full-year call — it belongs to the anchor year (its
        # after_F pick), not the partial year. The partial year only claims calls
        # proximity-matched to its own filed 10-Qs.
        transcripts = self._fetch_transcripts(
            ticker, cik, ten_qs, F, filings, date_from=F,
            used_dates=used_transcript_dates, include_after_f=False)
        if not ten_qs and not transcripts:
            return None
        def14as = [f for f in filings if "14A" in f["form"] and f["filing_date"] and date.fromisoformat(f["filing_date"]) > F]
        def14a = min(def14as, key=lambda f: date.fromisoformat(f["filing_date"]) - F) if def14as else None
        eight_ks = [f for f in filings if f["form"] == "8-K" and f["filing_date"] and date.fromisoformat(f["filing_date"]) > F and any(code in (f.get("items") or "") for code in ["2.02", "1.01", "5.02"])]
        eight_k = max(eight_ks, key=lambda f: f["filing_date"]) if eight_ks else None
        return {"ticker": ticker, "year": F.year + 1, "cik": cik, "fiscal_end": F.isoformat(),
                "ten_k": ten_k, "ten_qs": ten_qs, "def14a": def14a, "eight_k": eight_k,
                "transcripts": transcripts, "complete": False, "partial": True}

    def build_rolling_window(self, ticker: str, max_years: int = 5,
                             ) -> Tuple[Optional[List[dict]], Optional[str]]:
        """PART 2 (2026-07-01): the rolling analysis window replacing the year
        selector. Returns year-sets NEWEST FIRST:

          [current partial year (if any filings since the last 10-K),
           most recent COMPLETE fiscal year (the anchor — strict set required),
           ... up to `max_years`-1 further complete years, relaxed]

        Foreign private issuers (20-F/40-F, no 10-K) get a clear unsupported
        message instead of a confusing 'No 10-K found'."""
        self._current_ticker = ticker
        cik = get_cik(ticker)
        if not cik:
            return None, f"CIK not found for {ticker}"
        filings = self._get_all_filings(cik)
        ten_ks = sorted((f for f in filings if f["form"] == "10-K" and f.get("period_of_report")),
                        key=lambda f: f["period_of_report"], reverse=True)
        if not ten_ks:
            foreign = [f["form"] for f in filings if f["form"] in ("20-F", "40-F", "6-K")]
            if foreign:
                return None, (f"{ticker} is a foreign private issuer (files "
                              f"{'/'.join(sorted(set(foreign) - {'6-K'}) or ['6-K'])}, not 10-K). "
                              "Foreign filers are not yet supported.")
            return None, f"No 10-K filings found for {ticker}"

        used_transcripts: set = set()
        window: List[dict] = []
        errors: List[str] = []

        # Current partial year first (recency order) — claims the newest transcripts.
        partial = self._assemble_partial_year(ticker, cik, filings, ten_ks[0],
                                              used_transcript_dates=used_transcripts)
        if partial:
            window.append(partial)

        # Walk 10-Ks newest-first, keeping complete years until the window is full.
        # The FIRST complete year is the anchor; older years are assembled relaxed
        # (a missing DEF 14A five years ago should not shrink the window).
        anchor_found = False
        for tk in ten_ks:
            if sum(1 for w in window if not w.get("partial")) >= max_years:
                break
            year_set, err = self._assemble_year_set(
                ticker, cik, filings, tk, relaxed=anchor_found,
                used_transcript_dates=used_transcripts)
            if year_set is None:
                errors.append(err or "unknown")
                continue
            if not anchor_found and not year_set["complete"]:
                continue  # keep looking for the anchor (most recent COMPLETE year)
            anchor_found = True
            window.append(year_set)

        if not anchor_found:
            # No strictly-complete year exists (thin filers). Degrade: take the
            # most recent 10-K relaxed so the agent still produces an analysis.
            year_set, err = self._assemble_year_set(
                ticker, cik, filings, ten_ks[0], relaxed=True,
                used_transcript_dates=used_transcripts)
            if year_set is None:
                return None, (errors[0] if errors else (err or f"Could not assemble filings for {ticker}"))
            window.append(year_set)
        return window, None

    def _fetch_transcripts(self, ticker: str, cik: str, ten_qs: List[dict], F: date,
                           filings: List[dict], date_from: Optional[date] = None,
                           used_dates: Optional[set] = None,
                           include_after_f: bool = True,
                           date_to: Optional[date] = None) -> List[dict]:
        """Earnings-call transcripts from 8-K item 2.02 exhibits, assigned to
        quarters by DATE PROXIMITY. `date_from`/`date_to` bound the candidate
        scan (a 30-year filer would otherwise fetch hundreds of historical
        8-Ks); `used_dates` is shared across rolling-window years so a
        transcript is never assigned to two adjacent fiscal years. Without
        include_after_f the scan still extends past F (candidates for the
        partial year's own quarters) but the prior year's Q4 call is left for
        that year's after_F pick."""
        all_transcripts = []
        if date_to is None:
            date_to = F + timedelta(days=120) if include_after_f else date.today()
        for f in filings:
            if f["form"] != "8-K" or "2.02" not in (f.get("items") or "") or not f["filing_date"]:
                continue
            f_date = date.fromisoformat(f["filing_date"])
            if date_from and f_date < date_from:
                continue
            if f_date > date_to:
                continue
            acc = f["accession_number"]
            doc = f["primary_document"]
            if not acc or not doc:
                continue
            content = self._fetch_ex99(cik, acc.replace("-", ""))
            if not content:
                html = self._fetch_htm(cik, acc, doc)
                if html:
                    content = _strip_html(html)
            if content and len(content) > 500:
                all_transcripts.append({"date": f["filing_date"], "content": content, "source": "SEC-8K"})
        assigned = []
        used = used_dates if used_dates is not None else set()
        for q in ten_qs:
            q_date = date.fromisoformat(q["period_of_report"])
            available = [t for t in all_transcripts if t["date"] not in used]
            if not include_after_f:
                # Partial-year mode: a call reporting a quarter is always FILED
                # AFTER that quarter closes — never claim an earlier call (it
                # would be the prior year's Q4 call, owed to that year).
                available = [t for t in available if date.fromisoformat(t["date"]) > q_date]
            if not available:
                continue
            best = min(available, key=lambda t: abs((date.fromisoformat(t["date"]) - q_date).days))
            assigned.append(best)
            used.add(best["date"])
        if include_after_f:
            after_F = [t for t in all_transcripts if t["date"] not in used and date.fromisoformat(t["date"]) > F]
            if after_F:
                best = min(after_F, key=lambda t: (date.fromisoformat(t["date"]) - F).days)
                assigned.append(best)
                used.add(best["date"])
        return assigned

    def _fetch_ex99(self, cik: str, acc: str) -> str:
        idx_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc}/{acc}-index.htm"
        try:
            self._rate_limit()
            r = requests.get(idx_url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                return ""
            ex99 = re.findall(r'href="([^"]*ex-?99[^"]*\.(?:htm|html))"', r.text, re.IGNORECASE)
            if not ex99:
                return ""
            ex99_url = ex99[0]
            if not ex99_url.startswith("http"):
                ex99_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc}/{ex99_url.split('/')[-1]}"
            self._rate_limit()
            r_ex = requests.get(ex99_url, headers=HEADERS, timeout=15)
            if r_ex.status_code == 200 and len(r_ex.text) > 2000:
                return _strip_html(r_ex.text)
        except Exception as e:
            logger.debug("EX-99.1 failed: %s", e)
        return ""





# ============================================================================
# LAYER 0.5: Cleaning
# ============================================================================

def clean_10k(text: str) -> str:
    """Extract MD&A section from 10-K. Picks the Item 7 occurrence with the most content."""
    best_start, best_len = None, 0
    for m in re.finditer(r'Item\s*7[\.\s]', text, re.IGNORECASE):
        start = m.start()
        end_m = re.search(r'Item\s*7A[\.\s]|Item\s*8[\.\s]', text[start+50:], re.IGNORECASE)
        length = end_m.start() if end_m else min(50000, len(text) - start)
        if length > best_len:
            best_start, best_len = start, length
    if best_start is not None and best_len > 5000:
        end_m = re.search(r'Item\s*7A[\.\s]|Item\s*8[\.\s]', text[best_start+50:], re.IGNORECASE)
        if end_m:
            return text[best_start:best_start + 50 + end_m.start()].strip()
        return text[best_start:best_start + 50000].strip()
    n = len(text)
    return text[n//5:4*n//5][:50000]

def clean_10q(text: str) -> str:
    """Extract MD&A section from 10-Q. Prefers Item 2 with 'Management' in header, then longest."""
    candidates = []
    for m in re.finditer(r'Item\s*2[\.\s]', text, re.IGNORECASE):
        start = m.start()
        snippet = text[start:start+150]
        end_m = re.search(r'Item\s*[34][\.\s]', text[start+50:], re.IGNORECASE)
        length = end_m.start() if end_m else min(30000, len(text) - start)
        has_mgmt = bool(re.search(r'Management|MD&A|Discussion', snippet, re.IGNORECASE))
        candidates.append((start, length, has_mgmt))
    if candidates:
        candidates.sort(key=lambda x: (x[2], x[1]), reverse=True)
        start, length, _ = candidates[0]
        end_m = re.search(r'Item\s*[34][\.\s]', text[start+50:], re.IGNORECASE)
        if end_m:
            return text[start:start + 50 + end_m.start()].strip()
        return text[start:start + 30000].strip()
    return text[:30000]

def clean_8k(text: str) -> str:
    """Strip SEC header, keep material items only."""
    m = re.search(r'Item\s+\d+\.\d+', text, re.IGNORECASE)
    if m:
        text = text[m.start():]
    lines = text.split('\n')
    kept = []
    for line in lines:
        if re.match(r'^Item\s+\d+\.\d+', line.strip(), re.IGNORECASE):
            kept.append(line)
        elif kept:
            kept.append(line)
    return '\n'.join(kept) if kept else text


def clean_def14a(text: str) -> str:
    """Extract compensation discussion from DEF 14A."""
    patterns = [
        r'(Compensation Discussion\s*(?:and|&)\s*Analysis.*?)(?=\n\n[A-Z][a-z]+ [A-Z]|\nItem|\Z)',
        r'(Executive Compensation.*?)(?=\n\n[A-Z][a-z]+ [A-Z]|\nItem|\Z)',
        r'(Summary Compensation Table.*?)(?=\n\n[A-Z][a-z]+ [A-Z]|\nItem|\Z)',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.DOTALL | re.IGNORECASE)
        if m and len(m.group(1)) > 500:
            text = m.group(1)
            break
    text = re.sub(r'^(?:For|Against|Abstain|Broker\s+Non[\s-]*Votes?)\s*:?\s*[\d,]+', '', text, flags=re.MULTILINE | re.IGNORECASE)
    text = re.sub(r'(?:has served|joined|appointed|was elected).*?(?:since|from)\s+\d{4}.*?(?=\n\n|\Z)', '', text, flags=re.IGNORECASE | re.DOTALL)
    return text.strip()

def clean_transcript(text: str) -> str:
    """Strip SEC header boilerplate, speaker labels, timestamps, and disclaimers."""
    # Remove everything before the actual earnings content
    # Look for the start of the press release / earnings text
    start_markers = [
        r'(?:announced|reported|today announced|issued a press release).*?(?:results|financial results|quarter)',
        r'On\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}',
        r'Item\s+\d+\.\d+',
    ]
    for marker in start_markers:
        m = re.search(marker, text, re.IGNORECASE)
        if m:
            # Start from 200 chars before the match to capture context
            start = max(0, m.start() - 200)
            text = text[start:]
            break
    
    # Remove SEC header boilerplate lines
    boilerplate_patterns = [
        r'^.*UNITED STATES.*SECURITIES AND EXCHANGE COMMISSION.*$',
        r'^.*FORM\s+8-K.*$',
        r'^.*PURSUANT TO SECTION.*$',
        r'^.*Registrant.*telephone.*$',
        r'^.*\(Former name.*$',
        r'^.*Title of each class.*$',
        r'^.*Trading Symbol.*$',
        r'^.*Name of each exchange.*$',
        r'^.*Financial Statements and Exhibits.*$',
        r'^.*\(d\)\s+Exhibits.*$',
        r'^.*Exhibit Description.*$',
        r'^.*Press Release, dated.*$',
        r'^.*The press release.*furnished.*$',
    ]
    for pat in boilerplate_patterns:
        text = re.sub(pat, '', text, flags=re.MULTILINE | re.IGNORECASE)
    
    # Strip speaker labels and timestamps
    text = re.sub(r'^[A-Z][a-z-]+(?:\s+[A-Z][a-z-]+)?\s*[—–-]\s*(?:CEO|CFO|COO|President|Chairman|Analyst|VP|Director).*?\n', '', text, flags=re.MULTILINE)
    text = re.sub(r'^Operator:.*?\n', '', text, flags=re.MULTILINE)
    text = re.sub(r'\[\d{1,2}:\d{2}(?::\d{2})?\]', '', text)
    text = re.sub(r'(?:Safe Harbor|Forward[- ]Looking).*?(?=\n\n|\Z)', '', text, flags=re.IGNORECASE | re.DOTALL)
    
    # Clean up resulting whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'^\s*\n', '', text, flags=re.MULTILINE)
    
    return text.strip()

def clean_all(documents: dict) -> dict:
    """Apply all cleaning functions to fetched documents."""
    documents["ten_k_clean"] = clean_10k(documents["ten_k_text"])
    documents["ten_q_clean"] = [clean_10q(t) for t in documents["ten_q_texts"]]
    documents["def14a_clean"] = clean_def14a(documents["def14a_text"])
    documents["eight_k_clean"] = clean_8k(documents["eight_k_text"])
    documents["transcript_clean"] = [clean_transcript(t) for t in documents["transcript_texts"]]
    return documents


# ============================================================================
# LAYER 1: Chunking
# ============================================================================

# spaCy (en_core_web_sm) is the preferred sentence tokenizer. Guard BOTH the package
# import and the model load: if either is unavailable, fall back to the regex
# tokenizer in split_sentences() rather than crashing the whole agent on import.
try:
    import spacy
    nlp = spacy.load("en_core_web_sm")
except Exception as _spacy_exc:  # ImportError (no package) or OSError (no model)
    spacy = None
    nlp = None
    logger.warning(
        "spaCy unavailable (%s) — falling back to regex sentence tokenizer. "
        "Install with: pip install spacy && python -m spacy download en_core_web_sm",
        _spacy_exc,
    )

SECTION_RE = re.compile(r'^(Item\s+\d+[A-Z]?[\.:—]?\s+.+)$', re.MULTILINE | re.IGNORECASE)

def split_sections(text: str) -> List[dict]:
    matches = list(SECTION_RE.finditer(text))
    if not matches:
        return [{"section_name": "Full Document", "text": text}]
    sections = []
    for i, m in enumerate(matches):
        name = m.group(1).strip()
        start = m.end()
        end = matches[i+1].start() if i+1 < len(matches) else len(text)
        sections.append({"section_name": name, "text": text[start:end].strip()})
    return sections


def split_paragraphs(text: str, min_chars: int = 50) -> List[str]:
    paras = [p.strip() for p in re.split(r'\n{2,}', text)]
    return [p for p in paras if len(p) >= min_chars]


def split_sentences(text: str) -> List[str]:
    if nlp:
        doc = nlp(text)
        return [s.text.strip() for s in doc.sents if s.text.strip()]
    return [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if len(s.strip()) > 10]


def _filter_bad_sentences(sentences: List[str]) -> List[str]:
    """Remove SEC artifact fragments that survive HTML stripping."""
    filtered = []
    for s in sentences:
        stripped = s.strip()
        # Skip very short fragments (page numbers, single words)
        if len(stripped) < 12:
            continue
        # Skip purely numeric or CIK-like lines
        if re.match(r'^[\d\s|\-]+$', stripped):
            continue
        # Skip orphaned page header fragments
        if re.match(r'^\|?\s*\d+\s*$', stripped):
            continue
        # Skip lines that are just "PART", "Item", "ITEM" etc
        if re.match(r'^(PART|Item|ITEM)\s*$', stripped):
            continue
        # Skip XBRL metadata remnants
        if re.match(r'^(false|true|iso4217|[A-Z]+:\S+)$', stripped, re.IGNORECASE):
            continue
        # Skip lines that are just registered trademark symbols or special chars
        if re.match(r'^[®©™\|\s]+$', stripped):
            continue
        filtered.append(s)
    return filtered


def chunk_paragraph(text: str, max_chars: int = 800, min_chars: int = 300) -> List[str]:
    if len(text) <= max_chars:
        return [text]
    sents = split_sentences(text)
    # Filter out SEC artifact fragments
    sents = _filter_bad_sentences(sents)
    if not sents:
        return [text] if len(text) >= 50 else []
    chunks, current, cur_len = [], [], 0
    for s in sents:
        if cur_len + len(s) > max_chars and cur_len >= min_chars:
            chunks.append(' '.join(current))
            current, cur_len = [], 0
        current.append(s)
        cur_len += len(s)
    if current:
        chunks.append(' '.join(current))
    return chunks


def build_chunks(text: str, doc_type: str, ticker: str, year: int, accession: str = '', primary_doc: str = '') -> List[dict]:
    # REAL printed page numbers (Part 5): map each chunk to the filing's own
    # pagination. The old value here was a synthetic char_count//3000 estimate —
    # fake precision an analyst would eventually catch. page_number is now the
    # printed page, or None when the filing's pagination didn't survive cleaning.
    from src.utils.page_numbers import build_page_map, page_for_offset
    page_map = build_page_map(text)
    cursor = 0  # moving find() start so repeated chunk text maps in order
    sections = split_sections(text)
    chunks = []
    gi = 0  # running index → guarantees a UNIQUE chunk_id (section_name[:30] collides)
    for section in sections:
        paras = split_paragraphs(section["text"])
        for pi, para in enumerate(paras):
            sub_chunks = chunk_paragraph(para)
            for si, sub in enumerate(sub_chunks):
                sents = split_sentences(sub)
                page = None
                if page_map:
                    probe = sub[:80]
                    off = text.find(probe, cursor)
                    if off == -1:
                        off = text.find(probe)
                    if off != -1:
                        cursor = off
                        page = page_for_offset(page_map, off)
                chunks.append({
                    "chunk_id": f"{doc_type}_{section['section_name'][:30]}_{pi}_{si}_{gi}",
                    "text": sub,
                    "sentences": sents,
                    "metadata": {
                        "ticker": ticker, "year": year,
                        "source_doc_type": doc_type,
                        "section": section["section_name"],
                        "page_number": page,
                        "paragraph_index": pi,
                        "sentence_count": len(sents),
                        "accession_number": accession,
                        "primary_document": primary_doc
                    },
                    "embedding": None
                })
                gi += 1
    return chunks


def _quarter_label(period_iso: str, fiscal_end_iso: str) -> str:
    """Map a 10-Q period_of_report to its fiscal quarter (Q1/Q2/Q3) relative to
    the fiscal year end, so chunk labels read 'Q1' instead of an array index.
    Handles non-calendar fiscal years (uses month offset from the fiscal end)."""
    try:
        p = date.fromisoformat(period_iso)
        fye = date.fromisoformat(fiscal_end_iso)
        months_before = (fye.month - p.month) % 12
        q = 4 - (months_before // 3)
        return "Q%d" % q if 1 <= q <= 4 else ""
    except Exception:
        return ""


def _display_doc_type(doc_type: str, year=None) -> str:
    """Human-readable filing label for citations, e.g. '10-Q_Q1' -> '10-Q (Q1 2025)',
    'EARNINGS_CALL_0' -> 'Earnings Call', 'DEF14A' -> 'DEF 14A'."""
    if not doc_type:
        return ""
    yr = str(year) if year else ""
    if doc_type.startswith("10-Q_"):
        suffix = doc_type[len("10-Q_"):]
        if re.match(r'^Q[1-4]$', suffix):
            return ("10-Q (%s %s)" % (suffix, yr)).strip()
        return "10-Q"
    if doc_type.startswith("EARNINGS_CALL"):
        return "Earnings Call"
    if doc_type.startswith("DEF14A"):
        return "DEF 14A"
    if doc_type.startswith("8-K"):
        return "8-K"
    if doc_type.startswith("10-K"):
        return "10-K"
    return doc_type


def chunk_all(documents: dict) -> dict:
    """Chunk all cleaned documents."""
    ticker = documents["ticker"]
    year = documents["year"]
    documents["chunks_10k"] = build_chunks(documents["ten_k_clean"], "10-K", ticker, year, documents.get("ten_k", {}).get("accession_number", ""), documents.get("ten_k", {}).get("primary_document", ""))
    documents["chunks_10q"] = []
    fiscal_end = documents.get("fiscal_end", "")
    for i, text in enumerate(documents["ten_q_clean"]):
        ten_q_list = documents.get("ten_qs", [])
        acc = ten_q_list[i].get("accession_number", "") if i < len(ten_q_list) else ""
        doc = ten_q_list[i].get("primary_document", "") if i < len(ten_q_list) else ""
        period = ten_q_list[i].get("period_of_report", "") if i < len(ten_q_list) else ""
        q = _quarter_label(period, fiscal_end)
        suffix = q if q else str(i)
        documents["chunks_10q"].extend(build_chunks(text, f"10-Q_{suffix}", ticker, year, acc, doc))
    documents["chunks_def14a"] = build_chunks(documents["def14a_clean"], "DEF14A", ticker, year, documents.get("def14a", {}).get("accession_number", ""), documents.get("def14a", {}).get("primary_document", ""))
    documents["chunks_8k"] = build_chunks(documents["eight_k_clean"], "8-K", ticker, year, documents.get("eight_k", {}).get("accession_number", ""), documents.get("eight_k", {}).get("primary_document", ""))
    documents["chunks_transcript"] = []
    for i, text in enumerate(documents["transcript_clean"]):
        documents["chunks_transcript"].extend(build_chunks(text, f"EARNINGS_CALL_{i}", ticker, year, "", ""))
    return documents


def fetch_texts_for_set(result: dict, fetcher: Optional[SECFilingFetcher] = None,
                        ) -> Tuple[Optional[dict], Optional[str]]:
    """Download full text for an already-aligned document set. Tolerates a
    missing DEF 14A / 8-K (relaxed rolling-window years): those keys are set to
    empty strings so downstream clean/chunk layers simply produce no chunks."""
    ticker = result["ticker"]
    if fetcher is None:
        fetcher = SECFilingFetcher()
    fetcher._current_ticker = ticker  # enable cache reads in _fetch_htm()/_get_all_filings()
    cik = result["cik"]

    # 10-K text
    ten_k = result["ten_k"]
    html = fetcher._fetch_htm(cik, ten_k["accession_number"], ten_k["primary_document"])
    result["ten_k_text"] = _strip_html(html) if html else ""
    if not result["ten_k_text"]:
        return None, f"Could not download the 10-K text for {ticker} {result.get('year')}"
    result["ten_k_clean"] = clean_10k(result["ten_k_text"])

    # 10-Q texts
    result["ten_q_texts"] = []
    result["ten_q_clean"] = []
    for q in result["ten_qs"]:
        html = fetcher._fetch_htm(cik, q["accession_number"], q["primary_document"])
        text = _strip_html(html) if html else ""
        result["ten_q_texts"].append(text)
        result["ten_q_clean"].append(clean_10q(text) if text else "")

    # DEF 14A text — prefer DEF 14A over PX14A6G if multiple in window
    def14a = result.get("def14a")
    if def14a and "PX14A6G" in def14a.get("form", ""):
        # Search for a real DEF 14A within 90 days
        all_def14as = [f for f in fetcher._get_all_filings(cik) if "14A" in f["form"] and f["filing_date"]]
        F_date = date.fromisoformat(result["fiscal_end"])
        better = [f for f in all_def14as if "DEF 14A" in f["form"] and "PX" not in f["form"]
                  and abs((date.fromisoformat(f["filing_date"]) - F_date).days) < 90]
        if better:
            def14a = min(better, key=lambda f: abs((date.fromisoformat(f["filing_date"]) - F_date).days))
            result["def14a"] = def14a
    if def14a:
        html = fetcher._fetch_htm(cik, def14a["accession_number"], def14a["primary_document"])
        result["def14a_text"] = _strip_html(html) if html else ""
    else:
        result["def14a_text"] = ""
        result["def14a"] = {}
    if result["def14a_text"]:
        result["def14a_clean"] = clean_def14a(result["def14a_text"])

    # 8-K text
    eight_k = result.get("eight_k")
    if eight_k:
        html = fetcher._fetch_htm(cik, eight_k["accession_number"], eight_k["primary_document"])
        result["eight_k_text"] = _strip_html(html) if html else ""
    else:
        result["eight_k_text"] = ""
        result["eight_k"] = {}
    if result["eight_k_text"]:
        result["eight_k_clean"] = clean_8k(result["eight_k_text"])

    # Transcripts already have content from fetch
    result["transcript_texts"] = [t["content"] for t in result["transcripts"]]

    return result, None


def fetch_all_texts(ticker: str, year: int) -> Tuple[Optional[dict], Optional[str]]:
    """Back-compat: fetch and align a SINGLE fiscal year, then download texts.
    The rolling-window pipeline calls fetch_texts_for_set() per window year."""
    result, error = fetch_all_documents(ticker, year)
    if error:
        return None, error
    return fetch_texts_for_set(result)


def fetch_all_documents(ticker: str, year: int) -> Tuple[Optional[dict], Optional[str]]:
    if year < datetime.now().year - MAX_YEARS_BACK:
        return None, f"Year {year} is more than {MAX_YEARS_BACK} years back"
    return SECFilingFetcher().align_fiscal_year(ticker, year)

# ============================================================================
# LAYER 2: Embedding
# ============================================================================

import hashlib

# Disk cache for FinBERT embeddings — keyed by a hash of the (normalized) chunk
# text so identical content is never re-embedded across runs. This turns a cold
# ~90-180s embedding pass into a <1s cache load on warm re-runs.
_EMB_CACHE_DIR = os.path.join(CACHE_DIR, 'embeddings')
os.makedirs(_EMB_CACHE_DIR, exist_ok=True)
_EMB_MODEL_TAG = "finbert-mean-v1"

# WS1: boilerplate-opener stripping was TESTED (ws1_align_test.py) and found
# ineffective on F 2025 — only ~5% of chunks open with boilerplate and stripping
# left alignment essentially unchanged (319 -> 319 pairs). The real false-positive
# driver is FinBERT anisotropy, fixed by mean-centering at alignment time (see
# _apply_mean_centering). Stripping is kept as an opt-in normalization but is OFF
# by default so embeddings (and their disk cache keys) match the raw chunk text.
STRIP_BOILERPLATE_FOR_EMBEDDING = False

_BOILERPLATE_OPENER_RE = re.compile(
    r'^(?:\s*(?:'
    r'subject to|we (?:may|believe|expect|anticipate|intend|are|have|will|continue|do not|cannot|plan to|seek to)|'
    r'in addition|as a result|from time to time|as of|during the|'
    r'for the (?:three|nine|six|twelve|year|quarter|period|fiscal)|'
    r"the company(?:'s)?|management(?:'s)?|"
    r'these (?:forward[- ]looking )?statements|forward[- ]looking statements|'
    r'this (?:quarterly |annual )?report|see note|refer to|pursuant to|in accordance with|certain|'
    r'our (?:results|business|operations|revenues?|net|total|consolidated)'
    r')\b[^,.;]{0,80}[,;]\s*)+',
    re.IGNORECASE,
)


def _embedding_text(text: str) -> str:
    """Normalize chunk text for embedding by stripping leading boilerplate
    clause(s). Falls back to the original text when stripping would remove too
    much (keeps >=40 chars and >=40% of the content)."""
    if not text:
        return text
    stripped = _BOILERPLATE_OPENER_RE.sub('', text, count=1).strip()
    if len(stripped) >= max(40, int(len(text) * 0.4)):
        return stripped
    return text


def _emb_cache_path(emb_input: str) -> str:
    key = hashlib.sha256((_EMB_MODEL_TAG + "\n" + emb_input).encode("utf-8")).hexdigest()[:24]
    return os.path.join(_EMB_CACHE_DIR, key + ".json")


def _get_cached_embedding(emb_input: str):
    path = _emb_cache_path(emb_input)
    if os.path.exists(path):
        try:
            import json as _json
            with open(path) as f:
                return _json.load(f)
        except Exception:
            return None
    return None


def _cache_embedding(emb_input: str, embedding) -> None:
    """Write embedding atomically (temp + rename) so concurrent runs never see a
    half-written file."""
    path = _emb_cache_path(emb_input)
    try:
        import json as _json
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w") as f:
            _json.dump(embedding, f)
        os.replace(tmp, path)
    except Exception:
        pass


_EMBEDDER = None

def get_embedder():
    """Lazy-load + memoize the FinBERT embedder so the model is loaded once and
    reused across runs (and concurrent runs) instead of reloaded every time (WS6)."""
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    try:
        from transformers import AutoTokenizer, AutoModel
        import torch
        tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
        model = AutoModel.from_pretrained("ProsusAI/finbert")
        def embed(text):
            inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512, padding=True)
            with torch.no_grad():
                outputs = model(**inputs)
            return outputs.last_hidden_state.mean(dim=1).squeeze().tolist()
        _EMBEDDER = embed
        return embed
    except ImportError:
        return None

def embed_all_chunks(documents, embed_fn):
    """Embed all chunks across all document types, backed by a disk cache so
    re-runs skip FinBERT entirely (WS6)."""
    all_chunks = (documents.get("chunks_10k", []) + documents.get("chunks_10q", []) +
                  documents.get("chunks_def14a", []) + documents.get("chunks_8k", []) +
                  documents.get("chunks_transcript", []))
    hits = misses = 0
    for c in all_chunks:
        if c.get("embedding") is not None:
            continue
        emb_input = _embedding_text(c["text"]) if STRIP_BOILERPLATE_FOR_EMBEDDING else c["text"]
        cached = _get_cached_embedding(emb_input)
        if cached is not None:
            c["embedding"] = cached
            hits += 1
        else:
            emb = embed_fn(emb_input)
            c["embedding"] = emb
            _cache_embedding(emb_input, emb)
            misses += 1
    documents["_embed_stats"] = {"cache_hits": hits, "cache_misses": misses}
    return documents


# ============================================================================
# LAYER 3: Alignment
# ============================================================================

SIMILARITY_THRESHOLD = 0.86

DOCUMENT_PAIRS = [
    ("10-K", "EARNINGS_CALL", 1), ("10-K", "10-Q", 1), ("10-Q", "EARNINGS_CALL", 1),
    ("10-K", "8-K", 2), ("EARNINGS_CALL", "8-K", 2),
    ("10-K", "DEF14A", 3), ("10-Q", "8-K", 3), ("10-Q", "DEF14A", 3),
    ("EARNINGS_CALL", "DEF14A", 3), ("8-K", "DEF14A", 3),
]

SECTION_EQUIV = {
    "MD&A": ["Management's Discussion", "Management Discussion", "Discussion and Analysis"],
    "Risk Factors": ["Risk Factors", "Risks", "Principal Risks"],
}

def _section_boost(sec_a, sec_b):
    for canonical, variants in SECTION_EQUIV.items():
        a_match = any(v.lower() in sec_a.lower() for v in [canonical] + variants)
        b_match = any(v.lower() in sec_b.lower() for v in [canonical] + variants)
        if a_match and b_match:
            return 0.05
    return 0.0

def _apply_mean_centering(all_chunks):
    """WS1: mean-center embeddings (anisotropy correction). FinBERT embeddings are
    highly anisotropic — a dominant component shared by all finance text inflates
    every cosine, so topically-unrelated passages that merely share boilerplate
    align. Subtracting the corpus mean removes that shared component. On F 2025 this
    cut false-positive alignments from ~40% to ~0% at the 0.86 threshold while
    retaining genuine topical matches (319 -> 92 aligned pairs). Stores the centered
    vector on each chunk as '_emb_c'."""
    import numpy as np
    embs = [c["embedding"] for c in all_chunks if c.get("embedding") is not None]
    if len(embs) < 2:
        return
    mean = np.mean(np.array(embs, dtype=float), axis=0)
    for c in all_chunks:
        if c.get("embedding") is not None:
            c["_emb_c"] = (np.array(c["embedding"], dtype=float) - mean).tolist()


def _compute_similarity_matrix(chunks_a, chunks_b):
    import numpy as np
    # Prefer mean-centered embeddings (_emb_c) when present (WS1).
    emb_a = np.array([c.get("_emb_c", c["embedding"]) for c in chunks_a], dtype=float)
    emb_b = np.array([c.get("_emb_c", c["embedding"]) for c in chunks_b], dtype=float)
    norms_a = np.linalg.norm(emb_a, axis=1, keepdims=True)
    norms_b = np.linalg.norm(emb_b, axis=1, keepdims=True)
    a_norm = emb_a / np.maximum(norms_a, 1e-10)
    b_norm = emb_b / np.maximum(norms_b, 1e-10)
    return np.dot(a_norm, b_norm.T)

def _greedy_align(chunks_a, chunks_b, sim_matrix, threshold=SIMILARITY_THRESHOLD):
    import numpy as np, copy
    aligned = []
    used_b = set()
    for i in range(len(chunks_a)):
        row = sim_matrix[i]
        best_j, best_sim = None, -1
        secondaries = []
        for j in np.argsort(row)[::-1]:
            if row[j] < threshold:
                break
            if j not in used_b:
                if best_j is None:
                    best_j, best_sim = j, row[j]
                    used_b.add(j)
                else:
                    secondaries.append(chunks_b[j])
        if best_j is not None:
            b_chunk = copy.deepcopy(chunks_b[best_j])
            if secondaries:
                b_chunk["text"] += " " + " ".join(s["text"] for s in secondaries)
            aligned.append({
                "chunk_a": chunks_a[i], "chunk_b": b_chunk,
                "similarity": float(best_sim),
                "doc_type_a": chunks_a[i]["metadata"]["source_doc_type"],
                "doc_type_b": b_chunk["metadata"]["source_doc_type"]
            })
    return aligned

def _get_chunks_by_type(all_chunks, prefix):
    if prefix == "EARNINGS_CALL":
        return [c for c in all_chunks if "EARNINGS_CALL" in c["metadata"]["source_doc_type"]]
    return [c for c in all_chunks if c["metadata"]["source_doc_type"] == prefix or
            c["metadata"]["source_doc_type"].startswith(prefix + "_")]

def align_all_pairs(documents):
    """Run alignment across all 10 document pairs."""
    all_chunks = (documents.get("chunks_10k", []) + documents.get("chunks_10q", []) +
                  documents.get("chunks_def14a", []) + documents.get("chunks_8k", []) +
                  documents.get("chunks_transcript", []))

    # WS1: anisotropy correction before any pairwise similarity is computed.
    _apply_mean_centering(all_chunks)

    all_aligned = []
    for doc_a, doc_b, tier in DOCUMENT_PAIRS:
        chunks_a = _get_chunks_by_type(all_chunks, doc_a)
        chunks_b = _get_chunks_by_type(all_chunks, doc_b)
        if not chunks_a or not chunks_b:
            continue
        sim = _compute_similarity_matrix(chunks_a, chunks_b)
        for i, ca in enumerate(chunks_a):
            for j, cb in enumerate(chunks_b):
                boost = _section_boost(ca["metadata"]["section"], cb["metadata"]["section"])
                sim[i,j] = min(1.0, sim[i,j] + boost)
        pairs = _greedy_align(chunks_a, chunks_b, sim)
        for p in pairs:
            p["tier"] = tier
        all_aligned.extend(pairs)
    
    documents["aligned_pairs"] = all_aligned
    return documents


# ============================================================================
# LAYER 4: LLM Classification
# ============================================================================

# LLM_PROVIDERS imported from config.llm_config (see top of file)

# Deterministic cross-period guards (Part 2, 2026-07-01) — see the validation
# loop for rationale. Month names cover both "January 25, 2026" and
# "Jan 25, 2026" forms.
_AS_OF_DATE_RE = re.compile(
    r"\bas of\s+((?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?\s+\d{1,2},?\s+\d{4})", re.IGNORECASE)
_SUBANNUAL_RE = re.compile(
    r"\b(?:(?:first|second|third|fourth)\s+quarter|quarter(?:ly)?\b|"
    r"(?:three|six|nine)\s+months|first\s+half|second\s+half|"
    r"Q[1-4]\b|1[36]\s+weeks|26\s+weeks|39\s+weeks)", re.IGNORECASE)


def _is_table_fragment(s: str) -> bool:
    """True when a 'sentence' is dominated by figures — a table row that leaked
    through extraction, not a prose claim."""
    tokens = (s or "").split()
    if len(tokens) < 8:
        return False
    numeric = sum(1 for t in tokens if re.fullmatch(r"[\$%()\d.,\-—]+", t))
    return numeric / len(tokens) > 0.45

def _build_discrepancy_prompt(batch):
    """Build the LLM prompt for a topic batch."""
    topic = batch.get("topic", "Financial Analysis")
    prompt = "You are a forensic financial analyst. Compare these passages and identify discrepancies.\n\n"
    prompt += "Topic: %s\n\n" % topic
    for doc_label, passage in batch.get("passages", {}).items():
        if isinstance(passage, dict):
            sents = passage.get("sentences", [passage.get("text", "")])
        else:
            sents = [str(passage)]
        sents = sents[:12]
        prompt += "Passage %s (Section: %s):\n" % (doc_label, passage.get("metadata", {}).get("section", "") if isinstance(passage, dict) else "")
        for i, s in enumerate(sents):
            prompt += '[%d] "%s"\n' % (i, s)
        prompt += "\n"
    prompt += """
## CRITICAL: DOCUMENT CADENCES — READ BEFORE COMPARING (READ FIRST)
Each document type has a DIFFERENT reporting period. Numbers from different cadences are NOT discrepancies.

| Document  | Cadence              | Time Period Covered          |
|-----------|----------------------|------------------------------|
| 10-K      | ANNUAL (1 per year)  | FULL FISCAL YEAR (12 months) |
| 10-Q      | QUARTERLY (3 per yr) | SINGLE QUARTER (3 months)    |
| 8-K       | EVENT-DRIVEN         | Single material event/date   |
| DEF 14A   | ANNUAL (1 per year)  | Snapshot for annual meeting  |
| Earnings  | QUARTERLY (4 per yr) | Discussion of quarter ended  |

**TIME PERIOD MISMATCH EXAMPLES — DO NOT FLAG THESE:**
- 10-K says "$100B annual revenue" vs 10-Q says "$25B Q1 revenue" → NOT a discrepancy (annual vs quarterly)
- 10-K says "18% annual growth" vs earnings call says "22% Q3 growth" → NOT a discrepancy (different windows)
- 10-Q says "$5B operating income this quarter" vs 10-K says "$20B operating income this year" → NOT a discrepancy
- "year over year" in a 10-K means FULL YEAR vs prior year; "year over year" in a
  10-Q means THE QUARTER vs the same quarter last year. Opposite directions across
  those two windows (e.g. segment income fell for the full year but rose in the
  latest quarter) are NOT a contradiction.

**ONLY FLAG WHEN BOTH DOCUMENTS REFER TO THE SAME TIME PERIOD:**
- Both explicitly say "full year" / "fiscal year" / "annual"
- Both explicitly say the same quarter (e.g., "Q3 2024")
- Both refer to the same specific date or event
- The comparison is clearly apples-to-apples based on surrounding context

**POINT-IN-TIME SNAPSHOTS — DO NOT FLAG DIFFERENT "AS OF" DATES:**
Balances measured at a moment (remaining buyback authorization, cash on hand,
headcount, backlog, share count) change between dates by definition.
- "authorized to repurchase up to $58.5 billion as of January 25" vs "up to
  $38.5 billion as of April 26" → NOT a discrepancy (buybacks happened between
  the two dates). Two different "as of" dates are NEVER a contradiction.

**ANNUAL TOTAL vs PERIOD COMPONENT — DO NOT FLAG:**
- "paid cash dividends of $974 million in fiscal year 2026" vs "paid $244
  million during the second quarter" → NOT a discrepancy (the quarter is a
  component of the year).

**When in doubt about the time period, DO NOT flag. Err on the side of caution.**

## FORECAST vs ACTUAL — DO NOT FLAG TENSE DIFFERENCES AS CONTRADICTIONS
If one document uses FUTURE/forward-looking tense (expect, anticipate, plan, intend,
will, project, guidance, outlook) and another uses PAST/actual tense (experienced,
resulted, was, incurred, reported), and BOTH refer to the SAME event or metric, this
is NOT a contradiction — it is a forecast later being confirmed or reported as actuals.
Example: a 10-Q saying "we expect lower production" and a later 10-K saying "we
experienced lower production" describe the SAME outcome at two points in time. DO NOT
flag forecast-vs-actual pairs as discrepancies.

## IGNORE SHARED BOILERPLATE
Many passages open with identical generic phrasing ("Subject to...", "We may...",
"As a result,...", "Forward-looking statements...", "The Company..."). A shared
opener is NOT a discrepancy. Look PAST identical boilerplate openers and compare
only the substantive claims, figures, and facts that follow. If the only thing two
passages share is generic phrasing, return no discrepancy for that pair.

## TERMINOLOGY IS NOT A DISCREPANCY
Terminology clarifications (using different words to describe the same concept)
are NOT discrepancies. Only flag when the underlying facts or figures differ.
Examples that must NOT be flagged:
- "share purchases" vs "share repurchases" (same action)
- "non-marketable securities" vs "privately-held securities" (same asset class)
- an abbreviation vs its spelled-out form ("AIBs" vs "add-in board manufacturers")
- two passages quoting the IDENTICAL text (identical excerpts are never a discrepancy)

## OMISSIONS: DISCLOSURE EVOLUTION IS NOT A DISCREPANCY
Filings evolve. A LATER filing ADDING a new product, risk, customer category, or
detail that an earlier filing lacked is normal disclosure evolution — NOT an
omission_discrepancy. 10-Q risk-factor and MD&A sections are INCREMENTAL updates
to the 10-K by SEC rule, so a topic present in the 10-K but summarized or absent
in a 10-Q is NOT an omission either. Flag omission_discrepancy ONLY when:
- a LATER filing silently DROPS a material fact an EARLIER filing disclosed, and
  the drop changes what a careful reader would conclude; or
- two documents covering the SAME period disclose materially different facts.

## Discrepancy Types
"numerical_contradiction" — Same metric with different values (weight: 100)
"narrative_contradiction" — Statements that cannot both be true (weight: 80)
"omission_discrepancy" — Material claim in one doc, absent in the other (weight: 60)
"qualitative_tension" — Tonal or degree mismatch (weight: 40)

## Severity
"high" — Financial figures, revenue, guidance, material events
"medium" — Core business claim, requires interpretation
"low" — Tonal, stylistic, peripheral

## Output Format
Return ONLY a JSON array. Omit pairs with no discrepancy. If none, return [].

[{"pair":"10-K_vs_10-Q","is_discrepancy":true,"discrepancy_type":"numerical_contradiction","severity":"high","weight":100,"reasoning":"<one full sentence naming the specific conflicting facts or figures>","sentence_index_a":0,"sentence_index_b":0,"quoted_phrase_a":"...","quoted_phrase_b":"..."}]

CRITICAL: reasoning must be a complete, specific sentence (never the literal word "short"). sentence_index_a/b must match the [N] label. quoted_phrase must be exact text. Temperature is zero."""
    return prompt

def _parse_discrepancy_array(text):
    """Parse LLM response text into a list of discrepancy dicts via the shared
    tolerant extractor (fences, preamble, trailing commas, single quotes,
    comments). Returns [] on failure."""
    if not text or not isinstance(text, str):
        return []
    try:
        from src.utils.json_extractor import extract_json
        data = extract_json(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = [data]
    return data if isinstance(data, list) else []


def _call_llm(prompt, provider_idx=None, max_tokens=400):
    """Call LLM using shared config — delegates to llm_config.call_llm.

    PART 4c: default start tier is Groq-70B (this is the quality-sensitive
    classify step); callers may still pass an explicit provider_idx.
    requires_strict_json: classify output is a JSON array with exact sentence
    indices — jumbo batches (>4k words) promote to Fable 5 via the router."""
    if provider_idx is None:
        provider_idx = start_tier("corporate_discrepancy")
    try:
        return call_llm(prompt, max_tokens=max_tokens, provider_idx=provider_idx,
                        requires_strict_json=True)
    except Exception:
        return "", {"cached": False, "provider": "failed"}

# Known financial synonym/abbreviation pairs (Part 3b, 2026-07-01). Each group's
# members are interchangeable wording for the SAME concept; a "discrepancy" whose
# two excerpts differ only by one of these is a terminology clarification, not a
# contradiction. Phrases are matched on normalized (lowercase, single-space) text.
FINANCIAL_SYNONYMS = [
    ("share repurchases", ["share purchases", "share repurchases", "repurchases of shares",
                           "purchases of shares"]),
    ("non-marketable", ["non-marketable", "nonmarketable", "privately-held", "privately held"]),
    ("add-in board manufacturers", ["aibs", "add-in board manufacturers",
                                    "add-in board partners", "add-in board makers"]),
]


def _canonicalize_excerpt(s):
    """Normalize an excerpt for identical/synonym comparison: lowercase, collapse
    whitespace, strip punctuation, then map every known synonym variant to its
    canonical phrase so synonym-only differences compare equal."""
    t = re.sub(r"\s+", " ", (s or "")).strip().lower()
    for canonical, variants in FINANCIAL_SYNONYMS:
        for v in sorted(variants, key=len, reverse=True):
            t = t.replace(v, canonical)
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _validate_llm_response(response, batch):
    """Validate LLM response: minimal checks, trust LLM sentence indices.
    Only rejects entries where quoted phrases are completely absent from the passage."""
    valid = []
    for entry in response:
        if not isinstance(entry, dict) or not entry.get("is_discrepancy"):
            continue
        pair = entry.get("pair", "")
        parts = pair.split("_vs_")
        if len(parts) != 2:
            continue
        
        qa = entry.get("quoted_phrase_a", "")
        qb = entry.get("quoted_phrase_b", "")
        
        # Minimal check: at least some key words from quoted phrase appear in the passage
        doc_a, doc_b = parts
        passages = batch.get("passages", {})
        # Prefix match for doc labels (e.g. '10-Q' -> '10-Q_0')
        actual_a = doc_a
        if actual_a not in passages:
            for pk in passages:
                if pk == doc_a or pk.startswith(doc_a + '_'):
                    actual_a = pk
                    break
        actual_b = doc_b
        if actual_b not in passages:
            for pk in passages:
                if pk == doc_b or pk.startswith(doc_b + '_'):
                    actual_b = pk
                    break
        pa = passages.get(actual_a, {})
        pb = passages.get(actual_b, {})
        text_a = pa.get("text", "") if isinstance(pa, dict) else ""
        text_b = pb.get("text", "") if isinstance(pb, dict) else ""
        
        # Check that at least 2 significant words (>3 chars) from each quote appear in the text
        def words_in_text(phrase, text):
            if not phrase:
                return True
            words = [w for w in phrase.split() if len(w) > 3]
            if not words:
                return True
            matches = sum(1 for w in words if w.lower() in text.lower())
            return matches >= min(2, len(words))
        
        if qa and not words_in_text(qa, text_a):
            continue
        if qb and not words_in_text(qb, text_b):
            continue
        
        # Map each quoted phrase to the sentence that ACTUALLY contains it.
        # The previous logic keyed on only the FIRST WORD of the quote (e.g. "The",
        # "We", "During") and took the first hit, which routinely cited the wrong
        # sentence. Match the FULL quote instead, and only override the LLM's index
        # when a confident match is found; otherwise keep what the LLM returned.
        sents_a = pa.get("sentences", []) if isinstance(pa, dict) else []
        sents_b = pb.get("sentences", []) if isinstance(pb, dict) else []

        def _norm(s):
            return re.sub(r"\s+", " ", s or "").strip().lower()

        def _locate_sentence(quote, sents, fallback):
            """Return (index, grounded): the sentence that actually contains the
            quote, and whether it was confidently located (vs. falling back to the
            LLM-provided index)."""
            if not quote or not sents:
                return fallback, False
            nq = _norm(quote)
            if not nq:
                return fallback, False
            # 1) Strongest signal: the quote is contained in a sentence (or, for a
            #    long quote spanning sentences, the sentence is contained in it).
            for i, s in enumerate(sents):
                ns = _norm(s)
                if ns and (nq in ns or (len(nq) > 40 and ns in nq)):
                    return i, True
            # 2) Otherwise pick the sentence sharing the most significant words.
            q_words = {w for w in re.findall(r"[a-z0-9]+", nq) if len(w) > 3}
            if not q_words:
                return fallback, False
            best_i, best_overlap = fallback, 0
            for i, s in enumerate(sents):
                s_words = {w for w in re.findall(r"[a-z0-9]+", _norm(s)) if len(w) > 3}
                overlap = len(q_words & s_words)
                if overlap > best_overlap:
                    best_i, best_overlap = i, overlap
            # Require a meaningful overlap before trusting the location.
            if best_overlap >= max(2, (len(q_words) + 1) // 2):
                return best_i, True
            return fallback, False

        idx_a, grounded_a = _locate_sentence(qa, sents_a, entry.get("sentence_index_a") or 0)
        idx_b, grounded_b = _locate_sentence(qb, sents_b, entry.get("sentence_index_b") or 0)

        # Reject discrepancies whose SUBSTANTIVE quote cannot be grounded in the cited
        # passage's own sentences. These are misaligned/hallucinated claims — e.g. a
        # "purchase compliance credits from third parties" quote cited against a
        # vehicle-depreciation chunk — for which no correct excerpt exists. Empty
        # quotes (the absent side of an omission_discrepancy) are exempt.
        def _substantive(q):
            return len({w for w in re.findall(r"[a-z0-9]+", _norm(q)) if len(w) > 3}) >= 3
        if (qa and _substantive(qa) and not grounded_a) or (qb and _substantive(qb) and not grounded_b):
            continue

        # Drop spurious numerical_contradictions whose "conflicting figures" are bare
        # reference/footnote integers (e.g. "53" vs "39") carrying no magnitude — an
        # analyst would dismiss these instantly.
        if entry.get("discrepancy_type") == "numerical_contradiction":
            def _bare_number(q):
                return bool(re.fullmatch(r"\d{1,4}", re.sub(r"[^0-9a-z]", "", _norm(q))))
            if _bare_number(qa) or _bare_number(qb):
                continue

        # ── Identical-excerpt + synonym filters (Part 3b, 2026-07-01) ──────────
        # 1) If the two quoted excerpts are character-identical after whitespace/
        #    punctuation normalization, this is a citation artifact, not a
        #    discrepancy ("certificates of deposit" appearing verbatim in both).
        # 2) If they differ ONLY by a known financial synonym/abbreviation pair
        #    ("share purchases" vs "share repurchases", "AIBs" vs "add-in board
        #    manufacturers"), it is a terminology clarification — skip.
        # 3) Same canonical WORD SET (reordering only) → no factual difference.
        if qa and qb:
            ca, cb = _canonicalize_excerpt(qa), _canonicalize_excerpt(qb)
            if ca == cb or set(ca.split()) == set(cb.split()):
                continue
            # If the located source sentences themselves are the identical text,
            # the "conflict" lives inside one shared sentence — impossible.
            try:
                sa = _norm(sents_a[idx_a]) if sents_a else ""
                sb = _norm(sents_b[idx_b]) if sents_b else ""
                if sa and sa == sb:
                    continue
            except (IndexError, TypeError):
                pass

        # ── Cross-period false-positive guards (Part 2 rolling window,
        #    2026-07-01). The rolling window pairs a 10-K with 10-Qs from other
        #    quarters far more often than the single-year design did, and free-
        #    tier classifiers leak period-mismatch flags the prompt warns about.
        #    These guards are DETERMINISTIC (model-independent):
        #    1) "as of <date A>" vs "as of <date B>" — a point-in-time snapshot
        #       (buyback authorization remaining, cash balance, headcount)
        #       naturally changes between dates; two different as-of dates are
        #       never a contradiction.
        #    2) numerical_contradiction where exactly ONE side is sub-annual
        #       (quarter/half/nine-months) — annual totals vs period components
        #       are incomparable windows ($974M FY dividends vs $244M Q2).
        #    3) table fragments — sentences that are mostly figures are table
        #       rows, not claims; comparing two of them is noise.
        def _sent_or_quote(sents, idx, q):
            try:
                s = sents[idx] if sents else ""
            except (IndexError, TypeError):
                s = ""
            return s or (q or "")

        text_a = _sent_or_quote(sents_a, idx_a, qa)
        text_b = _sent_or_quote(sents_b, idx_b, qb)
        m_a, m_b = _AS_OF_DATE_RE.search(text_a), _AS_OF_DATE_RE.search(text_b)
        if m_a and m_b and _norm(m_a.group(1)) != _norm(m_b.group(1)):
            continue
        if entry.get("discrepancy_type") == "numerical_contradiction":
            sub_a = bool(_SUBANNUAL_RE.search(text_a))
            sub_b = bool(_SUBANNUAL_RE.search(text_b))
            if sub_a != sub_b:
                continue
        if _is_table_fragment(text_a) or _is_table_fragment(text_b):
            continue

        entry["sentence_index_a"] = idx_a
        entry["sentence_index_b"] = idx_b

        # WS2: guarantee a substantive reasoning string. The LLM sometimes echoes
        # the example placeholder ("short") or returns an empty/truncated value.
        reasoning = (entry.get("reasoning") or "").strip()
        if len(reasoning) < 20 or reasoning.lower() in ("short", "n/a", "none", "...", "tbd"):
            dtype = entry.get("discrepancy_type", "discrepancy").replace("_", " ")
            entry["reasoning"] = (
                'Potential %s between %s and %s: "%s" vs "%s".'
                % (dtype, doc_a, doc_b, (qa or "")[:120], (qb or "")[:120])
            ).strip()

        valid.append(entry)
    return valid

def classify_discrepancies(documents):
    """Build topic batches from aligned pairs and classify with LLM in PARALLEL."""
    aligned = documents.get("aligned_pairs", [])
    if not aligned:
        documents["discrepancies"] = []
        return documents
    
    # Group by 10-K anchor chunk
    batches = {}
    for pair in aligned:
        anchor = pair["chunk_a"]
        key = anchor["chunk_id"]
        if key not in batches:
            section = anchor["metadata"].get("section", "")
            text_preview = anchor["text"][:120]
            batches[key] = {
                "topic": "%s - %s" % (section, text_preview),
                "anchor": anchor,
                "passages": {anchor["metadata"]["source_doc_type"]: anchor}
            }
        other_doc = pair["chunk_b"]
        other_type = other_doc["metadata"]["source_doc_type"]
        if other_type not in batches[key]["passages"]:
            batches[key]["passages"][other_type] = other_doc
        batches[key]["tier"] = pair.get("tier", 2)
        batches[key]["similarity"] = pair.get("similarity", 0.86)
    
    # Filter to batches with >= 2 passages
    # valid_batches already computed above with boilerplate filter
    
    # Parallel LLM calls with ThreadPoolExecutor
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    # Pre-filter: skip batches where all passages are near-identical boilerplate
    filtered_batches = {}
    for key, batch in batches.items():
        texts = []
        for p in batch.get("passages", {}).values():
            t = p.get("text", "")[:200].strip()
            # Skip boilerplate-only passages
            boilerplate_patterns = [
                "incorporated herein by reference", "press release", "furnished herewith",
                "shall not be deemed filed", "forward-looking statements", "safe harbor"
            ]
            is_boilerplate = all(pat in t.lower() for pat in boilerplate_patterns[:2])
            if not is_boilerplate:
                texts.append(t)
        if len(set(texts)) >= 2:
            filtered_batches[key] = batch
    
    valid_batches = [(k, b) for k, b in filtered_batches.items() if len(b["passages"]) >= 2]
    print(f"Batches after boilerplate filter: {len(valid_batches)} (filtered out {len(batches) - len(filtered_batches)})")
    
    def process_batch(key, batch):
        prompt = _build_discrepancy_prompt(batch)
        response_text, _meta = _call_llm(prompt)
        parsed = _parse_discrepancy_array(response_text)
        valid = _validate_llm_response(parsed, batch)
        for v in valid:
            v["batch_key"] = key
            # Store minimal batch info without embeddings
            v["batch"] = {"topic": batch.get("topic",""), "passages_count": len(batch.get("passages",{})), "chunk_ids": {dt: p.get("chunk_id","") for dt, p in batch.get("passages", {}).items()}}
        return valid
    
    all_discrepancies = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(process_batch, k, b): k for k, b in valid_batches}
        for future in as_completed(futures):
            all_discrepancies.extend(future.result())
    
    documents["discrepancies"] = all_discrepancies
    documents["total_batches"] = len(valid_batches)
    return documents

def rank_discrepancies(documents):
    """Score and rank all discrepancies using weighted linear sum (0-100 scale).
    
    Components:
    - Severity: high=90, medium=50, low=15
    - Type objectivity: numerical=100, narrative=70, omission=50, qualitative=30
    - Pair importance: tier1=100, tier2=70, tier3=40
    - Confidence: similarity × 100
    
    Formula: 0.35×Severity + 0.30×Type + 0.15×Pair + 0.20×Confidence
    """
    severity_map = {"high": 90, "medium": 50, "low": 15}
    type_map = {"numerical_contradiction": 100, "narrative_contradiction": 70, 
                "omission_discrepancy": 50, "qualitative_tension": 30}
    tier_map = {1: 100, 2: 70, 3: 40}
    
    discrepancies = documents.get("discrepancies", [])
    if not discrepancies:
        documents["top_discrepancies"] = []
        return documents

    # WS2: build a chunk lookup so we can detect "same-filing" discrepancies
    # (both sides resolve to the same SEC accession) — almost always a false
    # positive — and penalize them before the top-5 selection.
    _chunk_lookup = {}
    for _key in ["chunks_10k", "chunks_10q", "chunks_def14a", "chunks_8k", "chunks_transcript"]:
        for _chunk in documents.get(_key, []):
            _chunk_lookup[_chunk["chunk_id"]] = _chunk

    def _accession_for(disc, doc_label):
        chunk_ids = disc.get("batch", {}).get("chunk_ids", {})
        cid = chunk_ids.get(doc_label, "")
        if not cid:
            for ck in chunk_ids:
                if ck == doc_label or ck.startswith(doc_label + "_"):
                    cid = chunk_ids[ck]
                    break
        return (_chunk_lookup.get(cid, {}).get("metadata", {}) or {}).get("accession_number", "")

    for d in discrepancies:
        sev = severity_map.get(d.get("severity", "low"), 15)
        typ = type_map.get(d.get("discrepancy_type", "qualitative_tension"), 30)

        tier = 2
        similarity = 0.86
        for pair in documents.get("aligned_pairs", []):
            if pair["chunk_a"]["chunk_id"] == d.get("batch_key"):
                tier = pair.get("tier", 2)
                similarity = pair.get("similarity", 0.86)
                break
        pair_score = tier_map.get(tier, 70)
        confidence = similarity * 100

        # Same-filing detection
        parts = d.get("pair", "").split("_vs_")
        same_filing = False
        if len(parts) == 2:
            acc_a = _accession_for(d, parts[0])
            acc_b = _accession_for(d, parts[1])
            same_filing = bool(acc_a) and acc_a == acc_b
        d["same_filing"] = same_filing

        score = 0.35 * sev + 0.30 * typ + 0.15 * pair_score + 0.20 * confidence
        if same_filing:
            score *= 0.4  # de-prioritize: both quotes come from the same document
            d["severity"] = {"high": "medium", "medium": "low", "low": "low"}.get(d.get("severity", "low"), "low")
        d["final_score"] = round(score, 1)
        d["score_components"] = {
            "severity": sev, "type_objectivity": typ,
            "pair_importance": pair_score, "confidence": round(confidence, 1),
            "same_filing": same_filing,
        }

    ranked = sorted(discrepancies, key=lambda x: x.get("final_score", 0), reverse=True)
    # Output EVERY discrepancy found (ranked by final_score), not just the top 5.
    for i, d in enumerate(ranked):
        d["rank"] = i + 1
    documents["top_discrepancies"] = ranked
    return documents

def build_citations(documents):
    """Build exact sentence citations for each top discrepancy with EDGAR URLs."""
    ticker = documents.get("ticker", "")
    year = documents.get("year", "")
    cik = documents.get("cik", "")
    
    # Build lookup: chunk_id -> chunk (with sentences + metadata)
    all_chunks = {}
    for key in ["chunks_10k", "chunks_10q", "chunks_def14a", "chunks_8k", "chunks_transcript"]:
        for chunk in documents.get(key, []):
            all_chunks[chunk["chunk_id"]] = chunk
    
    for d in documents.get("top_discrepancies", []):
        batch = d.get("batch", {})
        chunk_ids = batch.get("chunk_ids", {})
        pair_name = d.get("pair", "")
        parts = pair_name.split("_vs_")
        
        for side, doc_label in enumerate(parts):
            chunk_id = chunk_ids.get(doc_label, "")
            # If exact match fails, try prefix match (e.g. '10-Q' matching '10-Q_2')
            if not chunk_id:
                for ck in chunk_ids:
                    if ck.startswith(doc_label + '_') or ck == doc_label:
                        chunk_id = chunk_ids[ck]
                        break
            chunk = all_chunks.get(chunk_id, {})
            
            if chunk:
                idx = d.get("sentence_index_a" if side == 0 else "sentence_index_b") or 0
                sents = chunk.get("sentences", [])
                meta = chunk.get("metadata", {})
                doc_type = meta.get("source_doc_type", doc_label)
                clean_type = doc_type
                if "EARNINGS_CALL" in clean_type:
                    clean_type = "8-K"
                cik_int = int(cik) if cik else 0
                # Build direct .htm URL using CIK + accession
                # Accession format: 0001045810-23-000017 -> 000104581023000017
                acc = meta.get("accession_number", "")
                doc = meta.get("primary_document", "")
                cik_stripped = str(cik_int)
                acc_no_dashes = acc.replace("-", "")
                edgar_url = f"https://www.sec.gov/Archives/edgar/data/{cik_stripped}/{acc_no_dashes}/{doc}" if cik and acc and doc else ""
                source = {
                    "document_type": _display_doc_type(doc_type, year),
                    "document_type_raw": doc_type,
                    "section": meta.get("section", ""),
                    "page_number": meta.get("page_number"),  # printed page or None — never fabricated
                    "paragraph_index": meta.get("paragraph_index", 0),
                    "sentence_index": idx,
                    "sentence_text": sents[idx] if idx < len(sents) else "",
                    "edgar_url": edgar_url,
            "htm_link": edgar_url,
                    "year": year,
                    "ticker": ticker
                }
            else:
                source = {"document_type": _display_doc_type(doc_label, year), "document_type_raw": doc_label, "sentence_text": "", "edgar_url": "", "year": year, "ticker": ticker}

            if side == 0:
                d["source_a"] = source
            else:
                d["source_b"] = source

        # Clean, human-readable pair label for the UI (e.g. "10-K vs 10-Q (Q3 2025)")
        sa = (d.get("source_a") or {}).get("document_type", "")
        sb = (d.get("source_b") or {}).get("document_type", "")
        if sa and sb:
            d["pair_display"] = "%s vs %s" % (sa, sb)
        d.pop("batch", None)
    
    return documents


# ===

# ============================================================================
# LAYER 7: Output + Main run() function
# ============================================================================

# PART 2 (2026-07-01): rolling analysis window size. The window is the last N
# COMPLETE fiscal years plus the current partial year; there is no year
# selector any more. Overridable for ops/testing.
AGENT1_WINDOW_YEARS = int(os.environ.get("SOLLIA_AGENT1_WINDOW_YEARS", "5"))


def _audit_high_severity(findings: List[dict]) -> None:
    """Second-pass severity audit (2026-07-01 cost/quality balance): the volume
    classify runs on free tiers, which measurably over-assign "high" (e.g.
    terminology drift rated high). This ONE short paid-tier call reviews only
    the high-severity candidates and confirms/demotes each — judgment where it
    matters, at ~$0.02/run instead of ~$1.80 for full paid classification.
    Mutates findings in place; any failure leaves severities unchanged."""
    highs = [d for d in findings if str(d.get("severity", "")).lower() == "high"][:12]
    if not highs:
        return
    lines = []
    for i, d in enumerate(highs):
        qa = ((d.get("source_a") or {}).get("sentence_text") or d.get("quoted_phrase_a") or "")[:220]
        qb = ((d.get("source_b") or {}).get("sentence_text") or d.get("quoted_phrase_b") or "")[:220]
        lines.append('%d. [%s vs %s] reasoning: %s\n   A: "%s"\n   B: "%s"' % (
            i, (d.get("source_a") or {}).get("document_type", "?"),
            (d.get("source_b") or {}).get("document_type", "?"),
            str(d.get("reasoning", ""))[:200], qa, qb))
    prompt = (
        "You are the senior reviewer auditing HIGH-severity flags raised by a junior "
        "analyst comparing SEC filings of the same company.\n\n"
        "Severity rules:\n"
        '- "high" ONLY for conflicting financial figures, guidance, or material events '
        "that cannot both be true for the same period.\n"
        '- Terminology changes, disclosure evolution (a later filing renaming or adding '
        'a category), exhibit-numbering or citation artifacts, and tonal shifts are '
        '"low" or "not_a_discrepancy".\n'
        '- Differences that need interpretation but describe a real factual tension are "medium".\n\n'
        "Candidates:\n" + "\n".join(lines) +
        '\n\nReturn ONLY a JSON array, one entry per candidate, no other text:\n'
        '[{"id":0,"severity":"high|medium|low|not_a_discrepancy"}]')
    try:
        text, _meta = call_llm(prompt, max_tokens=300, temperature=0.0,
                               provider_idx=0, pin_tier=True,
                               agent="corporate_discrepancy", stage="severity_audit")
        from src.utils.json_extractor import extract_json
        verdicts = extract_json(text)
        if isinstance(verdicts, dict):
            verdicts = [verdicts]
        drop_ids = set()
        for v in verdicts:
            i = v.get("id")
            sev = str(v.get("severity", "")).lower()
            if not isinstance(i, int) or not (0 <= i < len(highs)):
                continue
            if sev == "not_a_discrepancy":
                drop_ids.add(id(highs[i]))
            elif sev in ("high", "medium", "low"):
                highs[i]["severity"] = sev
                highs[i]["severity_audited"] = True
        if drop_ids:
            findings[:] = [d for d in findings if id(d) not in drop_ids]
    except Exception:
        pass  # audit is best-effort; unreviewed severities stand


def _prepare_embedder(documents: dict):
    """Return (embed_fn, error). Mirrors the FinBERT-unavailable degradation:
    a warm run proceeds from the embedding cache; only abort when a chunk
    actually needs a fresh embedding."""
    embed_fn = get_embedder()
    if embed_fn is not None:
        return embed_fn, None
    _all_chunks = (documents.get("chunks_10k", []) + documents.get("chunks_10q", []) +
                   documents.get("chunks_def14a", []) + documents.get("chunks_8k", []) +
                   documents.get("chunks_transcript", []))
    _uncached = [c for c in _all_chunks
                 if c.get("embedding") is None and _get_cached_embedding(c["text"]) is None]
    if _uncached:
        return None, ("FinBERT not available (transformers + torch). %d of %d chunks "
                      "are uncached and cannot be embedded. Install with: "
                      "pip install transformers torch" % (len(_uncached), len(_all_chunks)))

    def _stub(_text):
        raise RuntimeError("embed_fn called despite full cache coverage")
    return _stub, None


def _analyze_year_set(documents: dict, add_log, _emit) -> Tuple[Optional[dict], Optional[str]]:
    """Run layers 0.5-6 (clean -> cite) on ONE aligned+fetched year set."""
    yr = documents.get("year")
    documents = clean_all(documents)
    documents = chunk_all(documents)
    n_chunks = sum(len(documents.get(k, [])) for k in (
        "chunks_10k", "chunks_10q", "chunks_def14a", "chunks_8k", "chunks_transcript"))
    add_log("CHUNK", "FY%s: %d chunks" % (yr, n_chunks))

    embed_fn, emb_err = _prepare_embedder(documents)
    if emb_err:
        return None, emb_err
    documents = embed_all_chunks(documents, embed_fn)
    documents = align_all_pairs(documents)
    n_pairs = len(documents.get("aligned_pairs", []))
    add_log("ALIGN", "FY%s: %d aligned pairs" % (yr, n_pairs))

    documents = classify_discrepancies(documents)
    add_log("CLASSIFY", "FY%s: LLM found %d discrepancies" % (yr, len(documents.get("discrepancies", []))))
    documents = rank_discrepancies(documents)
    documents = build_citations(documents)
    return documents, None


def run_discrepancy_agent(ticker: str, year: Optional[int] = None, progress=None) -> dict:
    """Run the full Corporate Discrepancy agent over the ROLLING 5-YEAR WINDOW.

    PART 2 (2026-07-01): the `year` parameter is DEPRECATED and ignored — the
    agent now automatically analyzes the last AGENT1_WINDOW_YEARS complete
    fiscal years plus the current partial year, aligning documents by date
    proximity within each fiscal year and sorting findings most-recent-first.

    progress: optional callback(message:str, kind:str) for live, human-readable
    milestone updates (WS5). Never exposes internal pipeline mechanics."""
    import datetime, time
    t0 = time.time()
    log = []

    def add_log(step, detail):
        log.append({"timestamp": datetime.datetime.utcnow().isoformat(), "step": step, "detail": detail})

    def _emit(msg, kind="info"):
        if progress:
            try:
                progress(msg, kind)
            except Exception:
                pass

    if year is not None:
        add_log("NOTE", "year=%s ignored — Agent 1 now uses the rolling %d-year window"
                % (year, AGENT1_WINDOW_YEARS))
    add_log("START", "Agent 1 initiated for %s (rolling %d-year window)" % (ticker, AGENT1_WINDOW_YEARS))
    _emit("Building the rolling filing window for %s (last %d fiscal years + current)..."
          % (ticker, AGENT1_WINDOW_YEARS), "search")

    # Layer 0: rolling window (newest first: [partial?, anchor, older...])
    fetcher = SECFilingFetcher()
    window, error = fetcher.build_rolling_window(ticker, max_years=AGENT1_WINDOW_YEARS)
    if error:
        _emit("Could not assemble the filing window: %s" % error, "flag")
        return {"ticker": ticker, "year": year, "error": error, "agent_log": log}
    _yrs = ["FY%s%s" % (w["year"], " (partial)" if w.get("partial") else "") for w in window]
    add_log("WINDOW", "Window: %s" % ", ".join(_yrs))
    _emit("Analyzing %s" % ", ".join(_yrs), "fetch")

    merged_top: List[dict] = []
    total_pairs = 0
    total_found = 0
    analyzed_years: List[dict] = []
    first_error: Optional[str] = None
    complete_seen = 0  # recency weighting: partial+anchor = 1.0, halving per older year

    for gi, year_set in enumerate(window):
        yr = year_set["year"]
        label = "FY%s%s" % (yr, " (current, partial)" if year_set.get("partial") else "")
        _emit("[%d/%d] Pulling and cross-examining %s filings..." % (gi + 1, len(window), label),
              "search")
        documents, err = fetch_texts_for_set(year_set, fetcher=fetcher)
        if err is None:
            documents, err = _analyze_year_set(documents, add_log, _emit)
        if err:
            add_log("SKIP", "%s skipped: %s" % (label, err))
            _emit("%s skipped: %s" % (label, err), "flag")
            first_error = first_error or err
            continue
        if year_set.get("partial"):
            recency_weight = 1.0
        else:
            recency_weight = 0.5 ** complete_seen
            complete_seen += 1
        year_top = documents.get("top_discrepancies", [])
        for d in year_top:
            d["fiscal_year"] = yr
            d["fiscal_period"] = label
            d["recency_weight"] = recency_weight  # read by score_corporate_discrepancy
            d["_recency"] = len(window) - gi  # window is newest-first
        merged_top.extend(year_top)
        total_pairs += len(documents.get("aligned_pairs", []))
        total_found += len(documents.get("discrepancies", []))
        analyzed_years.append({"fiscal_year": yr, "partial": bool(year_set.get("partial")),
                               "discrepancies": len(year_top),
                               "aligned_pairs": len(documents.get("aligned_pairs", []))})
        _emit("%s: %d discrepancies across %d aligned passages"
              % (label, len(year_top), len(documents.get("aligned_pairs", []))), "info")

    if not analyzed_years:
        error = first_error or "No fiscal year in the window could be analyzed"
        _emit("Could not analyze any fiscal year: %s" % error, "flag")
        return {"ticker": ticker, "year": year, "error": error, "agent_log": log}

    # Severity audit: one short paid-tier judgment call reviews the "high"
    # candidates (free-tier classify over-assigns high) — see _audit_high_severity.
    _audit_high_severity(merged_top)

    # Sort findings by RECENCY (most recent fiscal period first), then severity
    # rank within the period. Re-rank globally.
    merged_top.sort(key=lambda d: (-d.get("_recency", 0), -d.get("final_score", 0)))
    for i, d in enumerate(merged_top):
        d["rank"] = i + 1
        d.pop("_recency", None)

    anchor_year = next((a["fiscal_year"] for a in analyzed_years if not a["partial"]),
                       analyzed_years[0]["fiscal_year"])
    elapsed = time.time() - t0
    add_log("OUTPUT", "Complete in %.1fs — %d findings across %d fiscal periods"
            % (elapsed, len(merged_top), len(analyzed_years)))
    _emit("Corporate discrepancy scan complete: %d findings across %d fiscal periods (%.0fs)"
          % (len(merged_top), len(analyzed_years), elapsed), "done")

    # Scoring: same severity-gated formula, now over the merged window findings.
    _score, _signal, _rationale = score_corporate_discrepancy(merged_top)

    _result = {
        "ticker": ticker, "year": anchor_year,
        "agent": "Corporate Discrepancy",
        "run_timestamp": datetime.datetime.utcnow().isoformat(),
        "documents_analyzed": ["10-K", "10-Q", "8-K", "DEF14A", "EARNINGS_CALL"],
        "window": {"years": [a["fiscal_year"] for a in analyzed_years],
                   "includes_partial_year": any(a["partial"] for a in analyzed_years),
                   "per_year": analyzed_years,
                   "note": "Analyzing last %d fiscal years + current partial year"
                           % AGENT1_WINDOW_YEARS},
        "total_aligned_pairs": total_pairs,
        "total_discrepancies_found": total_found,
        "top_discrepancies": merged_top,
        "score": _score,
        "signal": _signal,
        "score_rationale": _rationale,
        "elapsed_seconds": round(elapsed, 1),
        "agent_log": log,
    }
    # PART 3: archive this run (manifest + index line). Exception-safe — never
    # let archiving break a run. Embeddings are NOT copied; they stay in the
    # content-addressed .cache/embeddings store the manifest references.
    try:
        from src.utils.run_archive import save_run
        save_run("corporate_discrepancy", ticker, anchor_year, _result, duration_ms=elapsed * 1000)
    except Exception:
        pass
    return _result
