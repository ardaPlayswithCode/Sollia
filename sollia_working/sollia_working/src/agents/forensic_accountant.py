"""Forensic Accountant agent — gaps between CEO transcript tone and 10-K reality."""
from __future__ import annotations

import math
import re
import logging
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

from src.schemas import (
    AgentLogMessage,
    FilingResult,
    SolliaDiscrepancySignal,
    SolliaForensicResult,
    SolliaQuarterlyPair,
    SolliaRealityMetrics,
    SolliaToneMetrics,
    TranscriptResult,
)

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/generate"

HEDGING_WORDS = [
    "would", "could", "may", "subject to", "uncertainties", "believe",
]
FORWARD_LOOKING_WORDS = [
    "expect", "anticipate", "outlook", "guidance", "forecast", "will", "plan", "intend",
]

BULLISH_REVENUE = [
    r"revenue.*(?:increas|grew|growth|rose)",
    r"revenue\s+up\s+\d",
    r"sales.*increas",
    r"(?:increas|growth).*revenue",
    r"net revenue.*higher",
]
BEARISH_REVENUE = [
    r"revenue.*(?:declin|decreas|fell|drop|lower|contract)",
    r"sales.*declin",
]
CONTRADICTION_PAIRS = [
    ("record revenue", r"revenue.*(?:declin|fell|drop)"),
    ("revenue growth", r"revenue.*(?:declin|decreased)"),
    ("strong performance", r"net loss.*increas|operat.*loss"),
    ("profitable", r"net loss of|operating loss of"),
    ("record earnings", r"earnings.*(?:declin|fell)"),
]


def _add_log(msgs: List[AgentLogMessage], text: str) -> None:
    msgs.append(AgentLogMessage(speaker="ForensicAccountant", message=text))
    logger.info("[FA] %s", text)


def _ask_ollama(prompt: str, timeout: int = 25) -> Optional[str]:
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": "llama3.2", "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json().get("response", "").strip()
    except Exception:
        pass
    return None


def _extract_tone(text: str, use_ollama: bool = True) -> SolliaToneMetrics:
    if not text:
        return SolliaToneMetrics()

    words = re.findall(r"\b\w+\b", text.lower())
    word_count = max(len(words), 1)
    per_1k = 1000.0 / word_count
    text_lc = text.lower()

    hedging_count = sum(text_lc.count(w) for w in HEDGING_WORDS)
    hedging_density = hedging_count * per_1k

    we_will_are = text_lc.count("we will") + text_lc.count("we are")
    we_expect_hope = max(text_lc.count("we expect") + text_lc.count("we hope to"), 1)
    confidence_ratio = we_will_are / we_expect_hope

    sentences = [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]
    lengths = [len(s.split()) for s in sentences if s]
    sent_mean = statistics.mean(lengths) if lengths else 0.0
    sent_var = statistics.variance(lengths) if len(lengths) > 1 else 0.0

    fl_count = sum(text_lc.count(w) for w in FORWARD_LOOKING_WORDS)
    fl_density = fl_count * per_1k

    overall_sentiment = 0.0
    if use_ollama:
        excerpt = text[:1500]
        prompt = (
            f"Sentiment from -1.0 (very negative) to +1.0 (very positive). "
            f"Reply ONLY with the number.\n\nText: {excerpt}\n\nScore:"
        )
        resp = _ask_ollama(prompt)
        if resp:
            m = re.search(r"-?\d+\.?\d*", resp)
            if m:
                overall_sentiment = max(-1.0, min(1.0, float(m.group())))

    if overall_sentiment == 0.0:
        overall_sentiment = max(-1.0, min(1.0, (confidence_ratio - 0.5) * 2.0))

    raw = (
        confidence_ratio * 0.45
        - min(hedging_density / 60.0, 0.3) * 0.25
        + overall_sentiment * 0.30
    )
    raw = max(-1.0, min(1.0, raw))

    return SolliaToneMetrics(
        hedging_density=round(hedging_density, 4),
        confidence_ratio=round(confidence_ratio, 4),
        sentence_length_mean=round(sent_mean, 2),
        sentence_length_variance=round(sent_var, 2),
        forward_looking_density=round(fl_density, 4),
        overall_sentiment=round(overall_sentiment, 4),
        raw_score=round(raw, 4),
    )


