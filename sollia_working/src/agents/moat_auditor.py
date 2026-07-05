"""
Sollia Agent 6 — Moat Auditor.

The second market-style agent (after Agent 5, the Constraint Auditor). It
pressure-tests whether a company's claimed competitive advantages ("moats") hold
up against what competitors say in their own SEC filings. For each of 8 moat
types it (1) extracts the company's own claim, (2) searches competitor filings for
a challenge, and (3) renders a verdict — DIRECT THREAT / WATCH / NO THREAT —
answering: is the challenge real? what's the timeline? what's the magnitude?

This is an AUDIT tool, not a forecast and not a market-share predictor. Like
Agent 5 it reports no bull/bear score (score is null; the synthesizer excludes it).

Provenance contract: every claim and every challenge is a VERBATIM sentence
extracted deterministically from a real SEC document, carried with its source,
date and EDGAR URL. The LLM never writes a quote — it only (a) classifies
extracted sentences into moat types, (b) SELECTS the clearest claim / strongest
challenge BY INDEX from a list we built, and (c) writes the analysis paragraph.
So a quote can never be hallucinated: it was pulled from the filing before the
model ever saw it.

Determinism: competitor lists, moat extractions and sector classification are
cached per ticker; all LLM calls are temperature 0 and byte-identically cached.
`run_timestamp`/`elapsed_ms` are the only time-varying fields — strip them to
compare two runs.

Environment: needs only SEC filings (10-K, 8-K, earnings-call EX-99.1) + the LLM
chain — both live here. No PDF parser or extra API keys (unlike Agent 5). spaCy
isn't installed, so named-competitor extraction is regex proper-noun detection
validated against the live SEC ticker database (only real SEC filers survive).
"""
from __future__ import annotations

import os
import re
import sys
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Callable

import requests

# Make `from src.xxx import ...` work standalone (file is src/agents/moat_auditor.py).
_SOLLIA_WORKING = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SOLLIA_WORKING not in sys.path:
    sys.path.insert(0, _SOLLIA_WORKING)

from src.config.llm_config import call_llm, start_tier  # noqa: E402
from src.data.sector_taxonomy import (  # noqa: E402
    SECTORS, match_sector, sector_list_text, same_group, SECTOR_TO_GROUP,
)
from src.data.moat_types import (  # noqa: E402
    MOAT_NAMES, MOATS, MOAT_DEFINITIONS, claim_moats_for_sentence,
    sentence_challenges_moat, moat_list_text,
)

try:  # transcripts + filings (already used by Agents 1-5)
    from src.providers.transcripts.sec_transcript import SECTranscriptProvider
    from src.providers.filings.sec_edgar import (
        SECEdgarProvider, get_cached_text, cache_text,
    )
except Exception:  # pragma: no cover - providers always present in the pipeline
    SECTranscriptProvider = None  # type: ignore
    SECEdgarProvider = None  # type: ignore
    get_cached_text = cache_text = None  # type: ignore

try:
    from src.utils.run_archive import save_run
except Exception:  # pragma: no cover
    def save_run(*_a, **_k):  # type: ignore
        return None


# ── cache locations ───────────────────────────────────────────────────────────
_CACHE_ROOT = os.path.join(_SOLLIA_WORKING, ".cache")
MOAT_CACHE_DIR = os.path.join(_CACHE_ROOT, "moat")                 # competitors + moats per ticker
SECTOR55_CACHE_DIR = os.path.join(_CACHE_ROOT, "sector_taxonomy")  # 55-sector classification (distinct from Agent 5's 14-sector cache)
os.makedirs(MOAT_CACHE_DIR, exist_ok=True)
os.makedirs(SECTOR55_CACHE_DIR, exist_ok=True)

AGENT_ID = "moat_auditor"

# ── tunables (bound runtime + LLM/SEC volume; see PART 9) ──────────────────────
MAX_COMPETITORS = 7                 # PART 2 Step 4 — cap the competitor list
COMPETITORS_TO_SEARCH = 5           # how many competitors we actually fetch+search (3-5 ideal)
N_TRANSCRIPTS_COMPANY = 4           # PART 3 Step 1 — last N company earnings calls
N_TRANSCRIPTS_COMPETITOR = 2        # PART 4 Step 1 — last N competitor earnings calls
N_8K_COMPETITOR = 4                 # PART 4 Step 1 — last N competitor 8-Ks
MAX_CANDIDATE_SENTENCES = 70        # classification prompt size guard
MAX_CHALLENGE_CANDIDATES = 10       # per-moat challenge candidates sent to the selector
SENT_MIN_LEN = 45                   # drop sentence fragments / nav junk
SENT_MAX_LEN = 500                  # drop run-on table dumps
_HEADERS = {"User-Agent": "Sollia contact@sollia.app", "Accept-Encoding": "gzip, deflate"}


# ===========================================================================
# Text helpers (deterministic — regex, no spaCy dependency)
# ===========================================================================
def _norm(s: Optional[str]) -> str:
    if not s:
        return ""
    s = re.sub(r"[‘’“”\"']", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _quote_in_text(quote: Optional[str], source: Optional[str]) -> bool:
    """True when a quote is present in the source (alnum/whitespace-normalized,
    with an 80%-prefix fallback for trailing truncation) — same contract Agents
    1-5 use so a consumer can trust a citation without re-reading the source."""
    q, src = _norm(quote), _norm(source)
    if not q or not src:
        return False
    if q in src:
        return True
    cut = max(1, int(len(q) * 0.8))
    probe = q[:cut]
    return len(probe) >= 20 and probe in src


_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9$\"'])")


def _split_sentences(text: str) -> List[str]:
    """Deterministic sentence segmentation (spaCy stand-in). Collapses whitespace
    first so extracted sentences are clean + verbatim-comparable."""
    if not text:
        return []
    text = re.sub(r"\s+", " ", text).strip()
    return [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]


def _strip_html(raw: str) -> str:
    """Strip HTML + common XBRL/entity noise; collapse whitespace. Regex-based so
    the text the LLM sees == the text quote-verification searches against."""
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&#160;", " ")
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"\b(?:iso4217|xbrli|us-gaap|dei|srt):\S+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_sentences(text: str) -> List[str]:
    """Sentences in a sane length band, de-duplicated (preserving order). Page
    furniture is stripped (2026-07-01 Part 6 fix): a leading page number +
    "Table of Contents" run-in otherwise fuses with the next heading and surfaces
    as a junk "claim" ("7 Table of Contents Leveraging our intellectual
    property, or IP."). Heading fragments with no verb-bearing prose are dropped."""
    out, seen = [], set()
    for s in _split_sentences(text):
        # strip leading page-number / Table of Contents run-ins
        s = re.sub(r"^(?:\d{1,3}\s+)?Table of Contents\s+", "", s).strip()
        if "table of contents" in s.lower():
            continue
        if not (SENT_MIN_LEN <= len(s) <= SENT_MAX_LEN):
            continue
        if len(s.split()) < 8:  # heading fragments ("Leveraging our IP.") aren't claims
            continue
        key = _norm(s)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _extract_json(text: Optional[str]):
    """JSON extraction from an LLM response via the shared tolerant extractor
    (fences, preamble, trailing commas, single quotes, comments). Keeps this
    module's None-on-failure contract."""
    if not text:
        return None
    try:
        from src.utils.json_extractor import extract_json
        return extract_json(text)
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_cache(path: str):
    try:
        if os.path.exists(path):
            with open(path) as fh:
                return json.load(fh)
    except Exception:
        pass
    return None


