"""News provider — Google News RSS → Finnhub → Yahoo Finance RSS."""
from __future__ import annotations

import re
import time
import logging
from datetime import datetime, timedelta
from typing import List, Tuple

import requests

from src.schemas import Headline

logger = logging.getLogger(__name__)

FINNHUB_BASE = "https://finnhub.io/api/v1"


class FinnhubNewsProvider:
    """
    Fetches financial news headlines.
    Chain: Google News RSS → Finnhub (12-month chunks, 0.3s delay) → Yahoo Finance RSS
    """

    def __init__(self) -> None:
        from config.settings import settings
        self._fh = settings.finnhub_api_key

    def get_news(
        self, ticker: str, start_date: str
    ) -> Tuple[List[Headline], List[str]]:
        all_h: List[Headline] = []
        providers: List[str] = []

        # Source 1: Google News RSS — primary, 101 articles, all sizes
        gn = self._google_news_rss(ticker)
        if gn:
            all_h.extend(gn)
            providers.append("GoogleNews")

        # Source 2: Finnhub — 12-month chunks, 0.3s delay, max 2 retries on 429
        fh = self._finnhub_chunked(ticker, start_date)
        if fh:
            all_h.extend(fh)
            providers.append("Finnhub")

        # Source 3: Yahoo Finance RSS — fallback if still empty
        if not all_h:
            yr = self._yahoo_rss(ticker)
            if yr:
                all_h.extend(yr)
                providers.append("YahooRSS")

        # Deduplicate by URL (fall back to headline as key)
        seen: set = set()
        deduped: List[Headline] = []
        for h in all_h:
            key = h.url or h.headline
            if key not in seen:
                seen.add(key)
                deduped.append(h)

        return deduped, providers

    # ── Google News RSS ───────────────────────────────────────────────────────

    def _google_news_rss(self, ticker: str) -> List[Headline]:
        """Fetch from Google News RSS: news.google.com/rss/search?q={TICKER}+stock"""
        try:
            url = f"https://news.google.com/rss/search?q={ticker}+stock"
            resp = requests.get(url, timeout=15)
            if resp.status_code != 200:
                return []
            items = re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)
            out: List[Headline] = []
            for item in items[:200]:
                title_m = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
                link_m = re.search(r"<link>(.*?)</link>", item, re.DOTALL)
                date_m = re.search(r"<pubDate>(.*?)</pubDate>", item, re.DOTALL)
                source_m = re.search(r"<source[^>]*>(.*?)</source>", item, re.DOTALL)
                if not title_m:
                    continue
                headline = re.sub(r"<!\[CDATA\[|\]\]>", "", title_m.group(1)).strip()
                link = (link_m.group(1) if link_m else "").strip()
                source = re.sub(r"<!\[CDATA\[|\]\]>", "", source_m.group(1) if source_m else "").strip()
                date_str = _parse_rfc_date(date_m.group(1) if date_m else "")
                out.append(Headline(headline=headline, url=link, source=source, date=date_str))
            return out
        except Exception as exc:
            logger.debug("Google News RSS failed: %s", exc)
            return []

    # ── Finnhub ───────────────────────────────────────────────────────────────

    def _finnhub_chunked(self, ticker: str, start_date: str) -> List[Headline]:
        try:
            start = datetime.strptime(start_date[:10], "%Y-%m-%d")
        except Exception:
            start = datetime.now() - timedelta(days=365 * 3)

        end = datetime.now()
        results: List[Headline] = []
        chunk_start = start

        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=365), end)
            chunk = self._finnhub_chunk(
                ticker,
                chunk_start.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
            )
            results.extend(chunk)
            chunk_start = chunk_end + timedelta(days=1)
            time.sleep(0.3)  # per spec: 0.3s delay between chunks

        return results

    def _finnhub_chunk(
        self, ticker: str, from_d: str, to_d: str, retries: int = 2
    ) -> List[Headline]:
        params = {
            "symbol": ticker,
            "from": from_d,
            "to": to_d,
            "token": self._fh,
        }
        for attempt in range(retries + 1):
            try:
                resp = requests.get(
                    f"{FINNHUB_BASE}/company-news",
                    params=params,
                    timeout=20,
                )
                if resp.status_code == 429:
                    if attempt < retries:
                        time.sleep(1.0)
                        continue
                    return []
                if resp.status_code == 200:
                    return self._parse_finnhub(resp.json())
            except Exception as exc:
                logger.debug("Finnhub chunk failed: %s", exc)
                if attempt < retries:
                    time.sleep(0.5)
        return []

    def _parse_finnhub(self, data: object) -> List[Headline]:
        if not isinstance(data, list):
            return []
        out: List[Headline] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            headline = item.get("headline", "") or item.get("summary", "")
            if not headline:
                continue
            url = item.get("url", "")
            source = item.get("source", "")
            ts = item.get("datetime", 0)
            date_str = ""
            if ts:
                try:
                    date_str = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                except Exception:
                    pass
            out.append(Headline(headline=headline, url=url, source=source, date=date_str))
        return out

    # ── Yahoo Finance RSS ─────────────────────────────────────────────────────

    def _yahoo_rss(self, ticker: str) -> List[Headline]:
        try:
            url = (
                f"https://feeds.finance.yahoo.com/rss/2.0/headline"
                f"?s={ticker}&region=US&lang=en-US"
            )
            resp = requests.get(url, timeout=15)
            if resp.status_code != 200:
                return []
            items = re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)
            out: List[Headline] = []
            for item in items[:60]:
                tm = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
                lm = re.search(r"<link>(.*?)</link>", item, re.DOTALL)
                dm = re.search(r"<pubDate>(.*?)</pubDate>", item, re.DOTALL)
                if not tm:
                    continue
                headline = re.sub(r"<!\[CDATA\[|\]\]>", "", tm.group(1)).strip()
                link = lm.group(1).strip() if lm else ""
                date_str = _parse_rfc_date(dm.group(1) if dm else "")
                out.append(Headline(
                    headline=headline,
                    url=link,
                    source="Yahoo Finance",
                    date=date_str,
                ))
            return out
        except Exception as exc:
            logger.debug("Yahoo RSS failed: %s", exc)
            return []


def _parse_rfc_date(raw: str) -> str:
    """Parse RFC-2822 date like 'Mon, 29 Apr 2026 12:00:00 +0000' → '2026-04-29'."""
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    # Fallback: parse first 16 chars
    try:
        dt = datetime.strptime(raw[:16], "%a, %d %b %Y")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""
