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

from src.config.llm_config import call_llm, start_tier
from src.utils.scoring import score_legal_proceedings
from src.utils.prompt_safety import neutralize_injections

logger = logging.getLogger(__name__)

# Module-level CIK cache — avoids re-downloading company_tickers.json on every call.
# get_filings() invokes get_cik() once per doc_type (3x), plus once in track() = 4x/request.
_CIK_CACHE: Dict[str, str] = {}


def _quote_in_text(quote: Optional[str], source: Optional[str]) -> bool:
    """True when an LLM-supplied quote is actually present in the source filing text.

    The 10-K/10-Q/8-K comparison prompts ask the model for "exact text", but that is
    a claim, not a guarantee. We confirm it with a pure string match (normalizing
    smart quotes, whitespace and case) so the JSON export can mark which citations a
    consumer can trust without re-reading the filing. For long quotes a representative
    prefix is enough, so minor trailing edits (ellipses, truncation) aren't penalized.
    No model call and no cache write — safe to run on every proceeding."""
    if not quote or not source:
        return False

    def _norm(s: str) -> str:
        s = re.sub(r"[‘’“”\"']", "", s)
        return re.sub(r"\s+", " ", s).strip().lower()

    q, src = _norm(quote), _norm(source)
    if not q or not src:
        return False
    if q in src:
        return True
    probe = q[:60]
    return len(probe) >= 20 and probe in src


# ── Financing-event guard (Part 4, 2026-07-01) ─────────────────────────────
# 8-Ks are dominated by corporate-finance events (Item 1.01/8.01 notes offerings,
# credit facilities, indentures). Those are NOT legal proceedings; if the LLM
# returns one anyway, this deterministic check drops it. An event stays when its
# text carries genuine legal language (settlement, lawsuit, investigation...).
_FINANCING_RE = re.compile(
    r'(?i)notes?\s+offering|debt\s+offering|senior\s+notes|underwriting\s+agreement|'
    r'credit\s+(?:facility|agreement)|indenture|term\s+loan|commercial\s+paper|'
    r'stock\s+(?:offering|issuance)|securities\s+offering|notes?\s+due\s+\d{4}')
_LEGAL_RE = re.compile(
    r'(?i)lawsuit|litigation|settle(?:ment|d)?|investigation|subpoena|enforcement|'
    r'plaintiff|defendant|court|judgment|consent\s+decree|complaint|class\s+action|'
    r'regulatory\s+action|proceeding')