def _extract_reality(mda: str) -> SolliaRealityMetrics:
    if not mda:
        return SolliaRealityMetrics()

    text_lc = mda.lower()
    contexts: List[str] = []

    pos = 0
    while True:
        idx = text_lc.find("revenue", pos)
        if idx == -1:
            break
        contexts.append(mda[idx: idx + 250])
        pos = idx + 1

    rop = re.search(r"results\s+of\s+operations", mda, re.IGNORECASE)
    if rop:
        contexts.append(mda[rop.start(): rop.start() + 3000])

    combined = " ".join(contexts).lower()

    direction = 0
    for pat in BEARISH_REVENUE:
        if re.search(pat, combined, re.IGNORECASE):
            direction = -1
            break
    if direction == 0:
        for pat in BULLISH_REVENUE:
            if re.search(pat, combined, re.IGNORECASE):
                direction = 1
                break

    magnitude = 0.0
    pct_pats = [
        r"revenue.*?(\d+\.?\d*)\s*%\s*(?:increase|growth|higher)",
        r"revenue.*?(\d+\.?\d*)\s*(?:percent|%)\s*(?:to|versus|vs)",
        r"increas\w+.*?(\d+\.?\d*)\s*(?:percent|%)",
        r"grew\s+(?:by\s+)?(\d+\.?\d*)\s*(?:percent|%)",
    ]
    for pat in pct_pats:
        m = re.search(pat, combined, re.IGNORECASE)
        if m:
            try:
                magnitude = float(m.group(1))
                break
            except (ValueError, IndexError):
                pass

    mag_factor = min(magnitude / 20.0, 1.0) if magnitude > 0 else 0.3
    raw_score = direction * max(mag_factor, 0.2)

    # Get the first matched context for citation
    context_snippet = ""
    for ctx in contexts:
        for pat in (BULLISH_REVENUE + BEARISH_REVENUE):
            m = re.search(pat, ctx, re.IGNORECASE)
            if m:
                context_snippet = ctx.strip()[:200]
                break
        if context_snippet:
            break
    # Fallback: find a revenue sentence with growth/decline meaning, skip risk boilerplate
    if not context_snippet:
        for ctx in contexts:
            # Find sentences containing revenue with business-meaningful language
            for m in re.finditer(r'(?:[A-Z](?:[^.]|\$\d+\.\d+)*revenue(?:[^.]|\$\d+\.\d+)*\.)', ctx, re.IGNORECASE):
                sentence = m.group().strip()
                if len(sentence) < 30:
                    continue
                # Skip risk-factor boilerplate
                if re.search(r'(?:risk|uncertain|may affect|could (?:harm|negatively|adversely))', sentence, re.IGNORECASE):
                    continue
                context_snippet = sentence[:200]
                break
            if context_snippet:
                break
        # Last resort: any sentence with revenue
        if not context_snippet:
            for ctx in contexts:
                m = re.search(r'(?:[A-Z](?:[^.]|\$\d+\.\d+)*revenue(?:[^.]|\$\d+\.\d+)*\.)', ctx, re.IGNORECASE)
                if m:
                    context_snippet = m.group().strip()[:200]
                    break

    return SolliaRealityMetrics(
        revenue_direction=direction,
        revenue_magnitude=round(magnitude, 2),
        raw_score=round(raw_score, 4),
        context_snippet=context_snippet,
    )


def _severity_label(sigma: float) -> Tuple[str, str]:
    a = abs(sigma)
    if a >= 2.0:
        return "RED", "#EF4444"
    if a >= 1.0:
        return "AMBER", "#F59E0B"
    return "GREEN", "#22C55E"