def _write_cache(path: str, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        pass


# ===========================================================================
# SEC document fetch layer (full raw HTML cached in its own namespace)
# ===========================================================================
def _fetch_filing_html(prov, ticker: str, filing: Dict[str, Any]) -> str:
    """Full raw 10-K/10-Q HTML for a filing, cached under content_type='moat_raw'
    so it never collides with another agent's text-cache entry (filings-cache
    contract). Returns '' if the document can't be located/fetched."""
    acc = filing.get("accession_number", "")
    if cache_text is not None and acc:
        cached = get_cached_text(ticker, acc, content_type="moat_raw")
        if cached:  # empty string = a cached FAILED fetch; treat as a miss
            return cached
    cik = filing.get("cik", "")
    acc_nd = filing.get("accession_nodash", acc.replace("-", ""))
    doc = filing.get("primary_doc", "")
    if not doc and cik and acc:
        try:
            doc = prov._find_primary_doc(cik, acc_nd, acc)
        except Exception:
            doc = ""
    html = ""
    if doc and cik:
        try:
            html = prov.fetch_html(cik, acc_nd, doc, ticker) or ""
        except Exception:
            html = ""
    if cache_text is not None and acc and html:  # never cache a failed fetch
        cache_text(ticker, acc, html, content_type="moat_raw")
    return html


# Debt-offering / legal-note 8-K exhibit boilerplate. These exhibits are pure
# noise for a moat-challenge search (and one of them — the DTC "Depository Trust
# Company" note legend — collided with a distribution keyword), so they're dropped.
_NOISE_8K_MARKERS = (
    "depository trust company", "cede & co", "unless this note", "indenture",
    "registration rights agreement", "promissory note", "aggregate principal amount",
    "notes will bear interest", "the global note", "supplemental indenture",
)


def _is_noise_exhibit(text: str) -> bool:
    """True for debt/legal-note 8-K exhibits that carry no competitive signal:
    known note boilerplate markers, or text that is overwhelmingly UPPER-CASE (the
    all-caps legal legends)."""
    low = text[:4000].lower()
    if any(mk in low for mk in _NOISE_8K_MARKERS):
        return True
    letters = [c for c in text[:2000] if c.isalpha()]
    if len(letters) > 200 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        return True
    return False


def _fetch_8k_docs(cik: str, acc: str) -> List[Dict[str, str]]:
    """Readable 8-K documents (body + EX-99 exhibits) for an accession, each as
    {url, text}. Skips the -index page, R<n>.htm XBRL render fragments (the
    cover-stub noise that made earlier agents confabulate), and debt/legal-note
    boilerplate exhibits (_is_noise_exhibit). Best-effort."""
    out: List[Dict[str, str]] = []
    if not cik or not acc:
        return out
    try:
        cik_int = int(cik)
        acc_nd = acc.replace("-", "")
        base = "https://www.sec.gov/Archives/edgar/data/%d/%s" % (cik_int, acc_nd)
        idx = requests.get(base + "/index.json", headers=_HEADERS, timeout=30)
        if idx.status_code != 200:
            return out
        for it in idx.json().get("directory", {}).get("item", []):
            low = it.get("name", "").lower()
            if not low.endswith(".htm"):
                continue
            if low.endswith("-index.htm") or re.match(r"^r\d+\.htm$", low):
                continue
            time.sleep(0.1)
            try:
                r = requests.get(base + "/" + it["name"], headers=_HEADERS, timeout=30)
            except Exception:
                continue
            if r.status_code == 200:
                txt = _strip_html(r.text)
                if len(txt) > 200 and not _is_noise_exhibit(txt):
                    out.append({"url": "%s/%s" % (base, it["name"]), "text": txt[:60000]})
    except Exception:
        pass
    return out


# ── 10-K section extraction (genuine-start detection skips TOC + cross-refs) ───
_CROSSREF_WORDS = {
    "for", "under", "above", "below", "of", "and", "in", "to", "section",
    "regarding", "generally", "set", "contained", "beginning", "herein", "as",
    "discussion", "described", "include", "including", "see", "refer",
}


def _genuine_section_start(clean: str, header_re: "re.Pattern") -> Optional[int]:
    """Pick the real section header from all matches: skip a header followed by a
    page number (TOC), a '(continued)' running header, or a lowercase preposition
    (an inline cross-reference). Falls back to the first match."""
    fallback = None
    for m in header_re.finditer(clean):
        s = m.start()
        seg = clean[s:s + 110]
        hm = header_re.match(seg)
        if not hm:
            continue
        if "continued" in seg[:hm.end() if hm else 40].lower():
            continue
        after = seg[hm.end():].lstrip()
        if not after or after[0].isdigit():
            continue  # TOC page number
        if fallback is None:
            fallback = s
        words = re.findall(r"[a-z]+", after.lower())
        if words and words[0] in _CROSSREF_WORDS:
            continue  # cross-reference
        return s
    return fallback


def _extract_section(clean: str, header_re: "re.Pattern",
                     end_res: List["re.Pattern"], cap: int = 220000) -> str:
    """Generic 10-K section slice: genuine start header -> first following end
    header (or a defensive cap). Returns '' if the start header isn't found."""
    if not clean:
        return ""
    start = _genuine_section_start(clean, header_re)
    if start is None:
        return ""
    rest = clean[start + 8:]
    end_rel = None
    for er in end_res:
        m = er.search(rest)
        if m:
            end_rel = m.start() if end_rel is None else min(end_rel, m.start())
    end = (start + 8 + end_rel) if end_rel is not None else min(len(clean), start + cap)
    return re.sub(r"\s+", " ", clean[start:end]).strip()


_RE_ITEM1 = re.compile(r"item\s*1[\.\:\)\s]+business", re.IGNORECASE)
_RE_ITEM1A = re.compile(r"item\s*1a[\.\:\)\s]*risk\s*factors", re.IGNORECASE)
_RE_ITEM1B = re.compile(r"item\s*1b\b", re.IGNORECASE)
_RE_ITEM1C = re.compile(r"item\s*1c\b", re.IGNORECASE)
_RE_ITEM2 = re.compile(r"item\s*2\b", re.IGNORECASE)
_RE_ITEM7 = re.compile(r"item\s*7[\.\:\)\s]+management", re.IGNORECASE)
_RE_ITEM7A = re.compile(r"item\s*7a\b", re.IGNORECASE)
_RE_ITEM8 = re.compile(r"item\s*8\b", re.IGNORECASE)


def extract_item1_business(clean: str) -> str:
    return _extract_section(clean, _RE_ITEM1, [_RE_ITEM1A, _RE_ITEM2])


def extract_item1a_risk(clean: str) -> str:
    return _extract_section(clean, _RE_ITEM1A, [_RE_ITEM1B, _RE_ITEM1C, _RE_ITEM2])


def extract_item7_mda(clean: str) -> str:
    return _extract_section(clean, _RE_ITEM7, [_RE_ITEM7A, _RE_ITEM8])


def extract_competition_subsection(item1_text: str) -> str:
    """The 'Competition' discussion inside Item 1 (where named competitors live).
    Returns a window starting at the Competition header; falls back to the head of
    Item 1 if there's no explicit header."""
    if not item1_text:
        return ""
    m = re.search(r"\bcompetiti(?:on|ve\s+landscape)\b", item1_text, re.IGNORECASE)
    if m:
        return item1_text[m.start():m.start() + 5000]
    return item1_text[:6000]


# ===========================================================================
# SEC company index (ticker <-> name) for verifying/identifying competitors
# ===========================================================================
_SEC_INDEX: Optional[Dict[str, Any]] = None
_SUFFIX_TOKENS = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies",
    "ltd", "limited", "llc", "lp", "plc", "holdings", "holding", "group", "the",
    "sa", "ag", "nv", "se", "class", "common", "stock", "ordinary", "shares",
    "trust", "reit", "spa", "ab", "as", "oyj", "cl",
}
_COMMON_WORD_BLOCK = {
    "we", "our", "us", "company", "competition", "competitors", "products",
    "services", "business", "market", "markets", "customers", "others", "other",
    "these", "their", "such", "those", "industry", "technology", "technologies",
    "solutions", "systems", "global", "international", "national", "general",
    "american", "united", "states", "new", "first", "world", "group", "the",
    "certain", "various", "including", "include", "among", "many", "some", "may",
}


def _company_core(title: str) -> str:
    """Distinctive core of a company name: lowercase, punctuation-stripped, with
    trailing corporate-form suffix tokens removed. 'ADVANCED MICRO DEVICES INC' ->
    'advanced micro devices'; 'Intel Corp' -> 'intel'."""
    t = re.sub(r"[^a-z0-9 ]", " ", (title or "").lower())
    toks = [w for w in t.split() if w]
    while toks and toks[-1] in _SUFFIX_TOKENS:
        toks.pop()
    while toks and toks[0] in _SUFFIX_TOKENS:
        toks.pop(0)
    return " ".join(toks).strip()


def _load_sec_index() -> Dict[str, Any]:
    """Load SEC company_tickers.json into {by_ticker, core_to_tickers}. Cached on
    disk (and in-process) so repeated runs are fast + deterministic."""
    global _SEC_INDEX
    if _SEC_INDEX is not None:
        return _SEC_INDEX
    disk = os.path.join(MOAT_CACHE_DIR, "sec_company_index.json")
    cached = _read_cache(disk)
    if cached and cached.get("by_ticker"):
        cached["core_to_tickers"] = {k: set(v) for k, v in cached.get("core_to_tickers", {}).items()}
        _SEC_INDEX = cached
        return _SEC_INDEX
    by_ticker: Dict[str, Any] = {}
    core_to_tickers: Dict[str, set] = {}
    try:
        resp = requests.get("https://www.sec.gov/files/company_tickers.json",
                            headers=_HEADERS, timeout=20)
        resp.raise_for_status()
        for item in resp.json().values():
            tk = str(item.get("ticker", "")).upper()
            if not tk:
                continue
            cik = str(item.get("cik_str", "")).zfill(10)
            title = item.get("title", "")
            by_ticker.setdefault(tk, {"cik": cik, "title": title, "ticker": tk})
            core = _company_core(title)
            if core:
                core_to_tickers.setdefault(core, set()).add(tk)
    except Exception:
        pass
    _SEC_INDEX = {"by_ticker": by_ticker, "core_to_tickers": core_to_tickers}
    _write_cache(disk, {"by_ticker": by_ticker,
                        "core_to_tickers": {k: sorted(v) for k, v in core_to_tickers.items()}})
    return _SEC_INDEX


