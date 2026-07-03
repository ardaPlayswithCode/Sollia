"""Page-number extraction for SEC filing citations (production polish, Part 5).

SEC EDGAR HTML carries printed page numbers as header/footer artifacts that
survive HTML->text cleaning as short standalone lines:

    "42"                       bare footer number on its own line
    "42 Table of Contents"     footer number + TOC backlink (very common)
    "Table of Contents 42"     header variant
    "Page 42" / "- 42 -"       explicit page labels
    "[PAGE 42]"                inline markers in older filings

build_page_map() scans CLEANED text once and returns marker offsets;
page_of_quote() maps a verbatim citation quote to the page it appears on.
Convention: a marker is treated as the FOOTER of the page that precedes it,
so a quote's page is the number of the first marker at/after the quote.
Filings without recognizable markers simply yield no page (callers omit the
field — the citation renders exactly as before).
"""
from __future__ import annotations

import re
from bisect import bisect_left
from typing import List, Optional, Tuple

__all__ = ["build_page_map", "page_of_quote", "page_for_offset"]

# Standalone footer/header lines. Bare numbers are only trusted as page markers
# when they sit alone on a line (with optional TOC backlink text around them).
_MARKER_RES = [
    re.compile(r"^\s*(?:Table of Contents\s+)?(\d{1,3})(?:\s+Table of Contents)?\s*$",
               re.MULTILINE),
    re.compile(r"^\s*-\s*(\d{1,3})\s*-\s*$", re.MULTILINE),
    re.compile(r"^\s*Page\s+(\d{1,3})\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"\[\s*PAGE\s+(\d{1,3})\s*\]", re.IGNORECASE),
]
# For text that was whitespace-flattened (newlines collapsed), the TOC-backlink
# pattern is still recoverable: "... risks. 42 Table of Contents The following..."
_FLAT_TOC_RE = re.compile(r"(?:(?<=[.;:])|(?<=\)))\s+(\d{1,3})\s+Table of Contents\s+")

_ALNUM_RE = re.compile(r"[^a-z0-9]")


def build_page_map(text: str) -> List[Tuple[int, int]]:
    """Scan cleaned filing text for page markers -> sorted [(offset, page)].

    Keeps only a self-consistent ascending run of page numbers (>= 3 markers)
    so stray numeric lines (tabular data, dollar figures) don't masquerade as
    pages. Returns [] when the filing has no usable printed pagination.
    """
    if not text:
        return []
    raw: List[Tuple[int, int]] = []
    for rx in _MARKER_RES:
        for m in rx.finditer(text):
            raw.append((m.start(), int(m.group(1))))
    if len(raw) < 3:
        for m in _FLAT_TOC_RE.finditer(text):
            raw.append((m.start(1), int(m.group(1))))
    raw.sort()

    # Longest ascending (by page) chain of plausible increments — page footers
    # increase by small steps; random figures jump around.
    best: List[Tuple[int, int]] = []
    cur: List[Tuple[int, int]] = []
    for off, page in raw:
        if not cur or 0 < page - cur[-1][1] <= 5:
            cur.append((off, page))
        else:
            if len(cur) > len(best):
                best = cur
            cur = [(off, page)]
    if len(cur) > len(best):
        best = cur
    return best if len(best) >= 3 else []


def page_for_offset(page_map: List[Tuple[int, int]], offset: int) -> Optional[int]:
    """Page containing `offset`: the page of the first marker at/after it
    (markers are footers). Past the last marker the page is genuinely unknown
    (the filing stopped printing numbers) -> None. A wrong page number costs
    analyst trust; a missing one doesn't."""
    if not page_map:
        return None
    offsets = [o for o, _ in page_map]
    i = bisect_left(offsets, offset)
    if i < len(page_map):
        return page_map[i][1]
    return None


def _alnum(s: str) -> str:
    return _ALNUM_RE.sub("", (s or "").lower())


def page_of_quote(text: str, quote: str,
                  page_map: Optional[List[Tuple[int, int]]] = None) -> Optional[int]:
    """Page number for a verbatim quote inside cleaned filing text.

    Locates the quote with the same alnum-normalized matching the quote
    verifier uses (whitespace/punctuation drift between the stored quote and
    the cleaned text must not lose the page). None when the quote isn't found
    or the filing has no pagination."""
    if not text or not quote:
        return None
    if page_map is None:
        page_map = build_page_map(text)
    if not page_map:
        return None
    idx = text.find(quote)
    if idx == -1:
        # alnum-normalized scan with an offset back-map
        norm_chars: List[str] = []
        back: List[int] = []
        for i, ch in enumerate(text):
            low = ch.lower()
            if low.isalnum():
                norm_chars.append(low)
                back.append(i)
        pos = "".join(norm_chars).find(_alnum(quote))
        if pos == -1 or not back:
            return None
        idx = back[min(pos, len(back) - 1)]
    return page_for_offset(page_map, idx)
