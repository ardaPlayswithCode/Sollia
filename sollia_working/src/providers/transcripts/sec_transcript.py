"""SEC Transcript Provider — extracts earnings call text from 8-K EX-99.1 filings only."""
from __future__ import annotations
import re
import requests
import os
from typing import List, Dict, Optional

SEC_BASE = "https://data.sec.gov"
EDGAR_BASE = "https://www.sec.gov"
HEADERS = {"User-Agent": "Sollia contact@sollia.app", "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), ".cache", "transcripts")
os.makedirs(CACHE_DIR, exist_ok=True)


class SECTranscriptProvider:
    """Fetch earnings transcripts exclusively from SEC 8-K EX-99.1 filings."""
    
    def get_transcripts(self, ticker: str) -> List[Dict]:
        ticker = ticker.upper()
        cache_path = os.path.join(CACHE_DIR, f"{ticker}.json")
        if os.path.exists(cache_path):
            import json
            with open(cache_path) as f:
                return json.load(f)
        
        cik = self._get_cik(ticker)
        if not cik:
            return []
        
        transcripts = self._fetch_transcripts(ticker, cik)
        
        import json
        with open(cache_path, 'w') as f:
            json.dump(transcripts, f, indent=2)
        return transcripts
    
    def _get_cik(self, ticker: str) -> Optional[str]:
        try:
            resp = requests.get(f"{SEC_BASE}/files/company_tickers.json", headers=HEADERS, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                for _, company in data.items():
                    if company.get("ticker", "").upper() == ticker.upper():
                        return str(company["cik_str"]).zfill(10)
        except Exception:
            pass
        return None
    
    def _fetch_transcripts(self, ticker: str, cik: str) -> List[Dict]:
        transcripts = []
        try:
            submissions_url = f"{SEC_BASE}/submissions/CIK{cik}.json"
            resp = requests.get(submissions_url, headers=HEADERS, timeout=30)
            if resp.status_code != 200:
                return []
            
            data = resp.json()
            filings = data.get("filings", {}).get("recent", {})
            
            for i, form in enumerate(filings.get("form", [])):
                if form == "8-K":
                    items = filings.get("items", [])[i] if i < len(filings.get("items", [])) else ""
                    if "2.02" in items:
                        acc = filings.get("accessionNumber", [])[i]
                        doc = filings.get("primaryDocument", [])[i]
                        if acc and doc:
                            text = self._fetch_ex99(cik, acc)
                            if text:
                                transcripts.append({
                                    "ticker": ticker,
                                    "date": filings.get("filingDate", [])[i],
                                    "quarter": self._guess_quarter(filings.get("filingDate", [])[i]),
                                    "text": text,
                                    "source": "SEC_8K_EX99"
                                })
            return transcripts
        except Exception:
            return []
    
    def _fetch_ex99(self, cik: str, acc: str) -> Optional[str]:
        acc_nd = acc.replace("-", "")
        acc_dash = acc
        cik_int = int(cik)
        try:
            idx_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik_int}/{acc_nd}/{acc_dash}-index.htm"
            resp = requests.get(idx_url, headers={"User-Agent": "Sollia contact@sollia.app"}, timeout=15)
            if resp.status_code != 200:
                return None
            
            match = re.search(r'href="([^"]*ex-?99[^"]*\.htm[l]?)"', resp.text, re.IGNORECASE)
            if match:
                doc = match.group(1)
                doc_url = f"{EDGAR_BASE}/Archives/edgar/data/{cik_int}/{acc_nd}/{doc}"
                doc_resp = requests.get(doc_url, headers={"User-Agent": "Sollia contact@sollia.app"}, timeout=15)
                if doc_resp.status_code == 200:
                    return self._strip_html(doc_resp.text)
        except Exception:
            pass
        return None
    
    def _strip_html(self, html: str) -> str:
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'&[a-z]+;', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()
    
    def _guess_quarter(self, date_str: str) -> str:
        if not date_str:
            return "Q?"
        month = int(date_str.split("-")[1]) if "-" in date_str else 0
        q = (month - 1) // 3 + 1
        return f"Q{q}"