def _resolve_ticker(tk: str) -> Optional[Dict[str, Any]]:
    """SEC record for a ticker (the verify gate: only real SEC filers resolve)."""
    if not tk:
        return None
    return _load_sec_index()["by_ticker"].get(str(tk).strip().upper().replace(".", "-"))


# ===========================================================================
# PART 1b — sector classification (one cached LLM call, 55-sector taxonomy)
# ===========================================================================
_CLASSIFY_PROMPT = (
    "Classify the company {company} (ticker: {ticker}) into the single CLOSEST "
    "sector from this list, based on its PRIMARY business.{context}\n\nSECTORS:\n"
    + sector_list_text()
    + "\n\nReturn ONLY the exact sector name from the list (or UNSUPPORTED if none "
      "fits), nothing else."
)

# Cache schema version. v2 = classification GROUNDED in the company's own 10-K
# Item 1 text (2026-07-05 Part-5 fix): the old "classify by name alone" call
# answered from the model's world knowledge — Groq-8B did not know OKLO (a
# 2024 de-SPAC) and filed a nuclear-reactor company under "Cloud
# Infrastructure & Platform", which then drove a fully-hallucinated
# cloud-provider competitor set. v1 entries re-classify on next run.
_SECTOR_CACHE_VERSION = 2


def _classify_sector(ticker: str, company: str, log: List[Dict[str, str]],
                     business_text: str = "") -> str:
    """Return a canonical 55-taxonomy sector (or 'UNSUPPORTED'). Cached per ticker
    at .cache/sector_taxonomy/{TICKER}.json (distinct from Agent 5's cache).
    `business_text` (the 10-K Item 1 opening) grounds the answer in what the
    company SAYS it does instead of what the model happens to know."""
    cache_path = os.path.join(SECTOR55_CACHE_DIR, "%s.json" % ticker.upper())
    cached = _read_cache(cache_path)
    if cached and cached.get("sector") and cached.get("v") == _SECTOR_CACHE_VERSION:
        log.append({"step": "CLASSIFY", "detail": "Sector (cached): %s" % cached["sector"]})
        return cached["sector"]
    excerpt = re.sub(r"\s+", " ", business_text or "").strip()[:2500]
    context = ("\n\nThe company's own 10-K business description begins:\n\"%s\""
               % excerpt) if excerpt else ""
    prompt = _CLASSIFY_PROMPT.format(company=company or ticker, ticker=ticker.upper(),
                                     context=context)
    try:
        text, _meta = call_llm(prompt, max_tokens=40, temperature=0.0,
                               provider_idx=start_tier(AGENT_ID), agent=AGENT_ID,
                               stage="classify", ticker=ticker.upper())
        sector = match_sector(text)
        if sector is None and "unsupported" not in (text or "").lower():
            # Unparseable answer (mid-storm garbage) — one fresh retry past the
            # byte cache so a poisoned response is not served forever.
            text, _meta = call_llm(prompt, max_tokens=40, temperature=0.0,
                                   provider_idx=start_tier(AGENT_ID), agent=AGENT_ID,
                                   stage="classify", ticker=ticker.upper(),
                                   bypass_cache=True)
            sector = match_sector(text)
    except Exception as exc:
        log.append({"step": "CLASSIFY", "detail": "Classification LLM failed: %s" % str(exc)[:120]})
        return "UNSUPPORTED"
    sector = sector or "UNSUPPORTED"
    _write_cache(cache_path, {"ticker": ticker.upper(), "company": company,
                              "sector": sector, "v": _SECTOR_CACHE_VERSION,
                              "grounded": bool(excerpt),
                              "classified_at": _now_iso()})
    log.append({"step": "CLASSIFY", "detail": "Classified %s as %s%s"
                % (ticker.upper(), sector,
                   " (grounded in Item 1)" if excerpt else " (name-only)")})
    return sector


# ===========================================================================
# PART 2 — competitor identification
# ===========================================================================
_EXCHANGE_TICKER_RE = re.compile(
    r"\((?:NYSE|NASDAQ|Nasdaq|NYSE\s*American|NYSE\s*Arca|NYSEAMERICAN|OTC)\s*[:\s]\s*([A-Z]{1,5})\)",
    re.IGNORECASE)
_PROPER_NOUN_RE = re.compile(r"\b([A-Z][A-Za-z0-9&.\-]+(?:\s+[A-Z][A-Za-z0-9&.\-]+){0,4})\b")


def _named_competitors_from_text(text: str, source_label: str, self_ticker: str,
                                 self_core: str) -> List[Dict[str, str]]:
    """Deterministic named-competitor extraction from a block of filing text.

    Two signals: (1) explicit '(NASDAQ: AMD)'-style exchange tickers, and (2)
    capitalized proper-noun phrases whose distinctive core resolves to EXACTLY ONE
    real SEC filer (ambiguous cores are skipped — the LLM step handles those). A
    candidate immediately preceded by the company's own name or a possessive
    ('our'/'its') is skipped — that's the company's OWN product (e.g. NVIDIA's
    'Cosmos' world-model), not a competitor. Only SEC-verified names survive, so
    this is high precision; callers feed it competitive-context sentences for
    recall (PART 2 Step 1)."""
    if not text:
        return []
    idx = _load_sec_index()
    by_ticker, core_to = idx["by_ticker"], idx["core_to_tickers"]
    self_short = self_core.split()[0] if self_core else ""
    found: Dict[str, Dict[str, str]] = {}

    def add(tk: str):
        tk = tk.upper().replace(".", "-")
        if tk == self_ticker or tk in found:
            return
        rec = by_ticker.get(tk)
        if rec:
            found[tk] = {"name": rec["title"], "ticker": tk,
                         "cik": rec["cik"], "source": source_label}

    for m in _EXCHANGE_TICKER_RE.finditer(text):
        add(m.group(1))

    for m in _PROPER_NOUN_RE.finditer(text):
        phrase = m.group(1).strip(" .&-")
        core = _company_core(phrase)
        if not core or core == self_core:
            continue
        toks = core.split()
        if len(toks) == 1 and (len(core) < 4 or core in _COMMON_WORD_BLOCK):
            continue
        # own-product guard: preceding word is a possessive or the company itself
        pre = text[max(0, m.start() - 22):m.start()].lower()
        if re.search(r"(?:\bour\b|\bits\b|\bmy\b)\s*$", pre) or (self_short and self_short in pre):
            continue
        tks = core_to.get(core)
        if tks and len(tks) == 1:
            add(next(iter(tks)))
    return list(found.values())


_COMPETITOR_PROMPT = (
    "The company {company} (ticker: {ticker}) operates primarily in the sector: "
    "{sector}.{context} Below is the complete list of Sollia sectors. Identify the top 3-7 "
    "PUBLIC companies that directly compete with {company}.\n\n"
    "Rank by DIRECTNESS of competition with the business described above: "
    "companies building the SAME KIND of product or technology first (direct "
    "technology peers), then (1) substitute products in the same sector; "
    "(2) companies in a DIFFERENT sector with a known cross-sector competitive "
    "overlap with {company} (e.g. a cloud provider making its own chips); "
    "(3) large customers building in-house alternatives. A generic same-sector "
    "incumbent belongs on the list ONLY if it genuinely competes for the same "
    "customers with a comparable offering.\n"
    "Only include companies that file 10-Ks with the SEC. Use the real US ticker. "
    "If you cannot identify genuine competitors, return an empty array [] — "
    "NEVER guess unrelated companies.\n\n"
    "SECTORS:\n" + sector_list_text() + "\n\n"
    "Return ONLY a JSON array, no prose:\n"
    '[{{"competitor": "Company Name", "ticker": "TICKER", "sector": "exact sector '
    'name from the list", "rationale": "one sentence on why they compete"}}]'
)


