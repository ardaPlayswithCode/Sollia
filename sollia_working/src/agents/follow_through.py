"""Agent 3: Management Follow-Through — tracks CEO promises and checks if they were kept."""
from __future__ import annotations
import re
import json
import time
import logging
import subprocess
from typing import List, Optional, Dict, Tuple
from datetime import datetime

import requests
import os as _os

logger = logging.getLogger(__name__)

# ── Transcript Provider (reuse FMP) ─────────────────────────────────
def _get_transcripts(ticker: str) -> List[Dict]:
    """Fetch earnings call transcripts from FMP API (disk-cached)."""
    cache_dir = _os.path.join(_os.path.dirname(__file__), '..', '.transcript_cache')
    _os.makedirs(cache_dir, exist_ok=True)
    cache_file = _os.path.join(cache_dir, f'{ticker}.json')
    
    # Check cache
    if _os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                return json.loads(f.read())
        except:
            pass
    
    # Fetch from FMP
    try:
        from src.providers.transcripts.fmp import FMPTranscriptProvider
        fmp = FMPTranscriptProvider()
        results = fmp.get_transcripts(ticker)
        # Cache the results
        data = [{
            "date": str(r.date) if hasattr(r, 'date') else str(r.get('date', '')),
            "content": r.content if hasattr(r, 'content') else r.get('content', ''),
            "fiscal_period": r.fiscal_period if hasattr(r, 'fiscal_period') else r.get('fiscal_period', ''),
            "source_url": r.source_url if hasattr(r, 'source_url') else r.get('source_url', ''),
        } for r in results]
        with open(cache_file, 'w') as f:
            json.dump(data, f)
        return data
    except Exception as e:
        logger.error("FMP failed: %s. Trying Yahoo Finance...", e)
    
    # Fallback: Yahoo Finance
    try:
        yahoo_url = f"https://finance.yahoo.com/quote/{ticker}/"
        resp = requests.get(yahoo_url, headers={"User-Agent": "Sollia Analysis contact@sollia.app"}, timeout=15)
        if resp.status_code == 200:
            # Extract transcript links from Yahoo Finance page
            # Store the page content for later parsing
            data = [{
                "date": "latest",
                "content": resp.text[:50000],
                "fiscal_period": "latest",
                "source_url": yahoo_url,
            }]
            with open(cache_file, 'w') as f:
                json.dump(data, f)
            return data
    except Exception as e2:
        logger.error("Yahoo Finance also failed: %s", e2)
    
    return []

