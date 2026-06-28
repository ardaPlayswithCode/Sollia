"""
Agent 1 — Corporate Discrepancy: Cross-document contradiction finder across 5 SEC filings.
Layer 0: Fiscal year alignment + data fetching with 3-tier fallback per document type.
"""
from __future__ import annotations
import os

from src.config.llm_config import LLM_PROVIDERS, RATE_LIMIT, call_llm, get_primary_model, start_tier
from src.utils.scoring import score_corporate_discrepancy

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
        # Enable cache reads in _get_all_filings()/_fetch_htm() for this run.
        self._current_ticker = ticker
        cik = get_cik(ticker)
        if not cik:
            return None, f"CIK not found for {ticker}"
        filings = self._get_all_filings(cik)
        ten_ks = [f for f in filings if f["form"] == "10-K" and f.get("period_of_report", "") and f.get("period_of_report", "")[:4] == str(year)]
        if not ten_ks:
            return None, f"No 10-K found for {ticker} in {year}"
        ten_k = max(ten_ks, key=lambda f: f.get("period_of_report", ""))
        F = date.fromisoformat(ten_k["period_of_report"])
        F_minus_1y = F.replace(year=F.year - 1)
        ten_qs = [f for f in filings if f["form"] == "10-Q" and f.get("period_of_report", "") and F_minus_1y < date.fromisoformat(f.get("period_of_report", "")) <= F]
        def14as = [f for f in filings if "14A" in f["form"] and f["filing_date"] and date.fromisoformat(f["filing_date"]) > F]
        def14a = min(def14as, key=lambda f: abs((date.fromisoformat(f["filing_date"]) - F).days)) if def14as else None
        eight_ks = [f for f in filings if f["form"] == "8-K" and f["filing_date"] and date.fromisoformat(f["filing_date"]) <= F and any(code in (f.get("items") or "") for code in ["2.02", "1.01", "5.02"])]
        eight_k = max(eight_ks, key=lambda f: f["filing_date"]) if eight_ks else None
        transcripts = self._fetch_transcripts(ticker, cik, ten_qs, F, filings)
        missing = []
        if not ten_qs: missing.append("10-Q")
        if not transcripts or len(transcripts) < 4: missing.append("EARNINGS_CALL")
        if not def14a: missing.append("DEF14A")
        if not eight_k: missing.append("8-K")
        if missing:
            return None, f"Insufficient filings for {ticker} in {year}. Missing: {', '.join(missing)}."
        return {"ticker": ticker, "year": year, "cik": cik, "fiscal_end": F.isoformat(), "ten_k": ten_k, "ten_qs": ten_qs, "def14a": def14a, "eight_k": eight_k, "transcripts": transcripts}, None

    def _fetch_transcripts(self, ticker: str, cik: str, ten_qs: List[dict], F: date, filings: List[dict]) -> List[dict]:
        all_transcripts = []
        for f in filings:
            if f["form"] != "8-K" or "2.02" not in (f.get("items") or "") or not f["filing_date"]:
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
        used_dates = set()
        for q in ten_qs:
            q_date = date.fromisoformat(q["period_of_report"])
            available = [t for t in all_transcripts if t["date"] not in used_dates]
            if not available:
                break
            best = min(available, key=lambda t: abs((date.fromisoformat(t["date"]) - q_date).days))
            assigned.append(best)
            used_dates.add(best["date"])
        after_F = [t for t in all_transcripts if t["date"] not in used_dates and date.fromisoformat(t["date"]) > F]
        if after_F:
            assigned.append(min(after_F, key=lambda t: (date.fromisoformat(t["date"]) - F).days))
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
    sections = split_sections(text)
    chunks, char_count = [], 0
    gi = 0  # running index → guarantees a UNIQUE chunk_id (section_name[:30] collides)
    for section in sections:
        paras = split_paragraphs(section["text"])
        for pi, para in enumerate(paras):
            sub_chunks = chunk_paragraph(para)
            for si, sub in enumerate(sub_chunks):
                sents = split_sentences(sub)
                char_count += len(sub)
                chunks.append({
                    "chunk_id": f"{doc_type}_{section['section_name'][:30]}_{pi}_{si}_{gi}",
                    "text": sub,
                    "sentences": sents,
                    "metadata": {
                        "ticker": ticker, "year": year,
                        "source_doc_type": doc_type,
                        "section": section["section_name"],
                        "page_number": max(1, char_count // 3000),
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


def fetch_all_texts(ticker: str, year: int) -> Tuple[Optional[dict], Optional[str]]:
    """Fetch and align all 5 documents, then download full text for each."""
    result, error = fetch_all_documents(ticker, year)
    if error:
        return None, error
    
    fetcher = SECFilingFetcher()
    fetcher._current_ticker = ticker  # enable cache reads in _fetch_htm()/_get_all_filings()
    cik = result["cik"]

    # 10-K text
    ten_k = result["ten_k"]
    html = fetcher._fetch_htm(cik, ten_k["accession_number"], ten_k["primary_document"])
    result["ten_k_text"] = _strip_html(html) if html else ""
    # Also try to extract MD&A immediately
    if result["ten_k_text"]:
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
    def14a = result["def14a"]
    # The alignment already picked the best by date. If it's a PX14A6G, try to find a real DEF 14A nearby
    if "PX14A6G" in def14a.get("form", ""):
        # Search for a real DEF 14A within 90 days
        all_def14as = [f for f in fetcher._get_all_filings(cik) if "14A" in f["form"] and f["filing_date"]]
        F_date = date.fromisoformat(result["fiscal_end"])
        better = [f for f in all_def14as if "DEF 14A" in f["form"] and "PX" not in f["form"] 
                  and abs((date.fromisoformat(f["filing_date"]) - F_date).days) < 90]
        if better:
            def14a = min(better, key=lambda f: abs((date.fromisoformat(f["filing_date"]) - F_date).days))
            result["def14a"] = def14a
    html = fetcher._fetch_htm(cik, def14a["accession_number"], def14a["primary_document"])
    result["def14a_text"] = _strip_html(html) if html else ""
    if result["def14a_text"]:
        result["def14a_clean"] = clean_def14a(result["def14a_text"])
    
    # 8-K text
    eight_k = result["eight_k"]
    html = fetcher._fetch_htm(cik, eight_k["accession_number"], eight_k["primary_document"])
    result["eight_k_text"] = _strip_html(html) if html else ""
    if result["eight_k_text"]:
        result["eight_k_clean"] = clean_8k(result["eight_k_text"])
    
    # Transcripts already have content from fetch
    result["transcript_texts"] = [t["content"] for t in result["transcripts"]]
    
    return result, None


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

**ONLY FLAG WHEN BOTH DOCUMENTS REFER TO THE SAME TIME PERIOD:**
- Both explicitly say "full year" / "fiscal year" / "annual"
- Both explicitly say the same quarter (e.g., "Q3 2024")
- Both refer to the same specific date or event
- The comparison is clearly apples-to-apples based on surrounding context

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
    """Parse LLM response text into a list of discrepancy dicts.
    Strips markdown code fences and extracts the JSON array. Returns [] on failure."""
    import json as _json
    if not text or not isinstance(text, str):
        return []
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
    try:
        data = _json.loads(cleaned)
    except Exception:
        m = re.search(r'\[.*\]', cleaned, re.DOTALL)
        if not m:
            return []
        try:
            data = _json.loads(m.group(0))
        except Exception:
            return []
    return data if isinstance(data, list) else []


def _call_llm(prompt, provider_idx=None, max_tokens=400):
    """Call LLM using shared config — delegates to llm_config.call_llm.

    PART 4c: default start tier is Groq-70B (this is the quality-sensitive
    classify step); callers may still pass an explicit provider_idx."""
    if provider_idx is None:
        provider_idx = start_tier("corporate_discrepancy")
    try:
        return call_llm(prompt, max_tokens=max_tokens, provider_idx=provider_idx)
    except Exception:
        return "", {"cached": False, "provider": "failed"}

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
                    "page_number": meta.get("page_number", 1),
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

def run_discrepancy_agent(ticker: str, year: int, progress=None) -> dict:
    """Run the full Corporate Discrepancy agent pipeline.

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

    add_log("START", "Agent 1 initiated for %s %d" % (ticker, year))
    _emit("Pulling FY%d filings for %s..." % (year, ticker), "search")

    # Layer 0: Fetch
    documents, error = fetch_all_texts(ticker, year)
    if error:
        _emit("Could not assemble a complete filing set: %s" % error, "flag")
        return {"ticker": ticker, "year": year, "error": error, "agent_log": log}
    _emit("Found 10-K, %d 10-Qs, 8-K, DEF 14A, %d earnings transcripts" % (
        len(documents.get("ten_qs", [])), len(documents.get("transcripts", []))), "fetch")
    add_log("FETCH", "Fetched all 5 document types")
    
    # Layer 0.5: Clean
    documents = clean_all(documents)
    add_log("CLEAN", "Cleaned all documents")
    
    # Layer 1: Chunk
    documents = chunk_all(documents)
    add_log("CHUNK", "Chunked into %d total chunks" % sum([
        len(documents.get("chunks_10k", [])), len(documents.get("chunks_10q", [])),
        len(documents.get("chunks_def14a", [])), len(documents.get("chunks_8k", [])),
        len(documents.get("chunks_transcript", []))
    ]))
    
    # Layer 2: Embed
    embed_fn = get_embedder()
    if embed_fn is None:
        # FinBERT (transformers + torch) is unavailable. A warm run can still proceed
        # entirely from the embedding cache; only abort if a chunk actually needs a
        # fresh embedding. This lets cached re-runs work on machines without torch.
        _all_chunks = (documents.get("chunks_10k", []) + documents.get("chunks_10q", []) +
                       documents.get("chunks_def14a", []) + documents.get("chunks_8k", []) +
                       documents.get("chunks_transcript", []))
        _uncached = [c for c in _all_chunks
                     if c.get("embedding") is None and _get_cached_embedding(c["text"]) is None]
        if _uncached:
            return {"ticker": ticker, "year": year,
                    "error": "FinBERT not available (transformers + torch). %d of %d chunks "
                             "are uncached and cannot be embedded. Install with: "
                             "pip install transformers torch" % (len(_uncached), len(_all_chunks)),
                    "agent_log": log}
        # Every chunk is in the cache — supply a stub that must never be invoked.
        def embed_fn(_text):
            raise RuntimeError("embed_fn called despite full cache coverage")
    _emit("Reading and aligning passages across the filing set...", "align")
    documents = embed_all_chunks(documents, embed_fn)
    add_log("EMBED", "Embedded all chunks with FinBERT")

    # Layer 3: Align
    documents = align_all_pairs(documents)
    n_pairs = len(documents.get("aligned_pairs", []))
    add_log("ALIGN", "Found %d aligned pairs" % n_pairs)
    _emit("Matched %d related passages to cross-examine" % n_pairs, "info")

    # Layer 4: LLM Classify
    _emit("Cross-referencing claims across the 10-K, 10-Qs, and earnings calls...", "analyze")
    documents = classify_discrepancies(documents)
    add_log("CLASSIFY", "LLM found %d discrepancies" % len(documents.get("discrepancies", [])))

    # Layer 5: Rank
    documents = rank_discrepancies(documents)
    n_top = len(documents.get("top_discrepancies", []))
    add_log("RANK", "Selected top %d discrepancies" % n_top)
    _emit("Flagged %d potential discrepancies for review" % n_top, "flag")
    
    # Layer 6: Citations
    documents = build_citations(documents)
    # Add ticker/year/cik for URL construction
    documents["ticker"] = ticker
    documents["year"] = year
    documents["cik"] = documents.get("cik", "")
    add_log("CITE", "Built exact sentence citations")
    
    elapsed = time.time() - t0
    add_log("OUTPUT", "Complete in %.1fs" % elapsed)
    _emit("Corporate discrepancy scan complete (%.0fs)" % elapsed, "done")

    # PART 2 scoring redesign. The OLD formula used ONLY the single worst
    # discrepancy (100 - top.final_score), so a filing with 1 high-severity
    # contradiction and one with 8 both scored 10 — no magnitude. The new
    # score_corporate_discrepancy() starts from a clean baseline (90 when there
    # are zero discrepancies — internally consistent) and subtracts rank-decayed
    # penalties over ALL discrepancies, so more/severe contradictions read lower
    # while a long tail of minor tensions does not floor it. See
    # src/utils/scoring.py for the formula and rationale.
    _score, _signal, _rationale = score_corporate_discrepancy(
        documents.get("top_discrepancies"))

    _result = {
        "ticker": ticker, "year": year,
        "agent": "Corporate Discrepancy",
        "run_timestamp": datetime.datetime.utcnow().isoformat(),
        "documents_analyzed": ["10-K", "10-Q", "8-K", "DEF14A", "EARNINGS_CALL"],
        "total_aligned_pairs": len(documents.get("aligned_pairs", [])),
        "total_discrepancies_found": len(documents.get("discrepancies", [])),
        "top_discrepancies": documents.get("top_discrepancies", []),
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
        save_run("corporate_discrepancy", ticker, year, _result, duration_ms=elapsed * 1000)
    except Exception:
        pass
    return _result
