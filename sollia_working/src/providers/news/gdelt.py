"""GDELT Project news provider — free historical news back to 2015."""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import List, Tuple

import requests

from src.schemas import Headline

logger = logging.getLogger(__name__)

GDLET_API = "https://api.gdeltproject.org/api/v2/doc/doc"


class GDELTProvider:
    """Fetches historical financial news from GDELT Project (free, 2015–present)."""

    def __init__(self) -> None:
        self._session = requests.Session()

    def get_news(
        self, ticker: str, start_date: str
    ) -> Tuple[List[Headline], List[str]]:
        """
        Fetch news articles for a ticker from start_date to present.
        GDELT query format uses datetime strings like 20200101000000.
        """
        all_headlines: List[Headline] = []
        providers: List[str] = []

        # Convert start_date to GDELT format
        try:
            start_dt = datetime.strptime(start_date[:10], "%Y-%m-%d")
        except Exception:
            start_dt = datetime(2020, 1, 1)

        end_dt = datetime.now()

        # GDELT requires 5+ seconds between requests
        chunk_start = start_dt
        while chunk_start < end_dt:
            # Calculate 3-month chunk end
            month = chunk_start.month + 3
            year = chunk_start.year
            if month > 12:
                month -= 12
                year += 1
            chunk_end = min(
                datetime(year, month, 1),
                end_dt,
            )

            for attempt in range(2):
                try:
                    articles = self._fetch_chunk(ticker, chunk_start, chunk_end)
                    all_headlines.extend(articles)
                    break
                except Exception as e:
                    logger.warning("GDELT chunk %s attempt %d failed: %s", 
                                   chunk_start.strftime("%Y-%m"), attempt + 1, str(e)[:100])
                    time.sleep(3.0)

            chunk_start = chunk_end
            time.sleep(6.0)  # GDELT requires 5+ seconds between requests

        if all_headlines:
            providers.append("GDELT")

        # Deduplicate by URL
        seen: set = set()
        deduped: List[Headline] = []
        for h in all_headlines:
            key = h.url or h.headline
            if key not in seen:
                seen.add(key)
                deduped.append(h)

        return deduped, providers

    def _fetch_chunk(
        self, ticker: str, start: datetime, end: datetime
    ) -> List[Headline]:
        """Fetch one 3-month chunk from GDELT."""
        start_str = start.strftime("%Y%m%d") + "000000"
        end_str = end.strftime("%Y%m%d") + "235959"

        # GDELT query: ticker + financial news filter
        query = f"{ticker} stock OR earnings OR revenue OR shares"
        url = (
            f"{GDLET_API}?query={requests.utils.quote(query)}"
            f"&mode=artlist&format=json&maxrecords=50"
            f"&startdatetime={start_str}&enddatetime={end_str}"
            f"&sort=datedesc"
        )

        resp = self._session.get(url, timeout=60)
        if resp.status_code != 200:
            logger.warning("GDELT returned %d: %s", resp.status_code, resp.text[:200])
            return []

        data = resp.json()
        articles = data.get("articles", [])
        headlines: List[Headline] = []

        for a in articles:
            title = a.get("title", "")
            if not title or len(title) < 15:
                continue

            # Filter out clearly non-financial articles
            title_lower = title.lower()
            if not any(
                kw in title_lower
                for kw in [
                    ticker.lower(),
                    "nvidia",
                    "stock",
                    "share",
                    "earnings",
                    "revenue",
                    "chip",
                    "gpu",
                    "ai",
                    "semiconductor",
                    "investor",
                    "analyst",
                    "price target",
                    "bull",
                    "bear",
                ]
            ):
                continue

            url = a.get("url", "")
            source = a.get("domain", "Unknown")
            date_str = ""
            raw_date = a.get("seendate", "")
            if raw_date:
                try:
                    # GDELT format: 20200408T123000Z
                    clean = raw_date.replace("T", "")[:8]
                    date_str = f"{clean[:4]}-{clean[4:6]}-{clean[6:8]}"
                except Exception:
                    pass

            headlines.append(
                Headline(
                    headline=title,
                    url=url,
                    source=source,
                    date=date_str,
                )
            )

        return headlines