def _extract_revenue_context(mda_text: str, skip: int = 0) -> str:
    """Extract a meaningful revenue sentence from MD&A, skipping risk boilerplate.
    Use skip=N to get the Nth match (for generating multiple signals from same 10-K)."""
    if not mda_text:
        return ""
    matches = []
    for m in re.finditer(r'(?:[A-Z](?:[^.]|\$\d+\.\d+)*revenue(?:[^.]|\$\d+\.\d+)*\.)', mda_text, re.IGNORECASE):
        sentence = m.group().strip()
        if len(sentence) < 30:
            continue
        if re.search(r'(?:risk|uncertain|may affect|could (?:harm|negatively|adversely))', sentence, re.IGNORECASE):
            continue
        matches.append(sentence[:200])
    if skip < len(matches):
        return matches[skip]
    # Fallback: use any sentence from MD&A at different positions
    sentences = re.findall(r'[A-Z][^.]*\.', mda_text)
    idx = min(skip, len(sentences) - 1) if sentences else 0
    return sentences[idx][:200] if sentences else mda_text[:200]

def _extract_transcript_context(text: str) -> str:
    """Extract first meaningful CEO quote from a press release or transcript."""
    if not text:
        return ""
    import re as _re
    # Try to find CEO attribution and extract the preceding quote
    ceo_patterns = [
        r'said\s+Dr\.\s+([A-Z][a-z]+\s+[A-Z][a-z]+),\s*(?:AMD\s+)?(?:chair\s+and\s+)?CEO',
        r'said\s+([A-Z][a-z]+\s+[A-Z][a-z]+),\s*(?:AMD\s+)?(?:executive\s+)?(?:vice\s+president|CEO|CFO|president|chief|treasurer)',
    ]
    for pat in ceo_patterns:
        m = _re.search(pat, text, _re.IGNORECASE)
        if m:
            # Find the quote before the attribution
            before = text[max(0, m.start()-600):m.start()]
            # Try quoted text first
            qm = list(_re.finditer(r'["“]([^"”]{20,300})["”]', before))
            if qm:
                # Return the quote WITH the opening curly quote preserved
                m_last = qm[-1]
                full_quote = before[m_last.start():m_last.end()]
                return full_quote.strip()[:200]
            # Try last sentence before attribution
            sentences = _re.split(r'(?<=[.!?])\s+', before)
            if sentences:
                return sentences[-1].strip()[:200]
    # Fallback: return first substantial text after headers
    # Strip SEC header boilerplate
    clean = _re.sub(r'^EX-\d+[\.\d]*\s+\d+\s+[\w-]+\.(?:htm|pdf)\s+EX-\d+[\.\d]*\s+(?:Document\s+)?', '', text.strip())
    # Skip past press release header fields (Contacts, phone numbers, etc.)
    lines = clean.split('\n')
    for line in lines:
        line = line.strip()
        if len(line) > 60 and not _re.match(r'^[\d\s\-()\.@A-Z]+$', line):
            return line[:200]
    return clean[:200]

def _first5(text: str) -> str:
    # Strip SEC exhibit header boilerplate before taking first words
    import re as _re
    clean = _re.sub(r'^EX-\d+[\.\d]*\s+\d+\s+[\w-]+\.(?:htm|pdf)\s+EX-\d+[\.\d]*\s+(?:Document\s+)?', '', text.strip())
    clean = _re.sub(r'^[A-Z]+\d+[A-Z]*\s+[A-Z]+\s*', '', clean.strip())
    # Strip leading digits/whitespace but preserve leading curly quotes
    clean = _re.sub(r'^[\d\s_]+', '', clean.strip())  # strip leading digits/space, keep punctuation quotes
    if not clean or len(clean) < 5:
        clean = text.strip()
    w = clean.split()[:5]
    return " ".join(w) + ("..." if len(clean.split()) > 5 else "")


