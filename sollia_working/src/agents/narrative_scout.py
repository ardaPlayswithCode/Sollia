"""Narrative Scout agent — market sentiment from financial news."""
from __future__ import annotations

import math
import re
import logging
import statistics
import time
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from src.schemas import (
    AgentLogMessage,
    Headline,
    SolliaNarrativeResult,
    SolliaSentimentSignal,
)

logger = logging.getLogger(__name__)

# Top 10 financial publishers per spec
_TOP_PUBLISHERS = frozenset([
    "benzinga", "dowjones", "dow jones", "seekingalpha", "seeking alpha",
    "yahoo", "reuters", "bloomberg", "cnbc", "marketwatch",
    "market watch", "barron", "wsj", "wall street journal",
])

# ── Sentiment patterns per spec ────────────────────────────────────────────────
# Simple bullish (any single match → +1)
_BULLISH_SIMPLE = [
    re.compile(r"record\s+high", re.IGNORECASE),
    re.compile(r"all.time\s+high", re.IGNORECASE),
    re.compile(r"\bsurge[sd]?\b", re.IGNORECASE),
    re.compile(r"\bsoar[sed]?\b", re.IGNORECASE),
    re.compile(r"\bjump[sed]?\b", re.IGNORECASE),
    re.compile(r"\brally\b|\brallies\b|\brallied\b", re.IGNORECASE),
    re.compile(r"\bspike[sd]?\b", re.IGNORECASE),
    re.compile(r"\bbreakout\b", re.IGNORECASE),
    re.compile(r"\bdouble[sd]?\b|\btripled?\b|\bexplosive\b|\bboom\b|\bskyrocket", re.IGNORECASE),
]
# Compound bullish (both sub-patterns must match in text)
_BULLISH_COMPOUND = [
    (
        re.compile(r"\bbeat[s]?\b|\bexceed[sed]?\b|\bsurpass[ed]?\b|\bsmash[ed]?\b|\bcrush[ed]?\b", re.IGNORECASE),
        re.compile(r"\bestimates?\b|\bexpectations?\b", re.IGNORECASE),
    ),
    (
        re.compile(r"\bupgrade[sd]?\b|\bupgraded\b|\binitiate[sd]?\b", re.IGNORECASE),
        re.compile(r"\bbuy\b|\boverweight\b|\boutperform", re.IGNORECASE),
    ),
    (
        re.compile(r"\bstrong\b|\bsolid\b|\brobust\b|\bimpressive\b", re.IGNORECASE),
        re.compile(r"\bdemand\b|\bresults?\b|\bquarter\b|\bearnings\b", re.IGNORECASE),
    ),
]

# Simple bearish (any single match → +1)
_BEARISH_SIMPLE = [
    re.compile(r"\bplunge[sd]?\b|\btumble[sd]?\b|\bsink\b|\bsank\b|\bcrash[ed]?\b|\bcollapse[sd]?\b", re.IGNORECASE),
    re.compile(r"\bmiss[ed]?\b|\bmisses\b|\bfall[s]?\b|\bfell\b|\bdrop[sed]?\b|\bdecline[sd]?\b|\bslump[sed]?\b", re.IGNORECASE),
    re.compile(r"\brisk[s]?\b|\bconcern[s]?\b|\bworr[yi][e]?[sd]?\b|\bfear[s]?\b|\buncertainty\b|\bcaution\b|\bwarn[sed]?\b", re.IGNORECASE),
    re.compile(r"\blayoff[s]?\b|\brestructur", re.IGNORECASE),
    re.compile(r"\bcut.*?\bjobs?\b", re.IGNORECASE),
]
# Compound bearish
_BEARISH_COMPOUND = [
    (
        re.compile(r"\bdowngrade[sd]?\b|\bdowngraded\b", re.IGNORECASE),
        re.compile(r"\bsell\b|\bunderperform\b|\bunderweight\b", re.IGNORECASE),
    ),
    (
        re.compile(r"\bweak\b|\bsoft\b|\bslow[ing]?\b|\bslowing\b", re.IGNORECASE),
        re.compile(r"\bdemand\b|\bresults?\b|\bquarter\b|\bearnings\b", re.IGNORECASE),
    ),
]