def _llm_competitors(company: str, ticker: str, sector: str,
                     log: List[Dict[str, str]],
                     business_text: str = "") -> List[Dict[str, str]]:
    excerpt = re.sub(r"\s+", " ", business_text or "").strip()[:1500]
    context = (" The company's own 10-K describes its business as: \"%s\"" % excerpt
               ) if excerpt else ""
    prompt = _COMPETITOR_PROMPT.format(company=company, ticker=ticker.upper(),
                                       sector=sector if sector != "UNSUPPORTED" else "its primary industry",
                                       context=context)
    try:
        text, _meta = call_llm(prompt, max_tokens=900, temperature=0.0,
                               provider_idx=start_tier(AGENT_ID), agent=AGENT_ID,
                               stage="competitors", ticker=ticker.upper())
    except Exception as exc:
        log.append({"step": "COMPETITORS", "detail": "Competitor LLM failed: %s" % str(exc)[:120]})
        return []
    data = _extract_json(text)
    out: List[Dict[str, str]] = []
    if isinstance(data, list):
        for d in data:
            if not isinstance(d, dict):
                continue
            tk = str(d.get("ticker", "")).strip().upper().replace(".", "-")
            name = str(d.get("competitor", "")).strip()
            if tk and name:
                out.append({"name": name, "ticker": tk,
                            "sector": str(d.get("sector", "")).strip(),
                            "rationale": str(d.get("rationale", "")).strip(),
                            "source": "llm"})
    return out


def _filter_llm_competitors(llm_comps: List[Dict[str, str]], target_sector: str
                            ) -> List[Dict[str, str]]:
    """PART 2 Step 3 — sector validation filter. Keep a competitor if it's in the
    target's sector OR its parent group OR its rationale states a cross-sector
    overlap; drop an unrelated-sector competitor with no overlap (hallucination
    guard). When the target sector is unknown, keep all (no basis to filter)."""
    if target_sector in ("UNSUPPORTED", "", None):
        return llm_comps
    out = []
    cross_kept = 0
    for c in llm_comps:
        csec = match_sector(c.get("sector", "")) or ""
        rationale = (c.get("rationale", "") or "").lower()
        # SPECIFIC overlap markers only (Part-5 tightening, 2026-07-05): the
        # old list included "compete", which appears in virtually every LLM
        # rationale and made the cross-sector escape hatch always-open.
        overlap_words = ("cross-sector", "cross sector", "in-house", "in house",
                        "custom", "vertical", "own chips", "own silicon",
                        "in-house alternative", "builds its own", "building its own",
                        "autonomous", "substitute")
        if csec == target_sector or same_group(csec, target_sector):
            c["_kept"] = "same sector/group"
            out.append(c)
        elif any(w in rationale for w in overlap_words) and cross_kept < 3:
            # Cross-sector threats are real (NVDA/GOOGL vs an automaker) but
            # bounded: at most 3, each with a SPECIFIC stated overlap.
            c["_kept"] = "cross-sector overlap"
            cross_kept += 1
            out.append(c)
        # else: unrelated sector, no specific overlap -> hallucination guard
    return out