def _check_contradictions(transcript: str, mda: str) -> Tuple[bool, str]:
    t = transcript.lower()
    m = mda.lower()
    for pos_phrase, neg_pat in CONTRADICTION_PAIRS:
        if pos_phrase in t and re.search(neg_pat, m, re.IGNORECASE):
            return True, f"Transcript mentions '{pos_phrase}' but 10-K shows the opposite"
    return False, ""


class ForensicAccountant:
    """Agent 1 — detects gaps between CEO transcript tone and 10-K financial reality."""

    def __init__(self) -> None:
        from src.providers.filings.sec_edgar import SECEdgarProvider
        from src.providers.transcripts.fmp import FMPTranscriptProvider
        self._edgar = SECEdgarProvider()
        self._fmp = FMPTranscriptProvider()
        self._log: List[AgentLogMessage] = []
        self._reality_cache: Dict[str, SolliaRealityMetrics] = {}

    def run(self, ticker: str) -> SolliaForensicResult:
        self._log = []
        self._reality_cache = {}
        ticker = ticker.upper()
        result = SolliaForensicResult(ticker=ticker)

        _add_log(self._log, f"Starting Forensic Accountant for {ticker}")

        company_name = self._edgar.get_company_name(ticker)
        cik = self._edgar.get_cik(ticker)
        if not cik:
            _add_log(self._log, f"CIK not found for {ticker}")
            result.conclusion = f"No SEC filings found for {ticker}."
            result.log_messages = self._log
            return result
        _add_log(self._log, f"Company: {company_name} (CIK {cik})")

        _add_log(self._log, "Fetching 10-K filings from SEC EDGAR...")
        filings_info = self._edgar.get_10k_filings(ticker)
        if not filings_info:
            _add_log(self._log, "No 10-K filings found.")
            result.conclusion = f"No 10-K filings for {ticker}."
            result.log_messages = self._log
            return result
        _add_log(self._log, f"Found {len(filings_info)} 10-K filings.")

        _add_log(self._log, "Fetching filing content...")
        filings: List[FilingResult] = self._fetch_filings_parallel(ticker, filings_info[:12])
        if not filings:
            result.conclusion = "Could not fetch 10-K content."
            result.log_messages = self._log
            return result

        most_recent = filings[0]
        _add_log(self._log, f"Most recent 10-K: filed {most_recent.filing_date}, period {most_recent.period_of_report}")

        current_ceo = most_recent.ceo_name or next(
            (f.ceo_name for f in filings[1:] if f.ceo_name), ""
        )
        _add_log(self._log, f"Current CEO: {current_ceo or 'Unknown'}")

        ceo_start = ""
        if current_ceo:
            for f in reversed(filings):
                if f.ceo_name and f.ceo_name.lower() == current_ceo.lower():
                    ceo_start = f.filing_date
        _add_log(self._log, f"CEO era start: {ceo_start or 'unknown'}")
        result.ceo_name = current_ceo
        result.ceo_start_date = ceo_start

        hist_end = most_recent.filing_date
        inception_date = filings[0].filing_date if filings else ""

        _add_log(self._log, "Fetching earnings call transcripts...")
        transcripts = self._fmp.get_transcripts(ticker)
        _add_log(self._log, f"Got {len(transcripts)} transcripts")

        pre_filing_transcripts = sorted(
            [t for t in transcripts if t.date and t.date < hist_end],
            key=lambda t: t.date,
            reverse=True,
        )
        seen_periods: set = set()
        deduped_pre: List[TranscriptResult] = []
        for t in pre_filing_transcripts:
            key = t.fiscal_period or t.date[:7]
            if key not in seen_periods:
                seen_periods.add(key)
                deduped_pre.append(t)
        hist_transcripts = deduped_pre[:12]

        twelfth_date = hist_transcripts[-1].date if hist_transcripts else ""

        candidates = [d for d in [ceo_start, inception_date, twelfth_date] if d]
        hist_start = min(candidates) if candidates else ""
        # Safety: cap at 5 years
        if hist_start:
            try:
                d_start = datetime.strptime(hist_start[:10], "%Y-%m-%d")
                d_end = datetime.strptime(hist_end[:10], "%Y-%m-%d")
                if (d_end - d_start).days > 5 * 365:
                    yr = int(hist_end[:4]) - 3
                    hist_start = f"{yr}{hist_end[4:]}"
            except Exception:
                pass
        if not hist_start:
            try:
                yr = int(hist_end[:4]) - 3
                hist_start = str(yr) + hist_end[4:]
            except Exception:
                hist_start = hist_end

        result.historical_start = hist_start
        result.historical_end = hist_end
        _add_log(self._log, f"Historical period: {hist_start} to {hist_end}")

        _add_log(self._log, "Building quarterly transcript-to-10K pairs...")
        hist_pairs = self._build_pairs(hist_transcripts, filings, hist_start, hist_end)
        _add_log(self._log, f"Built {len(hist_pairs)} historical pairs")

        if len(hist_pairs) >= 2:
            gaps = [p.gap for p in hist_pairs]
            base_mean = statistics.mean(gaps)
            base_std = statistics.stdev(gaps) if len(gaps) > 1 else 1.0
        else:
            base_mean = 0.0
            base_std = 1.0
            _add_log(self._log, "Insufficient historical data — using default baseline")

        result.baseline_mean = round(base_mean, 4)
        result.baseline_stddev = round(max(base_std, 0.01), 4)
        _add_log(self._log, f"Baseline: mean={base_mean:.4f}, stddev={base_std:.4f}")

        for i, pair in enumerate(hist_pairs):
            if i < 2:
                pair.rolling_sigma = 0.0
                continue
            prev = [hist_pairs[j].gap for j in range(i)]
            pm = statistics.mean(prev)
            ps = statistics.stdev(prev) if len(prev) > 1 else 1.0
            pair.rolling_sigma = round((pair.gap - pm) / max(ps, 0.01), 4)
        result.quarterly_pairs = hist_pairs

        _add_log(self._log, "Analyzing current period (test sample)...")
        cur_t = self._best_current_transcript(transcripts, most_recent)
        if cur_t:
            _add_log(self._log, f"Using transcript: {cur_t.fiscal_period} ({cur_t.date})")
            cur_tone = _extract_tone(cur_t.content, use_ollama=True)
        else:
            _add_log(self._log, "No transcript found — using MD&A as management tone proxy")
            result.fallback_used = True
            result.fallback_detail = "Transcript unavailable — using 10-K MD&A as proxy."
            cur_tone = _extract_tone(most_recent.mda_text, use_ollama=False)

        cur_reality = self._cached_reality(most_recent)
        cur_gap = cur_reality.raw_score - cur_tone.raw_score  # + = reality better than CEO tone = bullish
        _add_log(self._log, f"Tone={cur_tone.raw_score:.4f}, Reality={cur_reality.raw_score:.4f}, Gap={cur_gap:.4f}")
        result.current_gap = round(cur_gap, 4)

        sigma1 = (cur_gap - base_mean) / max(result.baseline_stddev, 0.01)
        result.sigma_1 = round(sigma1, 4)
        s1 = 50.0 + 30.0 * math.tanh(sigma1 / 2.0)  # + = bullish (positive gap = CEO underpromising)
        result.s1 = round(max(20.0, min(80.0, s1)), 2)
        _add_log(self._log, f"Discrepancy analysis: CEO language vs 10-K reality gap is {abs(sigma1):.1f} standard deviations from historical norm — {'notable divergence' if abs(sigma1) > 1.5 else 'moderate divergence' if abs(sigma1) > 0.7 else 'within normal range'}")

        _add_log(self._log, "Checking for direct contradictions...")
        tc_text = cur_t.content if cur_t else ""
        contradiction, contra_detail = _check_contradictions(tc_text, most_recent.mda_text)
        result.direct_contradiction = contradiction
        result.contradiction_detail = contra_detail
        if contradiction:
            _add_log(self._log, f"CONTRADICTION FOUND: {contra_detail}")
            result.s1 = 20.0

        _add_log(self._log, "Ranking top discrepancy signals...")
        cur_pair = SolliaQuarterlyPair(
            fiscal_period=cur_t.fiscal_period if cur_t else most_recent.period_of_report,
            transcript_date=cur_t.date if cur_t else "",
            filing_date=most_recent.filing_date,
            tone=cur_tone,
            reality=cur_reality,
            gap=round(cur_gap, 4),
            rolling_sigma=round(sigma1, 4),
        )
        # Top 5 discrepancy signals: ONLY pairs with the most recent 10-K
        # Rank by absolute rolling_sigma (divergence is a spectrum)
        sample_pairs = [cur_pair]
        for hp in hist_pairs:
            if hp.filing_date == most_recent.filing_date:
                sample_pairs.append(hp)
        # Remove duplicates
        seen = set()
        unique_pairs = []
        for p in sample_pairs:
            key = (p.fiscal_period, p.filing_date)
            if key not in seen:
                seen.add(key)
                unique_pairs.append(p)
        ranked = sorted(unique_pairs, key=lambda p: abs(p.rolling_sigma), reverse=True)
        # Fill to 5 by duplicating top pair (different MD&A quotes via skip parameter)
        while len(ranked) < 5:
            ranked.append(ranked[0])

        signals: List[SolliaDiscrepancySignal] = []
        for rank, pair in enumerate(ranked, 1):
            sev, col = _severity_label(pair.rolling_sigma)
            match_t = next((t for t in transcripts if t.fiscal_period == pair.fiscal_period), None)
            match_f = next((f for f in filings if f.filing_date == pair.filing_date), filings[0] if filings else None)
            if match_t and match_t.content:
                speaker = match_t.speaker_sections[0].get("speaker", "CEO") if match_t.speaker_sections else "CEO"
                t_cite = f"{pair.fiscal_period}, {speaker}: \"{_first5(_extract_transcript_context(match_t.content))}\""
            else:
                t_cite = f"{pair.fiscal_period}: (no transcript)"
            if match_f:
                clean_mda = re.sub(r'^[\d\s\W_]+', '', match_f.mda_text.strip())
                # Extract first meaningful revenue sentence from MD&A for citation
                ctx_for_cite = _extract_revenue_context(match_f.mda_text, skip=rank-1) if match_f.mda_text else clean_mda
                f_cite = f"{match_f.filing_date}, MD&A: \"{_first5(ctx_for_cite)}\""
                htm = match_f.htm_url
                # Get transcript URL if available
                t_url = htm  # fallback
                if match_t and hasattr(match_t, 'source_url'):
                    t_url = match_t.source_url
            else:
                f_cite = ""
                htm = ""
            # After flip: positive gap = reality > tone = CEO underpromised (bullish)
            # negative gap = CEO tone > reality = overpromised (bearish)
            direction = "underpromised (bullish)" if pair.gap > 0 else "overpromised (bearish)"
            signals.append(SolliaDiscrepancySignal(
                rank=rank,
                fiscal_period=pair.fiscal_period,
                severity=sev,
                severity_color=col,
                description=f"CEO {direction} vs 10-K (gap {pair.gap:+.3f})",
                transcript_citation=t_cite,
                filing_citation=f_cite,
                htm_link=htm,
                transcript_url=t_url,
            ))
        result.top_signals = signals

        result.discrepancy_metric = round(cur_gap, 4)
        if cur_gap > 0.3:  # reality significantly better than CEO tone = bullish
            result.discrepancy_label = f"+{cur_gap:.2f} [bullish | CEO notably underpromising vs 10-K | scale −2 to +2]"
        elif cur_gap > 0.1:  # reality somewhat better than CEO tone = mildly bullish
            result.discrepancy_label = f"+{cur_gap:.2f} [mildly bullish | CEO somewhat underpromising vs 10-K | scale −2 to +2]"
        elif cur_gap < -0.3:  # CEO significantly overpromised vs reality = bearish
            result.discrepancy_label = f"{cur_gap:.2f} [bearish | CEO notably overpromising vs 10-K | scale −2 to +2]"
        elif cur_gap < -0.1:  # CEO somewhat overpromised vs reality = mildly bearish
            result.discrepancy_label = f"{cur_gap:.2f} [mildly bearish | CEO slightly overpromising vs 10-K | scale −2 to +2]"
        else:
            result.discrepancy_label = f"{cur_gap:+.2f} [neutral | tone aligned with 10-K results | scale −2 to +2]"

        if contradiction:
            result.conclusion = f"A direct contradiction was found between the CEO's statements and the 10-K filing."
        elif sigma1 > 1.5:
            result.conclusion = f"The CEO is significantly overpromising relative to the 10-K results, a bearish signal for {ticker}."
        elif sigma1 < -1.5:
            result.conclusion = f"The CEO is underpromising relative to the 10-K results, a mildly bullish signal for {ticker}."
        else:
            result.conclusion = f"The gap between transcript tone and 10-K financials is within historical norms for {ticker}."

        result.log_messages = self._log
        _add_log(self._log, f"Forensic Accountant complete. S1={result.s1:.2f}")
        return result

    def _fetch_filings_parallel(self, ticker: str, filings_info: List[dict]) -> List[FilingResult]:
        results: Dict[int, FilingResult] = {}
        def _fetch(idx: int, info: dict) -> Tuple[int, FilingResult]:
            time.sleep(idx * 0.15)
            return idx, self._edgar.get_filing_result(ticker, info)
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(_fetch, i, info): i for i, info in enumerate(filings_info)}
            for future in as_completed(futures):
                try:
                    idx, fr = future.result()
                    results[idx] = fr
                except Exception as exc:
                    logger.debug("Filing fetch failed: %s", exc)
        return [results[i] for i in sorted(results.keys())]

    def _cached_reality(self, filing: FilingResult) -> SolliaRealityMetrics:
        key = filing.filing_date or filing.accession_number
        if key not in self._reality_cache:
            self._reality_cache[key] = _extract_reality(filing.mda_text)
        return self._reality_cache[key]

    def _build_pairs(self, hist_transcripts: List[TranscriptResult], filings: List[FilingResult], hist_start: str, hist_end: str) -> List[SolliaQuarterlyPair]:
        hist_filings = filings[1:] if len(filings) > 1 else filings
        pairs: List[SolliaQuarterlyPair] = []
        for t in hist_transcripts:
            t_date = t.fiscal_period_end or t.date
            if not t_date:
                continue
            if t_date < hist_start or t_date >= hist_end:
                continue
            best_f = None
            best_delta = float("inf")
            for f in hist_filings:
                if not f.period_of_report or f.period_of_report > t_date:
                    continue
                try:
                    d1 = datetime.strptime(t_date[:10], "%Y-%m-%d")
                    d2 = datetime.strptime(f.period_of_report[:10], "%Y-%m-%d")
                    delta = abs((d1 - d2).days)
                    if delta < best_delta:
                        best_delta = delta
                        best_f = f
                except Exception:
                    pass
            if best_f is None:
                continue
            tone = _extract_tone(t.content, use_ollama=False)
            reality = self._cached_reality(best_f)
            gap = reality.raw_score - tone.raw_score  # + = reality better than CEO tone = bullish
            pairs.append(SolliaQuarterlyPair(
                fiscal_period=t.fiscal_period,
                transcript_date=t.date,
                filing_date=best_f.filing_date,
                tone=tone,
                reality=reality,
                gap=round(gap, 4),
                rolling_sigma=0.0,
            ))
        pairs.sort(key=lambda p: p.transcript_date)
        return pairs

    def _best_current_transcript(self, transcripts: List[TranscriptResult], most_recent_filing: FilingResult) -> Optional[TranscriptResult]:
        if not transcripts:
            return None
        by_date = sorted(transcripts, key=lambda t: t.date, reverse=True)
        return by_date[0]
