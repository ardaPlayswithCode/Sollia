"""Transcript provider — fallback chain: SEC 8-K → StockAnalysis → Nasdaq → Yahoo Finance."""
from __future__ import annotations

import re
import time
import logging
from datetime import datetime
from typing import Dict, List, Optional

import requests

from src.schemas import TranscriptResult

logger = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}
_SEC_HEADERS = {
    "User-Agent": "Sollia Analysis contact@sollia.app",
    "Accept-Encoding": "gzip, deflate",
}

# Expected reporting month per fiscal quarter (Q1=May, Q2=Aug, Q3=Nov, Q4=Feb of next year)
_QUARTER_REPORT_MONTHS: Dict[int, int] = {1: 5, 2: 8, 3: 11, 4: 2}


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_quarter_year(text: str) -> tuple:
    """Return (quarter: int, year: int) from 'Q4 2026'-style text, else (0, 0)."""
    m = re.search(r"Q(\d)\s+(\d{4})", text, re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d{4})\s+Q(\d)", text, re.IGNORECASE)
    if m:
        return int(m.group(2)), int(m.group(1))
    return 0, 0


def _date_ok(date_str: str, quarter: int, year: int) -> bool:
    """True if listing date is within ±90 days of expected reporting month."""
    if not (date_str and quarter and year):
        return True
    expected_month = _QUARTER_REPORT_MONTHS.get(quarter, 0)
    if not expected_month:
        return True
    exp_year = year + 1 if quarter == 4 else year
    try:
        expected = datetime(exp_year, expected_month, 15)
        actual = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return abs((actual - expected).days) <= 90
    except Exception:
        return True


def _extract_speakers(text: str) -> List[Dict[str, str]]:
    sections: List[Dict[str, str]] = []
    for m in re.finditer(
        r"([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){1,3})\s*:\s*(.{50,500})",
        text[:8000],
        re.DOTALL,
    ):
        sections.append({"speaker": m.group(1).strip(), "text": m.group(2).strip()[:400]})
    return sections