def _merge_competitors(named: List[Dict[str, str]], llm_filtered: List[Dict[str, str]],
                       self_ticker: str, log: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """PART 2 Step 4 — merge + dedup + SEC-verify + prioritize + cap. Priority:
    10-K Item 1 named, then 10-K Item 1A, then LLM. Richest source wins a dup."""
    order = {"10-K Item 1": 0, "10-K Item 1A": 1, "llm": 2}
    by_ticker: Dict[str, Dict[str, str]] = {}
    for c in named + llm_filtered:
        tk = str(c.get("ticker", "")).upper().replace(".", "-")
        if not tk or tk == self_ticker:
            continue
        rec = _resolve_ticker(tk)   # SEC verification gate
        if not rec:
            continue
        c = dict(c)
        c["ticker"] = tk
        c["cik"] = rec["cik"]
        c["name"] = rec["title"] or c.get("name", tk)  # canonical SEC name
        prev = by_ticker.get(tk)
        if prev is None or order.get(c.get("source", "llm"), 9) < order.get(prev.get("source", "llm"), 9):
            if prev and c.get("source", "llm") != "llm" and not c.get("rationale"):
                c["rationale"] = prev.get("rationale", "")
            by_ticker[tk] = c
    merged = sorted(by_ticker.values(), key=lambda c: order.get(c.get("source", "llm"), 9))
    return merged[:MAX_COMPETITORS]


def _identify_competitors(prov, ticker: str, company: str, sector: str,
                          item1_text: str, item1a_text: str,
                          log: List[Dict[str, str]]) -> List[Dict[str, str]]:
    cache_path = os.path.join(MOAT_CACHE_DIR, "competitors_%s.json" % ticker.upper())
    cached = _read_cache(cache_path)
    # The competitor list is a FUNCTION of the sector — a list built under a
    # misclassified sector (OKLO-as-cloud) must not survive the sector fix, so
    # the cache is only valid when its recorded sector matches (2026-07-05).
    if cached and isinstance(cached.get("competitors"), list) \
            and cached.get("sector") == sector:
        log.append({"step": "COMPETITORS", "detail": "Competitor list (cached): %d" % len(cached["competitors"])})
        return cached["competitors"]

    self_core = _company_core(company)
    # Extract from COMPETITIVE-CONTEXT sentences (those mentioning compete/
    # competitor/competition), not a positional 'Competition' window: the first
    # literal 'competition' in a 10-K is often mid-narrative (a contest, or the
    # company's own product story), which is exactly what surfaced NVIDIA's own
    # 'Cosmos' as a phantom competitor. Competitive sentences are where rivals are
    # actually named, and they exclude the product-narrative noise.
    comp_1 = " ".join(s for s in _split_sentences(item1_text) if "compet" in s.lower())
    comp_1a = " ".join(s for s in _split_sentences(item1a_text) if "compet" in s.lower())
    named = _named_competitors_from_text(comp_1, "10-K Item 1", ticker.upper(), self_core)
    named += _named_competitors_from_text(comp_1a, "10-K Item 1A", ticker.upper(), self_core)

    llm_comps = _llm_competitors(company, ticker, sector, log,
                                 business_text=item1_text)
    llm_filtered = _filter_llm_competitors(llm_comps, sector)
    dropped = [c.get("ticker", "?") for c in llm_comps
               if c not in llm_filtered]
    if dropped:
        log.append({"step": "COMPETITORS",
                    "detail": "Sector-coherence gate dropped: %s" % ", ".join(dropped)})
    merged = _merge_competitors(named, llm_filtered, ticker.upper(), log)

    n_10k = sum(1 for c in merged if c.get("source", "").startswith("10-K"))
    log.append({"step": "COMPETITORS",
                "detail": "Identified %d competitors: %d from 10-K, %d from LLM"
                          % (len(merged), n_10k, len(merged) - n_10k)})
    _write_cache(cache_path, {"ticker": ticker.upper(), "competitors": merged,
                              "sector": sector, "built_at": _now_iso()})
    return merged


# ===========================================================================
# PART 3 — extract + classify the company's own moat claims
# ===========================================================================
def _company_candidate_sentences(prov, ticker: str, company: str, clean_10k: str,
                                 filing_meta: Dict[str, str],
                                 log: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """All moat-claim CANDIDATE sentences from the 10-K (Item 1 + Item 7) and the
    last N earnings calls. A sentence qualifies if it hits any moat claim keyword."""
    candidates: List[Dict[str, Any]] = []

    def harvest(text: str, doc_type: str, date: str, section: str, url: str):
        for s in _clean_sentences(text):
            kw = claim_moats_for_sentence(s)
            if kw:
                candidates.append({"sentence": s, "doc_type": doc_type, "date": date,
                                   "section": section, "url": url,
                                   "keyword_moats": sorted(kw)})

    item1 = extract_item1_business(clean_10k)
    item7 = extract_item7_mda(clean_10k)
    harvest(item1, filing_meta["doc_type"], filing_meta["date"], "Item 1 (Business)", filing_meta["url"])
    harvest(item7, filing_meta["doc_type"], filing_meta["date"], "Item 7 (MD&A)", filing_meta["url"])

    transcripts = _company_transcripts(prov, ticker)[:N_TRANSCRIPTS_COMPANY]
    for tr in transcripts:
        q = tr.get("quarter") or ""
        section = ("%s Earnings Call" % q) if re.match(r"Q[1-4]", q) else \
            ("Earnings Call (%s)" % tr["date"] if tr.get("date") else "Earnings Call")
        harvest(tr["text"], "Earnings Call", tr["date"], section, tr["url"])

    # Prioritize the strongest claims when over budget: moat-keyword density +
    # specificity (numbers/named products), then recency.
    candidates.sort(
        key=lambda c: (len(c["keyword_moats"]) + _specificity_score(c["sentence"]), c["date"]),
        reverse=True)
    log.append({"step": "MOAT_EXTRACT",
                "detail": "Extracted %d candidate moat sentences across %d moat types (pre-LLM)"
                          % (len(candidates), len({m for c in candidates for m in c["keyword_moats"]}))})
    return candidates[:MAX_CANDIDATE_SENTENCES]


_CLASSIFY_SENT_PROMPT = (
    "You are a forensic financial analyst. Classify each sentence below into the "
    "moat type(s) it AFFIRMATIVELY claims. A sentence can describe MULTIPLE moats. "
    "Only assign a moat if the sentence asserts the company HAS that advantage "
    "(e.g. real customer lock-in, scale cost advantage, brand pricing power) — NOT "
    "merely that it has a product, a platform, or operates in a market. When a "
    "sentence just describes a product or technology with no competitive-advantage "
    "claim, omit it.\n\n"
    "MOAT TYPES:\n" + moat_list_text() + "\n\n"
    "SENTENCES TO CLASSIFY:\n{sentences}\n\n"
    "Return ONLY a JSON array: [{{\"index\": 0, \"moat_types\": [\"Switching Costs\"]}}]. "
    "If a sentence claims NO moat, omit it. Temperature is zero; be deterministic. "
    "Use ONLY these exact moat-type names: " + ", ".join(MOAT_NAMES) + "."
)


def _classify_sentences(candidates: List[Dict[str, Any]], ticker: str,
                        log: List[Dict[str, str]]) -> Dict[str, List[Dict[str, Any]]]:
    """PART 3 Step 2 — one LLM call maps candidate sentences -> moat types. Falls
    back to the deterministic keyword moats if the LLM is unavailable/unparsable."""
    if not candidates:
        return {}
    listing = "\n".join('[%d] "%s"' % (i, c["sentence"]) for i, c in enumerate(candidates))
    prompt = _CLASSIFY_SENT_PROMPT.format(sentences=listing)
    by_moat: Dict[str, List[Dict[str, Any]]] = {m: [] for m in MOAT_NAMES}
    data = None
    try:
        text, _meta = call_llm(prompt, max_tokens=1600, temperature=0.0,
                               provider_idx=start_tier(AGENT_ID), agent=AGENT_ID,
                               stage="classify_moats", ticker=ticker.upper())
        data = _extract_json(text)
    except Exception as exc:
        log.append({"step": "MOAT_CLASSIFY", "detail": "Classification LLM failed (%s); using keyword fallback" % str(exc)[:80]})

    if isinstance(data, list):
        for d in data:
            if not isinstance(d, dict):
                continue
            try:
                i = int(d.get("index"))
            except Exception:
                continue
            if not (0 <= i < len(candidates)):
                continue
            for mt in d.get("moat_types", []) or []:
                mt = match_moat_name(mt)
                if mt:
                    by_moat[mt].append(candidates[i])
    else:
        # deterministic fallback: trust the keyword moats
        for c in candidates:
            for mt in c["keyword_moats"]:
                by_moat[mt].append(c)

    by_moat = {m: v for m, v in by_moat.items() if v}
    log.append({"step": "MOAT_CLASSIFY",
                "detail": "Classified into %d moat types: %s"
                          % (len(by_moat), ", ".join(by_moat.keys()) or "none")})
    return by_moat


def match_moat_name(raw: str) -> Optional[str]:
    """Resolve a possibly-noisy moat label to a canonical MOAT_NAMES entry."""
    if not raw:
        return None
    n = _norm(raw).replace("&", "and")
    for m in MOAT_NAMES:
        if _norm(m).replace("&", "and") == n or _norm(m).replace("&", "and") in n:
            return m
    return None


_CLEAREST_PROMPT = (
    'For the moat type "{moat}", select the SINGLE sentence that BEST REPRESENTS '
    "the company's claimed advantage for THIS moat type.\n\n"
    "Prioritize sentences that:\n"
    "1. Describe the MECHANISM of the moat — how the advantage actually works "
    "(for Switching Costs: what makes leaving expensive, e.g. an ecosystem/"
    "installed base customers have built on; for Network Effects: what grows "
    "more valuable with participants).\n"
    "2. Include specific metrics or evidence.\n"
    "3. Reference the company's CORE product/platform, not a partnership.\n\n"
    "A partnership or customer-win announcement is NOT a switching cost claim — "
    "prefer the durable structural claim over news, even if the news is more "
    "recent.\n\nSENTENCES:\n{sentences}\n\n"
    'Return ONLY: {{"selected_index": N, "rationale": "one sentence"}}'
)


def _specificity_score(sentence: str) -> int:
    """Deterministic tie-break / fallback: reward numbers, %, $ and named products."""
    sc = 0
    if re.search(r"\d", sentence):
        sc += 1
    if "%" in sentence or "$" in sentence:
        sc += 2
    sc += min(3, len(re.findall(r"\b[A-Z][A-Za-z0-9]{2,}\b", sentence)) // 3)
    return sc


def _select_clearest(moat: str, sents: List[Dict[str, Any]], ticker: str,
                     log: List[Dict[str, str]]) -> Dict[str, Any]:
    """PART 3 Step 3 — pick the clearest claim sentence for a moat (LLM by index,
    deterministic fallback by specificity then recency)."""
    # de-dup identical sentences, keep most recent occurrence's metadata
    uniq: List[Dict[str, Any]] = []
    seen = set()
    for s in sorted(sents, key=lambda x: x.get("date", ""), reverse=True):
        k = _norm(s["sentence"])
        if k not in seen:
            seen.add(k)
            uniq.append(s)
    if len(uniq) == 1:
        out = dict(uniq[0]); out["rationale"] = "Only claim located for this moat."; return out

    listing = "\n".join('[%d] "%s" — source: %s, date: %s'
                        % (i, s["sentence"], s["section"], s["date"]) for i, s in enumerate(uniq))
    selected, rationale = None, ""
    try:
        text, _meta = call_llm(_CLEAREST_PROMPT.format(moat=moat, sentences=listing),
                               max_tokens=200, temperature=0.0,
                               provider_idx=start_tier(AGENT_ID), agent=AGENT_ID,
                               stage="clearest", ticker=ticker.upper())
        d = _extract_json(text) or {}
        idx = d.get("selected_index")
        if isinstance(idx, int) and 0 <= idx < len(uniq):
            selected = uniq[idx]
            rationale = str(d.get("rationale", "")).strip()
    except Exception:
        pass
    if selected is None:  # deterministic fallback
        selected = sorted(uniq, key=lambda s: (_specificity_score(s["sentence"]), s.get("date", "")),
                          reverse=True)[0]
        rationale = "Most specific/recent claim (deterministic selection)."
    out = dict(selected); out["rationale"] = rationale
    return out


# ===========================================================================
# PART 4 — search competitor filings for challenges
# ===========================================================================
def _exhibit_priority(name: str) -> int:
    """Order 8-K docs so the likely earnings exhibit is tried first (fewer fetches):
    ex-99 names, then press-release/commentary names, then everything else. Naming
    is inconsistent across filers (NVIDIA uses 'q1fy27pr.htm'), so this is only an
    ordering hint — the actual match is by CONTENT in _resolve_transcript_url."""
    n = name.lower()
    if re.search(r"ex-?99", n):
        return 0
    if re.search(r"\b(pr|press|release|earnings|commentary|99)\b|pr\.htm|press|release", n):
        return 1
    return 2


def _resolve_transcript_url(cik: str, acc: str, text: str) -> Optional[str]:
    """URL of the document inside an 8-K accession that actually CONTAINS the
    transcript/press-release text. Robust to filename conventions because it
    content-matches a distinctive snippet of the known text rather than guessing
    the exhibit name. Returns the 8-K index page as a last resort (still a valid
    HTTP 200), or None if the accession can't be read."""
    if not cik or not acc:
        return None
    snippet = re.sub(r"[^a-z0-9]", "", (text or "")[:400].lower())[:120]
    try:
        cik_int = int(cik)
        acc_nd = acc.replace("-", "")
        base = "https://www.sec.gov/Archives/edgar/data/%d/%s" % (cik_int, acc_nd)
        idx = requests.get(base + "/index.json", headers=_HEADERS, timeout=20)
        if idx.status_code != 200:
            return None
        names = [it.get("name", "") for it in idx.json().get("directory", {}).get("item", [])]
        docs = [n for n in names if n.lower().endswith((".htm", ".html"))
                and not n.lower().endswith("-index.htm") and not n.lower().endswith("-index.html")
                and not re.match(r"^r\d+\.htm", n.lower())]
        index_url = "%s/%s-index.htm" % (base, acc)
        if not snippet:
            return index_url
        for name in sorted(docs, key=_exhibit_priority)[:5]:
            time.sleep(0.1)
            try:
                r = requests.get("%s/%s" % (base, name), headers=_HEADERS, timeout=30)
            except Exception:
                continue
            if r.status_code == 200 and snippet in re.sub(r"[^a-z0-9]", "", _strip_html(r.text).lower()):
                return "%s/%s" % (base, name)
        return index_url
    except Exception:
        return None


def _company_transcripts(prov, ticker: str) -> List[Dict[str, str]]:
    """Earnings-call transcripts (most recent first), each with the EX-99.1 exhibit
    URL that contains its text (resolved by matching the transcript date to the
    company's 8-K filings; cached per ticker so it's resolved once)."""
    if SECTranscriptProvider is None:
        return []
    try:
        trs = SECTranscriptProvider().get_transcripts(ticker) or []
    except Exception:
        trs = []
    trs = sorted(trs, key=lambda t: t.get("date", ""), reverse=True)
    cache_path = os.path.join(MOAT_CACHE_DIR, "ex99_%s.json" % ticker.upper())
    date_url: Dict[str, str] = _read_cache(cache_path) or {}
    need = {t.get("date", ""): (t.get("text") or t.get("content") or "")
            for t in trs if t.get("date") and t.get("date") not in date_url}
    if need:
        try:
            cik = prov.get_cik(ticker)
            filings = prov.get_all_filings(ticker)
            by_date: Dict[str, list] = {}
            for f in filings:
                if f.get("form") == "8-K" and f.get("filing_date") in need:
                    by_date.setdefault(f["filing_date"], []).append(f)
            # Only resolve (and negative-cache) when the filings list actually
            # loaded — an SEC throttle used to return [] here, which wrote ""
            # for EVERY date and permanently hid the earnings releases.
            if filings:
                for d, txt in need.items():
                    url = None
                    for f in by_date.get(d, []):
                        url = _resolve_transcript_url(cik, f.get("accession_number", ""), txt)
                        if url:
                            break
                    date_url[d] = url or ""
                _write_cache(cache_path, date_url)
        except Exception:
            pass
    out = []
    for tr in trs:
        d = tr.get("date", "")
        out.append({"text": tr.get("text") or tr.get("content") or "",
                    "date": d, "quarter": tr.get("quarter", ""),
                    "url": tr.get("url") or date_url.get(d) or "SEC 8-K EX-99.1"})
    return out


def _dynamic_challenge_terms(company: str) -> List[str]:
    """Company-name challenge phrases the keyword libraries can't know in advance
    (PART 4) — a competitor positioning explicitly against the target company."""
    core = _company_core(company)
    short = core.split()[0] if core else ""
    if not short or len(short) < 3:
        return []
    s = short
    return ["from %s" % s, "than %s" % s, "alternative to %s" % s, "replace %s" % s,
            "replaces %s" % s, "versus %s" % s, "vs %s" % s, "compared to %s" % s,
            "away from %s" % s, "off %s" % s]


def _competitor_documents(prov, comp: Dict[str, str]) -> List[Dict[str, str]]:
    """Fetch a competitor's searchable text blocks (10-K Item 1 + last N 8-Ks +
    last N earnings calls), each as {text, doc_type, date, url}. Cached per
    competitor ticker so repeated audits don't refetch."""
    ticker = comp["ticker"]
    cache_path = os.path.join(MOAT_CACHE_DIR, "compdocs_%s.json" % ticker)
    cached = _read_cache(cache_path)
    # An empty cached docs list is a MISS: it is what an SEC throttle leaves
    # behind, not evidence the competitor has no documents.
    if cached and isinstance(cached.get("docs"), list) and cached["docs"]:
        return cached["docs"]
    docs: List[Dict[str, str]] = []
    try:
        filings = prov.get_all_filings(ticker)
    except Exception:
        filings = []
    tenks = [f for f in filings if f.get("form") in ("10-K", "10-K/A")]
    eightks = [f for f in filings if f.get("form") == "8-K"]
    cik = comp.get("cik") or (tenks[0]["cik"] if tenks else "")

    if tenks:
        html = _fetch_filing_html(prov, ticker, tenks[0])
        clean = _strip_html(html)
        item1 = extract_item1_business(clean)
        docs.append({"text": item1 or clean[:60000], "doc_type": "10-K",
                     "date": tenks[0].get("filing_date", ""), "url": tenks[0].get("htm_url", "")})
    for f in eightks[:N_8K_COMPETITOR]:
        for d in _fetch_8k_docs(cik, f.get("accession_number", "")):
            docs.append({"text": d["text"], "doc_type": "8-K",
                         "date": f.get("filing_date", ""), "url": d["url"]})
    for tr in _company_transcripts(prov, ticker)[:N_TRANSCRIPTS_COMPETITOR]:
        docs.append({"text": tr["text"], "doc_type": "Earnings Call",
                     "date": tr["date"], "url": tr["url"],
                     "quarter": tr.get("quarter", "")})
    if docs:  # never cache an empty result (throttled fetch), so it retries
        _write_cache(cache_path, {"ticker": ticker, "docs": docs, "built_at": _now_iso()})
    return docs


def _search_challenges(prov, competitors: List[Dict[str, str]], claimed_moats: List[str],
                       company: str, log: List[Dict[str, str]],
                       emit: Callable) -> Dict[str, List[Dict[str, Any]]]:
    """PART 4 Step 2 — for every claimed moat, scan each competitor's documents for
    a challenge sentence (challenge keywords + dynamic company-name terms)."""
    extra = _dynamic_challenge_terms(company)
    by_moat: Dict[str, List[Dict[str, Any]]] = {m: [] for m in claimed_moats}
    n_docs = 0
    for comp in competitors[:COMPETITORS_TO_SEARCH]:
        emit("Searching %s filings for moat challenges..." % comp["ticker"], "search")
        docs = _competitor_documents(prov, comp)
        n_docs += len(docs)
        for doc in docs:
            doc_label = doc["doc_type"] + (
                " %s" % doc.get("quarter") if doc.get("quarter") else "")
            for s in _clean_sentences(doc["text"]):
                for moat in claimed_moats:
                    if sentence_challenges_moat(s, moat, extra):
                        by_moat[moat].append({
                            "sentence": s, "competitor": comp["name"],
                            "competitor_ticker": comp["ticker"], "doc_type": doc_label,
                            "date": doc["date"], "url": doc["url"]})
    n_cand = sum(len(v) for v in by_moat.values())
    log.append({"step": "CHALLENGE_SEARCH",
                "detail": "Searched %d competitor documents, found %d challenge candidates"
                          % (n_docs, n_cand)})
    return by_moat


_CHALLENGE_SELECT_PROMPT = (
    'The company {company} claims this moat.\nMOAT TYPE: {moat}\nCOMPANY CLAIM: '
    '"{claim}"\n\nBelow are sentences from COMPETITOR filings that may challenge it. '
    "Select the SINGLE strongest challenge. A strong challenge: is about a "
    "product/offering that EXISTS (shipping or announced with a timeline), not "
    "vague; directly addresses the same moat type; is from a named competitor.\n\n"
    "COMPETITOR SENTENCES:\n{sentences}\n\n"
    'Return ONLY: {{"selected_index": N, "rationale": "why this is strongest"}}. '
    'If none is a real challenge (all vague/off-topic), return '
    '{{"selected_index": null, "rationale": "No credible challenge found"}}.'
)


def _select_challenge(moat: str, claim: str, cands: List[Dict[str, Any]], ticker: str,
                      log: List[Dict[str, str]]) -> Optional[Dict[str, Any]]:
    """PART 4 Step 3 — LLM picks the strongest challenge BY INDEX (or null). No
    deterministic 'strongest' fallback: if the model can't judge, we honestly
    report no selected challenge rather than guess one."""
    if not cands:
        return None
    # de-dup + cap; prefer more recent + more specific candidates into the budget
    seen, uniq = set(), []
    for c in sorted(cands, key=lambda x: (_specificity_score(x["sentence"]), x.get("date", "")), reverse=True):
        k = _norm(c["sentence"])
        if k not in seen:
            seen.add(k); uniq.append(c)
    uniq = uniq[:MAX_CHALLENGE_CANDIDATES]
    listing = "\n".join('[%d] [%s] [%s, %s] "%s"'
                        % (i, c["competitor"], c["doc_type"], c["date"], c["sentence"])
                        for i, c in enumerate(uniq))
    try:
        text, _meta = call_llm(
            _CHALLENGE_SELECT_PROMPT.format(company=ticker.upper(), moat=moat,
                                            claim=claim, sentences=listing),
            max_tokens=220, temperature=0.0, provider_idx=start_tier(AGENT_ID),
            agent=AGENT_ID, stage="challenge_select", ticker=ticker.upper())
        d = _extract_json(text) or {}
        idx = d.get("selected_index")
        if isinstance(idx, int) and 0 <= idx < len(uniq):
            out = dict(uniq[idx]); out["select_rationale"] = str(d.get("rationale", "")).strip()
            return out
    except Exception:
        pass
    return None


# ===========================================================================
# PART 5 — moat-challenge analysis
# ===========================================================================
_VERDICTS = ("DIRECT THREAT", "WATCH", "NO THREAT")
_ANALYSIS_PROMPT = (
    "You are a forensic financial analyst. Analyze whether a competitor's statement "
    "is a credible challenge to a company's moat claim.\n\n"
    "COMPANY: {company} ({ticker})\nMOAT TYPE: {moat}\n"
    'COMPANY CLAIM: "{claim}" — {claim_src}, {claim_date}\n'
    'COMPETITOR CHALLENGE: "{challenge}" — {comp_name}, {comp_src}, {comp_date}\n\n'
    "Answer three questions in your analysis: (1) IS THE CHALLENGE REAL? (a shipping "
    "product / signed contract / working alternative, or merely aspirational?); "
    "(2) WHAT IS THE TIMELINE? (now / within 12 months / 3-5 years / unknown); "
    "(3) WHAT IS THE MAGNITUDE? (does it threaten a CORE moat the business depends "
    "on, or a PERIPHERAL one?).\n\n"
    "Write a concise 3-5 sentence analysis addressing all three. Then end with one "
    "verdict. DIRECT THREAT: real, imminent, and targets a core moat. WATCH: a "
    "credible challenge to a CORE moat that is not yet imminent. NO THREAT: not "
    "credible, not material, or PERIPHERAL to this moat.\n"
    "Your verdict MUST agree with your analysis: if your analysis calls the "
    "challenge peripheral or not material, the verdict is NO THREAT — do not "
    "hedge a peripheral challenge into WATCH.\n"
    "Begin your response with the verdict on its own line, then the analysis paragraph."
)

_NO_CHALLENGE_ANALYSIS = (
    "No competitor filing contained language challenging this moat. This does NOT "
    "mean the moat is impenetrable — only that competitors are not publicly claiming "
    "to challenge it in their SEC filings. Private-company competitors or non-filing "
    "entities may still pose threats.")

# When candidate sentences EXISTED but none survived credibility selection, saying
# "no competitor filing contained language" would be false — say what happened.
_NO_CREDIBLE_CHALLENGE_ANALYSIS = (
    "%d candidate statement(s) from competitor filings were reviewed for this moat, "
    "but none credibly challenges it (vague, off-topic, or aspirational language "
    "only). This does NOT mean the moat is impenetrable — private-company "
    "competitors or non-filing entities may still pose threats.")


def _analyze_pair(company: str, ticker: str, moat: str, claim: Dict[str, Any],
                  challenge: Optional[Dict[str, Any]], log: List[Dict[str, str]],
                  n_candidates: int = 0) -> Tuple[str, str]:
    """PART 5 — return (verdict, analysis). No challenge -> NO THREAT + standard
    text (no LLM call). Challenge -> LLM analysis; parse the leading verdict line."""
    if not challenge:
        if n_candidates > 0:
            return "NO THREAT", _NO_CREDIBLE_CHALLENGE_ANALYSIS % n_candidates
        return "NO THREAT", _NO_CHALLENGE_ANALYSIS
    prompt = _ANALYSIS_PROMPT.format(
        company=company, ticker=ticker.upper(), moat=moat,
        claim=claim["sentence"], claim_src=claim["section"], claim_date=claim["date"],
        challenge=challenge["sentence"], comp_name=challenge["competitor"],
        comp_src=challenge["doc_type"], comp_date=challenge["date"])
    try:
        text, _meta = call_llm(prompt, max_tokens=380, temperature=0.0,
                               provider_idx=start_tier(AGENT_ID), agent=AGENT_ID,
                               stage="analysis", ticker=ticker.upper())
    except Exception as exc:
        log.append({"step": "ANALYSIS", "detail": "Analysis LLM failed for %s: %s" % (moat, str(exc)[:80])})
        return "WATCH", ("A competitor challenge was located but the analysis model was "
                         "unavailable; manual review recommended. Challenge: \"%s\""
                         % challenge["sentence"])
    verdict, analysis = _parse_verdict(text)
    return verdict, analysis


def _parse_verdict(text: str) -> Tuple[str, str]:
    """Pull the verdict (first matching token) + analysis body from the response."""
    if not text:
        return "WATCH", ""
    upper = text.upper()
    verdict = "WATCH"
    pos = len(text)
    for v in _VERDICTS:  # earliest-occurring verdict token wins
        i = upper.find(v)
        if i != -1 and i < pos:
            verdict, pos = v, i
    # analysis = everything with the leading verdict label/line removed
    body = text.strip()
    body = re.sub(r"^\s*(?:VERDICT\s*[:\-]?\s*)?(?:DIRECT THREAT|WATCH|NO THREAT)\s*[:\.\-]?\s*",
                  "", body, count=1, flags=re.IGNORECASE).strip()
    return verdict, body or text.strip()


# ===========================================================================
# Agent entry point
# ===========================================================================
class MoatAuditor:
    """Agent 6 entry point. `.run(ticker, progress=None, year=None)` mirrors 1-5."""

    def run(self, ticker: str, progress: Optional[Callable] = None,
            year: Optional[int] = None) -> Dict[str, Any]:
        t0 = time.time()
        ticker = (ticker or "").strip().upper().replace(".", "-")
        log: List[Dict[str, str]] = []

        def emit(msg: str, kind: str = "info") -> None:
            if progress:
                try:
                    progress(msg, kind)
                except Exception:
                    pass

        if SECEdgarProvider is None:
            return self._error(ticker, ticker, log, t0, "SEC provider unavailable.")
        prov = SECEdgarProvider()
        try:
            company = prov.get_company_name(ticker) or ticker
        except Exception:
            company = ticker
        if not prov.get_cik(ticker):
            return self._error(ticker, company, log, t0, "Ticker does not resolve to an SEC filer.")

        # most recent 10-K
        emit("Fetching %s most recent 10-K..." % ticker, "fetch")
        try:
            tenks = prov.get_10k_filings(ticker)
        except Exception:
            tenks = []
        if not tenks:
            return self._error(ticker, company, log, t0, "No 10-K filing found for ticker.")
        # Plain 10-Ks before amendments: Part III 10-K/As (Tesla files one every
        # April) contain no Item 1/1A/7, which zeroed out all moat claims. Walk
        # candidates until item extraction yields substance.
        ordered = [f for f in tenks if f.get("form") == "10-K"] + \
                  [f for f in tenks if f.get("form") != "10-K"]
        tenk, clean_10k, item1, item1a = None, "", "", ""
        for cand in ordered[:3]:
            html = _fetch_filing_html(prov, ticker, cand)
            clean = _strip_html(html)
            if not clean:
                continue
            i1 = extract_item1_business(clean)
            i1a = extract_item1a_risk(clean)
            if i1 or i1a:
                tenk, clean_10k, item1, item1a = cand, clean, i1, i1a
                break
            if tenk is None:  # parseable but item-less: keep as last resort
                tenk, clean_10k = cand, clean
        if not clean_10k:
            return self._error(ticker, company, log, t0, "Could not fetch/parse the 10-K.")
        filing_meta = {"doc_type": "10-K %s" % _fy(tenk.get("period_of_report") or tenk.get("filing_date")),
                       "date": tenk.get("filing_date", ""), "url": tenk.get("htm_url", "")}

        # PART 1b — sector (classified AFTER the 10-K fetch so the company's own
        # Item 1 text grounds the answer; Part-5 fix 2026-07-05).
        emit("Classifying %s into a sector..." % ticker)
        sector = _classify_sector(ticker, company, log,
                                  business_text=item1 or clean_10k[:4000])

        # PART 2 — competitors
        emit("Identifying competitors (10-K + LLM, sector-filtered)...", "search")
        competitors = _identify_competitors(prov, ticker, company, sector, item1, item1a, log)

        # PART 3 — company moat claims
        emit("Extracting and classifying %s moat claims..." % ticker, "analyze")
        candidates = _company_candidate_sentences(prov, ticker, company, clean_10k, filing_meta, log)
        by_moat = _classify_sentences(candidates, ticker, log)
        claims: Dict[str, Dict[str, Any]] = {}
        for moat, sents in by_moat.items():
            claims[moat] = _select_clearest(moat, sents, ticker, log)

        if not claims:
            result = self._assemble(ticker, company, sector, competitors, [], log, t0,
                                    status="no_moats")
            self._archive(ticker, year, result, t0)
            emit("No moat claims identified in %s filings." % ticker, "done")
            return result

        # PART 4 — challenge search + selection
        challenge_cands = _search_challenges(prov, competitors, list(claims.keys()), company, log, emit)
        # PART 5 — analysis, per moat
        emit("Analyzing moat-challenge pairs...", "analyze")
        moats_out: List[Dict[str, Any]] = []
        for moat in [m for m in MOAT_NAMES if m in claims]:  # stable moat order
            claim = claims[moat]
            cands = challenge_cands.get(moat, [])
            challenge = _select_challenge(moat, claim["sentence"], cands, ticker, log)
            verdict, analysis = _analyze_pair(company, ticker, moat, claim, challenge, log,
                                              n_candidates=len(cands))
            moats_out.append(self._moat_block(moat, claim, challenge, verdict, analysis, clean_10k))

        result = self._assemble(ticker, company, sector, competitors, moats_out, log, t0,
                                status="success")
        self._archive(ticker, year, result, t0)
        n_dt = sum(1 for m in moats_out if m["verdict"] == "DIRECT THREAT")
        n_w = sum(1 for m in moats_out if m["verdict"] == "WATCH")
        n_nt = sum(1 for m in moats_out if m["verdict"] == "NO THREAT")
        log.append({"step": "OUTPUT",
                    "detail": "Moat audit complete — %d DIRECT THREAT, %d WATCH, %d NO THREAT"
                              % (n_dt, n_w, n_nt)})
        emit("Moat audit complete: %d moats audited (%d threats)." % (len(moats_out), n_dt), "done")
        return result

    # ── block + result assembly ───────────────────────────────────────────────
    def _moat_block(self, moat: str, claim: Dict[str, Any], challenge: Optional[Dict[str, Any]],
                    verdict: str, analysis: str, clean_10k: str) -> Dict[str, Any]:
        # Every quote is deterministically EXTRACTED from a real document (the LLM
        # only selects an index), so it is verbatim by provenance. For a 10-K
        # section claim we additionally confirm presence in the parsed 10-K.
        claim_page = None
        if "Item" in claim.get("section", ""):
            claim_verified = bool(_quote_in_text(claim["sentence"], clean_10k))
            try:  # printed page number, when the 10-K's pagination survives cleaning
                from src.utils.page_numbers import page_of_quote
                claim_page = page_of_quote(clean_10k, claim["sentence"])
            except Exception:
                claim_page = None
        else:
            claim_verified = bool(claim.get("sentence", "").strip())
        block = {
            "moat_type": moat,
            "company_claim": {
                "sentence": claim["sentence"], "source": claim["section"],
                "date": claim["date"], "url": claim["url"],
                "quote_verified": claim_verified,
                "selection_rationale": claim.get("rationale", ""),
                **({"page_number": claim_page} if claim_page else {}),
            },
            "strongest_challenge": None,
            "verdict": verdict,
            "analysis": analysis,
        }
        if challenge:
            block["strongest_challenge"] = {
                "competitor": challenge["competitor"],
                "competitor_ticker": challenge.get("competitor_ticker", ""),
                "sentence": challenge["sentence"], "source": challenge["doc_type"],
                "date": challenge["date"], "url": challenge["url"],
                "quote_verified": bool(challenge.get("sentence", "").strip()),
                "selection_rationale": challenge.get("select_rationale", ""),
            }
        return block

    def _assemble(self, ticker, company, sector, competitors, moats_out, log, t0, status
                  ) -> Dict[str, Any]:
        result = {
            "agent": "Moat Auditor", "agent_id": AGENT_ID,
            "ticker": ticker, "company": company, "sector": sector,
            "run_timestamp": _now_iso(), "status": status,
            "score": None, "signal": None,  # AUDIT — no bull/bear vote
            "competitors_analyzed": [
                {k: c.get(k, "") for k in ("name", "ticker", "source", "rationale")}
                for c in competitors],
            "moats_audited": moats_out,
            "agent_log": log,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
        }
        if status == "no_moats":
            result["message"] = "No moat claims identified in company filings."
        if not competitors:
            # Honest empty (Part 5, 2026-07-05): with no named competitors in
            # the 10-K and none the LLM could ground, say so — never pad the
            # list with unrelated large caps.
            result["competitor_note"] = ("No direct public competitors identified "
                                         "in SEC filings.")
        result["human_readable"] = render_human_readable(result)
        return result

    def _error(self, ticker, company, log, t0, msg) -> Dict[str, Any]:
        return {
            "agent": "Moat Auditor", "agent_id": AGENT_ID, "ticker": ticker,
            "company": company, "sector": None, "status": "error", "message": msg,
            "score": None, "signal": None, "competitors_analyzed": [], "moats_audited": [],
            "agent_log": log, "elapsed_ms": round((time.time() - t0) * 1000, 1),
        }

    def _archive(self, ticker, year, result, t0) -> None:
        try:
            save_run(AGENT_ID, ticker, year or datetime.now().year, result,
                     duration_ms=(time.time() - t0) * 1000.0)
        except Exception:
            pass  # archiving must never break a run


def _fy(date_str: Optional[str]) -> str:
    """'FY2026' label from a period-of-report / filing date, else ''."""
    if not date_str:
        return ""
    m = re.match(r"(\d{4})", str(date_str))
    return "FY%s" % m.group(1) if m else ""


# ===========================================================================
# Human-readable dashboard output (PART 6)
# ===========================================================================
def render_human_readable(result: Dict[str, Any]) -> str:
    if result.get("status") == "error":
        return "ERROR: %s" % result.get("message", "")
    lines: List[str] = []
    lines.append("MOAT AUDIT: %s (%s)" % (result.get("company", "?"), result.get("ticker", "?")))
    if result.get("sector"):
        lines.append("Sector: %s" % result["sector"])
    lines.append("")
    lines.append("COMPETITORS ANALYZED:")
    for c in result.get("competitors_analyzed", []):
        extra = " — %s" % c["rationale"] if c.get("rationale") else ""
        lines.append("  • %s (%s) — Source: %s%s" % (c.get("name", "?"), c.get("ticker", "?"),
                                                     c.get("source", "?"), extra))
    if result.get("status") == "no_moats":
        lines.append("")
        lines.append("No moat claims identified in company filings.")
        return "\n".join(lines)
    for i, m in enumerate(result.get("moats_audited", []), 1):
        lines.append("")
        lines.append("───")
        lines.append("MOAT %d: %s" % (i, m["moat_type"]))
        cc = m.get("company_claim", {})
        lines.append("  Company Claim (%s, %s):" % (cc.get("source", "?"), cc.get("date", "?")))
        lines.append("    \"%s\" [%s]" % (cc.get("sentence", ""), cc.get("url", "")))
        ch = m.get("strongest_challenge")
        if ch:
            lines.append("  Competitor Challenge (%s %s, %s):" % (ch.get("competitor", "?"),
                                                                 ch.get("source", ""), ch.get("date", "?")))
            lines.append("    \"%s\" [%s]" % (ch.get("sentence", ""), ch.get("url", "")))
        else:
            lines.append("  Competitor Challenge: None found.")
        lines.append("  Verdict: %s" % m.get("verdict", "?"))
        lines.append("  Analysis: %s" % m.get("analysis", ""))
    return "\n".join(lines)


def run_moat_auditor(ticker: str, progress: Optional[Callable] = None,
                     year: Optional[int] = None) -> Dict[str, Any]:
    """Module-level entry point (mirrors run_constraint_auditor)."""
    return MoatAuditor().run(ticker, progress=progress, year=year)


# ===========================================================================
# Deterministic self-test (no network/LLM) — proves the pure helpers
# ===========================================================================
def _selftest() -> None:
    print("== moat_auditor self-test ==")
    # section extraction
    sample = ("ITEM 1. BUSINESS Overview. We make chips. Competition. We compete with "
              "Advanced Micro Devices, Inc. and Intel Corporation, as well as Amazon.com, "
              "Inc. ITEM 1A. RISK FACTORS Our competition is intense and Qualcomm "
              "Incorporated may erode our share. ITEM 2. PROPERTIES None.")
    item1 = extract_item1_business(sample)
    assert "Advanced Micro Devices" in item1, item1
    assert "RISK FACTORS" not in item1, "Item 1 leaked into 1A"
    comp = extract_competition_subsection(item1)
    assert comp.startswith("Competition"), comp[:40]
    named = _named_competitors_from_text(comp, "10-K Item 1", "NVDA", _company_core("NVIDIA CORP"))
    tickers = {c["ticker"] for c in named}
    print("  named from Item 1:", sorted(tickers))
    assert "AMD" in tickers and "INTC" in tickers, tickers
    # verdict parsing
    v, a = _parse_verdict("DIRECT THREAT\nAMD's ROCm ships now and translates CUDA.")
    assert v == "DIRECT THREAT" and a.startswith("AMD"), (v, a)
    v2, _ = _parse_verdict("The verdict: NO THREAT. Nothing material.")
    assert v2 == "NO THREAT", v2
    # specificity + json
    assert _specificity_score("95% retention across $2B of bookings") > _specificity_score("we are sticky")
    assert _extract_json('```json\n[{"index":0,"moat_types":["Brand"]}]\n```') == [{"index": 0, "moat_types": ["Brand"]}]
    assert match_moat_name("ip and patents") == "IP & Patents"
    assert match_sector("Cybersecurity") == "Cybersecurity"
    print("ALL SELF-TESTS PASSED")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        tk = next((a for a in sys.argv[1:] if not a.startswith("-")), "NVDA")
        out = run_moat_auditor(tk, progress=lambda m, k="info": print("  [%s] %s" % (k, m)))
        print("\n" + (out.get("human_readable") or json.dumps(out, indent=2)[:4000]))
