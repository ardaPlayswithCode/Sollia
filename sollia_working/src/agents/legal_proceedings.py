"""
Agent 3 — Feature 1: Legal Proceedings Tracker
Uses universal cache and shared LLM config
"""

from __future__ import annotations
import logging
import json
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import sys
import os
import requests
import time
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.llm_config import call_llm

logger = logging.getLogger(__name__)

# Module-level CIK cache — avoids re-downloading company_tickers.json on every call.
# get_filings() invokes get_cik() once per doc_type (3x), plus once in track() = 4x/request.
_CIK_CACHE: Dict[str, str] = {}


def _dedup_proceedings(proceedings: List[Dict]) -> List[Dict]:
    """Merge legal proceedings/events that cite the SAME passage.

    The identity of a legal matter is the QUOTE it cites, NOT its label. Keying
    on case_name caused two opposite failures:

      * Under-merge (duplicate 8-K finding): a single 8-K passage is often
        extracted under several different item headers — the BlueOval SK DOE Loan
        appears as "Entry into a Material Definitive Agreement", "Creation of a
        Direct Financial Obligation" AND "Other Events", all quoting the same
        sentence. Different case_names => kept as 3 duplicates.
      * Over-merge (lost sub-findings): several DISTINCT disclosures share one
        case_name — Ford lists multiple "Asbestos Litigation" passages. Same
        case_name => the richer entry evicted the others, dropping e.g. "Annual
        payout and defense costs may become significant in the future."

    Keying on the (normalized) quote fixes both: identical quotes collapse to one
    matter; distinct quotes are preserved. Quote-less entries fall back to their
    case label so genuinely empty duplicates still collapse, but a substantial
    quote is NEVER merged across two different passages. When two entries share a
    key the richer one (more of prior_quote / current_quote / amount) is kept.
    """
    if not isinstance(proceedings, list):
        return proceedings

    def _norm(value) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    def _key(item):
        if not isinstance(item, dict):
            return None
        # A substantial cited passage is the strongest identity signal.
        for field in ("current_quote", "prior_quote"):
            q = _norm(item.get(field))
            if len(q) >= 30:
                return ("quote", q[:160])
        # No usable quote — fall back to the case/event label.
        label = _norm(item.get("case_name") or item.get("description"))
        return ("label", label) if label else None

    def _richness(item):
        score = 0
        for field in ("prior_quote", "current_quote", "amount"):
            v = item.get(field) if isinstance(item, dict) else None
            if v and str(v).strip().lower() not in ("", "null", "none"):
                score += 1
        return score

    result: List[Dict] = []
    index_by_key: Dict[tuple, int] = {}
    for item in proceedings:
        k = _key(item)
        if k is None:
            result.append(item)
            continue
        if k in index_by_key:
            existing = result[index_by_key[k]]
            if _richness(item) > _richness(existing):
                result[index_by_key[k]] = item  # replace with the richer duplicate
        else:
            index_by_key[k] = len(result)
            result.append(item)
    return result