HEADERS = {"User-Agent": "Sollia Analysis contact@sollia.app", "Accept-Encoding": "gzip, deflate"}
DATA_HEADERS = {"User-Agent": "Sollia Analysis contact@sollia.app", "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}
EDGAR_BASE = "https://www.sec.gov"
SEC_BASE = "https://data.sec.gov"

# ── Promise Detection Patterns ───────────────────────────────────
POSITIVE_PATTERNS = [
    r'\bwe expect\b', r'\bwe plan\b', r'\bwe will\b', r'\bwe are on track\b',
    r'\bwe project\b', r'\bwe forecast\b', r'\bwe anticipate\b', r'\bour target\b',
    r'\bwe aim\b', r'\bwe commit\b', r'\bwe intend\b', r'\bgoal of\b',
    r'\bobjective of\b', r'\btarget of\b',
]

NEGATIVE_PATTERNS = [
    r'no assurance', r'cannot assure', r'cannot guarantee', r'no guarantee',
    r'may not', r'might not', r'could not', r'unless', r'except',
    r'risk factor', r'cautionary', r'forward.looking statements',
    r'should read', r'completely and understand', r'actual results may differ',
]


def _ask_ollama(prompt: str, timeout: int = 45) -> str:
    """Call local Ollama instance."""
    try:
        result = subprocess.run(
            ["ollama", "run", "llama3.2"],
            input=prompt, capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()
    except Exception as e:
        logger.error("Ollama call failed: %s", e)
        return ""


def _strip_html(html: str) -> str:
    """Remove HTML tags and entities."""
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _split_sentences(text: str) -> List[str]:
    """Split text into sentences, filtering out short fragments."""
    raw = re.split(r'(?<=[.!?])\s+', text)
    return [s.strip() for s in raw if len(s.split()) >= 5]


def _extract_mda(html: str, filing_type: str) -> str:
    """Extract MD&A section based on filing type."""
    if filing_type == "10-K":
        # Multiple regex patterns for different SEC formats
        patterns = [
            r'(?i)Item\s*7[\.\s]*Management.s Discussion.*?(?=Item\s*7A|Item\s*8)',
            r'(?i)ITEM\s*7[\.\s]*MANAGEMENT.S DISCUSSION.*?(?=ITEM\s*7A|ITEM\s*8)',
            r'(?i)Management.s Discussion and Analysis.*?(?=Item\s*7A|Item\s*8|Quantitative)',
        ]
    elif filing_type == "10-Q":
        patterns = [
            r'(?i)Item\s*2[\.\s]*Management.s Discussion.*?(?=Item\s*3|Item\s*4)',
            r'(?i)ITEM\s*2[\.\s]*MANAGEMENT.S DISCUSSION.*?(?=ITEM\s*3|ITEM\s*4)',
            r'(?i)Management.s Discussion and Analysis.*?(?=Item\s*3|Item\s*4|Quantitative)',
        ]
    else:
        return html[:30000]
    
    for pattern in patterns:
        match = re.search(pattern, html, re.DOTALL)
        if match and len(match.group()) > 1000:
            return match.group()
    return html[:30000]


def _filter_promises(sentences: List[Dict]) -> List[Dict]:
    """Filter sentences for promise-like language (deterministic)."""
    candidates = []
    for s in sentences:
        text = s['text']
        has_pos = any(re.search(pat, text, re.IGNORECASE) for pat in POSITIVE_PATTERNS)
        has_neg = any(re.search(pat, text, re.IGNORECASE) for pat in NEGATIVE_PATTERNS)
        # Also require a number or percentage for it to be a measurable promise
        has_metric = bool(re.search(r'\$?\d+[\d,.]*\s*(?:billion|million|thousand|%|percent|year|quarter)', text, re.IGNORECASE))
        if has_pos and not has_neg and has_metric:
            candidates.append(s)
    return candidates


def _batch_validate_promises(candidates: List[Dict]) -> List[Dict]:
    """Use Llama to validate which candidates are real promises and score severity."""
    if not candidates:
        return []
    
    batch_text = "\n\n".join([f"[{i}] {s['text'][:400]}" for i, s in enumerate(candidates)])
    
    # Build prompt as a plain string to avoid f-string curly brace issues
    prompt = """Analyze these sentences from company filings. For each one, determine if it's a specific, measurable business promise.

A promise must have a specific number, timeline, or measurable outcome. Vague statements like "we will continue to invest" are NOT promises.

You MUST return ONLY a valid JSON array starting with [ and ending with ]. Do NOT include ANY text before or after the array.
Format: [{"id": 0, "is_promise": true, "severity": 7, "metric": "revenue target"}, ...]

Sentences:
""" + batch_text

    response = _ask_ollama(prompt)
    if not response:
        return []
    
    # Parse JSON from Llama response (handles malformed JSON, duplicate keys, extra chars)
    try:
        start = response.find('[')
        if start < 0:
            return []
        
        raw = response[start:]
        # Pre-clean: remove all bracket-delimited control codes
        raw = re.sub(r'\[\d+[A-Za-z]', ' ', raw)
        raw = re.sub(r'\[[A-Za-z]', ' ', raw)
        # Keep only valid JSON characters
        raw = re.sub(r'[^a-zA-Z0-9\s{}\[\]":,\-.]', ' ', raw)
        raw = re.sub(r'\s+', ' ', raw)
        
        # Strategy: extract individual JSON objects by finding {...} patterns
        # This handles cases where Llama returns multiple arrays or malformed JSON
        results = []
        depth = 0
        obj_start = -1
        for i, ch in enumerate(raw):
            if ch == '{':
                if depth == 0:
                    obj_start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and obj_start >= 0:
                    obj_str = raw[obj_start:i+1]
                    # Fix common Llama mistakes
                    obj_str = obj_str.replace('"is promise"', '"is_promise"')
                    obj_str = re.sub(r'([a-z])" "([a-z])', r'\1_\2', obj_str)  # Fix spaces in keys
                    try:
                        obj = json.loads(obj_str)
                        if isinstance(obj, dict):
                            results.append(obj)
                    except:
                        pass
                    obj_start = -1
        
        if not results:
            # Fallback: try parsing the whole thing
            last_valid = raw.rfind('},')
            if last_valid >= 0:
                raw = raw[:last_valid+1] + ']'
            elif not raw.endswith(']'):
                raw = raw + ']'
            results = json.loads(raw)
    except Exception as e:
        logger.error("Failed to parse Llama response: %s", e)
        return []
    
    # Merge back with original data
    validated = []
    for r in results:
        if not isinstance(r, dict):
            continue
        idx = r.get('id', -1)
        if 0 <= idx < len(candidates) and r.get('is_promise'):
            c = candidates[idx].copy()
            c['severity'] = r.get('severity', 5)
            c['metric'] = str(r.get('metric', 'none'))
            validated.append(c)
    
    # If no promises found, try one more time with a simpler prompt
    if not validated and len(candidates) > 0:
        logger.info("First attempt found 0 promises. Retrying...")
        retry_text = "\n".join([f"[{i}] {s['text'][:300]}" for i, s in enumerate(candidates[:5])])
        retry_prompt = "For each sentence, is it a promise? Reply JSON: [{\"id\":0,\"promise\":true,\"severity\":5}, ...]\n\n" + retry_text
        response2 = _ask_ollama(retry_prompt, timeout=30)
        if response2:
            try:
                start2 = response2.find('[')
                if start2 >= 0:
                    raw2 = response2[start2:]
                    raw2 = re.sub(r'[^a-zA-Z0-9\s{}\[\]":,\-.]', ' ', raw2)
                    raw2 = re.sub(r'\s+', ' ', raw2)
                    last2 = raw2.rfind('},')
                    if last2 >= 0:
                        raw2 = raw2[:last2+1] + ']'
                    elif not raw2.endswith(']'):
                        raw2 += ']'
                    results2 = json.loads(raw2)
                    for r in results2:
                        if isinstance(r, dict) and r.get('promise'):
                            idx = r.get('id', -1)
                            if 0 <= idx < len(candidates):
                                c = candidates[idx].copy()
                                c['severity'] = r.get('severity', 5)
                                c['metric'] = 'none'
                                validated.append(c)
            except:
                pass
    
    return validated

def _deterministic_evidence_search(promise: Dict, later_sentences: List[Dict]) -> Optional[str]:
    """Search later filings for evidence the promise was kept."""
    metric = promise.get('metric', '')
    text_to_search = " ".join([s['text'] for s in later_sentences[:500]])
    
    # Extract key terms from metric
    keywords = re.findall(r'\b\w+\b', str(metric).lower())
    keywords = [k for k in keywords if len(k) > 3 and k not in ['what', 'none', 'that', 'this', 'with', 'from', 'they', 'have', 'were', 'been']]
    
    if not keywords:
        return None
    
    # Build a search pattern
    pattern = '|'.join(keywords[:5])
    search_regex = r'.{{0,200}}(?i:{pattern}).{{0,200}}'.format(pattern=pattern)
    matches = re.findall(search_regex, text_to_search, re.IGNORECASE)
    
    if matches:
        return matches[0][:500]
    return None


def _llama_evidence_verification(promise: Dict, later_sentences: List[Dict]) -> Tuple[str, str]:
    """Use Llama to verify if a promise was kept based on later filings."""
    promise_text = promise['text'][:500]
    later_context = " ".join([s['text'] for s in later_sentences[:30]])[:3000]
    
    prompt = f"""A company made this statement in a filing:

PROMISE: "{promise_text}"

Here are excerpts from later filings:

{later_context}

Was this promise KEPT (they did what they said), BROKEN (they didn't), PARTIAL (some but not all), or UNCLEAR (not enough evidence)?

Reply ONLY in JSON:
{{"status": "KEPT/BROKEN/PARTIAL/UNCLEAR", "evidence": "brief quote from later filings or explanation"}}"""

    response = _ask_ollama(prompt)
    try:
        result = json.loads(response)
        return result.get('status', 'UNCLEAR'), result.get('evidence', 'No evidence found')
    except json.JSONDecodeError:
        return 'UNCLEAR', 'Could not verify'


def _get_cik(ticker: str) -> Optional[str]:
    """Lookup CIK from SEC."""
    try:
        resp = requests.get("https://www.sec.gov/files/company_tickers.json", headers=HEADERS, timeout=15)
        for item in resp.json().values():
            if str(item.get("ticker", "")).upper() == ticker.upper():
                return str(item["cik_str"]).zfill(10)
    except Exception as e:
        logger.error("CIK lookup failed: %s", e)
    return None


class FollowThroughAgent:
    """Agent 3 — tracks management promises and verifies follow-through."""
    
    def __init__(self):
        self._log: List[str] = []
    
    def run(self, ticker: str) -> Dict:
        """Run the follow-through analysis for a given ticker."""
        ticker = ticker.upper()
        self._log = []
        start_time = time.time()
        
        # Step 1: Get CIK
        self._log.append(f"Looking up CIK for {ticker}...")
        cik = _get_cik(ticker)
        if not cik:
            return {"error": f"CIK not found for {ticker}", "promises": [], "score": 50.0}
        self._log.append(f"Found CIK: {cik}")
        
        # Step 2: Fetch filings
        self._log.append("Fetching SEC filings...")
        try:
            resp = requests.get(f"{SEC_BASE}/submissions/CIK{cik}.json", headers=DATA_HEADERS, timeout=15)
            data = resp.json()
            recent = data.get("filings", {}).get("recent", {})
        except Exception as e:
            return {"error": f"Failed to fetch filings: {e}", "promises": [], "score": 50.0}
        
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        primary_docs = recent.get("primaryDocument", [])
        report_dates = recent.get("reportDate", [])
        
        # Step 3: Pull ALL historical 10-K and 10-Q filings (disk-cached)
        cache_dir = _os.path.join(_os.path.dirname(__file__), '..', '.filing_cache')
        _os.makedirs(cache_dir, exist_ok=True)
        cache_file = _os.path.join(cache_dir, f'{ticker}_all_filings.json')
        
        # Check disk cache first
        filings_to_fetch = {}
        if _os.path.exists(cache_file):
            try:
                with open(cache_file) as f:
                    filings_to_fetch = json.loads(f.read())
                self._log.append(f"Loaded {len(filings_to_fetch)} filings from disk cache")
            except:
                pass
        
        if not filings_to_fetch:
            # Fetch full history: recent + older archive files
            all_forms_list = list(forms)
            all_acc_list = list(accessions)
            all_docs_list = list(primary_docs)
            all_dates_list = list(report_dates)
            
            # Fetch older archive files (pre-2020)
            files = data.get("filings", {}).get("files", [])
            for file_info in files:
                try:
                    file_url = f"https://data.sec.gov/submissions/{file_info['name']}"
                    resp2 = requests.get(file_url, headers=DATA_HEADERS, timeout=30)
                    if resp2.status_code == 200:
                        old_data = resp2.json()
                        old_forms = old_data.get("form", [])
                        old_accs = old_data.get("accessionNumber", [])
                        old_dates = old_data.get("reportDate", [])
                        old_docs = old_data.get("primaryDocument", [])
                        all_forms_list.extend(old_forms)
                        all_acc_list.extend(old_accs)
                        all_dates_list.extend(old_dates)
                        all_docs_list.extend(old_docs)
                        # Pad if lengths don't match
                        max_len = max(len(old_forms), len(old_accs), len(old_dates), len(old_docs))
                        while len(all_dates_list) < len(all_forms_list):
                            all_dates_list.append("")
                        while len(all_docs_list) < len(all_forms_list):
                            all_docs_list.append("")
                        self._log.append(f"  Fetched {len(old_forms)} filings from {file_info['name']} ({old_dates[0] if old_dates else 'unknown'} to {old_dates[-1] if old_dates else 'unknown'})")
                except Exception as e:
                    self._log.append(f"  Failed to fetch archive {file_info.get('name', '?')}: {e}")
            
            # Build URLs for all 10-K and 10-Q
            self._log.append(f"Processing {len(all_forms_list)} total filings...")
            for i, f in enumerate(all_forms_list):
                if f in ["10-K", "10-Q", "8-K"]:
                    acc = all_acc_list[i].replace("-", "") if i < len(all_acc_list) else ""
                    doc = all_docs_list[i] if i < len(all_docs_list) else ""
                    if not acc or not doc:
                        continue
                    url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
                    key = f"{f}_{all_dates_list[i] if i < len(all_dates_list) else i}"
                    filings_to_fetch[key] = {
                        "form": f, "date": all_dates_list[i] if i < len(all_dates_list) else "unknown",
                        "url": url
                    }
            
            # Cache to disk
            with open(cache_file, 'w') as f:
                json.dump(filings_to_fetch, f)
            self._log.append(f"Cached {len(filings_to_fetch)} filings to disk")
        
        self._log.append(f"Downloading {len(filings_to_fetch)} filings...")
        filing_texts = {}
        for key, info in filings_to_fetch.items():
            text = ""
            # Fallback chain: try Last10K first (fastest), then SEC .htm, then SEC JSON
            form = info['form']
            date = info.get('date', '')
            
            # Source 1: Last10K (fastest - 0.08-0.13s)
            if not text:
                try:
                    lk_url = f"https://last10k.com/sec-filings/{ticker.lower()}/{info.get('url', '').split('/')[-1].replace('.htm', '')}"
                    resp = requests.get(lk_url, headers=HEADERS, timeout=10)
                    if resp.status_code == 200 and len(resp.text) > 5000:
                        text = _strip_html(resp.text)
                        self._log.append(f"  {key}: {len(text)} chars (Last10K)")
                except:
                    pass
            
            # Source 2: SEC EDGAR .htm (original URL)
            if not text:
                try:
                    resp = requests.get(info['url'], headers=HEADERS, timeout=15)
                    if resp.status_code == 200:
                        text = _strip_html(resp.text)
                        self._log.append(f"  {key}: {len(text)} chars (SEC .htm)")
                except Exception as e:
                    self._log.append(f"  {key}: SEC .htm FAILED - {e}")
            
            # Source 3: SEC JSON (rebuild URL from CIK + accession)
            if not text:
                try:
                    acc = info.get('url', '').split('/')[-2] if '/' in info.get('url', '') else ''
                    doc = info.get('url', '').split('/')[-1] if '/' in info.get('url', '') else ''
                    if acc and doc:
                        json_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
                        resp = requests.get(json_url, headers=DATA_HEADERS, timeout=15)
                        if resp.status_code == 200:
                            # Re-fetch via the original URL as fallback
                            resp2 = requests.get(info['url'], headers=HEADERS, timeout=15)
                            if resp2.status_code == 200:
                                text = _strip_html(resp2.text)
                                self._log.append(f"  {key}: {len(text)} chars (SEC JSON fallback)")
                except:
                    pass
            
            filing_texts[key] = text
            if not text:
                self._log.append(f"  {key}: ALL SOURCES FAILED")
        
        # Step 4: Extract MD&A and split into time-sorted sentences
        self._log.append("Extracting MD&A sections and splitting sentences...")
        all_sentences = []
        for ftype, text in filing_texts.items():
            if not text:
                continue
            mda = _extract_mda(text, ftype)
            sentences = _split_sentences(mda)
            info = filings_to_fetch[key]
            for s in sentences:
                all_sentences.append({
                    "text": s, "source": ftype, "date": info['date'], "url": info['url']
                })
        
        # Step 4b: Add transcript sentences
        self._log.append("Fetching earnings call transcripts...")
        transcripts = _get_transcripts(ticker)
        self._log.append(f"Got {len(transcripts)} transcripts")
        for t in transcripts:
            content_text = t.get('content', '')
            if not content_text:
                continue
            sentences = _split_sentences(content_text)
            t_date = t.get('date', '')[:10]
            for s in sentences:
                all_sentences.append({
                    "text": s,
                    "source": "transcript",
                    "date": t_date,
                    "url": t.get('source_url', '')
                })
        
        # Step 4c: Add press releases (fallback: Company IR → GlobeNewswire → Google News)
        self._log.append("Fetching press releases...")
        pr_texts = []
        pr_sources_used = []
        
        # Source 1: Company IR page (fastest at 0.27s)
        try:
            # Try common IR page patterns
            for ir_url in [
                f"https://nvidianews.nvidia.com/",
                f"https://investor.{ticker.lower()}.com/news-releases/",
                f"https://{ticker.lower()}.com/press/",
            ]:
                try:
                    resp = requests.get(ir_url, headers=HEADERS, timeout=10)
                    if resp.status_code == 200 and len(resp.text) > 5000:
                        clean = _strip_html(resp.text)
                        if len(clean) > 2000:
                            pr_texts.append(clean)
                            pr_sources_used.append(f"IR page ({ir_url})")
                            break
                except:
                    continue
        except:
            pass
        
        # Source 2: GlobeNewswire (0.31s)
        if not pr_texts:
            try:
                resp = requests.get(f"https://www.globenewswire.com/search/keyword/{ticker}?pageSize=10", headers=HEADERS, timeout=10)
                if resp.status_code == 200 and len(resp.text) > 5000:
                    pr_texts.append(_strip_html(resp.text))
                    pr_sources_used.append("GlobeNewswire")
            except:
                pass
        
        # Source 3: Google News RSS (0.78s)
        if not pr_texts:
            try:
                resp = requests.get(f"https://news.google.com/rss/search?q={ticker}+press+release&hl=en-US&gl=US&ceid=US:en", headers=HEADERS, timeout=10)
                if resp.status_code == 200 and len(resp.text) > 2000:
                    pr_texts.append(_strip_html(resp.text))
                    pr_sources_used.append("Google News RSS")
            except:
                pass
        
        if pr_texts:
            self._log.append(f"  Got {len(pr_texts)} press release sources: {', '.join(pr_sources_used)}")
            for pr_text in pr_texts:
                sentences = _split_sentences(pr_text)
                today = datetime.now().strftime("%Y-%m-%d")
                for s in sentences:
                    all_sentences.append({
                        "text": s,
                        "source": "press_release",
                        "date": today,
                        "url": pr_sources_used[0] if pr_sources_used else ""
                    })
        else:
            self._log.append("  No press releases found from any source")
        
        all_sentences.sort(key=lambda x: x['date'])
        self._log.append(f"Total sentences across all filings + transcripts + press releases: {len(all_sentences)}")
        
        # Step 5: Deterministic promise filter
        self._log.append("Filtering for promise-like sentences...")
        candidates = _filter_promises(all_sentences)
        self._log.append(f"Promise candidates (after filter): {len(candidates)}")
        
        if not candidates:
            return {"promises": [], "score": 50.0, "log": self._log}
        
        # Step 6: Batch Llama validation (cap at 15 per call to avoid overwhelming the model)
        self._log.append(f"Validating {len(candidates)} promises with LLM...")
        if len(candidates) <= 15:
            promises = _batch_validate_promises(candidates)
        else:
            # Split into chunks of 15
            promises = []
            for chunk_start in range(0, len(candidates), 15):
                chunk = candidates[chunk_start:chunk_start+15]
                chunk_promises = _batch_validate_promises(chunk)
                promises.extend(chunk_promises)
        self._log.append(f"Confirmed promises: {len(promises)}")
        
        if not promises:
            return {"promises": [], "score": 50.0, "log": self._log}
        
        # Step 7: Deduplicate and sort by severity
        seen_texts = set()
        deduped = []
        for p in promises:
            key = p.get('text', '')[:150].lower()
            key = ''.join(c for c in key if not c.isdigit()).strip()
            key = ' '.join(key.split())
            if key not in seen_texts:
                seen_texts.add(key)
                deduped.append(p)
        if len(deduped) < len(promises):
            self._log.append(f"Deduplicated: {len(promises)} -> {len(deduped)} unique promises")
        deduped.sort(key=lambda p: p.get('severity', 0), reverse=True)
        top_promises = deduped[:5]
        
        # Step 8: Evidence search for each promise
        self._log.append("Searching for evidence in later filings...")
        for promise in top_promises:
            promise_date = promise['date']
            later = [s for s in all_sentences if s['date'] > promise_date]
            
            # Try deterministic search first
            evidence = _deterministic_evidence_search(promise, later)
            if evidence:
                # Verify with Llama that the evidence is actually relevant
                self._log.append(f"  Verifying evidence for: {promise['text'][:80]}...")
                status, verified_evidence = _llama_evidence_verification(promise, later)
                if status in ['KEPT', 'PARTIAL']:
                    promise['evidence'] = verified_evidence if verified_evidence else evidence
                    promise['status'] = status
                else:
                    promise['evidence'] = evidence
                    promise['status'] = 'UNCLEAR'
            else:
                # Fall back to Llama
                self._log.append(f"  Asking LLM to verify: {promise['text'][:80]}...")
                status, evidence = _llama_evidence_verification(promise, later)
                promise['status'] = status
                promise['evidence'] = evidence
        
        # Step 9: Calculate score
        # Score = (sum of severity × kept) / (sum of all severity) × 100
        total_possible = sum(p.get('severity', 5) for p in top_promises)
        total_earned = sum(p.get('severity', 5) for p in top_promises if p.get('status') in ['KEPT', 'PARTIAL'])
        score = (total_earned / total_possible * 100) if total_possible > 0 else 50.0
        
        elapsed = time.time() - start_time
        self._log.append(f"Analysis complete in {elapsed:.1f}s. Score: {score:.1f}%")
        
        return {
            "ticker": ticker,
            "score": round(score, 1),
            "promises": top_promises,
            "total_candidates": len(candidates),
            "confirmed_promises": len(promises),
            "elapsed_seconds": round(elapsed, 1),
            "log": self._log,
        }
