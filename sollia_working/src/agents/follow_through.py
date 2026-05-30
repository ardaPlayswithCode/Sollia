"""Agent 3 — Management Follow-Through: Tracks CEO promises across filings, verifies if they were kept."""
from __future__ import annotations

import json
import logging
import os as _os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

SEC_BASE = "https://data.sec.gov"
EDGAR_BASE = "https://www.sec.gov"
HEADERS = {"User-Agent": "Sollia Analysis contact@sollia.app", "Accept-Encoding": "gzip, deflate"}
DATA_HEADERS = {"User-Agent": "Sollia Analysis contact@sollia.app", "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}

# Groq client (lazy init)
_groq_client = None

def _get_groq_client():
    """Lazy-init Groq client from official package if available, else use requests."""
    global _groq_client
    if _groq_client is not None:
        return _groq_client
    try:
        from groq import Groq
        _groq_client = Groq(api_key=_os.environ.get("GROQ_API_KEY", ""))
        return _groq_client
    except ImportError:
        _groq_client = False
        return False


def _ask_groq(prompt: str, timeout: int = 15) -> str:
    """Call Groq API for LLM inference."""
    api_key = _os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        logger.error("GROQ_API_KEY not set")
        return ""
    
    client = _get_groq_client()
    if client:
        try:
            resp = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0, max_tokens=500, timeout=timeout,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.warning("Groq client failed, falling back to requests: %s", e)
    
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": prompt}], "temperature": 0.0, "max_tokens": 500},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        else:
            logger.error("Groq API error: %s %s", resp.status_code, resp.text[:200])
    except Exception as e:
        logger.error("Groq call failed: %s", e)
    return ""


_ask_ollama = _ask_groq
import threading
_rate_lock = threading.Lock()
_last_groq_call = 0.0
_MIN_GROQ_INTERVAL = 3.0  # seconds between Groq calls (6000 TPM, need buffer for large prompts) (6000 TPM limit = ~100 tokens/sec, 2s buffer)

def _rate_limited_groq(prompt: str, timeout: int = 15) -> str:
    """Rate-limited wrapper around _ask_groq to avoid 429 errors."""
    global _last_groq_call
    with _rate_lock:
        elapsed = time.time() - _last_groq_call
        if elapsed < _MIN_GROQ_INTERVAL:
            time.sleep(_MIN_GROQ_INTERVAL - elapsed)
        _last_groq_call = time.time()
    return _ask_groq(prompt, timeout)



def _strip_html(html: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _extract_mda(text: str, form_type: str = "") -> str:
    """Extract MD&A section. For 8-Ks, returns full text (no MD&A section exists)."""
    if not text:
        return ""
    # 8-Ks are event-driven, no MD&A — return the full cleaned text
    if form_type == "8-K":
        return text[:50000]
    patterns = [
        r"(?:Item\s*7[\.:]|ITEM\s*7[\.:])\s*(?:Management.s\s*Discussion|MANAGEMENT.S\s*DISCUSSION)(.*?)(?:Item\s*7A|ITEM\s*7A|Item\s*8|ITEM\s*8)",
        r"(?:Item\s*2[\.:]|ITEM\s*2[\.:])\s*(?:Management.s\s*Discussion|MANAGEMENT.S\s*DISCUSSION)(.*?)(?:Item\s*3|ITEM\s*3)",
        r"Management.s\s*Discussion\s*(?:and|&)\s*Analysis(.*?)(?:Quantitative|Liquidity|Risk\s*Factors)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1)[:50000]
    if len(text) > 10000:
        return text[len(text)//4:len(text)//2]
    return text[:50000]

def _split_sentences(text: str) -> List[str]:
    if not text:
        return []
    sentences = re.split(r'(?<=[.!?])\s+', text)
    return [s.strip() for s in sentences if len(s.strip()) > 20]


def _filter_promises(sentences: List[Dict]) -> List[Dict]:
    pos_keywords = [
        'will', 'plan to', 'expect to', 'target', 'commit', 'goal', 'objective',
        'project', 'forecast', 'guidance', 'aim to', 'intend to', 'shall',
        'on track to', 'roadmap', 'pipeline', 'initiative'
    ]
    neg_keywords = ['may', 'might', 'could', 'possibly', 'potential', 'believe', 'think', 'hope']
    metric_keywords = [
        'revenue', 'growth', 'margin', 'profit', 'eps', 'earnings per share',
        'billion', 'million', 'percent', '%', 'dollar', '$',
        'launch', 'expand', 'reduce', 'increase', 'decrease', 'open', 'close',
        'hire', 'acquire', 'divest', 'buyback', 'repurchase', 'dividend',
        'store', 'location', 'customer', 'subscriber', 'user',
        'product', 'service', 'market', 'share', 'reduce cost', 'cut',
        'carbon neutral', 'emission', 'sustainability'
    ]
    candidates = []
    for s in sentences:
        text = s['text'].lower()
        has_pos = any(kw in text for kw in pos_keywords)
        has_neg = any(kw in text for kw in neg_keywords)
        has_metric = any(kw in text for kw in metric_keywords)
        is_accounting = any(kw in text for kw in [
            'accounting standard', 'asc ', 'fasb', 'gaap', 'stock-based comp',
            'amortization period', 'unearned', 'provisional amount', 'valuation allowance',
            'adopt this standard', 'adopt this guidance', 'accounting pronouncement',
            'adopt the standard', 'adopt the guidance', 'accounting standards update',
            'unrecognized tax benefit', 'tax authority', 'deferred tax asset',
            'based compensation expense', 'restricted stock unit',
        ])
        is_compensation = any(kw in text for kw in [
            'compensation committee', 'base salary', 'annual bonus', 'equity award',
            'restricted stock', 'stock option', 'severance', 'employment agreement',
            'consulting period', 'lump sum payment', 'per month during',
            'paid $', 'will be paid', 'will receive $', 'dividend', 'per share',
            'board of directors', 'bonus target', 'equity grant',
            'option grant', 'rsu grant', 'performance share', 'long-term incentive',
            'named executive', 'executive officer', 'say-on-pay',
            'signing bonus', 'option to purchase', 'exercise price',
            'fair market value', 'shares of common stock', 'commencement of',
            'payable in', 'equal quarterly installments', 'equal monthly installments',
        ])
        if has_pos and not has_neg and has_metric and not is_accounting and not is_compensation:
            candidates.append(s)
    return candidates


def _batch_validate_promises(candidates: List[Dict]) -> List[Dict]:
    if not candidates:
        return []
    batch_text = "\n\n".join([f"[{i}] {s['text'][:150]}" for i, s in enumerate(candidates)])
    prompt = """Analyze these sentences from company filings. For each one, determine if it's a specific, measurable business promise.

A promise must have a specific number, timeline, or measurable outcome. Vague statements like "we will continue to invest" are NOT promises.

You MUST return ONLY a valid JSON array. Each object MUST have ALL four fields: id, is_promise, severity (1-10), and metric (short description of what to check). Example: [{"id": 0, "is_promise": true, "severity": 7, "metric": "revenue target"}, {"id": 1, "is_promise": false, "severity": 0, "metric": "none"}]

Sentences:
""" + batch_text

    response = _rate_limited_groq(prompt)
    if not response:
        return []
    try:
        start = response.find('[')
        if start < 0:
            return []
        raw = response[start:]
        raw = re.sub(r'\[\d+[A-Za-z]', ' ', raw)
        raw = re.sub(r'\[[A-Za-z]', ' ', raw)
        raw = re.sub(r'[^a-zA-Z0-9\s{}\[\]":,\-.]', ' ', raw)
        raw = re.sub(r'\s+', ' ', raw)
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
                    obj_str = obj_str.replace('"is promise"', '"is_promise"')
                    obj_str = re.sub(r'([a-z])" "([a-z])', r'\1_\2', obj_str)
                    try:
                        obj = json.loads(obj_str)
                        if isinstance(obj, dict):
                            results.append(obj)
                    except:
                        pass
                    obj_start = -1
        if not results:
            last_valid = raw.rfind('},')
            if last_valid >= 0:
                raw = raw[:last_valid+1] + ']'
            elif not raw.endswith(']'):
                raw = raw + ']'
            results = json.loads(raw)
    except Exception as e:
        logger.error("Failed to parse Llama response: %s", e)
        return []
    
    validated = []
    for r in results:
        if not isinstance(r, dict):
            continue
        idx = r.get('id', -1)
        if 0 <= idx < len(candidates) and r.get('is_promise'):
            c = candidates[idx].copy()
            c['severity'] = r.get('severity') or 5
            c['metric'] = str(r.get('metric', 'none'))
            validated.append(c)
    
    if not validated and len(candidates) > 0:
        logger.info("First attempt found 0 promises. Retrying...")
        retry_text = "\n".join([f"[{i}] {s['text'][:150]}" for i, s in enumerate(candidates[:5])])
        retry_prompt = "For each sentence, is it a promise? Reply JSON: [{\"id\":0,\"promise\":true,\"severity\":5}, ...]\n\n" + retry_text
        response2 = _rate_limited_groq(retry_prompt, timeout=15)
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
                                c['severity'] = r.get('severity') or 5
                                c['metric'] = 'none'
                                validated.append(c)
            except:
                pass
    return validated


def _batch_verify_evidence(promises: List[Dict], later_sentences: List[Dict]) -> None:
    if not promises or not later_sentences:
        return
    later_context = " ".join([s['text'] for s in later_sentences[:50]])[:4000]
    promises_text = "\n\n".join([
        f"[{i}] PROMISE: \"{p['text'][:300]}\" (Metric: {p.get('metric', 'none')})"
        for i, p in enumerate(promises)
    ])
    prompt = f"""A company made these statements in earlier filings. Here are excerpts from LATER filings:

LATER FILINGS EXCERPTS:
{later_context}

EARLIER STATEMENTS TO VERIFY:
{promises_text}

For EACH statement above, determine if it was KEPT, BROKEN, PARTIAL, or UNCLEAR.
Reply ONLY with a JSON array: [{{"id": 0, "status": "KEPT", "evidence": "brief quote or explanation"}}, ...]"""

    response = _rate_limited_groq(prompt, timeout=30)
    if not response:
        return
    try:
        start = response.find('[')
        if start < 0:
            return
        raw = response[start:]
        raw = re.sub(r'[^a-zA-Z0-9\s{}\[\]":,\-.]', ' ', raw)
        raw = re.sub(r'\s+', ' ', raw)
        last = raw.rfind('},')
        if last >= 0:
            raw = raw[:last+1] + ']'
        elif not raw.endswith(']'):
            raw += ']'
        results = json.loads(raw)
        for r in results:
            if isinstance(r, dict):
                idx = r.get('id', -1)
                if 0 <= idx < len(promises):
                    status = r.get('status', 'UNCLEAR')
                    evidence = r.get('evidence', '')
                    if status in ('KEPT', 'PARTIAL'):
                        promises[idx]['status'] = 'EVIDENCE_FOUND'
                        promises[idx]['evidence'] = evidence
                    elif status == 'BROKEN':
                        promises[idx]['status'] = 'BROKEN'
                        promises[idx]['evidence'] = evidence
    except Exception as e:
        logger.error("Failed to parse evidence verification: %s", e)


def _deterministic_evidence_search(promise: Dict, later_sentences: List[Dict]) -> Optional[str]:
    metric = promise.get('metric', '')
    text_to_search = " ".join([s['text'] for s in later_sentences[:500]])
    keywords = re.findall(r'\b\w+\b', str(metric).lower())
    keywords = [k for k in keywords if len(k) > 3 and k not in ['what', 'none', 'that', 'this', 'with', 'from', 'they', 'have', 'were', 'been']]
    if not keywords:
        return None
    pattern = '|'.join(keywords[:5])
    try:
        search_regex = re.compile(r'.{0,200}(' + pattern + r').{0,200}', re.IGNORECASE)
        matches = search_regex.findall(text_to_search)
        if matches:
            return matches[0][:500]
    except:
        pass
    return None


def _get_cik(ticker: str) -> Optional[str]:
    try:
        resp = requests.get("https://www.sec.gov/files/company_tickers.json", headers=HEADERS, timeout=15)
        for item in resp.json().values():
            if str(item.get("ticker", "")).upper() == ticker.upper():
                return str(item["cik_str"]).zfill(10)
    except Exception as e:
        logger.error("CIK lookup failed: %s", e)
    return None


def _get_transcripts(ticker: str) -> List[Dict]:
    return []


class FollowThroughAgent:
    """Agent 3 — tracks management promises and verifies follow-through."""
    
    def __init__(self):
        self._log: List[str] = []
    
    def run(self, ticker: str) -> Dict:
        ticker = ticker.upper()
        self._log = []
        start_time = time.time()
        
        self._log.append(f"Looking up CIK for {ticker}...")
        cik = _get_cik(ticker)
        if not cik:
            return {"error": f"CIK not found for {ticker}", "promises": [], "score": 50.0}
        self._log.append(f"Found CIK: {cik}")
        
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
        
        cache_dir = _os.path.join(_os.path.dirname(__file__), '..', '..', '.filing_cache')
        _os.makedirs(cache_dir, exist_ok=True)
        cache_file = _os.path.join(cache_dir, f'{ticker}_all_filings.json')
        
        filings_to_fetch = {}
        if _os.path.exists(cache_file):
            try:
                with open(cache_file) as f:
                    filings_to_fetch = json.loads(f.read())
                self._log.append(f"Loaded {len(filings_to_fetch)} filings from disk cache")
            except:
                pass
        
        if not filings_to_fetch:
            all_forms_list = list(forms)
            all_items_list = list(recent.get("items", [""] * len(all_forms_list)))
            all_acc_list = list(accessions)
            all_docs_list = list(primary_docs)
            all_dates_list = list(report_dates)
            
            files = data.get("filings", {}).get("files", [])
            for file_info in files:
                try:
                    file_url = f"https://data.sec.gov/submissions/{file_info['name']}"
                    resp2 = requests.get(file_url, headers=DATA_HEADERS, timeout=30)
                    if resp2.status_code == 200:
                        old_data = resp2.json()
                        all_forms_list.extend(old_data.get("form", []))
                        all_items_list.extend(old_data.get("items", [""] * len(old_data.get("form", []))))
                        all_acc_list.extend(old_data.get("accessionNumber", []))
                        all_dates_list.extend(old_data.get("reportDate", []))
                        all_docs_list.extend(old_data.get("primaryDocument", []))
                        while len(all_dates_list) < len(all_forms_list):
                            all_dates_list.append("")
                        while len(all_docs_list) < len(all_forms_list):
                            all_docs_list.append("")
                        self._log.append(f"  Fetched archive filings")
                except Exception as e:
                    self._log.append(f"  Archive fetch failed: {e}")
            
            for i, f in enumerate(all_forms_list):
                # Only include 8-Ks with promise-relevant items (agreements, earnings, leadership)
                if f in ["10-K", "10-Q"] or (f == "8-K" and all_items_list[i] and any(
                    code in all_items_list[i] for code in ["1.01", "2.02", "5.02"])):
                    acc = all_acc_list[i].replace("-", "") if i < len(all_acc_list) else ""
                    doc = all_docs_list[i] if i < len(all_docs_list) else ""
                    if not acc or not doc:
                        continue
                    url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc}/{doc}"
                    key = f"{f}_{all_dates_list[i] if i < len(all_dates_list) else i}"
                    filings_to_fetch[key] = {"form": f, "date": all_dates_list[i] if i < len(all_dates_list) else "unknown", "url": url}
            
            with open(cache_file, 'w') as f:
                json.dump(filings_to_fetch, f)
            self._log.append(f"Cached {len(filings_to_fetch)} filings to disk")
        
        self._log.append(f"Downloading {len(filings_to_fetch)} filings...")
        text_cache_dir = _os.path.join(_os.path.dirname(__file__), '..', '..', '.filing_text_cache')
        _os.makedirs(text_cache_dir, exist_ok=True)
        text_cache_file = _os.path.join(text_cache_dir, f"{ticker}_texts.json")
        
        filing_texts = {}
        if _os.path.exists(text_cache_file):
            try:
                with open(text_cache_file) as f:
                    filing_texts = json.loads(f.read())
                self._log.append(f"Loaded {len(filing_texts)} filing texts from cache")
            except:
                pass
        
        uncached = {k: v for k, v in filings_to_fetch.items() if k not in filing_texts}
        if uncached:
            self._log.append(f"  {len(uncached)} uncached filings to download...")
            
            def _download_one(key_info):
                key, info = key_info
                text = ""
                try:
                    doc_id = info.get('url', '').split('/')[-1].replace('.htm', '')
                    lk_url = f"https://last10k.com/sec-filings/{ticker.lower()}/{doc_id}"
                    resp = requests.get(lk_url, headers=HEADERS, timeout=10)
                    if resp.status_code == 200 and len(resp.text) > 5000:
                        return key, _strip_html(resp.text)
                except:
                    pass
                try:
                    resp = requests.get(info['url'], headers=HEADERS, timeout=15)
                    if resp.status_code == 200:
                        return key, _strip_html(resp.text)
                except:
                    pass
                return key, ""
            
            with ThreadPoolExecutor(max_workers=8) as ex:
                futures = {ex.submit(_download_one, (k, v)): k for k, v in uncached.items()}
                for future in as_completed(futures):
                    try:
                        key, text = future.result()
                        filing_texts[key] = text
                    except:
                        pass
            
            try:
                with open(text_cache_file, 'w') as f:
                    json.dump(filing_texts, f)
            except:
                pass
        
        self._log.append("Extracting MD&A sections...")
        all_sentences = []
        for key, text in filing_texts.items():
            if not text:
                continue
            ftype = key.split('_')[0] if '_' in key else 'unknown'
            mda = _extract_mda(text, ftype)
            sentences = _split_sentences(mda)
            info = filings_to_fetch.get(key, {})
            for s in sentences:
                all_sentences.append({"text": s, "source": ftype, "date": info.get('date', ''), "url": info.get('url', '')})
        
        all_sentences.sort(key=lambda x: x['date'])
        self._log.append(f"Total sentences: {len(all_sentences)}")
        
        self._log.append("Filtering for promise-like sentences...")
        candidates = _filter_promises(all_sentences)
        self._log.append(f"Promise candidates: {len(candidates)}")
        
        if not candidates:
            return {"promises": [], "score": 50.0, "log": self._log}
        
        self._log.append(f"Validating {len(candidates)} promises with LLM...")
        if len(candidates) <= 15:
            promises = _batch_validate_promises(candidates)
        else:
            chunks = [candidates[i:i+15] for i in range(0, len(candidates), 15)]
            promises = []
            with ThreadPoolExecutor(max_workers=min(len(chunks), 8)) as ex:
                futures = {ex.submit(_batch_validate_promises, chunk): chunk for chunk in chunks}
                for future in as_completed(futures):
                    promises.extend(future.result())
        self._log.append(f"Confirmed promises: {len(promises)}")
        
        if not promises:
            return {"promises": [], "score": 50.0, "log": self._log}
        
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
            self._log.append(f"Deduplicated: {len(promises)} -> {len(deduped)}")
        deduped.sort(key=lambda p: p.get('severity') or 0, reverse=True)
        top_promises = deduped[:5]
        
        self._log.append("Verifying evidence...")
        latest_date = max((p['date'] for p in top_promises if p.get('date')), default="")
        later_keys = sorted(
            [k for k in filing_texts.keys()
             if filing_texts[k] and k.split('_')[0] in ['10-Q', '10-K']
             and k.split('_')[-1] > latest_date],
            key=lambda k: k.split('_')[-1]
        )[:3]
        
        later_sentences = []
        for key in later_keys:
            ftype = key.split('_')[0]
            mda = _extract_mda(filing_texts[key], ftype)
            for s in _split_sentences(mda):
                later_sentences.append({"text": s, "source": key, "date": key.split('_')[-1] if '_' in key else ''})
        
        if later_sentences:
            _batch_verify_evidence(top_promises, later_sentences)
        
        for promise in top_promises:
            if promise.get('status') not in ('EVIDENCE_FOUND', 'BROKEN'):
                evidence = _deterministic_evidence_search(promise, later_sentences)
                if evidence:
                    promise['evidence'] = evidence
                    promise['status'] = 'EVIDENCE_FOUND'
                else:
                    promise['status'] = 'NO_EVIDENCE'
                    promise['evidence'] = None
        
        total_possible = sum(p.get('severity', 5) for p in top_promises)
        total_earned = sum(p.get('severity', 5) for p in top_promises if p.get('status') == 'EVIDENCE_FOUND')
        score = (total_earned / total_possible * 100) if total_possible > 0 else 50.0
        
        elapsed = time.time() - start_time
        self._log.append(f"Complete in {elapsed:.1f}s. Score: {score:.1f}%")
        
        return {
            "ticker": ticker,
            "score": round(score, 1),
            "promises": top_promises,
            "total_candidates": len(candidates),
            "confirmed_promises": len(promises),
            "elapsed_seconds": round(elapsed, 1),
            "log": self._log,
        }
