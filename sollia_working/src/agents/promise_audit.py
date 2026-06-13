"""Agent 2 — Promise Audit: Management Follow-Through Tracker.

Scans all historical 10-K, 10-Q, and earnings call transcripts to identify
forward-looking promises made by management, then searches subsequent documents
for evidence of whether those promises were kept, broken, or remain unresolved.

Two-stage LLM pipeline: promise extraction → evidence matching.
Deterministic keyword filters reduce LLM context sizes.
Recency-weighted scoring — recent outcomes matter more than ancient history.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import threading
from datetime import date, datetime, timedelta
from src.config.llm_config import call_llm as _call_llm_config
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 0: CONSTANTS & CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

# LLM provider priority (from project spec)
# LLM priority imported from config.llm_config

# Stage config
STAGE1_BATCH_SIZE = 15
STAGE2_MAX_WORDS = 10000
STAGE2_TOP_N_CANDIDATES = 20
KEYWORD_MATCH_THRESHOLD = 2
NUMERIC_TOLERANCE = 0.05
DUPLICATE_SIMILARITY_THRESHOLD = 0.80

# Scoring config
STALE_YEARS = 3
DECAY_RATE_RESOLVED = 0.20
DECAY_RATE_PENDING = 0.25
DECAY_FLOOR_RESOLVED = 0.15
DECAY_FLOOR_PENDING = 0.20
DECAY_STALE_FIXED = 0.30

# Rate limiting
_MIN_LLM_INTERVAL = 3.0  # seconds between LLM calls
_llm_lock = threading.Lock()
_last_llm_call = 0.0

# Cache paths (set at runtime)
_CACHE_ROOT: str = ""

# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 0.4: KEYWORD LISTS
# ═══════════════════════════════════════════════════════════════════════════════

FUTURE_KEYWORDS = [
    # Commitment language
    "we will", "we plan to", "we intend to", "we expect to",
    "we are on track to", "we target", "we forecast",
    "our goal is to", "we aim to", "we are committed to",
    # Projection language
    "is expected to", "is projected to", "is anticipated to",
    "we project", "we anticipate", "we estimate",
    # Pipeline / in-progress
    "in development", "to be launched", "to be completed",
    "to be released", "under construction", "ramping up",
    # Guidance
    "we guide", "our guidance", "we are guiding",
    "looking ahead", "going forward", "we see",
]

METRIC_PATTERNS = [
    r'\$\d+(?:\.\d+)?\s*(?:million|billion)',  # dollar amounts
    r'\d{2,}\s*%',                                # percentages
    r'Q[1-4]\s*\d{4}',                            # quarter references
    r'fiscal\s*\d{4}',                            # fiscal year references
    r'by\s+(?:early|mid|late|the end of)\s',     # deadline language
]

EXCLUDE_PATTERNS = [
    r'(?i)share\s*(?:re)?purchase|ASR|buyback|repurchased',
    r'(?i)dividend|distribution to shareholders',
    r'(?i)RSU|restricted stock unit|equity award|stock option',
    r'(?i)employment agreement|severance|compensation arrangement',
    r'(?i)debt issuance|notes due|senior notes|credit facility',
    r'(?i)indenture|subsidiary guarantee|registration statement',
    r'(?i)amendment to.*bylaws|change in.*officer|appointment of',
]

# Compile all regex once
_metric_re = [re.compile(p) for p in METRIC_PATTERNS]
_exclude_re = [re.compile(p) for p in EXCLUDE_PATTERNS]

# Abbreviations that should NOT trigger sentence splits
_ABBREVIATIONS = {
    "Inc", "Corp", "Ltd", "LLC", "Co", "U.S", "e.g", "i.e", "vs",
    "Dr", "Mr", "Mrs", "Ms", "Prof", "Est", "approx", "et al",
    "Q1", "Q2", "Q3", "Q4", "FY", "H1", "H2",
    "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
}

# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 3: CLEANING
# ═══════════════════════════════════════════════════════════════════════════════

def strip_html(text: str) -> str:
    """Remove HTML tags. Option A (regex) first, Option B (BeautifulSoup) if needed."""
    # Remove script and style blocks first (they contain JS/CSS, not text)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<[^>]+>', ' ', text)
    if cleaned.count('<') > 10:
        try:
            cleaned = BeautifulSoup(text, "html.parser").get_text()
        except Exception:
            pass
    return cleaned


def strip_xbrl(text: str) -> str:
    """Remove XBRL markers, XML declarations, comments, script/style blocks."""
    text = re.sub(r'<\?xml[^>]*\?>', '', text)
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    for tag in ('link', 'script', 'style'):
        text = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', '', text, flags=re.DOTALL | re.IGNORECASE)
    for prefix in ('ix:', 'xbrli:', 'xbrldi:'):
        text = re.sub(rf'<{prefix}[^>]*>', ' ', text)
        text = re.sub(rf'</{prefix}[^>]*>', ' ', text)
    return text


def strip_boilerplate(text: str) -> str:
    """Remove page numbers, table of contents headers, separator lines."""
    text = re.sub(r'^\s*\d{1,3}\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^-{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^_{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'(?i)^\s*table\s+of\s+contents\s*$', '', text, flags=re.MULTILINE)
    return text


def normalize_whitespace(text: str) -> str:
    """Collapse 3+ newlines to 2, collapse multiple spaces/tabs, strip."""
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n +', '\n', text)
    return text.strip()


def normalize_unicode(text: str) -> str:
    """NFKC normalize + manual fixes for common SEC characters."""
    import unicodedata
    text = unicodedata.normalize('NFKC', text)
    replacements = {
        '\xa0': ' ',      # non-breaking space
        '\u201c': '"',    # left smart quote
        '\u201d': '"',    # right smart quote
        '\u2018': "'",    # left smart single quote
        '\u2019': "'",    # right smart single quote
        '\u2013': '-',    # en dash
        '\u2014': '--',   # em dash
        '\u2026': '...',  # ellipsis
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def strip_safe_harbour(text: str) -> str:
    """Remove safe harbour / forward-looking statement disclaimer paragraphs."""
    pattern = r'(?i)(this\s+(report|filing|document|presentation|transcript|call).*?forward[- ]?looking.*?(?=\n\n|\n\s*\n|$))'
    text = re.sub(pattern, '', text, flags=re.DOTALL)
    pattern2 = r'(?i)(safe\s+harbor?.*?under\s+the\s+private\s+securities.*?(?=\n\n|\n\s*\n|$))'
    text = re.sub(pattern2, '', text, flags=re.DOTALL)
    return text


def clean_transcript(text: str) -> str:
    """Strip speaker labels, timestamps, operator lines from transcripts."""
    # Timestamps
    text = re.sub(r'\[\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?\]', '', text)
    text = re.sub(r'\(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?\)', '', text)
    # Speaker labels (Name Name: or Name: at line start)
    text = re.sub(r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s*:', '', text, flags=re.MULTILINE)
    # Operator / moderator lines
    text = re.sub(
        r'(?i)^\s*(operator|coordinator|moderator|conference call|good\s+(morning|afternoon|evening)|welcome\s+to\s+the|thank\s+you\s+for\s+joining).*$',
        '', text, flags=re.MULTILINE
    )
    return text


def clean_document(text: str, doc_type: str = "filing") -> str:
    """Full cleaning pipeline for a document."""
    text = strip_html(text)
    text = strip_xbrl(text)
    text = strip_boilerplate(text)
    text = strip_safe_harbour(text)
    text = normalize_unicode(text)
    if doc_type == "transcript":
        text = clean_transcript(text)
    text = normalize_whitespace(text)
    return text


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 4: SENTENCE TOKENIZATION
# ═══════════════════════════════════════════════════════════════════════════════

def split_sentences(text: str) -> List[str]:
    """Split text into sentences with abbreviation protection.
    
    Uses spaCy if available (handles abbreviations natively),
    falls back to regex with abbreviation list.
    """
    if not text or not text.strip():
        return []
    
    # Regex with abbreviation protection (fast, no ML deps)
    # Protect abbreviations by temporarily replacing dots
    protected = text
    for abbr in sorted(_ABBREVIATIONS, key=len, reverse=True):
        escaped = re.escape(abbr)
        protected = re.sub(
            rf'\b{escaped}\.',
            abbr + '@@DOT@@',
            protected
        )
    
    # Split on sentence boundaries
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z"\'\(])', protected)
    
    # Restore abbreviations
    sentences = [s.replace('@@DOT@@', '.') for s in sentences]
    
    return [s.strip() for s in sentences if s.strip()]


def annotate_sentences(
    sentences: List[str],
    doc_metadata: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Attach source metadata to each sentence."""
    return [
        {
            "text": sent,
            "index": i,
            "ticker": doc_metadata.get("ticker", ""),
            "filing_type": doc_metadata.get("filing_type", ""),
            "filing_date": doc_metadata.get("filing_date", ""),
            "fiscal_year": doc_metadata.get("fiscal_year", 0),
            "section": doc_metadata.get("section", ""),
            "htm_url": doc_metadata.get("htm_url", ""),
        }
        for i, sent in enumerate(sentences)
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 5: STAGE 1 — DETERMINISTIC KEYWORD FILTER
# ═══════════════════════════════════════════════════════════════════════════════

def has_future_keyword(sentence: str) -> bool:
    """Check if sentence contains any future-oriented keyword."""
    lower = sentence.lower()
    return any(kw in lower for kw in FUTURE_KEYWORDS)


def has_metric_with_deadline(sentence: str) -> bool:
    """Check if sentence contains a metric AND a deadline indicator."""
    has_metric = any(pat.search(sentence) for pat in _metric_re)
    if not has_metric:
        return False
    # Deadline indicators (only match years in context, not standalone numbers)
    deadline_patterns = [
        r'Q[1-4]\s*\d{4}',                         # Q3 2025
        r'fiscal\s*(?:year\s*)?\d{4}',             # fiscal 2025, fiscal year 2025
        r'FY\s*\d{4}',                             # FY2025
        r'by\s+(?:early|mid|late|the end of|Q[1-4])', # by early 2025, by Q4
        r'by\s+\d{4}',                             # by 2026
        r'in\s+(?:fiscal\s*)?\d{4}',               # in 2025, in fiscal 2025
        r'calendar\s*\d{4}',                       # calendar 2025
    ]
    return any(re.search(pat, sentence, re.IGNORECASE) for pat in deadline_patterns)


def is_excluded(sentence: str) -> bool:
    """Check if sentence matches any exclusion pattern (transactional, not strategic)."""
    return any(pat.search(sentence) for pat in _exclude_re)


def filter_candidate_sentences(
    annotated_sentences: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Filter sentences: keep only strategic forward-looking statements."""
    candidates = []
    for s in annotated_sentences:
        text = s["text"]
        if not is_excluded(text) and (has_future_keyword(text) or has_metric_with_deadline(text)):
            candidates.append(s)
    return candidates


def deduplicate_candidates(
    candidates: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Remove exact duplicate sentences, keeping earliest occurrence."""
    seen = {}
    for c in candidates:
        key = c["text"].lower().strip()
        if key in seen:
            # Keep the one with earlier filing_date
            if c.get("filing_date", "") < seen[key].get("filing_date", ""):
                seen[key] = c
        else:
            seen[key] = c
    return list(seen.values())


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 6: STAGE 1 — LLM VALIDATION & CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

def _init_cache_paths():
    """Set up cache directory paths."""
    global _CACHE_ROOT
    if not _CACHE_ROOT:
        _CACHE_ROOT = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..', '..', '.cache'
        )
        os.makedirs(os.path.join(_CACHE_ROOT, 'llm'), exist_ok=True)


def _get_llm_cache(prompt_hash: str) -> Optional[Dict]:
    """Check LLM cache for a prompt hash."""
    _init_cache_paths()
    cache_file = os.path.join(_CACHE_ROOT, 'llm', f'{prompt_hash}.json')
    try:
        if os.path.exists(cache_file):
            with open(cache_file) as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _set_llm_cache(prompt_hash: str, prompt: str, response: str, model: str):
    """Store LLM response in cache."""
    _init_cache_paths()
    cache_file = os.path.join(_CACHE_ROOT, 'llm', f'{prompt_hash}.json')
    try:
        with open(cache_file, 'w') as f:
            json.dump({
                "prompt": prompt,
                "response": response,
                "model": model,
                "timestamp": datetime.now().isoformat(),
            }, f)
    except Exception:
        pass


def _call_llm(prompt: str, max_tokens: int = 2000) -> Tuple[str, str]:
    """Call LLM using universal config. Returns (response_text, model_used)."""
    import hashlib
    import json as _json
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]

    cached = _get_llm_cache(prompt_hash)
    if cached:
        return cached["response"], cached.get("model", "cached")

    try:
        text, meta = _call_llm_config(prompt, max_tokens=max_tokens, temperature=0.0)
        model = meta.get("model", "unknown")
        if text:
            _set_llm_cache(prompt_hash, prompt, text, model)
        return text, model
    except Exception as e:
        # Fallback: try direct requests without the lock
        logger.warning("Config LLM failed, trying direct: %s", str(e)[:80])
        try:
            import requests, os
            key = os.environ.get("GROQ_API_KEY", "")
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": "llama-3.1-8b-instant", "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": max_tokens},
                timeout=120
            )
            if r.status_code == 200:
                data = r.json()
                text = data["choices"][0]["message"]["content"].strip()
                _set_llm_cache(prompt_hash, prompt, text, "llama-3.1-8b-instant")
                return text, "llama-3.1-8b-instant"
        except Exception as e2:
            logger.warning("Direct Groq also failed: %s", str(e2)[:80])
        raise

