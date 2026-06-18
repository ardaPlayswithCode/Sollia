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

from src.config.llm_config import call_llm
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

# Significance weight per change category. A higher overall score => more/worse
# risk disclosure => more BEARISH. Mirrors the spec exactly.
CATEGORY_WEIGHTS = {
    "ESCALATED": 100,
    "NEW": 80,
    "REMOVED": 60,
    "DE-ESCALATED": 40,
    "REWORDED": 20,
}

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
        seg = clean[s:s + 90]
        m = re.match(r"item\s*1a[\.\:\)\s]*risk\s*factors[\.\:\s]*", seg, re.IGNORECASE)
        if not m:
            continue  # this "Item 1A" is not the Risk Factors header
        if "continued" in seg[:m.end()].lower():
            continue  # running page header
        after = seg[m.end():].lstrip()
        if not after or after[0].isdigit():
            continue  # empty or a TOC page number
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
        return ""
    start = _find_genuine_1a_start(clean, a_starts)
    if start is None:
        return ""

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
    return re.sub(r"\s+", " ", section).strip()


def _norm(s: Optional[str]) -> str:
    """Normalize for quote matching: drop smart/straight quotes, collapse
    whitespace, lowercase."""
    if not s:
        return ""
    s = re.sub(r"[‘’“”\"']", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


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


class RiskFactorAgent:
    def __init__(self):
        self.cache_root = os.path.join(_SOLLIA_WORKING, ".cache")
        os.makedirs(os.path.join(self.cache_root, "filings"), exist_ok=True)
        os.makedirs(os.path.join(self.cache_root, "texts"), exist_ok=True)
        self.provider = SECEdgarProvider()

    # ── fetching ──────────────────────────────────────────────────────────────

    def _normalize_filing(self, ticker, cik, acc_dash, filing_date, period, primary_doc):
        cik_str = str(cik).zfill(10) if cik not in (None, "") else ""
        acc_nd = (acc_dash or "").replace("-", "")
        url = ""
        if cik_str and acc_nd and primary_doc:
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik_str)}/{acc_nd}/{primary_doc}"
        return {
            "ticker": ticker,
            "cik": cik_str,
            "form": "10-K",
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

    def get_filing_text(self, filing: dict) -> str:
        """Full raw text (HTML) of a filing. Checks the shared text cache first,
        then fetches via the SEC EDGAR provider and caches the result."""
        if not filing:
            return ""
        ticker = filing.get("ticker", "")
        acc = filing.get("accession_number", "")
        if ticker and acc:
            cached = get_cached_text(ticker, acc)
            if cached:
                return cached
        html = self.provider.fetch_html(
            filing.get("cik", ""), filing.get("accession_nodash", ""),
            filing.get("primary_doc", ""), ticker)
        if html and ticker and acc:
            cache_text(ticker, acc, html)
        return html or ""

    # ── scoring ───────────────────────────────────────────────────────────────

    @staticmethod
    def _score(changes: List[dict]) -> int:
        """Weighted average of the (up to 10) change categories, 0-100. Higher =>
        more/worse risk disclosure. Neutral (50) when nothing changed."""
        if not changes:
            return 50
        vals = [CATEGORY_WEIGHTS.get(str(c.get("category", "")).upper(), 50) for c in changes]
        return int(round(sum(vals) / len(vals)))

    @staticmethod
    def _signal(score: int) -> str:
        if score >= 65:
            return "BEARISH"
        if score <= 35:
            return "BULLISH"
        return "NEUTRAL"

    # ── main entry ────────────────────────────────────────────────────────────

    def run(self, ticker: str, progress=None) -> dict:
        """Run the full Risk Factor Delta scan for a ticker."""
        ticker = (ticker or "").upper()

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
            result["summary"] = "Fewer than two 10-K filings available for comparison."
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
        if not old_rf or not new_rf:
            _emit("Item 1A could not be located in one or both 10-Ks.", "flag")
            result["summary"] = "Item 1A (Risk Factors) not found in one or both 10-Ks."
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
            response, meta = call_llm(prompt, max_tokens=LLM_MAX_TOKENS)
        except Exception as e:
            _emit("LLM comparison failed: %s" % str(e)[:120], "flag")
            result["summary"] = "Risk factor comparison failed (LLM error)."
            return result

        changes = self._parse_changes(response)
        if changes is None:
            _emit("Could not parse the comparison output.", "flag")
            result["summary"] = "Risk factor comparison returned an unparseable result."
            return result

        # Drop duplicate citations, then rank, cap at top 10, verify, link.
        changes = self._dedup_changes(changes)
        changes.sort(key=lambda c: c.get("rank", 999) if isinstance(c.get("rank"), int) else 999)
        changes = changes[:10]

        old_url, new_url = old_filing.get("url", ""), new_filing.get("url", "")
        top_changes = []
        for i, c in enumerate(changes, start=1):
            category = str(c.get("category", "")).upper().strip()
            old_q = (c.get("old_quote") or "").strip()
            new_q = (c.get("new_quote") or "").strip()
            old_ok = _verify_quote(old_q, old_rf)
            new_ok = _verify_quote(new_q, new_rf)
            # A finding is "verified" when every quote it actually carries checks
            # out (a missing OLD quote on a NEW risk does not count against it).
            verified = (old_ok is not False) and (new_ok is not False)
            top_changes.append({
                "rank": i,
                "category": category,
                "risk_title": (c.get("risk_title") or "").strip(),
                "old_quote": old_q,
                "new_quote": new_q,
                "summary": (c.get("summary") or "").strip(),
                "old_url": old_url,
                "new_url": new_url,
                "old_quote_verified": bool(old_ok),
                "new_quote_verified": bool(new_ok),
                "verified": bool(verified),
            })

        result["top_changes"] = top_changes
        result["changes_found"] = len(top_changes)
        result["score"] = self._score(top_changes)
        result["signal"] = self._signal(result["score"])
        result["summary"] = (
            "%d material change(s) in Item 1A between %s and %s." % (
                len(top_changes), result["old_filing_date"], result["new_filing_date"])
            if top_changes else
            "No material changes in Item 1A between the two 10-Ks.")

        verified_n = sum(1 for c in top_changes if c["verified"])
        _emit("Risk factor scan complete — %d change(s), %d quote-verified. Score %d (%s)." % (
            len(top_changes), verified_n, result["score"], result["signal"]), "done")
        return result

    @staticmethod
    def _dedup_changes(changes: List[dict]) -> List[dict]:
        """Drop changes that cite the SAME passage. A weaker model can emit the
        identical quote several times (often re-titled); the identity of a change
        is the quote it cites, so we key on the (normalized) new/old quote and
        keep the first occurrence. Quote-less entries fall back to their title."""
        def _n(v):
            return re.sub(r"[^a-z0-9]+", " ", str(v or "").lower()).strip()

        def _key(c):
            for field in ("new_quote", "old_quote"):
                q = _n(c.get(field))
                if len(q) >= 30:
                    return ("q", q[:160])
            t = _n(c.get("risk_title"))
            return ("t", t) if t else None

        seen, out = set(), []
        for c in changes:
            k = _key(c)
            if k is not None and k in seen:
                continue
            if k is not None:
                seen.add(k)
            out.append(c)
        return out

    @staticmethod
    def _parse_changes(response: str) -> Optional[List[dict]]:
        """Parse the LLM's JSON array of changes. Tolerates markdown fences and
        leading/trailing prose. Returns a list (possibly empty) or None on hard
        failure."""
        if not response:
            return []
        cleaned = response.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        data = None
        try:
            data = json.loads(cleaned)
        except Exception:
            # Salvage the first complete JSON array embedded in the response.
            m = re.search(r"\[.*\]", cleaned, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    data = None
        if data is None:
            # Output was likely truncated mid-array (hit max_tokens) — recover
            # every complete flat object so we keep the findings that did fit.
            parsed = []
            for obj in re.findall(r"\{[^{}]*\}", cleaned, re.DOTALL):
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
