"""
Agent 4 — Risk Factor Delta Scanner (10-K vs 10-K)

Compares Item 1A (Risk Factors) from a company's two most recent 10-K filings,
identifies what changed, and reports the top 10 most significant changes with
sentence-level quotes and SEC links.

Architecture (mirrors Agents 1-3):
  * Fetch the two most recent 10-Ks via the SEC EDGAR provider (cache-first).
  * Extract Item 1A from each (deterministic regex, HTML/XBRL stripped).
  * Call the LLM once with both full risk-factor sections.
  * Verify every quote is present in the source text (string match + fuzzy).
  * Build SEC links, score, and emit the top 10 findings.

Uses ONLY:
  * call_llm                        — src.config.llm_config
  * SECEdgarProvider + cache helpers — src.providers.filings.sec_edgar
"""

from __future__ import annotations

import logging
import json
import os
import re
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Make `from src....` imports resolve both under the backend (which already adds
# sollia_working to sys.path) and when this module is run standalone for testing.
_SOLLIA_WORKING = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SOLLIA_WORKING not in sys.path:
    sys.path.insert(0, _SOLLIA_WORKING)

from src.config.llm_config import call_llm, start_tier
from src.utils.scoring import score_risk_factors
from src.utils.prompt_safety import neutralize_injections
from src.providers.filings.sec_edgar import (
    SECEdgarProvider,
    get_cached_filings,
    get_cached_text,
    cache_text,
)

logger = logging.getLogger(__name__)

# Each Item 1A section is capped before going into the prompt. Sizing is driven
# by the Groq rate limits (measured): the primary llama-3.3-70b-versatile allows
# 12,000 tokens/min (and 100,000/day on the free tier), and the llama-3.1-8b
# fallback only 6,000 tokens/min — and max_tokens counts against the budget.
# SEC legalese tokenizes at ~5.4 chars/token, so two 8k-char sections (~3.0k
# tokens) + the template (~0.6k) + the 1.8k output reservation ≈ 5.4k tokens.
# That stays under BOTH models' per-request budgets, so the agent keeps working
# on the 8B fallback when the 70B's daily quota is spent (the OpenRouter backup
# key is currently invalid, so the 8B is the only safety net). Raise toward
# ~16000 for more coverage if you can rely on the 70B having daily budget.
MAX_SECTION_CHARS = 8000
LLM_MAX_TOKENS = 1800

# Per-finding score impact, as a function of the finding's 1-based rank. The
# score starts neutral at 50; each finding nudges it by category AND rank
# (higher-ranked findings move it more). ESCALATED/NEW push the score DOWN (more
# BEARISH); REMOVED/DE-ESCALATED push it UP (more BULLISH); REWORDED never moves
# it. Mirrors the spec's table exactly — see _calculate_score.
CATEGORY_IMPACT = {
    "ESCALATED":    lambda rank: -(12.0 - rank),        # rank 1 -> -11,  rank 10 -> -2 (more risk = more bearish)
    "NEW":          lambda rank: -(8.0 - rank * 0.6),   # rank 1 -> -7.4, rank 10 -> -2 (new risk = more bearish)
    "REMOVED":      lambda rank: 6.0 - rank * 0.4,     # rank 1 -> +5.6, rank 10 -> +2 (removed risk = more bullish)
    "DE-ESCALATED": lambda rank: 8.0 - rank * 0.6,     # rank 1 -> +7.4, rank 10 -> +2 (softer risk = more bullish)
    "REWORDED":     lambda rank: 0.0,                  # never moves the score
}

# Conditional-threat phrases. When the NEW quote contains one of these and the
# OLD quote does not, a "REWORDED" change is really an escalation — the new
# filing added a stated adverse consequence. Used by the re-classification pass.
_THREAT_PHRASES = (
    "if we are unable",
    "if we fail",
    "could have an adverse effect",
    "may not be able",
    "may result in",
)

# Grammatical "glue" words (length >= 4; shorter ones are dropped by the
# [a-z]{4,} tokenizer anyway) ignored when judging whether two quotes discuss
# the SAME business concern. Domain nouns (warranty, regulatory, financial, ...)
# are deliberately NOT here, so genuine topical overlap is preserved; only
# function words are stripped. Used by the update-only forced-match check.
_TOPIC_STOPWORDS = frozenset("""
that this these those with from will would could should shall might must have
been were their them they there here which what when where while because such
into onto over under about above below also both each more most some than then
does your ours being upon however including regarding generally very much many
only well other others between within during same like
""".split())

# EXACT prompt template (do not edit the wording). The two placeholders are
# substituted with .replace() — NOT str.format()/f-strings — because the JSON
# example below contains literal { } braces.
RISK_DELTA_PROMPT = """You are a forensic financial analyst comparing Risk Factors (Item 1A) from two 10-K filings.

## INPUT
OLD 10-K (prior year):
{old_risk_factors_full_text}

NEW 10-K (current year):
{new_risk_factors_full_text}

## TASK
Compare these two documents risk-by-risk. Identify what changed, what's new, and what disappeared. For each finding, provide the exact sentence pair that best illustrates the change.

## CLASSIFICATION
Classify every change into ONE of these categories:

1. **ESCALATED** — Same risk, but language got stronger/worse. Added words like "intense," "significant," "increasing," "may materially affect." The risk became more urgent or severe.
2. **NEW** — A risk appears in the NEW filing that has no match in the OLD filing.
3. **REMOVED** — A risk in the OLD filing has no match in the NEW filing.
4. **DE-ESCALATED** — Same risk, but language got softer. Removed alarming words, added mitigating language, or the risk seems less severe.
5. **REWORDED** — Same risk, different wording, same intensity level. Legal/structural rewrite with no clear escalation or de-escalation.

DO NOT flag UNCHANGED risks. Only report what changed.

## RANKING
Within each category, rank by significance — how meaningfully different the old and new text are. A complete rewrite outranks a one-word change.

Return the TOP 10 most significant changes overall, regardless of category. If fewer than 10 changes exist, return all of them.

## QUOTES
For each finding, select the EXACT sentence pair that best represents the change:
- Quote from OLD: the sentence from the old filing that shows what changed
- Quote from NEW: the corresponding sentence from the new filing that shows the result

The quotes must be VERBATIM from the provided text. Copy them exactly. Do not paraphrase.

## IMPORTANT RULES
- When in doubt between ESCALATED and REWORDED, lean toward REWORDED. Only flag ESCALATED when the language change clearly indicates a worsening situation.
- A risk being split into two risks is NOT a removal — it's REWORDED with the same content.
- A risk being merged from two risks is NOT new — it's REWORDED.
- Human capital management disclosures (employee referrals, listening systems,
  training programs, turnover statistics, engagement surveys) are OPERATIONAL
  metrics, not risk factors. Only classify such text as a risk factor if the text
  explicitly describes a THREAT (e.g. "failure to retain talent could harm our
  ability to execute"). Never report an HR practice itself as a NEW risk.
- If the OLD text described a program (e.g. employee development or training) and
  the NEW text also describes a program of the same kind, that is REWORDED, not
  REMOVED — even if the heading moved.
- A topic moving to a different section (e.g. climate discussion absorbed into a
  human-capital or sustainability section) is a SECTION RESTRUCTURE — do not
  report the topic as REMOVED unless its substance is gone from the entire text.
- If the OLD text uses future/forward-looking language ("may," "could," "might") and the NEW text uses present/past ("is," "has," "experienced"), this is ESCALATED — the risk materialized.
- If the OLD text uses present/past and the NEW text uses future/forward-looking, this is DE-ESCALATED — the risk receded.

## OUTPUT FORMAT
Return ONLY a JSON array. No other text.

[
  {
    "category": "ESCALATED",
    "risk_title": "Supply Chain Disruption",
    "rank": 1,
    "old_quote": "We face significant competition in our core markets.",
    "new_quote": "We face intense and increasing competition from well-funded new entrants with substantial resources.",
    "summary": "Language strengthened — added 'intense and increasing' and specified competitor advantages."
  },
  ...
]

If no meaningful changes exist, return [].

## CRITICAL
- Quote EXACTLY from the provided text. Never paraphrase.
- Only return JSON. No markdown, no explanations, no code blocks.
- Temperature is zero. Be deterministic.
"""


# ── text helpers ──────────────────────────────────────────────────────────────

def _strip_html(raw: str) -> str:
    """Strip HTML tags + common XBRL/entity noise; collapse whitespace.

    Regex-based (fast, deterministic whitespace) so the text the LLM sees is the
    exact same text quote-verification searches against."""
    if not raw:
        return ""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&#160;", " ")
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    # XBRL metadata fragments that survive tag stripping
    text = re.sub(r"\b(?:iso4217|xbrli|us-gaap|dei|srt):\S+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# Lowercase words that, when they follow "Item 1A. Risk Factors", mark a
# cross-reference ("...see Item 1A. Risk Factors for a discussion of...") rather
# than the genuine section header.
_CROSSREF_WORDS = {
    "for", "under", "above", "below", "of", "and", "in", "to", "section",
    "regarding", "generally", "set", "contained", "beginning", "herein", "as",
    "discussion", "described", "include", "including", "see", "refer",
}


def _find_genuine_1a_start(clean: str, a_starts: List[int]) -> Optional[int]:
    """Pick the real "Item 1A. Risk Factors" section header from all "Item 1A"
    occurrences, skipping the table-of-contents entry (followed by a page
    number), running "(Continued)" page headers, and inline cross-references
    (followed by a lowercase preposition)."""
    fallback = None
    for s in a_starts:
        seg = clean[s:s + 200]
        m = re.match(r"item\s*1a[\.\:\)\s]*risk\s*factors[\.\:\s]*", seg, re.IGNORECASE)
        if not m:
            continue  # this "Item 1A" is not the Risk Factors header
        if "continued" in seg[:m.end()].lower():
            continue  # running page header
        after = seg[m.end():].lstrip()
        if not after or after[0].isdigit():
            continue  # empty or a TOC page number
        # Cross-reference guards (2026-07-01 Part 5 fix). NVDA's Item 1 Human
        # Capital section reads: see "Item 1A. Risk Factors — Risks Related to
        # ... Matters" for a discussion of this potential impact. The word-after
        # check below misses it (a quoted SUBSECTION title follows, not a
        # preposition), which anchored extraction inside Item 1 and pulled the
        # HR-program text (employee listening systems, referrals) into the "risk
        # factors" the LLM compared. Detect the lead-in BEFORE the match and the
        # crossref tail AFTER it instead.
        before = clean[max(0, s - 70):s].lower()
        if re.search(r"(?:see|refer\s+to|described\s+in|discussed\s+in|set\s+forth\s+in|"
                     r"included\s+in|under)\s*[\"'“]?\s*$", before):
            continue  # preceded by a cross-reference lead-in
        if re.search(r"for\s+(?:a\s+discussion|additional\s+information|"
                     r"more\s+information|further\s+information)", seg[m.end():],
                     re.IGNORECASE):
            continue  # crossref tail ("... for a discussion of this potential impact")
        if fallback is None:
            fallback = s
        words = re.findall(r"[a-z]+", after.lower())
        if words and words[0] in _CROSSREF_WORDS:
            continue  # cross-reference, not the section body
        return s
    return fallback if fallback is not None else (a_starts[0] if a_starts else None)


def extract_risk_factors(text: str) -> str:
    """Extract Item 1A (Risk Factors) from a 10-K filing.

    Strips HTML/XBRL, locates the genuine "Item 1A. Risk Factors" header (not the
    TOC entry or cross-references), and returns through the first following
    Item 1B / 1C / 2. Running "(Continued)" page headers are removed."""
    clean = _strip_html(text)
    if not clean:
        return ""

    a_starts = [m.start() for m in re.finditer(r"item\s*1a\b", clean, re.IGNORECASE)]
    if not a_starts:
        # Part 6: SPAC-era / recent-IPO filings sometimes head the section
        # with a bare "RISK FACTORS" and no Item-1A label.
        return _extract_rf_fallback(clean)
    start = _find_genuine_1a_start(clean, a_starts)
    if start is None:
        return _extract_rf_fallback(clean)

    rest = clean[start + 8:]
    end_rel = None
    for pat in (r"item\s*1b\b", r"item\s*1c\b", r"item\s*2\b"):
        m = re.search(pat, rest, re.IGNORECASE)
        if m:
            end_rel = m.start() if end_rel is None else min(end_rel, m.start())
    # No end marker (rare) — cap defensively rather than running to EOF.
    end = (start + 8 + end_rel) if end_rel is not None else min(len(clean), start + 250000)

    section = clean[start:end]
    section = re.sub(r"item\s*1a[\.\:\)\s]*risk\s*factors\s*\(continued\)", " ",
                     section, flags=re.IGNORECASE)
    # Inline page-break artifacts (Part 1 quote-verification fix, 2026-07-04):
    # PDF-style pagination survives HTML stripping as "… subject 14 Table of
    # Contents to a number …" INSIDE sentences. The LLM (correctly) quotes the
    # sentence without the junk, then verification fails against the polluted
    # source. Never legitimate prose: real crossrefs say "the Table of
    # Contents", never "<number> Table of Contents".
    section = re.sub(r"\b\d{1,4}\s+table\s+of\s+contents\b", " ", section,
                     flags=re.IGNORECASE)
    section = re.sub(r"\s+", " ", section).strip()
    # TINY-SECTION FALL-THROUGH (Part-8 audit, 2026-07-05): GE's primary doc
    # carries only a cross-reference INDEX row ("Item 1A. Risk Factors 24-31")
    # and heads the real section "RISK FACTORS. The following discussion…" —
    # the Item-1A path then "succeeds" with a 27-char stub and the comparison
    # reports a false "no material changes". No genuine Item 1A is this short.
    if len(section) < 2000:
        fb = _extract_rf_fallback(clean)
        if len(fb) > len(section):
            return fb
    # Defang injection directives in this UNTRUSTED section before it feeds the
    # comparison prompt or is surfaced as a change quote (Part 3a, 2026-07-02).
    # No-op on genuine filing text (verified across 948 real docs).
    return neutralize_injections(section)


_SEC_ERROR_MARKERS = ("Request Rate Threshold Exceeded", "Undeclared Automated Tool",
                      "For security purposes, and to ensure that the public service remains available")


def _is_sec_error_page(html: Optional[str]) -> bool:
    """True when the 'filing text' is actually SEC EDGAR's HTTP-200 throttle /
    block page — must never be cached or parsed as a filing."""
    return bool(html) and any(m in html for m in _SEC_ERROR_MARKERS)


def _extract_rf_fallback(clean: str) -> str:
    """Fallback Item 1A extraction for filings that label the section without
    the standard 'Item 1A' header (SPAC-era / recent-IPO documents): anchor on
    a standalone 'RISK FACTORS' heading followed by risk prose, ending at the
    next Item/section marker (Part 6, 2026-07-04)."""
    if not clean:
        return ""
    for m in re.finditer(r"RISK\s+FACTORS", clean):
        after = clean[m.end():m.end() + 400].lower()
        # A real section start is followed by risk prose, not a page number
        # (TOC row) or another heading. GE-style integrated reports open with
        # "RISK FACTORS. The following discussion of the material factors,
        # events and uncertainties…".
        if not re.search(r"(summary of|carefully consider|principal risks|"
                         r"following risks|following discussion|material factors|"
                         r"events and uncertainties|our business|investing in)", after):
            continue
        rest = clean[m.end():]
        end_rel = None
        for pat in (r"item\s*1b\b", r"unresolved\s+staff\s+comments",
                    r"item\s*1c\b", r"item\s*2\b\s*[\.\:]?\s*propert",
                    # Integrated-report body headings (no Item labels):
                    r"\bLEGAL PROCEEDINGS\b", r"\bMINE SAFETY\b",
                    r"\bMANAGEMENT[’']S DISCUSSION\b",
                    r"\bQUANTITATIVE AND QUALITATIVE\b"):
            em = re.search(pat, rest) if pat.startswith(r"\b") else \
                 re.search(pat, rest, re.IGNORECASE)
            if em and em.start() > 500:
                end_rel = em.start() if end_rel is None else min(end_rel, em.start())
        end = m.end() + end_rel if end_rel is not None else min(len(clean), m.end() + 250000)
        section = clean[m.start():end]
        section = re.sub(r"\b\d{1,4}\s+table\s+of\s+contents\b", " ", section,
                         flags=re.IGNORECASE)
        section = re.sub(r"\s+", " ", section).strip()
        if len(section) > 2000:  # a genuine section, not a stray heading
            return neutralize_injections(section)
    return ""


def _norm(s: Optional[str]) -> str:
    """Normalize for quote matching: drop smart/straight quotes, collapse
    whitespace, lowercase. Also drops inline "<n> Table of Contents" page
    artifacts on BOTH sides so a quote can never fail verification over
    pagination junk (Part 1, 2026-07-04)."""
    if not s:
        return ""
    s = re.sub(r"[‘’“”\"']", "", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    s = re.sub(r"\b\d{1,4} table of contents\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _verify_quote(quote: Optional[str], source: str) -> Optional[bool]:
    """Confirm an LLM-supplied quote is actually present in the source text.

    Returns True/False, or None when there is no quote to verify (e.g. a NEW
    risk has no OLD quote). Tries an exact (normalized) match first, then a
    fuzzy match on the first 80% of the quote to tolerate trailing edits."""
    if not quote or not str(quote).strip():
        return None
    q, src = _norm(quote), _norm(source)
    if not q or not src:
        return False
    if q in src:
        return True
    cut = max(1, int(len(q) * 0.8))
    probe = q[:cut]
    return len(probe) >= 20 and probe in src


def _reground_quote(quote: Optional[str], source: str) -> Optional[str]:
    """Repair a non-verbatim LLM quote by re-anchoring it to the SOURCE text.

    The measured failure modes (TSLA 2026-07-04 audit) both start verbatim:
    the model either STITCHES two non-adjacent sentences together ("…manage
    these components effectively." + "For example, as companies…") or rewrites
    a later clause ("licenses and permitting" → "licenses and approvals for
    additional expansion"). Anchor on the quote's opening words, then return
    the SOURCE's own text from that anchor to a sentence boundary near the
    original quote's length — verbatim by construction. Returns None when even
    the opening words can't be found (a from-the-first-word paraphrase)."""
    q = (quote or "").strip().rstrip("…").strip()
    if len(q) < 40 or not source:
        return None
    words = q.split()
    for n in (10, 8, 6, 5):
        if len(words) < n:
            continue
        pat = r"\s+".join(re.escape(w) for w in words[:n])
        # Smart-vs-straight apostrophes/quotes must not break the anchor.
        pat = re.sub(r"['’‘]", "['’‘]", pat).replace('"', '["“”]')
        m = re.search(pat, source, re.IGNORECASE)
        if not m:
            continue
        window = source[m.start(): m.start() + len(q) + 300]
        ends = [e.end() for e in re.finditer(r"[.!?](?=\s|$)", window)]
        cut = next((e for e in ends if e >= int(len(q) * 0.85)), None)
        if cut is None:
            cut = next((e for e in reversed(ends) if e >= int(len(q) * 0.5)), None)
        repaired = window[:cut].strip() if cut else window[:len(q)].strip()
        return repaired or None
    return None


def _is_referral_stub(rf_text: str) -> bool:
    """True when a 10-Q's Item 1A is ONLY a referral back to the annual
    report's risk factors ("…subject to … the factors discussed in Part I,
    Item 1A, Risk Factors in our Annual Report on Form 10-K…") with no
    substantive updates. Such a section documents NO changes — comparing it
    against the 10-K can only produce hallucinated findings."""
    t = _norm(rf_text)
    if not t or len(t) > 900:
        return False
    # "No material changes" in a short section is conclusive on its own
    # (OKLO: "There are no material changes to the risk factors previously
    # disclosed under the Risk Factors section in ... Form 10-K").
    no_changes = "no material change" in t
    refers = ("form 10-k" in t or "annual report" in t) and any(
        p in t for p in ("discussed in", "described in", "set forth in",
                         "referred to in", "included in", "contained in",
                         "disclosed under", "disclosed in", "previously disclosed"))
    has_updates = any(p in t for p in ("except", "other than", "the following",
                                       "updated", "supplement", "in addition"))
    return (no_changes or refers) and not has_updates


def _verify_or_repair(quote: Optional[str], source: str):
    """Verify a quote against its source; on failure, try re-grounding it.
    Returns (quote_or_repaired_quote, ok) with ok ∈ {True, False, None} —
    None means "no quote to verify" (absent side), exactly like _verify_quote."""
    v = _verify_quote(quote, source)
    if v is not False:
        return (quote or "").strip(), v
    fixed = _reground_quote(quote, source)
    if fixed and _verify_quote(fixed, source) is True:
        return fixed, True
    return (quote or "").strip(), False


# Characters that legitimately end a quoted sentence/clause. Anything else means
# the model cut the quote off at its max_tokens limit.
_SENTENCE_END = (".", "?", "!", '"', ")", "]", "”", "’")


def _mark_if_truncated(quote: Optional[str]) -> str:
    """Append an ellipsis to a quote that doesn't end on sentence-ending
    punctuation. The comparison prompt can hit max_tokens and cut a quote off
    mid-sentence; without this the UI shows a sentence that just stops, which
    reads like a bug rather than a truncation."""
    q = (quote or "").strip()
    if q and not q.endswith(_SENTENCE_END):
        return q + "…"
    return q


def _estimate_risk_count(full_html: str, section_text: str) -> int:
    """Best-effort count of distinct risk factors in the section.

    Heuristic: modern 10-Ks lead each risk factor with a bolded thesis sentence
    (~60-400 chars). We count unique bold/strong (or bold-styled) runs of that
    length that fall inside the extracted section. Shorter bold runs are inline
    emphasis or category labels; longer ones are whole paragraphs. This is an
    approximation (non-critical metadata) — returns 0 if structure can't be
    parsed, and is applied identically to both filings so the delta is meaningful."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(full_html, "html.parser")
        section_norm = re.sub(r"\s+", " ", section_text).lower()
        candidates = list(soup.find_all(["b", "strong"]))
        for tag in soup.find_all(style=True):
            st = (tag.get("style") or "").lower()
            if "bold" in st or "font-weight:7" in st or "font-weight: 7" in st:
                candidates.append(tag)
        seen, count = set(), 0
        for tag in candidates:
            t = re.sub(r"\s+", " ", tag.get_text()).strip()
            if not (60 <= len(t) <= 400):
                continue
            tl = t.lower()
            if tl in seen:
                continue
            if tl in section_norm:
                seen.add(tl)
                count += 1
        return count
    except Exception:
        return 0


def detect_format(ten_q_text: str, ten_k_text: str, ten_q_html: str = "", ten_k_html: str = "") -> str:
    """Classify a 10-Q's Risk Factors section as a full 'reprint' or 'update_only'.

    Deterministic (no LLM). A 10-Q either reprints the entire Item 1A (like a
    10-K — compare it cumulatively) or carries only a short "Updates to Risk
    Factors" delta (compare it against the 10-K baseline). Any ONE of three
    checks is sufficient to call it a reprint; otherwise we default to the safer
    'update_only'."""
    # Check 1 — length. A reprint reproduces the full 15-30 page section; an
    # update-only filing is typically 1-3 pages. >70% of the 10-K's length is a
    # reprint.
    if ten_k_text and len(ten_q_text) > 0.7 * len(ten_k_text):
        return "reprint"

    # Check 2 — structure. A reprint leads each risk with a bolded thesis title,
    # so its title count tracks the 10-K's (within ±30%); an update-only filing
    # usually has a single "Updates to Risk Factors" header.
    q_risks = _estimate_risk_count(ten_q_html, ten_q_text) if ten_q_html else 0
    k_risks = _estimate_risk_count(ten_k_html, ten_k_text) if ten_k_html else 0
    if k_risks > 0 and q_risks > 0 and abs(q_risks - k_risks) / k_risks < 0.3:
        return "reprint"

    # Check 3 — content. Companies don't rewrite the intro paragraph for an
    # update-only filing, so a reprint's opening words overlap the 10-K's.
    q_words = set(ten_q_text[:500].lower().split())
    k_words = set(ten_k_text[:500].lower().split())
    if k_words and len(q_words & k_words) / len(k_words) > 0.8:
        return "reprint"

    # Ambiguous — treat as update-only (compare against the 10-K baseline).
    return "update_only"


class RiskFactorAgent:
    def __init__(self):
        self.cache_root = os.path.join(_SOLLIA_WORKING, ".cache")
        os.makedirs(os.path.join(self.cache_root, "filings"), exist_ok=True)
        os.makedirs(os.path.join(self.cache_root, "texts"), exist_ok=True)
        self.provider = SECEdgarProvider()

    # ── fetching ──────────────────────────────────────────────────────────────

    def _normalize_filing(self, ticker, cik, acc_dash, filing_date, period, primary_doc, form="10-K"):
        cik_str = str(cik).zfill(10) if cik not in (None, "") else ""
        acc_nd = (acc_dash or "").replace("-", "")
        url = ""
        if cik_str and acc_nd and primary_doc:
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik_str)}/{acc_nd}/{primary_doc}"
        return {
            "ticker": ticker,
            "cik": cik_str,
            "form": form,
            "accession_number": acc_dash or "",
            "accession_nodash": acc_nd,
            "filing_date": filing_date or "",
            "period_of_report": period or "",
            "primary_doc": primary_doc or "",
            "url": url,
        }

    def _tenks_from_cache(self, ticker: str) -> List[dict]:
        """Parse 10-Ks from the cached raw SEC submissions JSON, if present.

        The shared .cache/filings/{TICKER}_filings.json is written by Agents 1/3
        in raw SEC format; reading it here avoids a network round-trip and keeps
        the cache format consistent (we never write an incompatible shape)."""
        cached = get_cached_filings(ticker)
        if not isinstance(cached, dict):
            return []
        recent = cached.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        if not forms:
            return []
        cik = cached.get("cik", "")
        accs = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        periods = recent.get("reportDate", [])
        docs = recent.get("primaryDocument", [])
        out = []
        for i, f in enumerate(forms):
            if f == "10-K":
                out.append(self._normalize_filing(
                    ticker, cik,
                    accs[i] if i < len(accs) else "",
                    dates[i] if i < len(dates) else "",
                    periods[i] if i < len(periods) else "",
                    docs[i] if i < len(docs) else "",
                ))
        return out

    def _all_filings_from_cache(self, ticker: str) -> List[dict]:
        """Parse 10-K AND 10-Q filings from the cached raw SEC submissions JSON,
        if present — the same shared .cache/filings/{TICKER}_filings.json that
        _tenks_from_cache reads, avoiding a network round-trip."""
        cached = get_cached_filings(ticker)
        if not isinstance(cached, dict):
            return []
        recent = cached.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        if not forms:
            return []
        cik = cached.get("cik", "")
        accs = recent.get("accessionNumber", [])
        dates = recent.get("filingDate", [])
        periods = recent.get("reportDate", [])
        docs = recent.get("primaryDocument", [])
        out = []
        for i, f in enumerate(forms):
            if f in ("10-K", "10-Q"):
                out.append(self._normalize_filing(
                    ticker, cik,
                    accs[i] if i < len(accs) else "",
                    dates[i] if i < len(dates) else "",
                    periods[i] if i < len(periods) else "",
                    docs[i] if i < len(docs) else "",
                    form=f,
                ))
        return out

    def _get_all_filings(self, ticker: str) -> List[dict]:
        """All 10-K and 10-Q filings for a ticker, newest first. Cache-first (the
        shared submissions JSON), falling back to the SEC EDGAR provider on a
        cache miss. Used by the 10-Q phase to locate a 10-Q filed after the most
        recent 10-K."""
        filings = self._all_filings_from_cache(ticker)
        if not filings:
            provider_filings = self.provider.get_all_filings(ticker)
            filings = [
                self._normalize_filing(
                    ticker, f.get("cik"), f.get("accession_number", ""),
                    f.get("filing_date", ""), f.get("period_of_report", ""),
                    f.get("primary_doc", ""), form=f.get("form", ""))
                for f in provider_filings if f.get("form") in ("10-K", "10-Q")
            ]
        filings = [f for f in filings if f.get("accession_number")]
        filings.sort(key=lambda f: f.get("filing_date", ""), reverse=True)
        return filings

    def get_two_most_recent_10ks(self, ticker: str) -> Tuple[Optional[dict], Optional[dict]]:
        """Return (old_filing, new_filing) — the two most recent 10-Ks, newest as
        `new_filing`. Checks the shared cache first, then the SEC EDGAR provider.
        Returns (None, None) if fewer than two 10-Ks exist."""
        ten_ks = self._tenks_from_cache(ticker)

        if len(ten_ks) < 2:
            # Cache miss / incomplete — fetch fresh via the SEC EDGAR provider.
            provider_filings = self.provider.get_10k_filings(ticker)
            ten_ks = [
                self._normalize_filing(
                    ticker, f.get("cik"), f.get("accession_number", ""),
                    f.get("filing_date", ""), f.get("period_of_report", ""),
                    f.get("primary_doc", ""))
                for f in provider_filings if f.get("form") == "10-K"
            ]

        # Keep originals only (drop 10-K/A amendments already excluded above),
        # newest first.
        ten_ks = [f for f in ten_ks if f.get("accession_number")]
        ten_ks.sort(key=lambda f: f.get("filing_date", ""), reverse=True)
        if len(ten_ks) < 2:
            return None, None
        new_filing, old_filing = ten_ks[0], ten_ks[1]
        return old_filing, new_filing

    def get_filing_text(self, filing: dict, bypass_cache: bool = False) -> str:
        """Full raw text (HTML) of a filing. Checks the shared text cache first,
        then fetches via the SEC EDGAR provider and caches the result.

        SEC throttle pages ("Request Rate Threshold Exceeded") are HTTP-200
        HTML, so they can masquerade as filing text; they are never cached,
        never returned, and a cached one is refetched (Part 6, 2026-07-04 —
        the OKLO "Item 1A could not be located" failure class).
        bypass_cache=True skips the cache read to heal a poisoned entry."""
        if not filing:
            return ""
        ticker = filing.get("ticker", "")
        acc = filing.get("accession_number", "")
        if ticker and acc and not bypass_cache:
            cached = get_cached_text(ticker, acc)
            if cached and not _is_sec_error_page(cached):
                return cached
        html = self.provider.fetch_html(
            filing.get("cik", ""), filing.get("accession_nodash", ""),
            filing.get("primary_doc", ""), ticker)
        if _is_sec_error_page(html):
            return ""
        if html and ticker and acc:
            cache_text(ticker, acc, html)
        return html or ""

    # ── scoring ───────────────────────────────────────────────────────────────

    @staticmethod
    def _calculate_score(changes: List[dict]) -> int:
        """Score Item 1A change on a 0-100 scale where 50 is neutral — "nothing
        material changed." Each finding nudges the needle by an amount set by its
        category AND its rank (higher-ranked findings matter more): ESCALATED/NEW
        push toward BEARISH, REMOVED/DE-ESCALATED toward BULLISH, REWORDED never
        moves it. The total is clamped to [10, 90] — no company carries zero or
        maximal risk."""
        score = 50.0
        for i, c in enumerate(changes, start=1):
            impact = CATEGORY_IMPACT.get(str(c.get("category", "")).upper().strip())
            if impact is None:
                continue
            rank = c.get("rank")
            if not isinstance(rank, int) or rank < 1:
                rank = i
            score += impact(rank)
        return int(round(max(10.0, min(90.0, score))))

    @staticmethod
    def _signal(score: int) -> str:
        """Map a score to a signal: below 35 BEARISH, above 65 BULLISH,
        35-65 (inclusive) NEUTRAL."""
        if score > 65:
            return "BULLISH"
        if score < 35:
            return "BEARISH"
        return "NEUTRAL"

    # ── main entry ────────────────────────────────────────────────────────────

    def run(self, ticker: str, progress=None) -> dict:
        """Run the full Risk Factor Delta scan for a ticker."""
        _t0 = datetime.now()  # PART 3: run-archive performance block
        ticker = (ticker or "").strip().upper().replace(".", "-")  # BRK.A -> BRK-A (SEC form)

        def _emit(msg, kind="info"):
            if progress:
                try:
                    progress(msg, kind)
                except Exception:
                    pass

        result = {
            "ticker": ticker,
            "agent": "Agent 4 — Risk Factor Delta",
            "run_timestamp": datetime.now().isoformat(),
            "old_filing_date": "",
            "new_filing_date": "",
            "total_risks_old": 0,
            "total_risks_new": 0,
            "changes_found": 0,
            "top_changes": [],
            "score": 50,
            "signal": "NEUTRAL",
        }

        _emit("Fetching the two most recent 10-K filings...", "search")
        old_filing, new_filing = self.get_two_most_recent_10ks(ticker)
        if not old_filing or not new_filing:
            _emit("Need two 10-K filings to compare — not enough found.", "flag")
            result["summary"] = ("Fewer than two 10-K filings available for comparison — "
                                 "the company may have gone public recently; the risk-factor "
                                 "delta needs two annual filings.")
            return result

        result["old_filing_date"] = old_filing.get("filing_date", "")
        result["new_filing_date"] = new_filing.get("filing_date", "")
        result["old_url"] = old_filing.get("url", "")
        result["new_url"] = new_filing.get("url", "")
        result["cik"] = new_filing.get("cik", "")

        _emit("Downloading filing text (cache-first)...", "fetch")
        old_html = self.get_filing_text(old_filing)
        new_html = self.get_filing_text(new_filing)
        if not old_html or not new_html:
            _emit("Filing text unavailable for one or both 10-Ks.", "flag")
            result["summary"] = "Could not retrieve filing text for both 10-Ks."
            return result

        _emit("Extracting Item 1A (Risk Factors) from both filings...", "analyze")
        old_rf = extract_risk_factors(old_html)
        new_rf = extract_risk_factors(new_html)
        # Part 6: an empty extraction can mean a poisoned cached text (an SEC
        # throttle page cached as the filing) — refetch that side once past
        # the cache before giving up; a fresh success also heals the cache.
        if not old_rf:
            fresh = self.get_filing_text(old_filing, bypass_cache=True)
            if fresh:
                old_rf = extract_risk_factors(fresh)
        if not new_rf:
            fresh = self.get_filing_text(new_filing, bypass_cache=True)
            if fresh:
                new_rf = extract_risk_factors(fresh)
        if not old_rf or not new_rf:
            missing = []
            if not old_rf:
                missing.append("the %s 10-K" % (result["old_filing_date"] or "older"))
            if not new_rf:
                missing.append("the %s 10-K" % (result["new_filing_date"] or "newer"))
            _emit("Item 1A could not be located in %s." % " or ".join(missing), "flag")
            result["summary"] = ("Item 1A (Risk Factors) could not be extracted from %s. "
                                 "If the company went public recently, its early filings "
                                 "may not carry a standard risk-factors section."
                                 % " and ".join(missing))
            return result

        result["total_risks_old"] = _estimate_risk_count(old_html, old_rf)
        result["total_risks_new"] = _estimate_risk_count(new_html, new_rf)

        old_clip = old_rf[:MAX_SECTION_CHARS]
        new_clip = new_rf[:MAX_SECTION_CHARS]

        prompt = (RISK_DELTA_PROMPT
                  .replace("{old_risk_factors_full_text}", old_clip)
                  .replace("{new_risk_factors_full_text}", new_clip))

        _emit("Comparing risk factors risk-by-risk...", "analyze")
        try:
            # requires_strict_json routes this to Groq-70B (empirically 3/3
            # clean+valid on this exact prompt shape; see MODEL_PROFILES).
            response, meta = call_llm(prompt, max_tokens=LLM_MAX_TOKENS,
                                      provider_idx=start_tier("risk_factors"),
                                      requires_strict_json=True)
            if self._parse_changes(response) is None:
                # A garbage response cached during a rate-limit storm would be
                # served forever — retry once past the cache; the fresh reply
                # overwrites the poisoned entry.
                _emit("Comparison response unparseable — retrying fresh...", "flag")
                response, meta = call_llm(prompt, max_tokens=LLM_MAX_TOKENS,
                                          provider_idx=start_tier("risk_factors"),
                                          requires_strict_json=True,
                                          bypass_cache=True)
        except Exception as e:
            _emit("LLM comparison failed: %s" % str(e)[:120], "flag")
            result["summary"] = "Risk factor comparison failed (LLM error)."
            return result

        top_changes = self._build_top_changes(
            response, old_rf, new_rf, old_filing.get("url", ""), new_filing.get("url", ""))
        if top_changes is None:
            _emit("Could not parse the comparison output.", "flag")
            result["summary"] = "Risk factor comparison returned an unparseable result."
            return result

        # Part 1 retry: findings dropped for unverifiable quotes get ONE fresh
        # comparison pass (past the byte cache) to recover them; keep whichever
        # result grounds more findings. Unverified quotes are never emitted.
        drops = getattr(self, "_last_unverified_drops", 0)
        if drops:
            _emit("%d finding(s) failed verbatim verification — retrying comparison once..." % drops, "flag")
            try:
                response2, _ = call_llm(prompt, max_tokens=LLM_MAX_TOKENS,
                                        provider_idx=start_tier("risk_factors"),
                                        requires_strict_json=True,
                                        bypass_cache=True)
                tc2 = self._build_top_changes(
                    response2, old_rf, new_rf,
                    old_filing.get("url", ""), new_filing.get("url", ""))
                drops2 = getattr(self, "_last_unverified_drops", 0)
                if tc2 is not None and (drops2 < drops or
                                        (drops2 == drops and len(tc2) > len(top_changes))):
                    top_changes = tc2
            except Exception:
                pass

        result["top_changes"] = top_changes
        result["changes_found"] = len(top_changes)
        # Provisional 10-K-only score (the 10-Q phase below may refine it). PART 2:
        # routed through the shared scorer so the band/signal/rationale are unified.
        _score, _signal, _rationale = score_risk_factors(top_changes)
        result["score"] = _score
        result["signal"] = _signal
        result["score_rationale"] = _rationale
        result["summary"] = (
            "%d material change(s) in Item 1A between %s and %s." % (
                len(top_changes), result["old_filing_date"], result["new_filing_date"])
            if top_changes else
            "No material changes in Item 1A between the two 10-Ks.")

        # ── 10-Q phase (additive) ──────────────────────────────────────────────
        # The annual 10-K vs 10-K delta above is the baseline; a 10-Q filed AFTER
        # the latest 10-K carries more up-to-date changes. Compare it against the
        # correct baseline (previous 10-Q or the 10-K) per its format. Best-effort:
        # any failure leaves ten_q_findings None and the 10-K result intact.
        result["ten_q_findings"] = None
        try:
            self._run_10q_phase(ticker, new_filing, new_rf, new_html, result, _emit)
        except Exception as e:
            _emit("10-Q check skipped: %s" % str(e)[:120], "flag")

        # Final score reflects BOTH comparisons; the 10-Q is weighted 1.5x because
        # it's more recent. With no 10-Q this is exactly the 10-K score as before.
        # PART 2: final score via the shared scorer — same category+rank model
        # (already the most mature scorer), now with the 10-Q blended in (1.5x,
        # more recent), the band unified to [10,95], and a rationale attached.
        tqf = result.get("ten_q_findings")
        if tqf and tqf.get("changes") is not None:
            _score, _signal, _rationale = score_risk_factors(top_changes, tqf["changes"])
            result["score"] = _score
            result["signal"] = _signal
            result["score_rationale"] = _rationale

        # Completion message shows SEVERITY COUNTS (Part 7.1, 2026-07-05), not
        # the raw score — mirroring the card headers (runs.py _RISK_SEVERITY:
        # ESCALATED/NEW=high, REMOVED=medium, DE-ESCALATED/REWORDED=low).
        _sev_of = {"ESCALATED": "high", "NEW": "high", "REMOVED": "medium",
                   "DE-ESCALATED": "low", "REWORDED": "low"}
        _all_changes = list(top_changes) + list(
            (result.get("ten_q_findings") or {}).get("changes") or [])
        _counts = {"high": 0, "medium": 0, "low": 0}
        for c in _all_changes:
            _counts[_sev_of.get(str(c.get("category", "")).upper(), "medium")] += 1
        _emit("Risk factor scan complete — %d high · %d medium · %d low." % (
            _counts["high"], _counts["medium"], _counts["low"]), "done")
        # PART 3: archive this run (manifest + index line). Exception-safe.
        try:
            from src.utils.run_archive import save_run
            save_run("risk_factors", ticker, datetime.now().year, result,
                     duration_ms=(datetime.now() - _t0).total_seconds() * 1000)
        except Exception:
            pass
        return result

    def _build_top_changes(self, response: str, old_rf: str, new_rf: str,
                           old_url: str, new_url: str,
                           update_only: bool = False) -> Optional[List[dict]]:
        """Parse one LLM comparison response into the verified, ranked top-10
        change rows. Shared by the 10-K and 10-Q paths — only the response, the
        quote-verification sources (old_rf/new_rf) and the SEC links
        (old_url/new_url) differ. Returns None when the response can't be parsed
        at all (the caller emits the unparseable-output message).

        When `update_only` is set — an update-only 10-Q compared against the full
        10-K baseline (OLD = 10-K, NEW = the short update) — an extra pass drops
        unprovable REMOVED findings and un-forces topically-unrelated matches.
        See _filter_update_only. The 10-K and 10-Q-reprint paths pass False and
        are byte-for-byte unaffected."""
        changes = self._parse_changes(response)
        if changes is None:
            return None

        # Drop duplicate citations, recover escalations the model under-classified,
        # (update-only) drop unprovable removals / un-force mismatches, then rank,
        # cap at top 10, verify, link.
        changes = self._dedup_changes(changes)
        changes = self._reclassify_escalations(changes)
        if update_only:
            changes = self._filter_update_only(changes)
        changes.sort(key=lambda c: c.get("rank", 999) if isinstance(c.get("rank"), int) else 999)
        # Output EVERY significant change found (ranked), not just the top 10.

        from src.utils.page_numbers import build_page_map, page_of_quote
        old_pm = build_page_map(old_rf)
        new_pm = build_page_map(new_rf)
        top_changes = []
        # ZERO-UNVERIFIED CONTRACT (Part 1, 2026-07-04): quotes are verified,
        # then REPAIRED by re-anchoring to the source when the model stitched
        # or rewrote them; findings whose substantive quote still can't be
        # grounded verbatim are DROPPED (never shown), counted here so the
        # caller can retry the whole comparison once past the byte cache.
        self._last_unverified_drops = 0
        for c in changes:
            category = str(c.get("category", "")).upper().strip()
            old_q = (c.get("old_quote") or "").strip()
            new_q = (c.get("new_quote") or "").strip()
            old_q, old_ok = _verify_or_repair(old_q, old_rf)
            new_q, new_ok = _verify_or_repair(new_q, new_rf)
            if old_ok is False or new_ok is False:
                self._last_unverified_drops += 1
                continue
            # Mark quotes the model cut off at max_tokens so a sentence that
            # just stops reads as truncated, not broken.
            entry = {
                "rank": len(top_changes) + 1,
                "category": category,
                "risk_title": (c.get("risk_title") or "").strip(),
                "old_quote": _mark_if_truncated(old_q),
                "new_quote": _mark_if_truncated(new_q),
                "summary": (c.get("summary") or "").strip(),
                "old_url": old_url,
                "new_url": new_url,
                # None (side legitimately absent, e.g. no OLD quote on a NEW
                # risk) must read as "nothing to verify", not "unverified".
                "old_quote_verified": old_ok is not False,
                "new_quote_verified": new_ok is not False,
                "verified": True,
            }
            # Printed page numbers (Part 5) — attached only when the filing's
            # pagination survived cleaning AND the quote was located.
            pg = page_of_quote(old_rf, old_q, old_pm) if old_q else None
            if pg:
                entry["old_quote_page"] = pg
            pg = page_of_quote(new_rf, new_q, new_pm) if new_q else None
            if pg:
                entry["new_quote_page"] = pg
            top_changes.append(entry)
        return top_changes

    @staticmethod
    def _build_comparison_prompt(old_clip: str, new_clip: str, old_doc_type: str,
                                 old_date: str, new_doc_type: str, new_date: str) -> str:
        """Build the comparison prompt for a non-10-K-vs-10-K pairing. Uses the
        SAME template as the 10-K path — only the INPUT-section labels change to
        name what's actually being compared (e.g. OLD 10-K vs NEW 10-Q). The
        label swaps run BEFORE the text substitutions so document text can never
        collide with a label."""
        return (RISK_DELTA_PROMPT
                .replace("OLD 10-K (prior year):", "OLD %s (%s):" % (old_doc_type, old_date))
                .replace("NEW 10-K (current year):", "NEW %s (%s):" % (new_doc_type, new_date))
                .replace("{old_risk_factors_full_text}", old_clip)
                .replace("{new_risk_factors_full_text}", new_clip))

    def _run_10q_phase(self, ticker: str, new_filing: dict, new_rf: str,
                       new_html: str, result: dict, emit) -> None:
        """Check for a 10-Q filed AFTER the most recent 10-K and, when found,
        compare its Risk Factors against the correct baseline. Populates
        result['ten_q_findings'] (left as None when there's no usable 10-Q).

        The four rules:
          * reprint  -> compare vs the previous 10-Q in this cycle, or the 10-K
                        if this is the cycle's first 10-Q;
          * update_only -> always compare vs the 10-K baseline.
        Cache-first throughout (get_filing_text checks the text cache first)."""
        emit("Checking for more recent 10-Q filings...", "search")
        new_filing_date = new_filing.get("filing_date", "")
        # Current-cycle 10-Qs are those filed after the latest 10-K.
        cycle_qs = [f for f in self._get_all_filings(ticker)
                    if f.get("form") == "10-Q" and f.get("filing_date", "") > new_filing_date]
        if not cycle_qs:
            return  # no 10-Q after the latest 10-K — show 10-K vs 10-K only

        ten_q = max(cycle_qs, key=lambda f: f.get("filing_date", ""))
        ten_q_html = self.get_filing_text(ten_q)
        ten_q_rf = extract_risk_factors(ten_q_html) if ten_q_html else ""
        if not ten_q_rf:
            return  # couldn't isolate Item 1A in the 10-Q

        fmt = detect_format(ten_q_rf, new_rf, ten_q_html, new_html)
        emit("Found 10-Q from %s — format: %s" % (ten_q.get("filing_date", ""), fmt), "fetch")

        # REFERRAL-STUB SHORT-CIRCUIT (Part 1, 2026-07-04): TSLA-style 10-Qs
        # carry a one-sentence Item 1A that only refers back to the 10-K's risk
        # factors. There is NOTHING to compare — forcing the LLM to find
        # changes against a 327-char stub made it invent "ESCALATED" findings
        # whose quotes were 10-K text (all failed verification). Report the
        # honest zero instead of asking the model to hallucinate.
        if fmt == "update_only" and _is_referral_stub(ten_q_rf):
            emit("10-Q Item 1A only refers back to the 10-K's risk factors — "
                 "no new risk disclosures this quarter.", "analyze")
            result["ten_q_findings"] = {
                "format": fmt,
                "filing_date": ten_q.get("filing_date", ""),
                "filing_url": ten_q.get("url", ""),
                "compared_against": "10-K",
                "compared_date": new_filing_date,
                "compared_url": new_filing.get("url", ""),
                "changes": [],
                "changes_found": 0,
                "no_material_changes": True,
            }
            return

        # Pick the baseline (OLD) per the four rules. Default is the 10-K.
        old_doc_type, old_filing, old_rf, old_date = "10-K", new_filing, new_rf, new_filing_date
        if fmt == "reprint":
            prev_qs = [f for f in cycle_qs
                       if f.get("filing_date", "") < ten_q.get("filing_date", "")]
            if prev_qs:
                prev_q = max(prev_qs, key=lambda f: f.get("filing_date", ""))
                prev_html = self.get_filing_text(prev_q)
                prev_rf = extract_risk_factors(prev_html) if prev_html else ""
                if prev_rf:  # fall back to the 10-K baseline if extraction fails
                    old_doc_type, old_filing, old_rf, old_date = (
                        "10-Q", prev_q, prev_rf, prev_q.get("filing_date", ""))

        prompt = self._build_comparison_prompt(
            old_rf[:MAX_SECTION_CHARS], ten_q_rf[:MAX_SECTION_CHARS],
            old_doc_type, old_date, "10-Q", ten_q.get("filing_date", ""))

        emit("Comparing 10-Q risk factors against %s..." % old_doc_type, "analyze")
        try:
            response, meta = call_llm(prompt, max_tokens=LLM_MAX_TOKENS,
                                      provider_idx=start_tier("risk_factors"),
                                      requires_strict_json=True)
            if self._parse_changes(response) is None:
                emit("10-Q comparison unparseable — retrying fresh...", "flag")
                response, meta = call_llm(prompt, max_tokens=LLM_MAX_TOKENS,
                                          provider_idx=start_tier("risk_factors"),
                                          requires_strict_json=True,
                                          bypass_cache=True)
        except Exception as e:
            emit("10-Q comparison failed: %s" % str(e)[:120], "flag")
            return

        ten_q_changes = self._build_top_changes(
            response, old_rf, ten_q_rf, old_filing.get("url", ""), ten_q.get("url", ""),
            update_only=(fmt == "update_only"))
        if ten_q_changes is None:
            emit("Could not parse the 10-Q comparison output.", "flag")
            return

        # Part 1 retry (same contract as the 10-K path): one fresh pass when
        # findings were dropped for unverifiable quotes.
        drops = getattr(self, "_last_unverified_drops", 0)
        if drops:
            emit("%d 10-Q finding(s) failed verbatim verification — retrying once..." % drops, "flag")
            try:
                response2, _ = call_llm(prompt, max_tokens=LLM_MAX_TOKENS,
                                        provider_idx=start_tier("risk_factors"),
                                        requires_strict_json=True,
                                        bypass_cache=True)
                tc2 = self._build_top_changes(
                    response2, old_rf, ten_q_rf,
                    old_filing.get("url", ""), ten_q.get("url", ""),
                    update_only=(fmt == "update_only"))
                drops2 = getattr(self, "_last_unverified_drops", 0)
                if tc2 is not None and (drops2 < drops or
                                        (drops2 == drops and len(tc2) > len(ten_q_changes))):
                    ten_q_changes = tc2
            except Exception:
                pass

        emit("10-Q comparison complete — %d changes found" % len(ten_q_changes), "analyze")
        result["ten_q_findings"] = {
            "format": fmt,
            "filing_date": ten_q.get("filing_date", ""),
            "filing_url": ten_q.get("url", ""),
            "compared_against": old_doc_type,
            "compared_date": old_date,
            "compared_url": old_filing.get("url", ""),
            "changes": ten_q_changes,
            "changes_found": len(ten_q_changes),
        }

    @staticmethod
    def _reclassify_escalations(changes: List[dict]) -> List[dict]:
        """Deterministic safety net for the weaker fallback model, which sometimes
        labels a genuine escalation as REWORDED. Promote a REWORDED change to
        ESCALATED when the NEW quote either (a) adds conditional-threat language
        the OLD quote lacks, or (b) expands the OLD quote (>1.5x its length and
        fully contains it). Other categories are left untouched."""
        for c in changes:
            if str(c.get("category", "")).upper() != "REWORDED":
                continue
            old_q = (c.get("old_quote") or "").strip().lower()
            new_q = (c.get("new_quote") or "").strip().lower()
            if not new_q:
                continue
            added_threat = any(p in new_q and p not in old_q for p in _THREAT_PHRASES)
            expanded = bool(old_q) and len(new_q) > len(old_q) * 1.5 and old_q in new_q
            if added_threat or expanded:
                c["category"] = "ESCALATED"
                c["summary"] = (
                    "Risk expanded — added conditional adverse-effect language."
                    if added_threat else
                    "Risk expanded — the new filing broadens the prior risk statement.")
        return changes

    @staticmethod
    def _topic_words(quote: Optional[str]) -> set:
        """Significant (4+ letter, non-stopword) words in a quote, used to judge
        whether two quotes discuss the same business concern. Numbers and short
        grammatical glue are ignored — see _TOPIC_STOPWORDS."""
        return {w for w in re.findall(r"[a-z]{4,}", (quote or "").lower())
                if w not in _TOPIC_STOPWORDS}

    @staticmethod
    def _quotes_unrelated(old_quote: str, new_quote: str) -> bool:
        """True when an OLD/NEW quote pair shares ZERO significant words — i.e.
        the model paired two risks about different concerns (e.g. Ford's
        warranty-accounting OLD vs regulatory-compliance NEW). Deliberately
        conservative: it only fires on a complete topical disjoint, and only when
        BOTH quotes carry enough content words (>=2) to judge, so a genuine
        reword that merely shares one topic word (e.g. 'financial') is kept."""
        old_w = RiskFactorAgent._topic_words(old_quote)
        new_w = RiskFactorAgent._topic_words(new_quote)
        if len(old_w) < 2 or len(new_w) < 2:
            return False  # too little content to call it a mismatch
        return len(old_w & new_w) == 0

    @staticmethod
    def _filter_update_only(changes: List[dict]) -> List[dict]:
        """Post-process the findings from an UPDATE-ONLY 10-Q comparison. An
        update-only 10-Q is a PARTIAL document listing only this quarter's
        changes, compared against the full 10-K baseline (OLD = 10-K, NEW = the
        short update). Two corrections the model can't make reliably on a partial
        document:

          * REMOVED is unprovable. A risk present in the 10-K but absent from the
            short update was simply not re-stated this quarter — NOT deleted from
            the company's disclosures. A removal can only be read from a full
            reprint, so every REMOVED finding is dropped here.
          * Forced matches. With no clear counterpart for a genuinely new risk,
            the model may pair it with an unrelated OLD sentence (Ford: warranty
            accounting OLD vs regulatory-compliance-agreement NEW) and call it a
            change. When the OLD/NEW quotes share no topical words, the match is
            spurious: reclassify the finding as NEW with no OLD quote, so the new
            risk stands on its own.

        Only invoked for update-only comparisons; the 10-K and 10-Q-reprint paths
        never call this."""
        out = []
        for c in changes:
            if str(c.get("category", "")).upper().strip() == "REMOVED":
                continue  # a partial update cannot establish a removal
            old_q = (c.get("old_quote") or "").strip()
            new_q = (c.get("new_quote") or "").strip()
            if old_q and new_q and RiskFactorAgent._quotes_unrelated(old_q, new_q):
                c = dict(c)  # copy — don't mutate the parsed object in place
                c["category"] = "NEW"
                c["old_quote"] = ""  # the paired OLD sentence was a wrong match
                c["summary"] = ("New risk surfaced in the 10-Q with no matching "
                                "risk in the 10-K baseline to compare against.")
            out.append(c)
        return out

    @staticmethod
    def _is_identical_pair(c: dict) -> bool:
        """True when a finding's OLD and NEW quotes are the same text after
        normalizing quotes/whitespace/case. The comparison model occasionally
        emits a "change" whose two quotes are character-for-character identical
        (e.g. Ford's Takata recall paragraph) — a hallucinated escalation with no
        real delta. Such findings are dropped before dedup, because a lone
        identical pair has nothing to collapse against in the pair-key pass."""
        old_q, new_q = _norm(c.get("old_quote")), _norm(c.get("new_quote"))
        return bool(old_q) and bool(new_q) and old_q == new_q

    @staticmethod
    def _dedup_changes(changes: List[dict]) -> List[dict]:
        """Drop changes that cite the SAME passage, then collapse systematic
        find/replace edits. A weaker model can emit the identical quote several
        times (often re-titled), so the identity of a change is the quote it
        cites: we key on the (normalized) new/old quote and keep the first
        occurrence. We ALSO key on the (old_quote, new_quote) PAIR — the 70B
        model sometimes returns several findings for one risk with identical old
        AND new quotes and only slightly different titles; a finding matching
        EITHER key is a duplicate. Quote-less entries fall back to their title.
        Finally _collapse_global_changes folds a systematic word-for-word
        substitution reported once per risk factor into a single finding."""
        # Pre-dedup: drop hallucinated "changes" whose OLD and NEW quotes are the
        # same text. A lone identical pair never matches the pair-key below (there
        # is no second copy to collapse into), so it must be removed up front or
        # it survives and gets mislabeled ESCALATED.
        changes = [c for c in changes if not RiskFactorAgent._is_identical_pair(c)]

        def _n(v):
            return re.sub(r"[^a-z0-9]+", " ", str(v or "").lower()).strip()

        def _single_key(c):
            for field in ("new_quote", "old_quote"):
                q = _n(c.get(field))
                if len(q) >= 30:
                    return ("q", q[:160])
            t = _n(c.get("risk_title"))
            return ("t", t) if t else None

        def _pair_key(c):
            # A real (old, new) pair only when BOTH sides carry text — keeps NEW
            # (no old) / REMOVED (no new) and quote-less findings from collapsing.
            old_q, new_q = _n(c.get("old_quote")), _n(c.get("new_quote"))
            if old_q and new_q:
                return ("p", old_q[:160], new_q[:160])
            return None

        seen, out = set(), []
        for c in changes:
            keys = [k for k in (_single_key(c), _pair_key(c)) if k is not None]
            if any(k in seen for k in keys):
                continue
            seen.update(keys)
            out.append(c)
        return RiskFactorAgent._collapse_global_changes(out)

    @staticmethod
    def _first_word_diff(old_quote: str, new_quote: str) -> Optional[Tuple[str, str]]:
        """Return the first (old_word, new_word) pair where two quotes diverge,
        walking their words in parallel. Used to spot a one-word substitution
        (e.g. "vehicles" -> "products"). Returns None when the aligned words never
        differ within the shorter quote's length."""
        old_words = re.findall(r"[a-z0-9]+", (old_quote or "").lower())
        new_words = re.findall(r"[a-z0-9]+", (new_quote or "").lower())
        for ow, nw in zip(old_words, new_words):
            if ow != nw:
                return (ow, nw)
        return None

    @staticmethod
    def _collapse_global_changes(changes: List[dict]) -> List[dict]:
        """Collapse a systematic find/replace into one finding. When a company
        swaps a term throughout Item 1A (e.g. "vehicles" -> "products"), the model
        tends to report it once per affected risk factor — many near-identical
        findings that are really a single editorial change. We group findings by
        the first differing word pair between their old/new quotes; when 3+ share
        the same substitution we drop them all and emit ONE REWORDED summary in
        the slot of the first, so the systematic change counts exactly once."""
        groups: Dict[Tuple[str, str], List[int]] = {}
        for i, c in enumerate(changes):
            old_q, new_q = (c.get("old_quote") or "").strip(), (c.get("new_quote") or "").strip()
            if not old_q or not new_q:
                continue
            pair = RiskFactorAgent._first_word_diff(old_q, new_q)
            if pair:
                groups.setdefault(pair, []).append(i)

        drop, summary_at = set(), {}
        for (old_w, new_w), idxs in groups.items():
            if len(idxs) < 3:
                continue
            drop.update(idxs)
            tmpl = changes[idxs[0]]
            summary_at[idxs[0]] = {
                "category": "REWORDED",
                "risk_title": "Systematic wording change: '%s' → '%s'" % (old_w, new_w),
                "rank": tmpl.get("rank"),
                "old_quote": tmpl.get("old_quote", ""),
                "new_quote": tmpl.get("new_quote", ""),
                "summary": ("Systematic rewording across %d risk factors — '%s' replaced "
                            "with '%s' throughout Item 1A. Counted as one editorial change, "
                            "not %d separate risks." % (len(idxs), old_w, new_w, len(idxs))),
            }

        if not drop:
            return changes
        out = []
        for i, c in enumerate(changes):
            if i in summary_at:
                out.append(summary_at[i])
            elif i not in drop:
                out.append(c)
        return out

    @staticmethod
    def _parse_changes(response: str) -> Optional[List[dict]]:
        """Parse the LLM's JSON array of changes. Tolerates markdown fences and
        leading/trailing prose. Returns a list (possibly empty) or None on hard
        failure."""
        if not response:
            return []
        # Shared tolerant extractor first (fences, preamble, trailing commas,
        # single quotes, comments) — see src/utils/json_extractor.
        try:
            from src.utils.json_extractor import extract_json
            data = extract_json(response)
        except ValueError:
            data = None
        if data is None:
            # Output was likely truncated mid-array (hit max_tokens) — recover
            # every complete flat object so we keep the findings that did fit.
            parsed = []
            for obj in re.findall(r"\{[^{}]*\}", response, re.DOTALL):
                try:
                    parsed.append(json.loads(obj))
                except Exception:
                    pass
            if not parsed:
                return None
            data = parsed
        if isinstance(data, dict):
            # Defensive: a model may wrap the array under a key.
            for v in data.values():
                if isinstance(v, list):
                    data = v
                    break
            else:
                data = [data]
        if not isinstance(data, list):
            return None
        return [c for c in data if isinstance(c, dict)]


def run_risk_factors(ticker: str, progress=None) -> dict:
    return RiskFactorAgent().run(ticker, progress=progress)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Agent 4 — Risk Factor Delta")
    parser.add_argument("ticker", nargs="?", default="F")
    args = parser.parse_args()

    def _p(msg, kind="info"):
        print("  [%s] %s" % (kind, msg))

    out = RiskFactorAgent().run(args.ticker, progress=_p)
    print(json.dumps(out, indent=2))