def _build_stage1_prompt(batch: List[Dict[str, Any]]) -> str:
    """Build the Stage 1 LLM prompt for promise extraction."""
    system = """Identify forward-looking strategic promises from company filings.
Return ONLY a JSON array of promises (empty array if none found).

For each promise: {"is_promise":true, "text":"verbatim","category":"revenue|product|margin|rd|market|other","severity":1-10,"deadline":"Q3 2024"|null,"measurable_outcome":"one sentence","search_terms":["term1",...]}

Severity: 1-3=minor, 4-6=meaningful, 7-8=major, 9-10=company-defining.
Exclude: compensation, buybacks, dividends, debt, legal, governance, historical statements, vague aspirations.
Generate 5-10 specific search terms for evidence retrieval."""

    sentences_block = "\n".join(
        f'[{i}] "{s["text"]}"' for i, s in enumerate(batch)
    )
    
    return f"{system}\n\n---\n\nSentences to analyze:\n{sentences_block}"


def _parse_stage1_response(response: str, batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Parse and validate the Stage 1 LLM response."""
    if not response:
        return []
    
    # Strip markdown code fences and any text before/after JSON
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
    
    # Aggressively extract JSON array — find first [ to last ]
    match = re.search(r'\[.*\]', cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)
    
    # Try to parse JSON
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Retry: extract first [ to last ]
        match = re.search(r'\[.*\]', cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                logger.warning("Failed to parse Stage 1 response JSON — using deterministic fallback")
                # Generate promises with deterministic search_terms from the candidate sentences
                fallback_promises = []
                for s in batch:
                    text = s.get("text", s.get("sentence", ""))
                    if not text:
                        continue
                    words = [w.strip('.,;:!?()[]{}') for w in text.lower().split() if len(w) > 3]
                    search_terms = list(set(words[:8])) if words else [text[:30]]
                    fallback_promises.append({
                        "is_promise": True,
                        "text": text,
                        "category": "other",
                        "severity": 5,
                        "deadline": None,
                        "measurable_outcome": "",
                        "search_terms": search_terms
                    })
                return fallback_promises
        else:
            logger.warning("No JSON array found in Stage 1 response")
            return []
    
    if not isinstance(data, list):
        return []
    
    # Validate each promise
    valid_categories = {"revenue", "product", "margin", "rd", "market", "other"}
    promises = []
    
    for item in data:
        if not item.get("is_promise"):
            continue
        if item.get("category") not in valid_categories:
            continue
        sev = item.get("severity")
        if not isinstance(sev, int) or not (1 <= sev <= 10):
            continue
        if not item.get("text"):
            continue
        if not isinstance(item.get("search_terms"), list):
            continue
        
        # Verbatim check: does the text appear in the batch?
        batch_texts = [s["text"] for s in batch]
        if item["text"] not in batch_texts:
            # Try fuzzy match
            found = False
            for bt in batch_texts:
                if item["text"][:50] in bt or bt[:50] in item["text"]:
                    item["text"] = bt
                    found = True
                    break
            if not found:
                logger.warning("Stage 1 response text not found verbatim in batch")
                continue
        
        # Find the original sentence metadata
        source = next((s for s in batch if s["text"] == item["text"]), batch[0] if batch else {})
        
        promises.append({
            **item,
            "source": {
                "ticker": source.get("ticker", ""),
                "filing_type": source.get("filing_type", ""),
                "filing_date": source.get("filing_date", ""),
                "fiscal_year": source.get("fiscal_year", 0),
                "section": source.get("section", ""),
                "sentence_index": source.get("index", 0),
                "htm_url": source.get("htm_url", ""),
                "accession": source.get("accession", ""),
            }
        })
    
    return promises


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT (for backend/agents.py)
# ═══════════════════════════════════════════════════════════════════════════════

class PromiseAuditAgent:
    """Agent 2 — Management Follow-Through Tracker."""
    
    def __init__(self):
        self._log: List[Dict[str, Any]] = []
    
    def _add_log(self, step: str, detail: str):
        self._log.append({"timestamp": datetime.now().isoformat(), "step": step, "detail": detail})
        logger.info("[Agent2] %s: %s", step, detail)
    
    def run(self, ticker: str) -> Dict[str, Any]:
        """Run the full promise audit pipeline — Stage 1 + Stage 2 + Scoring."""
        self._log = []
        ticker = ticker.upper()
        
        # Import providers (lazy to avoid circular imports at module level)
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
        from src.providers.filings.sec_edgar import SECEdgarProvider, cache_filings
        from src.providers.transcripts.sec_transcript import SECTranscriptProvider
        from src.utils.fiscal_alignment import get_fy_bundles_in_window
        
        edgar = SECEdgarProvider()
        sec_trans = SECTranscriptProvider()
        
        # ── Step 1: Fetch all filings ────────────────────────────────────
        self._add_log("FETCH", f"Fetching all filings for {ticker}...")
        all_filings_raw = edgar.get_all_filings(ticker)
        if not all_filings_raw:
            return {"error": f"No filings available for {ticker}"}
        cache_filings(ticker, all_filings_raw)
        
        ten_years_ago = date.today() - timedelta(days=365.25 * 10)
        all_filings = [f for f in all_filings_raw if f.get('filing_date', '') >= str(ten_years_ago)]
        self._add_log("FETCH", f"Fetched {len(all_filings_raw)} total, {len(all_filings)} within 10yr window")
        
        # ── Step 2: Fetch transcripts ────────────────────────────────────
        self._add_log("FETCH", "Fetching transcripts...")
        transcripts_raw = sec_trans.get_transcripts(ticker)
        transcripts = [t for t in transcripts_raw if str(t["date"] if isinstance(t, dict) else t.date) >= str(ten_years_ago)]
        self._add_log("FETCH", f"Fetched {len(transcripts_raw)} total, {len(transcripts)} within 10yr window")
        
        # ── Step 3: Build fiscal year bundles ─────────────────────────────
        self._add_log("ALIGN", f"Building FY bundles ({len(all_filings)} filings, {len(transcripts)} transcripts)...")
        bundles = get_fy_bundles_in_window(ticker, all_filings, transcripts, max_years=10)
        self._add_log("ALIGN", f"Built {len(bundles)} FY bundles")
        
        # ── Step 4: Stage 1 — Extract promises ───────────────────────────
        self._add_log("STAGE1", "Starting Stage 1: Promise extraction...")
        promises, stage1_logs = extract_promises_from_documents(ticker, all_filings, transcripts, bundles)
        self._log.extend(stage1_logs)
        
        total_promises = len(promises)
        self._add_log("STAGE1", f"Extracted {total_promises} promises")
        
        if not promises:
            return {
                "ticker": ticker,
                "agent": "Agent 2 — Promise Audit",
                "run_timestamp": datetime.now().isoformat(),
                "total_filings_analyzed": len(all_filings),
                "total_promises_extracted": 0,
                "total_kept": 0, "total_broken": 0, "total_partial": 0,
                "total_pending": 0, "total_stale": 0,
                "score": 50.0,
                "top_promises": [],
                "agent_log": self._log,
            }
        
        # ── Step 5: Stage 2 — Evidence search ────────────────────────────
        self._add_log("STAGE2", "Starting Stage 2: Evidence search...")
        
        # Pre-tokenize all documents ONCE for fast evidence search
        self._add_log("STAGE2", "Pre-tokenizing documents for evidence search...")
        tokenized_docs = {}
        from src.providers.filings.sec_edgar import get_cached_text
        for filing in all_filings:
            acc = filing.get('accession_number', '')
            if acc:
                text = get_cached_text(ticker, acc)
                if text:
                    tokenized_docs[acc] = split_sentences(text)
        for t in transcripts:
            t_date = str(t.get('date', '') if isinstance(t, dict) else (t.date if hasattr(t, 'date') and t.date else ''))
            content_text = getattr(t, 'content', '') or ''
            if content_text and t_date:
                tokenized_docs[f"TR_{t_date}"] = split_sentences(content_text)
        self._add_log("STAGE2", f"Pre-tokenized {len(tokenized_docs)} documents")
        
        # Collect all evidence candidates using pre-tokenized cache
        all_evidence = []
        for promise in promises:
            candidates = _search_evidence_fast(promise, all_filings, transcripts, tokenized_docs)
            for c in candidates:
                c["promise_id"] = promise.get("promise_id", "")
            all_evidence.extend(candidates)
        
        # Deduplicate evidence by text
        seen_texts = set()
        unique_evidence = []
        for ev in all_evidence:
            key = ev.get("sentence", "")[:100].lower()
            if key not in seen_texts:
                seen_texts.add(key)
                unique_evidence.append(ev)
        
        self._add_log("STAGE2_DETERMINISTIC", f"Found {len(unique_evidence)} unique evidence candidates")
        # Build URL lookup from evidence candidates for later matching
        evidence_url_lookup = {}
        for ev in unique_evidence:
            key = ev.get("sentence", "")[:100].lower()
            if key:
                evidence_url_lookup[key] = ev.get("htm_url", "")
        
        # Stage 2 LLM matching (unified call, split if >10k words)
        evidence_results = []
        if unique_evidence:
            prompt = build_stage2_prompt(promises, unique_evidence)
            words = len(prompt.split())
            
            if words <= STAGE2_MAX_WORDS:
                response, model = _call_llm(prompt, max_tokens=4000)
                if response:
                    evidence_results = parse_stage2_response(response, promises)
                    self._add_log("STAGE2_LLM", f"Matched {len(evidence_results)} promises to evidence (model: {model})")
            else:
                self._add_log("STAGE2_LLM", f"Prompt too large ({words} words), splitting by category...")
                # Split by category
                categories = set(p.get("category", "other") for p in promises)
                for cat in categories:
                    cat_promises = [p for p in promises if p.get("category") == cat]
                    cat_evidence = [e for e in unique_evidence 
                                   if e.get("promise_id") in {p["promise_id"] for p in cat_promises}]
                    cat_prompt = build_stage2_prompt(cat_promises, cat_evidence)
                    response, model = _call_llm(cat_prompt, max_tokens=4000)
                    if response:
                        cat_results = parse_stage2_response(response, cat_promises)
                        evidence_results.extend(cat_results)
                self._add_log("STAGE2_LLM", f"Split across {len(categories)} categories, matched {len(evidence_results)}")
        else:
            self._add_log("STAGE2_LLM", "No evidence candidates found — all promises unresolved")
        
        # Deterministic fallback: if LLM returned no results, try text-based matching directly
            if not evidence_results:
                self._add_log("STAGE2_LLM", "LLM returned no results — using deterministic fallback")
                for p in promises:
                    promise_text = p.get("text", "")[:100].lower()
                    best_score = 0
                    best_ev = None
                    for ev in unique_evidence:
                        ev_text = ev.get("sentence", "").lower()
                        # Simple overlap score
                        words = set(promise_text.split())
                        ev_words = set(ev_text.split())
                        overlap = len(words & ev_words)
                        if overlap > best_score:
                            best_score = overlap
                            best_ev = ev
                    if best_ev and best_score >= 2:
                        ev_url = best_ev.get("htm_url", "")
                        promise_url = p.get("promise_url", "")
                        if ev_url and ev_url == promise_url:
                            # Same filing — skip
                            continue
                        evidence_results.append({
                            "promise_id": p.get("promise_id", ""),
                            "status": "KEPT",
                            "evidence_text": best_ev.get("sentence", ""),
                            "evidence_filing": best_ev.get("filing_type", ""),
                            "evidence_date": best_ev.get("filing_date", ""),
                            "evidence_url": ev_url,
                            "evidence_id": "",
                            "confidence": "low",
                            "reasoning": "Deterministic keyword match"
                        })
                    else:
                        evidence_results.append({
                            "promise_id": p.get("promise_id", ""),
                            "status": "UNRESOLVED",
                            "evidence_text": "",
                            "evidence_filing": "",
                            "evidence_date": "",
                            "evidence_id": "",
                            "confidence": "low",
                            "reasoning": "No evidence found"
                        })
        
        # ── Step 6: Merge evidence results into promises ──────────────────
        evidence_map = {r["promise_id"]: r for r in evidence_results}
        
        for p in promises:
            pid = p.get("promise_id", "")
            match = evidence_map.get(pid, {})
            p["status"] = match.get("status", "UNRESOLVED")
            # Look up evidence details from unique_evidence by evidence_id
            ev_id = match.get("evidence_id", "")
            matched_ev = None
            for ev in unique_evidence:
                ev_sid = f"EVID_{unique_evidence.index(ev):03d}"
                if ev_id == ev_sid:
                    matched_ev = ev
                    break
            if matched_ev:
                ev_url = matched_ev.get("htm_url", "")
                # Skip if same filing as promise
                if ev_url and ev_url == p.get("promise_url", ""):
                    p["evidence_text"] = ""
                    p["evidence_filing"] = ""
                    p["evidence_date"] = ""
                    p["evidence_url"] = ""
                    p["status"] = "PENDING"
                    p["reasoning"] = "Evidence from same filing as promise — excluded"
                else:
                    p["evidence_text"] = matched_ev.get("sentence", "")
                    p["evidence_filing"] = match.get("evidence_filing") or matched_ev.get("filing_type", "")
                    p["evidence_date"] = match.get("evidence_date") or matched_ev.get("filing_date", "")
                    p["evidence_url"] = ev_url
            else:
                p["evidence_text"] = match.get("evidence_text", "")
                p["evidence_filing"] = match.get("evidence_filing", "")
                p["evidence_date"] = match.get("evidence_date", "")
                p["evidence_url"] = ""
            p["confidence"] = match.get("confidence", "low")
            p["reasoning"] = match.get("reasoning", "")
            # If evidence URL matches promise URL, mark as PENDING (same filing)
            if p.get("evidence_url") and p.get("promise_url") and p["evidence_url"] == p["promise_url"]:
                p["evidence_url"] = ""
                p["evidence_text"] = ""
                p["status"] = "PENDING"
                p["reasoning"] = "Evidence from same filing as promise — excluded" 
            
            # If no evidence match and deadline is in the past, mark as BROKEN
            if p["status"] == "UNRESOLVED" and p.get("deadline_date"):
                try:
                    dl_date = date.fromisoformat(p["deadline_date"])
                    if dl_date < date.today():
                        p["status"] = "BROKEN"
                        p["reasoning"] = f"Deadline {p['deadline_date']} has passed with no evidence of completion"
                except (ValueError, TypeError):
                    pass
        
        # ── Step 7: Calculate decay and score ────────────────────────────
        self._add_log("SCORE", "Calculating credibility score...")
        
        for p in promises:
            p["decay"] = round(calculate_decay(
                p.get("status", "UNRESOLVED"),
                p.get("source", {}).get("filing_date", ""),
                p.get("deadline_date"),
                p.get("evidence_date"),
            ), 2)
        
        score, breakdown = calculate_score(promises)
        self._add_log("SCORE", f"Calculated credibility score: {score}")
        
        # Count statuses
        status_counts = {"KEPT": 0, "PARTIAL": 0, "BROKEN": 0, "PENDING": 0, "UNRESOLVED": 0, "STALE": 0}
        for p in promises:
            s = p.get("status", "UNRESOLVED")
            if s in status_counts:
                status_counts[s] += 1
        
        # ── Step 8: Select top 5 ─────────────────────────────────────────
        top5 = select_top_promises(promises, 5)
        
        # Build output
        return {
            "ticker": ticker,
            "agent": "Agent 2 — Promise Audit",
            "run_timestamp": datetime.now().isoformat(),
            "total_filings_analyzed": len(all_filings),
            "total_promises_extracted": total_promises,
            "total_kept": status_counts["KEPT"],
            "total_broken": status_counts["BROKEN"],
            "total_partial": status_counts["PARTIAL"],
            "total_pending": status_counts["PENDING"],
            "total_stale": status_counts.get("STALE", 0),
            "score": score,
            "top_promises": [
                {
                    "rank": i + 1,
                    "promise_id": p.get("promise_id", ""),
                    "text": p.get("text", ""),
                    "category": p.get("category", ""),
                    "severity": p.get("severity", 5),
                    "status": p.get("status", "UNRESOLVED"),
                    "decay": p.get("decay", 0.5),
                    "weighted_importance": p.get("weighted_importance", 0),
                    "deadline": p.get("deadline"),
                    "promise_source": p.get("source", {}).get("filing_type", ""),
                    "promise_date": p.get("source", {}).get("filing_date", ""),
                    "promise_url": p.get("source", {}).get("htm_url", ""),
                    "evidence_text": p.get("evidence_text"),
                    "evidence_filing": p.get("evidence_filing"),
                    "evidence_url": p.get("evidence_url", ""),
                    "evidence_date": p.get("evidence_date"),
                    "confidence": p.get("confidence", "low"),
                    "reasoning": p.get("reasoning", ""),
                }
                for i, p in enumerate(top5)
            ],
            "agent_log": self._log,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 6.5: STAGE 1 — EXECUTION LOOP
# ═══════════════════════════════════════════════════════════════════════════════

def generate_promise_id(ticker: str, filing_date: str, index: int) -> str:
    """Generate a unique promise ID: TICKER_YYYY-MM-DD_INDEX."""
    date_clean = filing_date.replace("-", "")[:8] if filing_date else "00000000"
    return f"{ticker}_{date_clean}_{index:03d}"


def extract_promises_from_documents(
    ticker: str,
    all_filings: List[Dict[str, Any]],
    transcripts: List[Any],
    fy_bundles: Dict[int, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Stage 1: Extract promises from all 10-K, 10-Q MD&A and transcript texts.
    
    Returns:
        (promises, agent_log_entries)
    """
    from src.providers.filings.sec_edgar import SECEdgarProvider, get_cached_text, cache_text
    
    edgar = SECEdgarProvider()
    all_candidates = []
    log_entries = []
    
    # Only process 10-K and 10-Q (Agent 2's 3 sources: 10-K MD&A, 10-Q MD&A, transcripts)
    target_forms = {'10-K', '10-K/A', '10-Q', '10-Q/A'}
    
    for filing in all_filings:
        form = filing.get('form', '')
        if form not in target_forms:
            continue
        
        acc = filing.get('accession_number', '')
        ticker_f = filing.get('ticker', ticker)
        filing_date = filing.get('filing_date', '')
        
        # Check text cache first
        cached_mda = get_cached_text(ticker_f, acc) if acc else None
        
        if cached_mda and len(cached_mda) > 500:
            mda_text = cached_mda
            log_entries.append({
                "timestamp": datetime.now().isoformat(),
                "step": "STAGE1_TEXT",
                "detail": f"Cache hit: {form} {acc[:25]}... ({len(mda_text)} chars)"
            })
        else:
            # Fetch and extract MD&A
            try:
                html = edgar.fetch_html(
                    filing.get('cik', ''),
                    filing.get('accession_nodash', ''),
                    filing.get('primary_doc', ''),
                    ticker_f
                )
                if html:
                    mda_text = edgar.extract_mda(html)
                    if mda_text and len(mda_text) > 100:
                        if acc:
                            cache_text(ticker_f, acc, mda_text)
                        log_entries.append({
                            "timestamp": datetime.now().isoformat(),
                            "step": "STAGE1_TEXT",
                            "detail": f"Fetched MD&A: {form} {acc[:25]}... ({len(mda_text)} chars)"
                        })
                    else:
                        log_entries.append({
                            "timestamp": datetime.now().isoformat(),
                            "step": "STAGE1_TEXT",
                            "detail": f"No MD&A found: {form} {acc[:25]}... — skipping"
                        })
                        continue
                else:
                    continue
            except Exception as e:
                log_entries.append({
                    "timestamp": datetime.now().isoformat(),
                    "step": "STAGE1_TEXT",
                    "detail": f"Failed to fetch {form} {acc[:25]}...: {str(e)[:80]}"
                })
                continue
        
        # Clean and tokenize
        cleaned = clean_document(mda_text, doc_type="filing")
        sentences = split_sentences(cleaned)
        
        # Annotate with metadata
        meta = {
            "ticker": ticker,
            "filing_type": form,
            "filing_date": filing_date,
            "fiscal_year": int(filing.get('period_of_report', '0000')[:4]) if filing.get('period_of_report') else 0,
            "section": "Item 7. MD&A" if '10-K' in form else "Item 2. MD&A",
            "htm_url": filing.get('htm_url', ''),
        }
        annotated = annotate_sentences(sentences, meta)
        
        # Filter candidates
        candidates = filter_candidate_sentences(annotated)
        all_candidates.extend(candidates)
    
    # Also process transcripts (cleaned via SEC EDGAR pipeline)
    for t in transcripts:
        content = getattr(t, 'content', '') or ''
        if not content or len(content) < 200:
            continue
        
        t_date = str(t.get('date', '') if isinstance(t, dict) else (t.date if hasattr(t, 'date') and t.date else ''))
        
        cleaned = clean_document(content, doc_type="transcript")
        sentences = split_sentences(cleaned)
        
        meta = {
            "ticker": ticker,
            "filing_type": "Transcript",
            "filing_date": t_date,
            "fiscal_year": int(t_date[:4]) if t_date else 0,
            "section": "Earnings Call",
            "htm_url": getattr(t, 'source_url', '') or '',
        }
        annotated = annotate_sentences(sentences, meta)
        candidates = filter_candidate_sentences(annotated)
        all_candidates.extend(candidates)
    
    # Deduplicate
    deduped = deduplicate_candidates(all_candidates)
    log_entries.append({
        "timestamp": datetime.now().isoformat(),
        "step": "STAGE1_DETERMINISTIC",
        "detail": f"Flagged {len(deduped)} candidate sentences from {len(all_filings)} filings + {len(transcripts)} transcripts"
    })
    
    # If no candidates, return early
    if not deduped:
        return [], log_entries
    
    # Batch and call LLM
    batches = []
    for i in range(0, len(deduped), STAGE1_BATCH_SIZE):
        batches.append(deduped[i:i + STAGE1_BATCH_SIZE])
    
    all_promises = []
    promise_counter = 0
    
    for batch_idx, batch in enumerate(batches):
        prompt = _build_stage1_prompt(batch)
        response, model = _call_llm(prompt, max_tokens=2000)
        
        if response:
            promises = _parse_stage1_response(response, batch)
            for p in promises:
                promise_counter += 1
                p["promise_id"] = generate_promise_id(
                    ticker,
                    p.get("source", {}).get("filing_date", ""),
                    promise_counter
                )
                # Parse deadline_date if deadline is provided
                if p.get("deadline"):
                    p["deadline_date"] = _parse_deadline_date(p["deadline"])
                else:
                    p["deadline_date"] = None
            all_promises.extend(promises)
            log_entries.append({
                "timestamp": datetime.now().isoformat(),
                "step": "STAGE1_LLM",
                "detail": f"Batch {batch_idx+1}/{len(batches)}: {len(promises)} promises from {len(batch)} candidates (model: {model})"
            })
        else:
            log_entries.append({
                "timestamp": datetime.now().isoformat(),
                "step": "STAGE1_LLM",
                "detail": f"Batch {batch_idx+1}/{len(batches)}: LLM call failed, skipping {len(batch)} candidates"
            })
    
    log_entries.append({
        "timestamp": datetime.now().isoformat(),
        "step": "STAGE1_LLM",
        "detail": f"Extracted {len(all_promises)} promises ({len(batches)} batches)"
    })
    
    return all_promises, log_entries


def _parse_deadline_date(deadline: str) -> Optional[str]:
    """Parse a natural language deadline into YYYY-MM-DD format.
    
    Examples:
        'fiscal 2025' -> '2025-12-31' (conservative: end of calendar year)
        'Q3 2024' -> '2024-09-30'
        'H2 2024' -> '2024-12-31'
    """
    if not deadline:
        return None
    
    deadline_lower = deadline.lower().strip()
    
    # Q1-Q4 2024
    m = re.match(r'q([1-4])\s*(\d{4})', deadline_lower)
    if m:
        q, year = int(m.group(1)), int(m.group(2))
        quarter_end_months = {1: 3, 2: 6, 3: 9, 4: 12}
        month = quarter_end_months.get(q, 12)
        return f"{year}-{month:02d}-30" if month != 2 else f"{year}-02-28"
    
    # fiscal 2025 / fiscal year 2025
    m = re.match(r'fiscal\s*(?:year\s*)?(\d{4})', deadline_lower)
    if m:
        return f"{m.group(1)}-12-31"
    
    # FY2025
    m = re.match(r'fy\s*(\d{4})', deadline_lower)
    if m:
        return f"{m.group(1)}-12-31"
    
    # H1/H2 2024
    m = re.match(r'h([12])\s*(\d{4})', deadline_lower)
    if m:
        half, year = int(m.group(1)), int(m.group(2))
        return f"{year}-06-30" if half == 1 else f"{year}-12-31"
    
    # Just a year
    m = re.match(r'^(\d{4})$', deadline_lower)
    if m:
        return f"{m.group(1)}-12-31"
    
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 7: STAGE 2 — EVIDENCE SEARCH & MATCHING
# ═══════════════════════════════════════════════════════════════════════════════

def _search_evidence_fast(
    promise: Dict[str, Any],
    all_filings: List[Dict[str, Any]],
    transcripts: List[Any],
    tokenized_cache: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    """Fast evidence search using pre-tokenized sentence cache.
    
    Searches all filings and transcripts dated AFTER the promise date.
    Returns top N candidates by keyword match count.
    """
    search_terms = promise.get("search_terms", [])
    if not search_terms:
        return []
    
    terms_lower = [t.lower() for t in search_terms]
    promise_date = promise.get("source", {}).get("filing_date", "")
    
    candidates = []
    
    for filing in all_filings:
        if filing.get('filing_date', '') <= promise_date:
            continue
        # Skip the same filing the promise came from
        promise_acc = promise.get('source', {}).get('accession', '')
        if promise_acc and filing.get('accession_number', '') == promise_acc:
            continue
            continue
        acc = filing.get('accession_number', '')
        sents = tokenized_cache.get(acc, [])
        for i, sent in enumerate(sents):
            sent_lower = sent.lower()
            matches = sum(1 for t in terms_lower if t in sent_lower)
            if matches >= KEYWORD_MATCH_THRESHOLD:
                ctx_start = max(0, i - 1)
                ctx_end = min(len(sents), i + 2)
                context = " ".join(sents[ctx_start:ctx_end])
                candidates.append({
                    "text": context,
                    "sentence": sent,
                    "filing_type": filing.get('form', ''),
                    "filing_date": filing.get('filing_date', ''),
                    "accession_number": acc,
                    "htm_url": filing.get('htm_url', ''),
                    "match_count": matches,
                })
    
    for t in transcripts:
        t_date = str(t.get('date', '') if isinstance(t, dict) else (t.date if hasattr(t, 'date') and t.date else ''))
        if t_date <= promise_date:
            continue
        key = f"TR_{t_date}"
        sents = tokenized_cache.get(key, [])
        for i, sent in enumerate(sents):
            sent_lower = sent.lower()
            matches = sum(1 for t in terms_lower if t in sent_lower)
            if matches >= KEYWORD_MATCH_THRESHOLD:
                ctx_start = max(0, i - 1)
                ctx_end = min(len(sents), i + 2)
                context = " ".join(sents[ctx_start:ctx_end])
                candidates.append({
                    "text": context,
                    "sentence": sent,
                    "filing_type": "Transcript",
                    "filing_date": t_date,
                    "accession_number": "",
                    "htm_url": getattr(t, 'source_url', '') or '',
                    "match_count": matches,
                })
    
    candidates.sort(key=lambda x: x["match_count"], reverse=True)
    return candidates[:STAGE2_TOP_N_CANDIDATES]


def search_evidence_candidates(
    promise: Dict[str, Any],
    all_filings: List[Dict[str, Any]],
    transcripts: List[Any],
) -> List[Dict[str, Any]]:
    """For a single promise, search all later documents for evidence.
    
    Uses loose keyword overlap: ≥2 matching search_terms in a sentence.
    """
    search_terms = promise.get("search_terms", [])
    if not search_terms:
        return []
    
    # Convert search terms to lowercase for case-insensitive matching
    terms_lower = [t.lower() for t in search_terms]
    promise_date = promise.get("source", {}).get("filing_date", "")
    
    candidates = []
    
    # Search 10-K and 10-Q filings dated AFTER the promise
    for filing in all_filings:
        if filing.get('filing_date', '') <= promise_date:
            continue
        # Skip the same filing the promise came from
        promise_acc = promise.get('source', {}).get('accession', '')
        if promise_acc and filing.get('accession_number', '') == promise_acc:
            continue
            continue
        
        # Get text (from cache or skip for now — in production, fetch MD&A)
        # For Stage 2, we search the filing metadata and any cached text
        acc = filing.get('accession_number', '')
        from src.providers.filings.sec_edgar import get_cached_text
        text = get_cached_text(promise.get("source", {}).get("ticker", ""), acc)
        if not text:
            continue
        
        sentences = split_sentences(text)
        for i, sent in enumerate(sentences):
            sent_lower = sent.lower()
            matches = sum(1 for t in terms_lower if t in sent_lower)
            if matches >= KEYWORD_MATCH_THRESHOLD:
                # Get surrounding context (±1 sentence)
                context_start = max(0, i - 1)
                context_end = min(len(sentences), i + 2)
                context = " ".join(sentences[context_start:context_end])
                
                candidates.append({
                    "text": context,
                    "sentence": sent,
                    "filing_type": filing.get('form', ''),
                    "filing_date": filing.get('filing_date', ''),
                    "accession_number": acc,
                    "htm_url": filing.get('htm_url', ''),
                    "match_count": matches,
                })
    
    # Also search transcripts
    for t in transcripts:
        t_date = str(t.get('date', '') if isinstance(t, dict) else (t.date if hasattr(t, 'date') and t.date else ''))
        if t_date <= promise_date:
            continue
        
        content = getattr(t, 'content', '') or ''
        if not content:
            continue
        
        sentences = split_sentences(content)
        for i, sent in enumerate(sentences):
            sent_lower = sent.lower()
            matches = sum(1 for t in terms_lower if t in sent_lower)
            if matches >= KEYWORD_MATCH_THRESHOLD:
                context_start = max(0, i - 1)
                context_end = min(len(sentences), i + 2)
                context = " ".join(sentences[context_start:context_end])
                
                candidates.append({
                    "text": context,
                    "sentence": sent,
                    "filing_type": "Transcript",
                    "filing_date": t_date,
                    "accession_number": "",
                    "htm_url": getattr(t, 'source_url', '') or '',
                    "match_count": matches,
                })
    
    # Sort by match count and take top N
    candidates.sort(key=lambda x: x["match_count"], reverse=True)
    return candidates[:STAGE2_TOP_N_CANDIDATES]


def build_stage2_prompt(
    promises: List[Dict[str, Any]],
    all_evidence: List[Dict[str, Any]],
) -> str:
    """Build the unified Stage 2 LLM prompt for evidence matching."""
    # Build promises section
    promises_str = ""
    for i, p in enumerate(promises):
        pid = p.get("promise_id", f"PROMISE_{i:03d}")
        promises_str += f"Promise {pid}:\n"
        promises_str += f"  Text: \"{p.get('text', '')}\"\n"
        promises_str += f"  Deadline: {p.get('deadline', 'none')}\n"
        promises_str += f"  Measurable outcome: {p.get('measurable_outcome', '')}\n\n"
    
    # Build evidence section
    evidence_str = ""
    for i, ev in enumerate(all_evidence):
        eid = f"EVID_{i:03d}"
        evidence_str += f"{eid} [{ev.get('filing_type', '')} {ev.get('filing_date', '')}]: \"{ev.get('sentence', '')}\"\n\n"
    
    prompt = f"""You are a forensic financial analyst. Match management promises 
to evidence from later filings, and determine the outcome of each.

## Promises
{promises_str}

## Evidence Candidates
{evidence_str}

## Task
For each promise, find the single best matching evidence candidate 
(if any). Then classify the outcome:

- KEPT: Evidence clearly confirms the promise was fulfilled.
- PARTIAL: Some but not all of the promise was fulfilled, or it 
  was fulfilled but significantly late or below target.
- BROKEN: The deadline has passed and no evidence of completion 
  exists, OR later evidence explicitly contradicts the promise.
- PENDING: The deadline has not yet arrived (regardless of evidence).
- UNRESOLVED: No deadline was given, and no clear evidence exists 
  to confirm or contradict.

For evidence: if the status is KEPT or PARTIAL, specify the 
evidence sentence ID, the filing where it appears, and the date.
If BROKEN, PENDING, or UNRESOLVED, set evidence fields to null.

Also provide:
- confidence: "high", "medium", or "low"
- reasoning: one-sentence explanation.

## Output Format
Return ONLY a JSON array. One object per promise.

[
  {{
    "promise_id": "PROMISE_001",
    "status": "KEPT",
    "evidence_id": "EVID_001",
    "evidence_filing": "10-Q Q3 FY2025",
    "evidence_date": "2024-11-15",
    "confidence": "high",
    "reasoning": "61% gross margin explicitly exceeds the 60% target."
  }},
  ...
]

## CRITICAL
- Return ONLY the JSON array, no other text.
- Every promise in the input MUST have an entry in the output.
- If no evidence matches a promise, still include it with 
  status = BROKEN, PENDING, or UNRESOLVED as appropriate.
- Temperature is zero. Be deterministic."""
    
    return prompt


def parse_stage2_response(
    response: str,
    promises: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Parse and validate the Stage 2 LLM response."""
    if not response:
        return []
    
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
    
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r'\[.*\]', cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                logger.warning("Failed to parse Stage 2 response")
                return []
        else:
            return []
    
    if not isinstance(data, list):
        return []
    
    valid_statuses = {"KEPT", "PARTIAL", "BROKEN", "PENDING", "UNRESOLVED"}
    results = []
    
    for item in data:
        pid = item.get("promise_id", "")
        status = item.get("status", "").upper()
        if status not in valid_statuses:
            continue
        
        result = {
            "promise_id": pid,
            "status": status,
            "evidence_id": item.get("evidence_id") if status in ("KEPT", "PARTIAL") else None,
            "evidence_filing": item.get("evidence_filing") if status in ("KEPT", "PARTIAL") else None,
            "evidence_date": item.get("evidence_date") if status in ("KEPT", "PARTIAL") else None,
            "confidence": item.get("confidence", "low"),
            "reasoning": item.get("reasoning", ""),
        }
        results.append(result)
    
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# LAYER 8: SCORING
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_decay(
    status: str,
    promise_date_str: str,
    deadline_date_str: Optional[str],
    evidence_date_str: Optional[str],
) -> float:
    """Calculate recency decay factor for a promise."""
    today = date.today()
    
    # Parse dates
    promise_date = None
    if promise_date_str:
        try:
            promise_date = date.fromisoformat(promise_date_str)
        except ValueError:
            pass
    
    deadline_date = None
    if deadline_date_str:
        try:
            deadline_date = date.fromisoformat(deadline_date_str)
        except ValueError:
            pass
    
    evidence_date = None
    if evidence_date_str:
        try:
            evidence_date = date.fromisoformat(evidence_date_str)
        except ValueError:
            pass
    
    if status in ("KEPT", "PARTIAL", "BROKEN"):
        resolution_date = evidence_date if status in ("KEPT", "PARTIAL") else (
            min(deadline_date, today) if deadline_date else today
        )
        if resolution_date:
            years_since = (today - resolution_date).days / 365.25
            return max(DECAY_FLOOR_RESOLVED, 1.0 - (years_since * DECAY_RATE_RESOLVED))
        return DECAY_FLOOR_RESOLVED
    
    elif status == "PENDING":
        if promise_date:
            years_since = (today - promise_date).days / 365.25
            return max(DECAY_FLOOR_PENDING, 1.0 - (years_since * DECAY_RATE_PENDING))
        return DECAY_FLOOR_PENDING
    
    elif status == "UNRESOLVED":
        if promise_date:
            years_since = (today - promise_date).days / 365.25
            if years_since > STALE_YEARS:
                return DECAY_STALE_FIXED  # STALE
            return max(DECAY_FLOOR_PENDING, 1.0 - (years_since * DECAY_RATE_PENDING))
        return DECAY_FLOOR_PENDING
    
    return 1.0


def calculate_score(promises: List[Dict[str, Any]]) -> Tuple[float, Dict[str, float]]:
    """Calculate the 0-100 credibility score from classified promises."""
    weighted_kept = 0.0
    weighted_partial = 0.0
    weighted_broken = 0.0
    
    for p in promises:
        severity = p.get("severity", 5)
        decay = p.get("decay", 0.5)
        weighted = severity * decay
        p["weighted_importance"] = round(weighted, 2)
        
        status = p.get("status", "UNRESOLVED")
        if status == "KEPT":
            weighted_kept += weighted
        elif status == "PARTIAL":
            weighted_partial += weighted
        elif status in ("BROKEN", "STALE"):
            weighted_broken += weighted
        # PENDING and UNRESOLVED (non-stale) excluded
    
    weighted_total = weighted_kept + weighted_partial + weighted_broken
    
    if weighted_total > 0:
        score = ((weighted_kept + 0.5 * weighted_partial) / weighted_total) * 100
    else:
        score = 50.0  # No track record yet — neutral
    
    return round(score, 1), {
        "weighted_kept": round(weighted_kept, 2),
        "weighted_partial": round(weighted_partial, 2),
        "weighted_broken": round(weighted_broken, 2),
        "weighted_total": round(weighted_total, 2),
    }


def select_top_promises(promises: List[Dict[str, Any]], n: int = 5) -> List[Dict[str, Any]]:
    """Select top N promises by weighted importance, prioritizing BROKEN > PARTIAL > KEPT > PENDING."""
    status_priority = {"BROKEN": 0, "STALE": 0, "PARTIAL": 1, "KEPT": 2, "PENDING": 3, "UNRESOLVED": 4}
    
    sorted_promises = sorted(
        promises,
        key=lambda p: (
            status_priority.get(p.get("status", "UNRESOLVED"), 4),
            -p.get("weighted_importance", 0),
        )
    )
    
    return sorted_promises[:n]


# Module-level run function for backend/agents.py compatibility
def run_promise_audit(ticker: str) -> Dict[str, Any]:
    """Run Agent 2 and return results dict."""
    agent = PromiseAuditAgent()
    return agent.run(ticker)


print("promise_audit.py module ready")
print(f"Functions: run_promise_audit, PromiseAuditAgent.run")
print(f"Constants: {len(FUTURE_KEYWORDS)} future keywords, {len(METRIC_PATTERNS)} metric patterns, {len(EXCLUDE_PATTERNS)} exclude patterns")