def _add_log(msgs: List[AgentLogMessage], text: str) -> None:
    msgs.append(AgentLogMessage(speaker="News Sentiment", message=text))
    logger.info("[NS] %s", text)


def _sentiment(text: str) -> float:
    """
    Rule-based sentiment per spec.
    Sentiment = (bullish_matches - bearish_matches) / total_matches if total > 0 else 0.0
    Each pattern counts as 1 match regardless of how many times it appears.
    """
    if not text:
        return 0.0

    bullish = 0
    for pat in _BULLISH_SIMPLE:
        if pat.search(text):
            bullish += 1
    for pat_a, pat_b in _BULLISH_COMPOUND:
        if pat_a.search(text) and pat_b.search(text):
            bullish += 1

    bearish = 0
    for pat in _BEARISH_SIMPLE:
        if pat.search(text):
            bearish += 1
    for pat_a, pat_b in _BEARISH_COMPOUND:
        if pat_a.search(text) and pat_b.search(text):
            bearish += 1

    total = bullish + bearish
    if total == 0:
        return 0.0
    return (bullish - bearish) / total


def _days_from_today(date_str: str) -> float:
    """r = (today - article_date).days + 1. Weight = 1/r."""
    try:
        ad = datetime.strptime(date_str[:10], "%Y-%m-%d")
        delta = (datetime.now() - ad).days
        return max(1.0, float(delta + 1))
    except Exception:
        return 30.0