class FMPTranscriptProvider:
    """
    Earnings call transcript provider.
    Chain: SEC EDGAR 8-K → StockAnalysis → Nasdaq → Yahoo Finance
    """

    def __init__(self) -> None:
        pass

    def get_transcripts(self, ticker: str) -> List[TranscriptResult]:
        """Return list of TranscriptResult, newest first. Empty list triggers MD&A fallback."""
        ticker = ticker.upper()
        
        # Check disk cache first
        import json as _json, os as _os
        cache_dir = _os.path.join(_os.path.dirname(__file__), '..', '..', '.transcript_cache')
        _os.makedirs(cache_dir, exist_ok=True)
        cache_file = _os.path.join(cache_dir, '%s.json' % ticker)
        
        try:
            if _os.path.exists(cache_file):
                cache_age = _os.path.getmtime(cache_file)
                if _os.path.getmtime(cache_file) > _os.path.getmtime(__file__):
                    with open(cache_file) as f:
                        data = _json.load(f)
                    results = [TranscriptResult(**item) for item in data]
                    if results:
                        logger.info("[TRANSCRIPT] %d transcripts from cache", len(results))
                        return results
        except Exception:
            pass
        
        for method in (self._sec_8k, self._stock_analysis, self._nasdaq, self._yahoo_finance):
            try:
                results = method(ticker)
                if results:
                    logger.info("[TRANSCRIPT] %d transcripts from %s", len(results), method.__name__)
                    # Save to cache
                    try:
                        with open(cache_file, 'w') as f:
                            _json.dump([r.dict() for r in results], f, default=str)
                    except Exception:
                        pass
                    return results
            except Exception as exc:
                logger.debug("[TRANSCRIPT] %s failed: %s", method.__name__, exc)
        return []

    # ── Source 1: SEC EDGAR 8-K ───────────────────────────────────────────────

    def _sec_8k(self, ticker: str) -> List[TranscriptResult]:
        """Scan 8-K filings for earnings call transcript exhibits."""
        from src.providers.filings.sec_edgar import SECEdgarProvider
        edgar = SECEdgarProvider()
        cik = edgar.get_cik(ticker)
        if not cik:
            return []

        resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers=_SEC_HEADERS,
            timeout=15,
        )
        if resp.status_code != 200:
            return []

        recent = resp.json().get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accs = recent.get("accessionNumber", [])

        results: List[TranscriptResult] = []
        checked = 0

        for i, form in enumerate(forms):
            if checked >= 120:  # no hard count cap, rely on date filter below
                break
            if form != "8-K":
                continue
            checked += 1

            acc_dash = accs[i] if i < len(accs) else ""
            acc_nd = acc_dash.replace("-", "")
            date = dates[i] if i < len(dates) else ""

            idx_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{int(cik)}/{acc_nd}/{acc_dash}-index.htm"
            )
            try:
                time.sleep(0.12)
                idx_resp = requests.get(idx_url, headers=_SEC_HEADERS, timeout=10)
                if idx_resp.status_code != 200:
                    continue

                idx_lc = idx_resp.text.lower()
                if not any(kw in idx_lc for kw in ["transcript", "earnings", "commentary", "ex-99"]):
                    continue

                # Find all .htm exhibit URLs in the index
                exhibit_paths = re.findall(
                    r'href="(/Archives/edgar/data/\d+/\d+/[^"]+\.htm)"',
                    idx_resp.text,
                    re.IGNORECASE,
                )
                for full_path in exhibit_paths[:6]:
                    doc_url = f"https://www.sec.gov{full_path}"
                    try:
                        time.sleep(0.10)
                        doc_resp = requests.get(doc_url, headers=_SEC_HEADERS, timeout=15)
                        if doc_resp.status_code != 200 or len(doc_resp.text) < 2000:
                            continue
                        doc_lc = doc_resp.text.lower()
                        if not any(
                            kw in doc_lc
                            for kw in ["transcript", "earnings call", "operator:", "quarter ended"]
                        ):
                            continue
                        clean = _strip_html(doc_resp.text)
                        if len(clean) < 500:
                            continue
                        results.append(TranscriptResult(
                            ticker=ticker,
                            fiscal_period=date[:7],
                            fiscal_period_end=date,
                            date=date,
                            content=clean[:50000],
                            source="SEC-8K",
                            source_url=doc_url,
                            speaker_sections=_extract_speakers(clean)[:20],
                        ))
                        # Stop if we have enough and the transcript is older than 6 years
                        if len(results) >= 8:
                            try:
                                oldest_date = datetime.strptime(date, "%Y-%m-%d")
                                if (datetime.now() - oldest_date).days > 6 * 365:
                                    return results
                            except:
                                pass
                        break
                    except Exception:
                        pass
            except Exception:
                pass

        results.sort(key=lambda x: x.date, reverse=True)
        return results

    # ── Source 2: StockAnalysis.com ───────────────────────────────────────────

    def _stock_analysis(self, ticker: str) -> List[TranscriptResult]:
        """Scrape transcript listing from stockanalysis.com with date validation."""
        url = f"https://stockanalysis.com/stocks/{ticker.lower()}/transcripts/"
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=15)
        if resp.status_code != 200:
            return []

        # Extract relative transcript links
        link_pattern = rf'/stocks/{re.escape(ticker.lower())}/transcripts/[^"/#]+'
        links = list(dict.fromkeys(re.findall(link_pattern, resp.text, re.IGNORECASE)))
        if not links:
            return []

        results: List[TranscriptResult] = []
        for rel_link in links[:15]:
            if len(results) >= 24:
                break
            t_url = f"https://stockanalysis.com{rel_link}"
            try:
                time.sleep(0.3)
                t_resp = requests.get(t_url, headers=_BROWSER_HEADERS, timeout=15)
                if t_resp.status_code != 200:
                    continue
                html = t_resp.text
                clean = _strip_html(html)
                if len(clean) < 500:
                    continue

                q, yr = _parse_quarter_year(html)
                date_m = re.search(r"(\d{4}-\d{2}-\d{2})", html)
                date_str = date_m.group(1) if date_m else ""

                # Validate date within ±90 days of expected reporting month
                if not _date_ok(date_str, q, yr):
                    logger.debug("SA date mismatch ticker=%s yr=%s q=%s date=%s", ticker, yr, q, date_str)
                    continue

                fp = f"{yr} Q{q}" if q and yr else rel_link.rstrip("/").split("/")[-1]
                results.append(TranscriptResult(
                    ticker=ticker,
                    fiscal_period=fp,
                    fiscal_period_end=date_str,
                    date=date_str,
                    content=clean[:50000],
                    source="StockAnalysis",
                    speaker_sections=_extract_speakers(clean)[:20],
                ))
            except Exception:
                pass

        results.sort(key=lambda x: x.date, reverse=True)
        return results

    # ── Source 3: Nasdaq.com ──────────────────────────────────────────────────

    def _nasdaq(self, ticker: str) -> List[TranscriptResult]:
        """Fetch earnings transcripts from Nasdaq.com."""
        url = f"https://www.nasdaq.com/market-activity/stocks/{ticker.lower()}/earnings"
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=15)
        if resp.status_code != 200:
            return []

        transcript_links = re.findall(
            r'href="([^"]*(?:transcript|earnings.call)[^"]*)"',
            resp.text,
            re.IGNORECASE,
        )
        results: List[TranscriptResult] = []
        seen: set = set()
        for link in transcript_links[:8]:
            if link in seen:
                continue
            seen.add(link)
            if not link.startswith("http"):
                link = "https://www.nasdaq.com" + link
            try:
                time.sleep(0.3)
                t_resp = requests.get(link, headers=_BROWSER_HEADERS, timeout=15)
                if t_resp.status_code != 200:
                    continue
                clean = _strip_html(t_resp.text)
                if len(clean) < 500:
                    continue
                q, yr = _parse_quarter_year(t_resp.text)
                date_m = re.search(r"(\d{4}-\d{2}-\d{2})", t_resp.text)
                date_str = date_m.group(1) if date_m else ""
                fp = f"{yr} Q{q}" if q and yr else "Unknown"
                results.append(TranscriptResult(
                    ticker=ticker,
                    fiscal_period=fp,
                    fiscal_period_end=date_str,
                    date=date_str,
                    content=clean[:50000],
                    source="Nasdaq",
                    speaker_sections=_extract_speakers(clean)[:20],
                ))
            except Exception:
                pass
        return results

    # ── Source 4: Yahoo Finance ───────────────────────────────────────────────

    def _yahoo_finance(self, ticker: str) -> List[TranscriptResult]:
        """Try Yahoo Finance RSS for earnings call transcript mentions."""
        url = (
            f"https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={ticker}&region=US&lang=en-US"
        )
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=15)
        if resp.status_code != 200:
            return []

        items = re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)
        results: List[TranscriptResult] = []
        for item in items[:30]:
            title_m = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
            link_m = re.search(r"<link>(.*?)</link>", item, re.DOTALL)
            date_m = re.search(r"<pubDate>(.*?)</pubDate>", item, re.DOTALL)
            if not title_m:
                continue
            title = re.sub(r"<!\[CDATA\[|\]\]>", "", title_m.group(1)).strip()
            if "transcript" not in title.lower() and "earnings call" not in title.lower():
                continue
            link = link_m.group(1).strip() if link_m else ""
            date_str = ""
            if date_m:
                raw = date_m.group(1).strip()
                # "Mon, 29 Apr 2026 12:00:00 +0000"
                try:
                    dt = datetime.strptime(raw[:16], "%a, %d %b %Y")
                    date_str = dt.strftime("%Y-%m-%d")
                except Exception:
                    pass
            q, yr = _parse_quarter_year(title)
            fp = f"{yr} Q{q}" if q and yr else "Unknown"
            results.append(TranscriptResult(
                ticker=ticker,
                fiscal_period=fp,
                fiscal_period_end=date_str,
                date=date_str,
                content=title,
                source="Yahoo Finance",
                speaker_sections=[],
            ))
        return results
