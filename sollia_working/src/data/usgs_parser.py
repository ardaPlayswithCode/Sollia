"""
USGS Mineral Commodity Summaries (MCS) parser — supply/demand extraction (PART 3a).

Reads the annual per-commodity MCS PDF (e.g. mcs2025-phosphate.pdf) and extracts,
from the "Salient Statistics—United States" table:
  * Production (the supply starting point) for the last 5 years,
  * Consumption, apparent (the demand starting point),
plus the data unit, the publication date (MCS publishes every January → Jan 1 of
the edition year), and a VERBATIM citation line for each number (the table row,
which contains the exact figure, verifiable character-for-character against the
PDF text — PART 3b / PART 7b #8).

Withheld values ("W"), footnote superscripts ("apparent1"), and estimate prefixes
("e19,800") are handled. Commodities the US withholds (all-"W" rows, e.g. Silicon)
return None for that metric, so the caller degrades to data_unavailable rather
than inventing a number (PART 9).

Pure-Python: requests + pdfplumber (no Ghostscript/system deps).
"""
from __future__ import annotations

import io
import re
from datetime import date
from typing import Any, Dict, List, Optional

import requests

try:
    import pdfplumber
    _HAVE_PDFPLUMBER = True
except Exception:  # pragma: no cover
    _HAVE_PDFPLUMBER = False

USGS_HDR = {"User-Agent": "Sollia contact@sollia.app"}
USGS_URL = "https://pubs.usgs.gov/periodicals/mcs{year}/mcs{year}-{slug}.pdf"

# USGS commodity name (as stored in constraint_database) -> MCS filename slug.
# Where a guess would be wrong, the explicit mapping wins; unmapped names fall
# back to a lowercase/hyphen slug and 404 -> data_unavailable (graceful).
USGS_SLUG: Dict[str, str] = {
    "Silicon": "silicon", "Gallium": "gallium", "Germanium": "germanium",
    "Hafnium": "hafnium", "Cobalt": "cobalt", "Lithium": "lithium",
    "Nickel": "nickel", "Copper": "copper", "Rare Earths": "rare-earths",
    "Graphite": "graphite", "Manganese": "manganese", "Silver": "silver",
    "Titanium": "titanium-mineral-concentrates", "Vanadium": "vanadium",
    "Niobium": "niobium", "Iron Ore": "iron-ore",
    "Bauxite and Alumina": "bauxite-alumina", "Zirconium": "zirconium-hafnium",
    "Phosphate Rock": "phosphate", "Potash": "potash", "Sulfur": "sulfur",
    "Salt": "salt", "Cement": "cement", "Iron and Steel Slag": "iron-steel-slag",
    "Stone, Crushed": "stone-crushed", "Sand and Gravel, Construction": "sand-gravel-construction",
    "Iodine": "iodine", "Helium": "helium", "Fluorspar": "fluorspar",
    "Rhenium": "rhenium", "Uranium": "uranium",
}

_WITHHELD = {"W", "—", "-", "NA", "N/A", "(1)"}


def slug_for(commodity: str) -> str:
    return USGS_SLUG.get(commodity) or re.sub(r"[^a-z0-9]+", "-", commodity.lower()).strip("-")


def _num_tokens(line: str) -> List[Optional[float]]:
    """All numeric/withheld value tokens on a salient-statistics row, in order.
    'W'/'—' -> None; strips 'e' estimate prefix, commas, and trailing footnote
    markers. Footnote superscripts attached to the LABEL (e.g. 'apparent1') are
    dropped by the caller taking only the trailing N tokens."""
    out: List[Optional[float]] = []
    for tok in line.split():
        t = tok.strip()
        if t in _WITHHELD:
            out.append(None)
            continue
        m = re.fullmatch(r"e?\(?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\)?", t)
        if m:
            try:
                out.append(float(m.group(1).replace(",", "")))
            except Exception:
                pass
    return out


def _parse_unit(text: str) -> str:
    m = re.search(r"\(Data in ([^,)]+(?:,[^,)]+)?)", text)
    if m:
        return m.group(1).strip()
    return "metric tons"