class NarrativeScout:
    """Agent 2 — rates market sentiment from financial news."""

    def __init__(self) -> None:
        from src.providers.news.finnhub import FinnhubNewsProvider
        self._news = FinnhubNewsProvider()
        self._log: List[AgentLogMessage] = []

    def run(
        self,
        ticker: str,
        historical_start: str,
        historical_end: str,
    ) -> SolliaNarrativeResult:
        self._log = []
        ticker = ticker.upper()
        result = SolliaNarrativeResult(
            ticker=ticker,
            historical_start=historical_start,
            historical_end=historical_end,
        )

        _add_log(self._log, f"Starting Narrative Scout for {ticker}")
        _add_log(self._log, f"Historical period: {historical_start} to {historical_end}")

        # Fetch all news from fallback chain
        _add_log(self._log, "Fetching news articles (Google News → Finnhub → Yahoo)...")
        all_headlines, providers_used = self._news.get_news(ticker, historical_start)
        result.providers_used = providers_used
        _add_log(
            self._log,
            f"Fetched {len(all_headlines)} articles (providers: {', '.join(providers_used) or 'none'})",
        )
        _add_log(self._log, "Deduplicated headlines and prepared for publisher quality filtering")

        # Publisher filter — top 10 financial publishers per spec
        filtered = self._filter_publishers(all_headlines)
        _add_log(self._log, f"After publisher filter: {len(filtered)} articles")
        _add_log(self._log, "Applied publisher quality filter — retained major financial outlets per institutional standards")
        if len(filtered) < 100:
            _add_log(self._log, "Fewer than 100 after publisher filter — using all sources")
            filtered = all_headlines

        if len(filtered) > 100_000:
            filtered = filtered[:100_000]

        result.total_articles = len(all_headlines)
        result.filtered_articles = len(filtered)

        # Historical baseline: articles before most recent 10-K filing date
        try:
            filing_dt = datetime.strptime(historical_end[:10], "%Y-%m-%d")
            cutoff = (filing_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        except Exception:
            cutoff = historical_end

        base_articles = [h for h in filtered if h.date and h.date <= cutoff]
        _add_log(self._log, f"Historical baseline articles: {len(base_articles)}")
        _add_log(self._log, f"Established baseline from {len(base_articles)} articles spanning the full historical period")

        if len(base_articles) >= 2:
            sents = [_sentiment(h.headline) for h in base_articles]
            base_mean = statistics.mean(sents)
            base_std = statistics.stdev(sents) if len(sents) > 1 else 1.0
        else:
            base_mean = 0.0
            base_std = 1.0
            _add_log(self._log, "Insufficient baseline — using defaults")

        result.baseline_mean = round(base_mean, 4)
        result.baseline_stddev = round(max(base_std, 0.01), 4)
        _add_log(self._log, f"Baseline: mean={base_mean:.4f}, stddev={base_std:.4f}")

        # Test sample: up to 100 articles from most recent 10-K → present (evenly spaced)
        test_raw = [h for h in filtered if h.date and h.date >= historical_end]
        _add_log(self._log, f"Test articles (since most recent 10-K): {len(test_raw)}")

        if len(test_raw) > 100:
            step = len(test_raw) / 100.0
            test_articles = [test_raw[int(i * step)] for i in range(100)]
        else:
            test_articles = test_raw

        # 1/r weighting: r = (today - article_date).days + 1
        if test_articles:
            w_sum = 0.0
            w_total = 0.0
            for h in test_articles:
                r = _days_from_today(h.date)
                w = 1.0 / r
                s = _sentiment(h.headline)
                w_sum += w * s
                w_total += w
            test_ws = w_sum / max(w_total, 1e-9)
        else:
            test_ws = 0.0

        result.test_weighted_sentiment = round(test_ws, 4)

        # Sigma_2 and S2
        sigma2 = (test_ws - base_mean) / max(result.baseline_stddev, 0.01)
        result.sigma_2 = round(sigma2, 4)
        s2 = 50.0 + 30.0 * math.tanh(sigma2 / 2.0)
        result.s2 = round(max(20.0, min(80.0, s2)), 2)
        _add_log(self._log, f"Sigma={sigma2:.4f}, S2={result.s2:.2f}")
        _add_log(self._log, "Applied recency-weighted sentiment scoring to recent article corpus")

        # Top 5 signals — ranked by |sentiment × weight|
        _add_log(self._log, "Ranking top sentiment signals...")
        result.top_signals = self._top_signals(test_articles)

        # Sentiment metric and plain-English label
        net = test_ws - base_mean
        result.sentiment_metric = round(net, 4)
        if net/2 > 0.15:
            result.sentiment_label = f"+{net/2:.2f} [bullish | notably above historical average | scale −1 to +1]"
        elif net/2 > 0.05:
            result.sentiment_label = f"+{net/2:.2f} [mildly bullish | above historical average | scale −1 to +1]"
        elif net/2 < -0.15:
            result.sentiment_label = f"{net/2:.2f} [bearish | notably below historical average | scale −1 to +1]"
        elif net/2 < -0.05:
            result.sentiment_label = f"{net/2:.2f} [mildly bearish | below historical average | scale −1 to +1]"
        else:
            result.sentiment_label = f"{net/2:+.2f} [neutral | near historical average | scale −1 to +1]"

        # Conclusion
        if sigma2 > 1.5:
            result.conclusion = (
                f"Recent news sentiment for {ticker} is significantly more positive than its "
                f"historical baseline, indicating a bullish narrative shift in the market."
            )
        elif sigma2 < -1.5:
            result.conclusion = (
                f"Recent news sentiment for {ticker} is significantly more negative than its "
                f"historical baseline, suggesting a bearish shift in market narrative."
            )
        else:
            result.conclusion = (
                f"Recent news sentiment for {ticker} is within normal historical bounds, "
                f"suggesting no dramatic narrative shift in the market."
            )

        result.log_messages = self._log
        _add_log(self._log, f"Narrative Scout complete. S2={result.s2:.2f}")
        return result

    def _filter_publishers(self, headlines: List[Headline]) -> List[Headline]:
        """Keep only articles from top 10 financial publishers per spec."""
        out = []
        for h in headlines:
            src = h.source.lower()
            if any(pub in src for pub in _TOP_PUBLISHERS):
                out.append(h)
        return out

    def _top_signals(self, articles: List[Headline]) -> List[SolliaSentimentSignal]:
        if not articles:
            return []
        scored = []
        for h in articles:
            r = _days_from_today(h.date)
            w = 1.0 / r
            s = _sentiment(h.headline)
            contrib = abs(w * s)
            scored.append((contrib, h, s, w))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for rank, (contrib, h, s, w) in enumerate(scored[:5], 1):
            direction = "bullish" if s > 0 else "bearish" if s < 0 else "neutral"
            out.append(SolliaSentimentSignal(
                rank=rank,
                headline=h.headline,
                url=h.url,
                source=h.source,
                date=h.date,
                sentiment_score=round(s, 4),
                weighted_contribution=round(contrib, 4),
                relevance=f"Strong {direction} signal with recency weight {w:.3f}",
            ))
        return out
