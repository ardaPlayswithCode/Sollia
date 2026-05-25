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

        # Source 1: Google News RSS — keyword rotation for historical coverage
        gn = self._google_news_rss(ticker)
        if gn:
            all_h.extend(gn)
            providers.append("GoogleNews")

        # Source 2: Finnhub — 12-month chunks, 0.3s delay, max 2 retries on 429
        fh = self._finnhub_chunked(ticker, start_date)
        if fh:
            all_h.extend(fh)
            providers.append("Finnhub")

        # Source 3: Yahoo Finance RSS (with retry on 429)
        yr = self._yahoo_rss(ticker)
        if yr:
            all_h.extend(yr)
            providers.append("YahooRSS")

        # Source 4: Yahoo Finance News API v8 (search endpoint)
        if not all_h or len(all_h) < 50:
            yf_api = self._yahoo_api_v8(ticker)
            if yf_api:
                all_h.extend(yf_api)
                providers.append("YahooAPIv8")

        # Deduplicate by URL (fall back to headline as key)
        seen: set = set()
        deduped: List[Headline] = []
        for h in all_h:
            key = h.url or h.headline
            if key not in seen:
                seen.add(key)
                deduped.append(h)

        return deduped, providers

    # ── GDELT Project ─────────────────────────────────────────────

    def _gdelt_news(self, ticker: str, start_date: str) -> List[Headline]:
        """Fetch historical news from GDELT Project (free, 2015-present)."""
        try:
            from src.providers.news.gdelt import GDELTProvider
            provider = GDELTProvider()
            headlines, _ = provider.get_news(ticker, start_date)
            return headlines
        except Exception as e:
            logger.debug("GDELT fetch failed: %s", e)
            return []

    # ── Google News RSS ───────────────────────────────────────────────────────

    # Financial keywords for rotating queries to get historical coverage
    _NEWS_KEYWORDS = [
        "earnings", "revenue", "data+center", "gaming", "quarterly+results",
        "acquisition", "guidance", "dividend", "AI+chip", "semiconductor",
        "GPU", "partnership", "margin", "growth", "investor",
        "product+launch", "buyback", "forecast", "CEO", "stock+price",
    ]

    def _google_news_rss(self, ticker: str) -> List[Headline]:
        """Fetch from Google News RSS with keyword rotation for historical coverage."""
        all_items: List[Headline] = []
        seen_urls: set = set()

        # Use top 10 keywords for good coverage without too many requests
        for kw in self._NEWS_KEYWORDS[:10]:
            try:
                url = f"https://news.google.com/rss/search?q={ticker}+{kw}&hl=en-US&gl=US&ceid=US:en"
                resp = requests.get(url, timeout=15)
                if resp.status_code != 200:
                    continue
                items = re.findall(r"<item>(.*?)</item>", resp.text, re.DOTALL)
                for item in items[:100]:  # 100 per keyword
                    title_m = re.search(r"<title>(.*?)</title>", item, re.DOTALL)
                    link_m = re.search(r"<link>(.*?)</link>", item, re.DOTALL)
                    date_m = re.search(r"<pubDate>(.*?)</pubDate>", item, re.DOTALL)
                    source_m = re.search(r"<source[^>]*>(.*?)</source>", item, re.DOTALL)
                    if not title_m or not link_m:
                        continue
                    link = link_m.group(1).strip()
                    if link in seen_urls:
                        continue
                    seen_urls.add(link)
                    headline = re.sub(r"<!\[CDATA\[|\]\]>", "", title_m.group(1)).strip()
                    source = re.sub(r"<!\[CDATA\[|\]\]>", "", source_m.group(1) if source_m else "").strip()
                    date_str = _parse_rfc_date(date_m.group(1) if date_m else "")
                    all_items.append(Headline(
                        headline=headline, url=link, source=source, date=date_str
                    ))
                time.sleep(0.5)  # Rate limit between queries
            except Exception as exc:
                logger.debug("Google News RSS keyword '%s' failed: %s", kw, exc)
                continue

        return all_items

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

    # ── Yahoo Finance News API v8 ──────────────────────────────────────────

    def _yahoo_api_v8(self, ticker: str) -> List[Headline]:
        """Fetch from Yahoo Finance search/news API v8."""
        try:
            url = f"https://query1.finance.yahoo.com/v1/finance/search?q={ticker}&newsCount=100"
            resp = requests.get(url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            })
            if resp.status_code != 200:
                return []
            data = resp.json()
            news_items = data.get("news", [])
            out: List[Headline] = []
            for item in news_items:
                title = item.get("title", "")
                if not title:
                    continue
                link = item.get("link", "")
                source = item.get("publisher", "Yahoo Finance")
                ts = item.get("providerPublishTime", 0)
                date_str = ""
                if ts:
                    try:
                        date_str = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                    except Exception:
                        pass
                out.append(Headline(
                    headline=title,
                    url=link,
                    source=str(source),
                    date=date_str,
                ))
            return out
        except Exception as exc:
            logger.debug("Yahoo API v8 failed: %s", exc)
            return []

    # ── Yahoo Finance RSS ─────────────────────────────────────────────────────

    def _yahoo_rss(self, ticker: str) -> List[Headline]:
        url = (
            f"https://feeds.finance.yahoo.com/rss/2.0/headline"
            f"?s={ticker}&region=US&lang=en-US"
        )
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=15)
                if resp.status_code == 429:
                    time.sleep(2.0 * (attempt + 1))
                    continue
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
                logger.debug("Yahoo RSS attempt %d failed: %s", attempt + 1, exc)
                time.sleep(1.0)
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
