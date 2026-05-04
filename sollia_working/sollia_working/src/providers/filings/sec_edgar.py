"""SEC EDGAR provider — pulls 10-K filings and parses MD&A."""
from __future__ import annotations
import re
import time
import logging
from typing import Dict, List, Optional, Tuple
import requests

logger = logging.getLogger(__name__)

SEC_BASE = "https://data.sec.gov"
EDGAR_BASE = "https://www.sec.gov"
_HEADERS = {"User-Agent": "Sollia Analysis contact@sollia.app", "Accept-Encoding": "gzip, deflate"}
_DATA_HEADERS = {"User-Agent": "Sollia Analysis contact@sollia.app", "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}


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
            if form in ("10-K", "10-K/A"):
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

    def fetch_html(self, cik: str, acc_nd: str, doc: str) -> str:
        url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{doc}"
        try:
            time.sleep(0.15)
            resp = requests.get(url, headers=_HEADERS, timeout=30)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            logger.warning("Failed to fetch filing doc: %s", exc)
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
        text = re.sub(r'&nbsp;|&#160;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'&#8220;', '“', text)
        text = re.sub(r'&#8221;', '”', text)
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
                    bad_words = ["segment", "information", "note", "item", "part", "report", "pricewaterhouse"]
                    if not any(bad in name.lower() for bad in bad_words):
                        return name
        return ""