def _years(line: str) -> List[int]:
    # Match 4-digit 19xx/20xx years NOT adjacent to another digit. Crucially this
    # still catches the estimate year written as "2025e" (a trailing-\b regex
    # misses it because 'e' is a word char), while comma-grouped values like
    # "21,600" never false-match (they don't start 19/20 with 2 trailing digits).
    return [int(y) for y in re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", line)]


def _find_block(lines: List[str]) -> Optional[int]:
    for i, l in enumerate(lines):
        if "Salient Statistics" in l:
            return i
    return None


def _row_values(lines: List[str], start: int, label_pat: str, n_years: int) -> Optional[Dict[str, Any]]:
    """First row in the salient-statistics block matching label_pat; returns the
    last n_years numeric values + the verbatim row text. None if withheld/absent."""
    rx = re.compile(label_pat, re.IGNORECASE)
    for i in range(start, min(start + 30, len(lines))):
        line = lines[i]
        if rx.search(line):
            vals = _num_tokens(line)
            tail = vals[-n_years:] if len(vals) >= n_years else vals
            if any(v is not None for v in tail):
                return {"values": tail, "row_text": line.strip()}
            return None  # matched but withheld (all W)
    return None


def _cagr(values: List[Optional[float]], years: List[int]) -> Optional[float]:
    """Trailing CAGR across the first and last NON-withheld points, using the real
    year span between them (PART 3a). None if <2 valid points or a zero start."""
    pts = [(years[i], v) for i, v in enumerate(values) if v is not None and i < len(years)]
    if len(pts) < 2:
        return None
    (y0, v0), (y1, v1) = pts[0], pts[-1]
    span = y1 - y0
    if span <= 0 or v0 <= 0 or v1 <= 0:
        return None
    return (v1 / v0) ** (1.0 / span) - 1.0


def parse_mcs_text(text: str, n_years_hint: int = 5) -> Optional[Dict[str, Any]]:
    """Parse the salient-statistics table out of MCS page-1 text. Returns
    production/consumption series + unit + the verbatim citation rows, or None."""
    lines = [l for l in text.split("\n") if l.strip()]
    blk = _find_block(lines)
    if blk is None:
        return None
    years = _years(lines[blk])
    if not years:
        # year header sometimes wraps to the next line
        years = _years(lines[blk + 1]) if blk + 1 < len(lines) else []
    if not years:
        return None
    n = len(years)
    unit = _parse_unit(text)
    prod = _row_values(lines, blk, r"^\s*Production\b", n)
    cons = _row_values(lines, blk, r"Consumption,\s*apparent", n)
    return {"years": years, "unit": unit, "production": prod, "consumption": cons,
            "n_years": n}


def fetch_mcs_pdf(commodity: str, year: int) -> Optional[Dict[str, Any]]:
    """Download an MCS PDF for a commodity, trying the requested year then the two
    prior editions (PART 7c #3: 'use prior year, flag as latest available')."""
    if not _HAVE_PDFPLUMBER:
        return None
    slug = slug_for(commodity)
    for yr in (year, year - 1, year - 2):
        url = USGS_URL.format(year=yr, slug=slug)
        try:
            r = requests.get(url, headers=USGS_HDR, timeout=30)
        except Exception:
            continue
        if r.status_code == 200 and r.content[:4] == b"%PDF":
            return {"year": yr, "url": url, "content": r.content,
                    "is_latest": (yr == year)}
    return None


def extract(commodity: str, current_year: int) -> Optional[Dict[str, Any]]:
    """Full pipeline: fetch the MCS PDF, parse the salient-statistics table, and
    return supply (Production + 5-yr CAGR) and demand (apparent consumption) blocks
    with verbatim citation rows and the publication date (Jan 1 of the edition).

    Returns None if pdfplumber is missing or the PDF can't be fetched. Returns a
    dict with possibly-None supply/demand if a metric is withheld."""
    fetched = fetch_mcs_pdf(commodity, current_year)
    if not fetched:
        return None
    try:
        with pdfplumber.open(io.BytesIO(fetched["content"])) as pdf:
            text = pdf.pages[0].extract_text() or ""
            if len(pdf.pages) > 1:
                text += "\n" + (pdf.pages[1].extract_text() or "")
    except Exception:
        return None

    parsed = parse_mcs_text(text)
    if not parsed:
        return None

    pub_year = fetched["year"]
    pub_date = date(pub_year, 1, 1).isoformat()
    years = parsed["years"]
    unit = parsed["unit"]
    latest_year = years[-1]
    note_year = "" if fetched["is_latest"] else " (%d data — latest available)" % pub_year

    out: Dict[str, Any] = {
        "source": "USGS Mineral Commodity Summaries %d" % pub_year,
        "url": fetched["url"], "publication_date": pub_date, "unit": unit,
        "commodity": commodity, "scope": "United States", "supply": None, "demand": None,
    }

    prod = parsed["production"]
    if prod and prod["values"] and prod["values"][-1] is not None:
        cagr = _cagr(prod["values"], years)
        out["supply"] = {
            "value": prod["values"][-1], "unit": unit,
            "growth_rate_pct": round(cagr * 100.0, 2) if cagr is not None else 0.0,
            "source": out["source"] + note_year, "date": pub_date, "url": fetched["url"],
            "quote": prod["row_text"], "quote_verified": _verify(prod["row_text"], text),
            "scope": "United States",
            "growth_calc": "CAGR of %s (%s)" % (
                [v for v in prod["values"] if v is not None],
                "%d-%d" % (years[0], latest_year)),
        }
    cons = parsed["consumption"]
    if cons and cons["values"] and cons["values"][-1] is not None:
        out["demand"] = {
            "value": cons["values"][-1], "unit": unit,
            "source": out["source"] + note_year, "date": pub_date, "url": fetched["url"],
            "quote": cons["row_text"], "quote_verified": _verify(cons["row_text"], text),
            "scope": "United States",
        }
    return out


def _verify(quote: str, text: str) -> bool:
    def norm(s):
        return re.sub(r"\s+", " ", re.sub(r"[‘’“”\"']", "", s)).strip().lower()
    q, t = norm(quote), norm(text)
    return bool(q) and (q in t or (len(q) >= 20 and q[:60] in t))


if __name__ == "__main__":
    import sys, json
    com = sys.argv[1] if len(sys.argv) > 1 else "Phosphate Rock"
    yr = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    res = extract(com, yr)
    print(json.dumps(res, indent=2, default=str) if res else "no data for %s" % com)