def _is_financing_event(ev: Dict) -> bool:
    """True when an extracted 8-K event is a financing event with no legal content."""
    txt = " ".join(str(ev.get(k) or "") for k in
                   ("description", "current_quote", "prior_quote", "case_name", "item"))
    return bool(_FINANCING_RE.search(txt)) and not _LEGAL_RE.search(txt)


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
        key = ticker.strip().upper().replace(".", "-")  # " aapl " -> AAPL; BRK.A -> BRK-A (SEC form)
        if key in _CIK_CACHE:
            return _CIK_CACHE[key]
        try:
            url = "https://www.sec.gov/files/company_tickers.json"
            resp = requests.get(url, headers={"User-Agent": "Sollia contact@sollia.app"},
                                timeout=(10, 30))
            data = resp.json()
            for item in data.values():
                if item.get("ticker", "").upper() == key:
                    cik = str(item["cik_str"]).zfill(10)
                    _CIK_CACHE[key] = cik
                    return cik
        except Exception:
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
            resp = requests.get(url, headers={"User-Agent": "Sollia contact@sollia.app", "Host": "data.sec.gov"},
                                timeout=(10, 30))
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
        except Exception:
            pass
        return None

    def _fetch_8k_text(self, filing: Dict) -> Optional[str]:
        """Return the readable 8-K narrative (body + EX-99 exhibits).

        SEC lists an inline-XBRL *cover* stub as an 8-K's primaryDocument — a few KB
        of us-gaap tags with no event text. get_filing_text() therefore fed the model
        XBRL noise, and it confabulated event "quotes" that matched no document. Here
        we fetch every human-readable .htm in the accession (skipping the R<n>.htm
        XBRL render fragments and the -index page), keep document order (8-K body
        first, exhibits after), and stash per-document text on the filing dict so each
        quote can later be cited to the exact exhibit that contains it. Cached in a
        SEPARATE namespace from the raw-HTML texts cache so it can never collide with
        Agent 1/2 content there (see filings-cache contract)."""
        if not filing or not filing.get("accession") or not filing.get("cik"):
            return None
        ticker = filing.get("ticker", "")
        acc = filing["accession"]
        cache_file = f"{self.cache_root}/texts/{ticker}_8k_texts.json"
        docs = None
        if os.path.exists(cache_file):
            try:
                docs = json.load(open(cache_file)).get(acc)
            except Exception:
                docs = None
        if docs is None:
            docs = []
            try:
                cik_int = int(filing["cik"])
                acc_clean = acc.replace("-", "")
                base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_clean}"
                idx = requests.get(base + "/index.json",
                                   headers={"User-Agent": "Sollia contact@sollia.app"}, timeout=30)
                if idx.status_code == 200:
                    for it in idx.json().get("directory", {}).get("item", []):
                        low = it.get("name", "").lower()
                        if not low.endswith(".htm"):
                            continue
                        if low.endswith("-index.htm") or re.match(r"^r\d+\.htm$", low):
                            continue  # skip the index page and XBRL render fragments
                        time.sleep(0.1)
                        r = requests.get(base + "/" + it["name"],
                                         headers={"User-Agent": "Sollia contact@sollia.app"}, timeout=30)
                        if r.status_code == 200:
                            soup = BeautifulSoup(r.text, "html.parser")
                            for tag in soup(["script", "style"]):
                                tag.extract()
                            txt = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
                            if len(txt) > 40:
                                docs.append({"url": f"{base}/{it['name']}", "text": txt[:8000]})
                store = {}
                if os.path.exists(cache_file):
                    try:
                        store = json.load(open(cache_file))
                    except Exception:
                        store = {}
                store[acc] = docs
                tmp = "%s.%d.tmp" % (cache_file, os.getpid())
                with open(tmp, "w") as f:
                    json.dump(store, f)
                os.replace(tmp, cache_file)
            except Exception:
                pass
        filing["_8k_docs"] = docs
        if not docs:
            return None
        return "\n\n".join(d["text"] for d in docs)[:9000]

    @staticmethod
    def _cite_doc(quote: Optional[str], docs: Optional[List[Dict]]) -> Optional[str]:
        """Return the URL of the doc whose text actually contains `quote` (alnum
        match, with an 80%-prefix fallback for trailing truncation), else None. Lets
        an 8-K quote be cited to the exact exhibit it came from rather than the cover."""
        qa = re.sub(r"[^a-z0-9]", "", (quote or "").rstrip("…").lower())
        if len(qa) < 20 or not docs:
            return None
        probe = qa[:max(20, int(len(qa) * 0.8))]
        for d in docs:
            ta = re.sub(r"[^a-z0-9]", "", (d.get("text") or "").lower())
            if qa in ta or probe in ta:
                return d.get("url")
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
            # Part II order is Item 1 (Legal Proceedings) -> Item 1A (Risk Factors)
            # -> Item 2. BOTH patterns MUST stop at Item 1A; otherwise the entire
            # Risk Factors block is swallowed into the "legal" text (truncated to
            # 3000 chars it is ~90% risk-factor boilerplate) and the model mines it
            # for phantom supply-chain / competition "proceedings" (PART 1a leak).
            # Item 6 is a final backstop for filings that omit Item 1A and Item 2.
            patterns = [
                r'PART\s+II.*?Item\s+1\.\s+Legal\s+Proceedings\.?(.*?)(?=Item\s+1A\.|Item\s+2\.|Item\s+6\.)',
                r'Item\s+1\.\s+Legal\s+Proceedings\.?(.*?)(?=Item\s+1A\.|Item\s+2\.|Item\s+6\.)',
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
        """True when a 10-K/10-Q legal section contains actual litigation narrative
        rather than a bare cross-reference.

        Many issuers (e.g. NVDA) reduce Part II Item 1 to "Refer to Part I, Note 10
        ... for a discussion of our legal proceedings since ..." — the substance lives
        in the financial-statement notes or the 10-K. Such a section has nothing to
        diff; feeding it to the LLM only invites phantom proceedings mined from
        mis-captured neighboring text. We classify a section as a PURE REFERENCE
        (not substantive) only when it is short, dominated by reference language, AND
        names no litigation. A long section, or one naming a lawsuit / plaintiff /
        settlement / investigation, is always substantive even if it also says
        "refer to". The old logic returned False on ANY "refer to" — that wrongly
        discarded real legal sections that merely point to a note for more detail."""
        if not text:
            return False
        t = re.sub(r"\s+", " ", text).strip().lower()
        if len(t) < 40:
            return False
        legal_words = (
            'lawsuit', 'litigat', 'asbestos', 'patent', ' v. ', ' vs ', 'settle',
            'plaintiff', 'defendant', 'complaint', 'damages', 'injunction',
            'class action', 'alleg', 'antitrust', 'subpoena', 'indictment',
            'arbitrat', 'jury', 'verdict', 'breach of contract', 'infring',
            'consent decree', 'enforcement action', 'investigation', 'claim',
        )
        has_legal = any(w in t for w in legal_words)
        is_reference = bool(re.search(
            r'\b(refer to|see note|see part|see item|incorporated by reference|'
            r'discussed in|set forth in|described in|for a discussion of)\b', t))
        # Pure cross-reference: short, reference-dominated, no litigation narrative.
        if is_reference and not has_legal and len(t) < 800:
            return False
        return has_legal or len(t) >= 300

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
        # Scale from -10..10 to 0..100 (assuming max 5 events each -2 = -10).
        # SETTLED/DISMISSED push UP (more bullish), NEW/ESCALATED/QUIETLY_REMOVED
        # push DOWN (more bearish), UNCHANGED holds at 50 — already on the unified
        # bullishness scale. Clamped to [10, 90] like every Sollia agent.
        score = 50 + (total * 5)
        return max(10, min(90, score))

    def track(self, ticker: str, progress=None) -> Dict:
        _t0 = time.time()  # PART 3: run-archive performance block
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

                if doc_type == "8-K":
                    # 8-K primaryDocument is an iXBRL cover stub, not the event text;
                    # pull the real body + EX-99 exhibits so the model sees real text.
                    curr_text = self._fetch_8k_text(curr)
                    prev_text = self._fetch_8k_text(prev)
                    curr_raw, prev_raw = curr_text, prev_text
                else:
                    curr_raw = self.get_filing_text(curr)
                    prev_raw = self.get_filing_text(prev)
                    curr_text = self.extract_legal_section(curr_raw, doc_type) if curr_raw else None
                    prev_text = self.extract_legal_section(prev_raw, doc_type) if prev_raw else None

                # Defang prompt-injection directives in this UNTRUSTED filing
                # text before it reaches the comparison prompt or is surfaced as
                # a quote (Part 3a, 2026-07-02). No-op on genuine filing text.
                curr_text = neutralize_injections(curr_text) if curr_text else curr_text
                prev_text = neutralize_injections(prev_text) if prev_text else prev_text

                if curr_raw and prev_raw:
                    if curr_text and prev_text:
                        # PART 1a reference gate: a 10-K Item 3 / 10-Q Part II Item 1
                        # that only cross-references the notes or the prior 10-K has no
                        # proceedings to diff. Feeding it to the LLM makes it confabulate
                        # proceedings from text that isn't legal content at all, so we
                        # surface an explicit NO_UPDATE finding and skip the LLM call.
                        # Only skip when NEITHER side is substantive — if either filing
                        # names a real matter we still diff (e.g. a matter that was
                        # detailed before but reduced to a reference now).
                        if doc_type in ("10-K", "10-Q") and not (
                            self._is_substantive(curr_text)
                            or self._is_substantive(prev_text)):
                            results["findings"].append(_empty_finding(
                                doc_type, prev, curr,
                                "No new legal proceedings in %s — section cross-references "
                                "prior disclosures (NO_UPDATE)." % doc_type))
                            continue
                        # Build specialized prompt
                        if doc_type == "10-K":
                            prompt = f"""Compare legal proceedings in these 10-K filings. Be concise. Only output JSON.

Report ONLY actual legal proceedings: lawsuits, litigation, regulatory or government investigations, enforcement actions, settlements, judgments, indictments, or material legal claims.
Do NOT report risk factors, market or competitive conditions, supply-chain or manufacturing challenges, accounting policies, or general business risks — NONE of these are legal proceedings.

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
                            prompt = f"""Compare legal proceedings in these 10-Q filings. Be concise. Only output JSON.

Report ONLY actual legal proceedings: lawsuits, litigation, regulatory or government investigations, enforcement actions, settlements, judgments, indictments, or material legal claims.
Do NOT report risk factors, market or competitive conditions, supply-chain or manufacturing challenges, customer-demand fluctuations, accounting policies, or general business risks — NONE of these are legal proceedings.
If a section only cross-references other documents ("Refer to Note X", "See our Form 10-K") with no specific lawsuit or claim, return an empty proceedings array with has_update=false.

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
                            prompt = f"""Compare LEGAL PROCEEDINGS disclosed in 8-K filings.

Only report legal proceedings: lawsuits, investigations, regulatory or enforcement actions, subpoenas, consent decrees, and litigation settlements. Do NOT report financing events (notes offerings, debt issuances, credit facilities, stock issuances, indentures), ordinary commercial agreements, or other corporate finance activity as legal proceedings — a $3.5B notes offering is a financing event, not a legal proceeding.

If neither filing discloses a legal proceeding, return an empty "events" list and set "summary" to one line describing what the filings actually covered (e.g. "8-K filings covered a debt offering — no legal proceedings to report.").

PRIOR ({prev['filing_date']}):
{prev_text[:8000]}

CURRENT ({curr['filing_date']}):
{curr_text[:8000]}

Output ONLY valid JSON. Do NOT include ANY text before or after the JSON. Do NOT use markdown code blocks. Do NOT write Python code. Do NOT explain. Return ONLY the JSON object, nothing else:
{{
  "events": [
    {{
      "item": "1.03|8.01|other",
      "description": "brief description of the LEGAL proceeding",
      "prior_quote": "exact text or null",
      "current_quote": "exact text",
      "counterparties": ["list"],
      "amount": "dollar amount or null",
      "status": "NEW|AMENDED|TERMINATED"
    }}
  ],
  "summary": "brief overview"
}}"""

                        response, _ = call_llm(prompt, max_tokens=2000,
                                               provider_idx=start_tier("legal_proceedings"),
                                               agent="legal_proceedings", ticker=ticker)
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

                            try:
                                data = json.loads(cleaned)
                            except json.JSONDecodeError:
                                # The LLM occasionally emits a stray backslash inside a
                                # quoted legal citation (e.g. "Invalid \escape"), which
                                # is not a valid JSON escape. Double any lone backslash
                                # that is not part of a legal escape and retry once
                                # before falling through to the unparseable handler.
                                sanitized = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', cleaned)
                                data = json.loads(sanitized)
                            # Extract proceedings list (for 10-K/10-Q) or events (for 8-K)
                            proceedings_list = data.get('proceedings', data.get('events', []))
                            # Merge duplicate findings that describe the same case/event.
                            proceedings_list = _dedup_proceedings(proceedings_list)
                            # Deterministic backstop (Part 4, 2026-07-01): an 8-K
                            # financing event (notes offering, credit facility...) is
                            # NOT a legal proceeding — drop it even if the LLM
                            # returned it, and say what the filing actually covered.
                            if doc_type == "8-K" and proceedings_list:
                                kept_events = [ev for ev in proceedings_list
                                               if not _is_financing_event(ev)]
                                if not kept_events and not data.get('summary'):
                                    data['summary'] = ("8-K filing covered a financing "
                                                       "event — no legal proceedings to report.")
                                if len(kept_events) < len(proceedings_list):
                                    data['summary'] = ((data.get('summary') or "").strip() or
                                                       "8-K filing covered a financing event — "
                                                       "no legal proceedings to report.")
                                proceedings_list = kept_events
                            # Confirm each LLM-quoted excerpt is actually present in the
                            # source filing text, so the export can flag trustworthy
                            # citations. Pure string match — no model call, no cache write.
                            for _proc in proceedings_list:
                                _proc["current_quote_verified"] = _quote_in_text(
                                    _proc.get("current_quote"), curr_text)
                                _proc["prior_quote_verified"] = _quote_in_text(
                                    _proc.get("prior_quote"), prev_text)
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
                                "_8k_curr_docs": curr.get("_8k_docs"),
                                "_8k_prev_docs": prev.get("_8k_docs"),
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
        # Sort proceedings by priority (HIGH first) — output ALL of them, no cap.
            for finding in results["findings"]:
                priority_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
                finding["proceedings"].sort(key=lambda x: priority_order.get(x.get("priority", "MEDIUM"), 1))
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
            # 8-K events span multiple exhibits; cite each quote to the exact .htm
            # that actually contains it (the cover-stub URL verifies against nothing).
            curr_docs = finding.pop("_8k_curr_docs", None) or []
            prev_docs = finding.pop("_8k_prev_docs", None) or []
            all_docs = curr_docs + prev_docs  # an 8-K quote may sit in either side's exhibits
            for proc in finding.get("proceedings", []):
                proc["edgar_url"] = self._cite_doc(proc.get("current_quote"), all_docs) or curr_url
                proc["prior_edgar_url"] = self._cite_doc(proc.get("prior_quote"), all_docs) or prior_url
        # PART 2: single agent-level legal-trend score aggregated over EVERY
        # proceeding/event across 10-K, 10-Q and 8-K (the agent previously emitted
        # only per-finding agent3_score and had no agent-level score — the backend
        # logged a hard-coded 50). score_legal_proceedings() weights each matter by
        # the direction of its status (NEW/ESCALATED/QUIETLY_REMOVED bearish;
        # SETTLED/DISMISSED bullish). agent3_score mirrors `score` for back-compat.
        _score, _signal, _rationale = score_legal_proceedings(results["findings"])
        results["score"] = _score
        results["agent3_score"] = _score
        results["signal"] = _signal
        results["score_rationale"] = _rationale
        # PART 3: archive this run (manifest + index line). Exception-safe.
        try:
            from src.utils.run_archive import save_run
            save_run("legal_proceedings", ticker, datetime.now().year, results,
                     duration_ms=(time.time() - _t0) * 1000)
        except Exception:
            pass
        _emit("Legal proceedings review complete.", "done")
        return results


def track_legal(ticker: str, progress=None) -> Dict:
    return LegalProceedingsTracker().track(ticker, progress=progress)


if __name__ == "__main__":
    result = track_legal("F")
    print(json.dumps(result, indent=2))