class LegalProceedingsTracker:
    def __init__(self):
        self.cache_root = "/Users/ardaturgut/Desktop/Desktop - Arda’s Mac Studio/Sollia/sollia_working/.cache"
        os.makedirs(f"{self.cache_root}/filings", exist_ok=True)
        os.makedirs(f"{self.cache_root}/texts", exist_ok=True)

    def get_cik(self, ticker: str) -> Optional[str]:
        key = ticker.upper()
        if key in _CIK_CACHE:
            return _CIK_CACHE[key]
        try:
            url = "https://www.sec.gov/files/company_tickers.json"
            resp = requests.get(url, headers={"User-Agent": "Sollia contact@sollia.app"})
            data = resp.json()
            for item in data.values():
                if item.get("ticker", "").upper() == key:
                    cik = str(item["cik_str"]).zfill(10)
                    _CIK_CACHE[key] = cik
                    return cik
        except:
            pass
        return None

    def get_filings(self, ticker: str, form_type: str) -> List[Dict]:
        cik = self.get_cik(ticker)
        if not cik:
            return []

        cache_file = f"{self.cache_root}/filings/{ticker}_filings.json"
        filings_data = None

        if os.path.exists(cache_file):
            with open(cache_file, 'r') as f:
                filings_data = json.load(f)

        if not filings_data:
            url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            resp = requests.get(url, headers={"User-Agent": "Sollia contact@sollia.app", "Host": "data.sec.gov"})
            if resp.status_code == 200:
                filings_data = resp.json()
                # Also fetch historical archives
                for file_info in filings_data.get("filings", {}).get("files", []):
                    try:
                        time.sleep(0.1)
                        r = requests.get(f"https://data.sec.gov/submissions/{file_info['name']}", 
                                        headers={"User-Agent": "Sollia contact@sollia.app", "Host": "data.sec.gov"}, timeout=30)
                        if r.status_code == 200:
                            old = r.json()
                            for key in ["form", "accessionNumber", "filingDate", "reportDate", "items", "primaryDocument"]:
                                if key in old and key in filings_data["filings"]["recent"]:
                                    filings_data["filings"]["recent"][key].extend(old.get(key, []))
                    except Exception:
                        pass
                with open(cache_file, 'w') as f:
                    json.dump(filings_data, f)

        if not filings_data:
            return []

        filings = []
        recent = filings_data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        accessions = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])

        for i in range(len(forms)):
            if forms[i] == form_type:
                    filings.append({
                        "accession": accessions[i] if i < len(accessions) else None,
                        "filing_date": dates[i] if i < len(dates) else None,
                        "cik": cik,
                        "ticker": ticker,
                        "primary_document": primary_docs[i] if i < len(primary_docs) else None
                    })

        filings.sort(key=lambda x: x["filing_date"], reverse=True)
        return filings

    def get_filing_text(self, filing: Dict) -> Optional[str]:
        if not filing or not filing.get("accession"):
            return None

        cache_file = f"{self.cache_root}/texts/{filing['ticker']}_texts.json"

        if os.path.exists(cache_file):
            with open(cache_file, 'r') as f:
                texts = json.load(f)
                if filing["accession"] in texts:
                    return texts[filing["accession"]]

        acc_clean = filing["accession"].replace("-", "")
        cik_int = int(filing['cik'])

        index_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{filing['accession']}-index.htm"

        try:
            resp = requests.get(index_url, headers={"User-Agent": "Sollia contact@sollia.app"}, timeout=30)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, 'html.parser')

                doc_url = None
                for link in soup.find_all('a'):
                    href = link.get('href', '')
                    if href and 'ix?doc=' in href:
                        parsed = urlparse(href)
                        params = parse_qs(parsed.query)
                        if 'doc' in params:
                            doc_url = params['doc'][0]
                            break
                    elif href and href.endswith('.htm') and not href.endswith('-index.htm'):
                        doc_url = href

                if doc_url:
                    if doc_url.startswith('/'):
                        full_url = f"https://www.sec.gov{doc_url}"
                    else:
                        full_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}/{doc_url}"

                    doc_resp = requests.get(full_url, headers={"User-Agent": "Sollia contact@sollia.app"}, timeout=60)
                    if doc_resp.status_code == 200:
                        text = doc_resp.text

                        if os.path.exists(cache_file):
                            with open(cache_file, 'r') as f:
                                texts = json.load(f)
                        else:
                            texts = {}
                        texts[filing["accession"]] = text
                        # Atomic write — Agent 1 and Agent 3 share this texts cache
                        # file and may run concurrently (WS6).
                        tmp = "%s.%d.tmp" % (cache_file, os.getpid())
                        with open(tmp, 'w') as f:
                            json.dump(texts, f)
                        os.replace(tmp, cache_file)
                        return text
        except:
            pass
        return None

    def extract_legal_section(self, text: str, doc_type: str) -> Optional[str]:
        if not text:
            return None

        def clean(raw):
            if not raw:
                return ""
            raw = re.sub(r'<[^>]+>', ' ', raw)
            raw = re.sub(r'\s+', ' ', raw)
            return raw.strip()

        if doc_type == "10-K":
            patterns = [
                r'Item\s+3\.\s+Legal\s+Proceedings.*?<div[^>]*>(.*?)</div>',
                r'Item\s+3\.\s+Legal\s+Proceedings(.*?)Item\s+4\.',
            ]
            for pattern in patterns:
                match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
                if match:
                    cleaned = clean(match.group(1))
                    if len(cleaned) > 50:
                        return cleaned[:5000]
            return None

        elif doc_type == "10-Q":
            patterns = [
                r'PART\s+II.*?Item\s+1\.\s+Legal\s+Proceedings\.?(.*?)(?=Item\s+2\.)',
                r'Item\s+1\.\s+Legal\s+Proceedings\.?(.*?)(?=Item\s+1A\.|Item\s+2\.)',
            ]
            for pattern in patterns:
                match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
                if match:
                    cleaned = clean(match.group(1))
                    if cleaned:
                        return cleaned[:3000]
            return None

        else:
            # 8-K - extract body from XML if needed
            if text.strip().startswith('<?xml'):
                body_match = re.search(r'<body[^>]*>(.*?)</body>', text, re.DOTALL | re.IGNORECASE)
                if body_match:
                    text = body_match.group(1)
                text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text)
            return text[:5000]

    def _is_substantive(self, text: str) -> bool:
        if not text or len(text) < 30:
            return False
        text_lower = text.lower()
        if re.search(r'see\s+note|refer\s+to|incorporated by reference', text_lower):
            return False
        legal_words = ['lawsuit', 'litigation', 'asbestos', 'patent', 'v.', 'vs.', 'settlement', 'plaintiff', 'defendant']
        return any(word in text_lower for word in legal_words)

    def _calculate_score(self, proceedings):
        """Calculate a score from 0-100 based on proceedings status changes."""
        if not proceedings:
            return 50  # neutral
        
        total = 0
        for p in proceedings:
            status = p.get('status', '')
            if status == 'ESCALATED':
                total -= 2
            elif status == 'NEW':
                total -= 1
            elif status == 'SETTLED':
                total += 1
            elif status == 'DISMISSED':
                total += 2
            elif status == 'QUIETLY_REMOVED':
                total -= 1
            # UNCHANGED adds 0
        # Scale from -10..10 to 0..100 (assuming max 5 events each -2 = -10)
        score = 50 + (total * 5)
        return max(0, min(100, score))

    def track(self, ticker: str, progress=None) -> Dict:
        cik = self.get_cik(ticker)
        results = {"ticker": ticker, "timestamp": datetime.now().isoformat(), "findings": []}

        def _emit(msg, kind="info"):
            if progress:
                try:
                    progress(msg, kind)
                except Exception:
                    pass

        _emit("Scanning legal proceedings across 10-K, 10-Q, and 8-K filings...", "search")

        def _empty_finding(dt, prv, cur, reason):
            """WS3: a placeholder finding so a doc type with no legal content is
            shown explicitly ('No legal proceedings in 10-Q') instead of omitted."""
            return {
                "doc_type": dt,
                "prior_date": (prv or {}).get("filing_date", ""),
                "current_date": (cur or {}).get("filing_date", ""),
                "proceedings": [],
                "summary": reason,
                "agent3_score": 50,
                "empty": True,
                "cik": cik,
                "current_accession": (cur or {}).get("accession", ""),
                "current_primary_doc": (cur or {}).get("primary_document", ""),
                "prior_accession": (prv or {}).get("accession", ""),
                "prior_primary_doc": (prv or {}).get("primary_document", ""),
            }

        for doc_type in ["10-K", "10-Q", "8-K"]:
            filings = self.get_filings(ticker, doc_type)
            _emit("Reviewing %s legal disclosures for changes..." % doc_type, "analyze")
            if len(filings) >= 2:
                curr = filings[0]
                prev = filings[1]

                curr_raw = self.get_filing_text(curr)
                prev_raw = self.get_filing_text(prev)

                if curr_raw and prev_raw:
                    curr_text = self.extract_legal_section(curr_raw, doc_type)
                    prev_text = self.extract_legal_section(prev_raw, doc_type)

                    if curr_text and prev_text:
                        # Build specialized prompt
                        if doc_type == "10-K":
                            prompt = f"""Compare legal proceedings in 10-K filings. Be concise. Only output JSON.

PRIOR ({prev['filing_date']}):
{prev_text[:4000]}

CURRENT ({curr['filing_date']}):
{curr_text[:4000]}

Output ONLY valid JSON. Do NOT include ANY text before or after the JSON. Do NOT use markdown code blocks. Do NOT write Python code. Do NOT explain. Return ONLY the JSON object, nothing else:
{{
  "proceedings": [
    {{
      "case_name": "string",
      "prior_quote": "exact text from prior or null",
      "current_quote": "exact text from current",
      "status": "NEW|ESCALATED|SETTLED|DISMISSED|UNCHANGED|QUIETLY_REMOVED",
      "liability_change": "string or null"
    }}
  ],
  "summary": "brief overview"
}}"""

                        elif doc_type == "10-Q":
                            prompt = f"""Compare legal proceedings in 10-Q filings. Be concise. Only output JSON.

PRIOR ({prev['filing_date']}):
{prev_text[:3000]}

CURRENT ({curr['filing_date']}):
{curr_text[:3000]}

Output ONLY valid JSON. Do NOT include ANY text before or after the JSON. Do NOT use markdown code blocks. Do NOT write Python code. Do NOT explain. Return ONLY the JSON object, nothing else:
{{
  "proceedings": [
    {{
      "case_name": "string or null",
      "prior_quote": "exact text or null",
      "current_quote": "exact text or null",
      "status": "NEW|ESCALATED|SETTLED|NO_UPDATE"
    }}
  ],
  "has_update": true/false,
  "summary": "one sentence"
}}"""

                        else:
                            prompt = f"""Compare legal events in 8-K filings.

PRIOR ({prev['filing_date']}):
{prev_text[:4000]}

CURRENT ({curr['filing_date']}):
{curr_text[:4000]}

Output ONLY valid JSON. Do NOT include ANY text before or after the JSON. Do NOT use markdown code blocks. Do NOT write Python code. Do NOT explain. Return ONLY the JSON object, nothing else:
{{
  "events": [
    {{
      "item": "1.01|1.03|8.01",
      "description": "brief description",
      "prior_quote": "exact text or null",
      "current_quote": "exact text",
      "counterparties": ["list"],
      "amount": "dollar amount or null",
      "status": "NEW|AMENDED|TERMINATED"
    }}
  ],
  "summary": "brief overview"
}}"""

                        response, _ = call_llm(prompt, max_tokens=2000)
                        try:
                            cleaned = response.strip()
                            # Remove markdown
                            if cleaned.startswith('```json'):
                                cleaned = cleaned[7:]
                            if cleaned.startswith('```'):
                                cleaned = cleaned[3:]
                            if cleaned.endswith('```'):
                                cleaned = cleaned[:-3]
                            cleaned = cleaned.strip()
                            
                            data = json.loads(cleaned)
                            # Extract proceedings list (for 10-K/10-Q) or events (for 8-K)
                            proceedings_list = data.get('proceedings', data.get('events', []))
                            # Merge duplicate findings that describe the same case/event.
                            proceedings_list = _dedup_proceedings(proceedings_list)
                            # Add score
                            score = self._calculate_score(proceedings_list)
                            # Store in findings with score
                            results["findings"].append({
                                "doc_type": doc_type,
                                "prior_date": prev['filing_date'],
                                "current_date": curr['filing_date'],
                                "proceedings": proceedings_list,
                                "summary": data.get('summary', ''),
                                "agent3_score": score,
                                "current_accession": curr.get("accession", ""),
                                "current_primary_doc": curr.get("primary_document", ""),
                                "prior_accession": prev.get("accession", ""),
                                "prior_primary_doc": prev.get("primary_document", ""),
                                "empty": False,
                                "cik": cik
                            })
                        except Exception as e:
                            # Fallback: store raw response
                            results["findings"].append({
                                "doc_type": doc_type,
                                "prior_date": prev['filing_date'],
                                "current_date": curr['filing_date'],
                                "proceedings": [],
                                "summary": "Could not parse legal proceedings for %s." % doc_type,
                                "error": str(e),
                                "agent3_score": 50,
                                "empty": True,
                                "current_accession": curr.get("accession", ""),
                                "current_primary_doc": curr.get("primary_document", ""),
                                "prior_accession": prev.get("accession", ""),
                                "prior_primary_doc": prev.get("primary_document", ""),
                                "cik": cik,
                            })
                    else:
                        # WS3: legal section absent for this doc type — surface it.
                        results["findings"].append(_empty_finding(
                            doc_type, prev, curr,
                            "No legal proceedings disclosed in %s." % doc_type))
                else:
                    results["findings"].append(_empty_finding(
                        doc_type, prev, curr,
                        "No legal proceedings in %s — filing text unavailable." % doc_type))
            else:
                _prev = filings[1] if len(filings) > 1 else None
                _curr = filings[0] if filings else None
                results["findings"].append(_empty_finding(
                    doc_type, _prev, _curr,
                    "No legal proceedings in %s — not enough filings to compare." % doc_type))
        # Limit to top 5 proceedings per finding by priority
            for finding in results["findings"]:
                if len(finding["proceedings"]) > 5:
                    priority_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
                    finding["proceedings"].sort(key=lambda x: priority_order.get(x.get("priority", "MEDIUM"), 1))
                    finding["proceedings"] = finding["proceedings"][:5]
        # WS3: attach separate current + prior EDGAR URLs to each finding and each
        # proceeding, so the UI can render two links per row (prior quote + current
        # quote), each pointing at its own filing.
        for finding in results["findings"]:
            cik_int = int(finding.get("cik", "0")) if finding.get("cik") else 0
            curr_acc = finding.get("current_accession", "")
            curr_doc = finding.get("current_primary_doc", "")
            prior_acc = finding.get("prior_accession", "")
            prior_doc = finding.get("prior_primary_doc", "")
            curr_url = ""
            prior_url = ""
            if cik_int and curr_acc and curr_doc:
                curr_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{curr_acc.replace('-', '')}/{curr_doc}"
            if cik_int and prior_acc and prior_doc:
                prior_url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{prior_acc.replace('-', '')}/{prior_doc}"
            finding["current_edgar_url"] = curr_url
            finding["prior_edgar_url"] = prior_url
            for proc in finding.get("proceedings", []):
                proc["edgar_url"] = curr_url
                proc["prior_edgar_url"] = prior_url
        _emit("Legal proceedings review complete.", "done")
        return results


def track_legal(ticker: str, progress=None) -> Dict:
    return LegalProceedingsTracker().track(ticker, progress=progress)


if __name__ == "__main__":
    result = track_legal("F")
    print(json.dumps(result, indent=2))
