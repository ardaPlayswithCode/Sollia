"""SEC EDGAR provider — pulls 10-K filings and parses MD&A."""
from __future__ import annotations
import re
import time
import logging
from typing import Dict, List, Optional, Tuple
import requests

logger = logging.getLogger(__name__)
# ── Unified cache paths (shared across all agents) ─────────────────────────
import os as _os
_CACHE_ROOT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', '..', '..', '.cache')
_FILINGS_CACHE_DIR = _os.path.join(_CACHE_ROOT, 'filings')
_TEXTS_CACHE_DIR = _os.path.join(_CACHE_ROOT, 'texts')
_LLM_CACHE_DIR = _os.path.join(_CACHE_ROOT, 'llm')
for _d in (_CACHE_ROOT, _FILINGS_CACHE_DIR, _TEXTS_CACHE_DIR, _LLM_CACHE_DIR):
    _os.makedirs(_d, exist_ok=True)


SEC_BASE = "https://data.sec.gov"
EDGAR_BASE = "https://www.sec.gov"
_HEADERS = {"User-Agent": "Sollia Analysis contact@sollia.app", "Accept-Encoding": "gzip, deflate"}
_DATA_HEADERS = {"User-Agent": "Sollia Analysis contact@sollia.app", "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}



# ── Universal cache helpers ────────────────────────────────────────────────

def _load_json(path: str):
    """Load JSON file, return None if missing/corrupt."""
    import json as _json
    try:
        if _os.path.exists(path):
            with open(path) as f:
                return _json.load(f)
    except Exception:
        pass
    return None

def _save_json(path: str, data) -> bool:
    """Save data as JSON. Returns True on success."""
    import json as _json
    try:
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            _json.dump(data, f, default=str)
        return True
    except Exception:
        return False

def get_cached_filings(ticker: str) -> dict | None:
    """Load cached filing metadata for a ticker. Returns dict keyed by accession_number."""
    path = _os.path.join(_FILINGS_CACHE_DIR, f'{ticker.upper()}_filings.json')
    return _load_json(path)

def cache_filings(ticker: str, filings: list[dict]) -> bool:
    """Cache filing metadata keyed by accession_number."""
    cache = {}
    for f in filings:
        acc = f.get('accession_number', '')
        if acc:
            cache[acc] = {
                'cik': f.get('cik', ''),
                'form': f.get('form', ''),
                'filing_date': f.get('filing_date', ''),
                'period_of_report': f.get('period_of_report', ''),
                'primary_doc': f.get('primary_doc', ''),
                'htm_url': f.get('htm_url', ''),
                'accession_nodash': f.get('accession_nodash', ''),
            }
    path = _os.path.join(_FILINGS_CACHE_DIR, f'{ticker.upper()}_filings.json')
    return _save_json(path, cache)

def get_cached_text(ticker: str, accession_number: str) -> str | None:
    """Load cached cleaned text for a specific filing."""
    # Text cache is a single JSON dict keyed by accession
    path = _os.path.join(_TEXTS_CACHE_DIR, f'{ticker.upper()}_texts.json')
    all_texts = _load_json(path) or {}
    return all_texts.get(accession_number)

def cache_text(ticker: str, accession_number: str, text: str) -> bool:
    """Cache cleaned text for a specific filing (merges into existing dict)."""
    path = _os.path.join(_TEXTS_CACHE_DIR, f'{ticker.upper()}_texts.json')
    all_texts = _load_json(path) or {}
    all_texts[accession_number] = text
    return _save_json(path, all_texts)

def get_cached_llm(prompt_hash: str) -> dict | None:
    """Load cached LLM response. Returns {prompt, response, model, timestamp} or None."""
    path = _os.path.join(_LLM_CACHE_DIR, f'{prompt_hash}.json')
    return _load_json(path)

def cache_llm(prompt_hash: str, prompt: str, response: str, model: str) -> bool:
    """Cache LLM prompt+response with metadata."""
    from datetime import datetime as _dt
    path = _os.path.join(_LLM_CACHE_DIR, f'{prompt_hash}.json')
    return _save_json(path, {
        'prompt': prompt,
        'response': response,
        'model': model,
        'timestamp': _dt.now().isoformat(),
    })


class SECEdgarProvider:
    def __init__(self) -> None:
        self._cik_cache: Dict[str, str] = {}
        self._name_cache: Dict[str, str] = {}

    def get_cik(self, ticker: str) -> Optional[str]:
        key = ticker.upper()
        if key in self._cik_cache:
            return self._cik_cache[key]
        try:
            resp = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers=_HEADERS, timeout=15,
            )
            resp.raise_for_status()
            for item in resp.json().values():
                if str(item.get("ticker", "")).upper() == key:
                    cik = str(item["cik_str"]).zfill(10)
                    self._cik_cache[key] = cik
                    self._name_cache[key] = item.get("title", "")
                    return cik
        except Exception as exc:
            logger.error("CIK lookup failed for %s: %s", ticker, exc)
        return None

    def get_company_name(self, ticker: str) -> str:
        key = ticker.upper()
        if key in self._name_cache:
            return self._name_cache[key]
        cik = self.get_cik(ticker)
        if not cik:
            return ticker
        try:
            resp = requests.get(f"{SEC_BASE}/submissions/CIK{cik}.json", headers=_DATA_HEADERS, timeout=15)
            resp.raise_for_status()
            name = resp.json().get("name", ticker)
            self._name_cache[key] = name
            return name
        except Exception:
            return ticker

    def get_10k_filings(self, ticker: str) -> List[dict]:
        """Get 10-K filings only (backwards compatible)."""
        return [f for f in self.get_all_filings(ticker) if f.get('form', '') in ('10-K', '10-K/A')]

    def get_10q_filings(self, ticker: str) -> List[dict]:
        """Get 10-Q filings only."""
        return [f for f in self.get_all_filings(ticker) if f.get('form', '') in ('10-Q', '10-Q/A')]

    def get_all_filings(self, ticker: str) -> List[dict]:
        """Get ALL 10-K and 10-Q filings (no year filter)."""
        cik = self.get_cik(ticker)
        if not cik:
            return []
        all_filings: List[dict] = []
        try:
            url = f"{SEC_BASE}/submissions/CIK{cik}.json"
            resp = requests.get(url, headers=_DATA_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Submissions fetch failed for %s: %s", ticker, exc)
            return []
        all_filings.extend(self._parse_filing_block(cik, data.get("filings", {}).get("recent", {})))
        all_filings.sort(key=lambda x: x.get("filing_date", ""), reverse=True)
        return all_filings

    def _parse_filing_block(self, cik: str, block: dict) -> List[dict]:
        out: List[dict] = []
        forms = block.get("form", [])
        accs = block.get("accessionNumber", [])
        dates = block.get("filingDate", [])
        periods = block.get("reportDate", [])
        primary = block.get("primaryDocument", [])
        for i, form in enumerate(forms):
            if form in ("10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A") or "14A" in form:
                acc_dash = accs[i] if i < len(accs) else ""
                acc_nd = acc_dash.replace("-", "")
                pd = primary[i] if i < len(primary) else ""
                htm_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{pd}" if pd else ""
                out.append({
                    "cik": cik,
                    "form": form,
                    "accession_number": acc_dash,
                    "accession_nodash": acc_nd,
                    "filing_date": dates[i] if i < len(dates) else "",
                    "period_of_report": periods[i] if i < len(periods) else "",
                    "primary_doc": pd,
                    "htm_url": htm_url,
                })
        return out

    def fetch_html(self, cik: str, acc_nd: str, doc: str, ticker: str = "") -> str:
        """Fetch filing HTML with 3-tier fallback: SEC .htm -> SEC retry -> EDGAR full-text."""
        # Source 1: SEC EDGAR direct .htm (primary, ~0.2s)
        url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{doc}"
        try:
            time.sleep(0.15)
            resp = requests.get(url, headers=_HEADERS, timeout=30)
            resp.raise_for_status()
            if len(resp.text) > 5000:
                return resp.text
        except Exception as exc:
            logger.debug("SEC .htm failed: %s", exc)

        # Source 2: SEC direct retry with alternate User-Agent
        if ticker:
            retry_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{doc}"
            try:
                resp = requests.get(retry_url, headers={**_HEADERS, "User-Agent": "Mozilla/5.0"}, timeout=30)
                if resp.status_code == 200 and len(resp.text) > 5000:
                    return resp.text
            except Exception as exc:
                logger.debug("SEC retry failed: %s", exc)

        # Source 3: EDGAR full-text search (SEC-approved, 2016+)
        efts_url = f"https://efts.sec.gov/LATEST/search-index?q={ticker}&dateRange=custom&startdt=2002-01-01&enddt=2026-12-31&forms=10-K,10-Q"
        try:
            resp = requests.get(efts_url, headers=_HEADERS, timeout=15)
            if resp.status_code == 200 and len(resp.text) > 5000:
                return self._strip_html(resp.text)
        except Exception as exc:
            logger.debug("EDGAR full-text search failed: %s", exc)

        return ""

    def _find_primary_doc(self, cik: str, acc_nd: str, acc_dash: str) -> str:
        idx_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{acc_dash}-index.htm"
        try:
            time.sleep(0.12)
            resp = requests.get(idx_url, headers=_HEADERS, timeout=15)
            text = resp.text
            for m in re.finditer(
                r'<a href="[^"]*/([^"]+\.htm)"[^>]*>(?:[^<]*(?:10-K|Annual)[^<]*)</a>',
                text, re.IGNORECASE
            ):
                fname = m.group(1)
                if fname and fname.endswith(".htm"):
                    return fname
            m2 = re.search(r'/Archives/edgar/data/\d+/\d+/([^"]+\.htm)', text, re.IGNORECASE)
            if m2:
                return m2.group(1)
        except Exception:
            pass
        return ""

    def get_filing_result(self, ticker: str, info: dict):
        from src.schemas import FilingResult
        cik = info["cik"]
        acc_dash = info.get("accession_number", "")
        acc_nd = info.get("accession_nodash", acc_dash.replace("-", ""))
        doc = info.get("primary_doc", "")
        if not doc:
            doc = self._find_primary_doc(cik, acc_nd, acc_dash)
        htm_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{doc}" if doc else ""
        html = self.fetch_html(cik, acc_nd, doc) if doc else ""
        mda = self.extract_mda(html) if html else ""
        ceo = self.extract_ceo_name(html) if html else ""
        name = self.get_company_name(ticker)
        return FilingResult(
            ticker=ticker,
            company_name=name,
            cik=cik,
            filing_type=info.get("form", "10-K"),
            period_of_report=info.get("period_of_report", ""),
            filing_date=info.get("filing_date", ""),
            accession_number=acc_dash,
            htm_url=htm_url,
            mda_text=mda,
            full_text=html[:80000] if html else "",
            ceo_name=ceo,
        )

    @staticmethod
    def _strip_html(html: str) -> str:
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'&#\d+;', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def extract_mda(self, html: str) -> str:
        clean = self._strip_html(html)
        starts = [
            r"item\s+7[\.\s:]*management[\'s]*\s+discussion\s+and\s+analysis",
            r"management[\'s]*\s+discussion\s+and\s+analysis\s+of",
            r"results\s+of\s+operations",
        ]
        start_pos = -1
        for pat in starts:
            for m in re.finditer(pat, clean, re.IGNORECASE):
                pos = m.start()
                after = clean[pos:pos + 500].strip()
                # Skip TOC entries: "Item 7... 36 Item 7A" pattern with page numbers
                if re.match(r'[^.]{0,100}\d{1,3}\s+Item\s+7A', after[:120]):
                    continue
                if len(after) > 200:
                    start_pos = pos
                    break
            if start_pos != -1:
                break
        if start_pos == -1:
            return clean[:30000]
        ends = [
            r"item\s+7a[\.\s:]*quantitative",
            r"item\s+8[\.\s:]*financial\s+statements",
        ]
        end_pos = len(clean)
        for pat in ends:
            m = re.search(pat, clean[start_pos + 200:], re.IGNORECASE)
            if m:
                end_pos = start_pos + 200 + m.start()
                break
        return clean[start_pos:end_pos][:30000]

    def extract_ceo_name(self, html: str) -> str:
        """Extract CEO name from filing signature block."""
        clean = self._strip_html(html)
        patterns = [
            r'/s/\s+\S+\s+\S+\s+([A-Z][-a-zA-Z]+\s+[A-Z][-a-zA-Z]+(?:\s+[A-Z][-a-zA-Z]+)?)\s+(?:President|Chief)',
            r'/s/\s+[A-Z][-A-Z\s]+\n\s*([A-Z][-a-zA-Z]+\s+[A-Z][-a-zA-Z]+(?:\s+[A-Z][-a-zA-Z]+)?)',
            r'/s/\s+([A-Z][-A-Za-z]+\s+[A-Z][-A-Za-z]+(?:\s+[A-Z][-A-Za-z]+)?)',
        ]
        for pat in patterns:
            m = re.search(pat, clean)
            if m:
                name = m.group(1).strip()
                if name.isupper():
                    name = name.title()
                parts = name.split()
                cleaned = []
                for p in parts:
                    if "-" in p:
                        subparts = p.split("-")
                        p = "-".join(s[0].upper() + s[1:].lower() if s else "" for s in subparts)
                    cleaned.append(p)
                name = " ".join(cleaned)
                if 2 <= len(name.split()) <= 4:
                    bad_words = ["segment", "information", "note", "item", "part", "report", "pricewaterhouse", "deloitte", "touche", "llp", "ernst", "young", "kpmg", "pricewaterhousecoopers", "pwc", "grant", "thornton", "auditor", "accounting", "audit"]
                    if not any(bad in name.lower() for bad in bad_words):
                        return name
        return ""
