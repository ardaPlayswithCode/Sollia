"""
USGS Mineral Commodity Summaries (MCS) parser — supply/demand extraction (PART 3a).

Reads the annual per-commodity MCS PDF (e.g. mcs2025-phosphate.pdf) and extracts,
from the "Salient Statistics—United States" table:
  * Production (the supply starting point) — handling BOTH flat rows
    ("Production, marketable1 480 ...") and parent/subrow layouts
    ("Production:" followed by "Mine ..." / "Total (rounded) ..."),
  * Consumption (the demand starting point) — handling the label variants USGS
    actually prints: "Consumption, apparent", the "Apparent" subrow under a
    "Consumption:" parent, "Consumption, reported", "Consumption, estimated",
plus the data unit, the publication date (MCS publishes every January → Jan 1 of
the edition year), and a VERBATIM citation line for each number (the table row,
which contains the exact figure, verifiable character-for-character against the
PDF text — PART 3b / PART 7b #8).

Per-metric DIAGNOSTICS distinguish the four honest failure modes so the caller
can explain exactly why a number is missing (2026-07-01 audit, Part 1):
  * "ok"        — a real number was extracted,
  * "withheld"  — the row exists but every value is "W" (USGS withholds company-
                  proprietary data, e.g. US lithium/silicon production),
  * "none"      — the row is all "—" (the US produces NONE; value 0, not missing),
  * "na"        — the row is all "NA" (not available/estimated),
  * "not_found" — no matching row in the salient-statistics block,
  * "not_published" — USGS does not publish the metric at all (e.g. hafnium).

When a metric's LATEST year is missing but an earlier year has data, the most
recent non-missing value is used and `data_year` records which year it describes
(e.g. germanium consumption was last estimated in 2021).

WORLD production is also extracted (the "World total" row of the World Mine
Production and Reserves table) — USGS usually publishes world data even when the
US figure is withheld, so the caller can show world context for withheld rows.

Pure-Python: requests + pdfplumber (no Ghostscript/system deps).
"""
from __future__ import annotations

import io
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    import pdfplumber
    _HAVE_PDFPLUMBER = True
except Exception:  # pragma: no cover
    _HAVE_PDFPLUMBER = False

USGS_HDR = {"User-Agent": "Sollia contact@sollia.app"}
USGS_URL = "https://pubs.usgs.gov/periodicals/mcs{year}/mcs{year}-{slug}.pdf"

# Bump when extraction logic changes: get_market_data treats a cached payload with
# a different parser_version as stale, so parser upgrades auto-invalidate old
# (possibly null) cached extracts without manual cache surgery.
PARSER_VERSION = 4

# USGS commodity name (as stored in constraint_database) -> MCS filename slug.
# Where a guess would be wrong, the explicit mapping wins; unmapped names fall
# back to a lowercase/hyphen slug and 404 -> data_unavailable (graceful).
USGS_SLUG: Dict[str, str] = {
    "Silicon": "silicon", "Gallium": "gallium", "Germanium": "germanium",
    "Hafnium": "zirconium-hafnium", "Cobalt": "cobalt", "Lithium": "lithium",
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

# Commodities whose US production/consumption USGS does not publish AT ALL (not
# merely withheld): hafnium is a byproduct of zirconium refining and only its
# import/export tonnage appears (inside the zirconium sheet). The caller gets a
# citable sheet URL + a precise reason instead of a misleading "withheld".
NOT_PUBLISHED: Dict[str, str] = {
    "Hafnium": ("USGS does not publish US hafnium production or consumption "
                "(hafnium is a byproduct of zirconium refining; only import/export "
                "tonnage is reported, inside the zirconium sheet)"),
}

_WITHHELD_TOK = {"W"}
_NONE_TOK = {"—", "–", "-"}
_NA_TOK = {"NA", "N/A"}

# Production sub-rows in preference order: an explicit total first, then the
# primary/mine figure. ("Secondary" is deliberately last — scrap recovery is not
# primary supply.)
_PROD_SUBROWS = [
    r"^\s*Total\b", r"^\s*Mine\b", r"^\s*Mineral concentrates", r"^\s*Marketable\b",
    r"^\s*Primary\b", r"^\s*Recovered elemental\b", r"^\s*Refinery\b",
    r"^\s*Smelter\b", r"^\s*Secondary\b",
]
# Consumption label variants in preference order (apparent > reported > estimated).
_CONS_PATTERNS = [
    r"Consumption,\s*apparent", r"^\s*Apparent[e]?\b",
    r"Consumption,\s*reported", r"^\s*Reported\b",
    r"Consumption,\s*estimated", r"^\s*Estimated\b",
]


def slug_for(commodity: str) -> str:
    return USGS_SLUG.get(commodity) or re.sub(r"[^a-z0-9]+", "-", commodity.lower()).strip("-")


def _value_tokens(line: str) -> List[Tuple[str, Optional[float]]]:
    """All value-position tokens on a salient-statistics row, in order, as
    (kind, value) pairs. kind: 'num' | 'W' | 'none' | 'NA'. Strips 'e' estimate
    prefix, commas, and parenthesized negatives; ignores label words."""
    out: List[Tuple[str, Optional[float]]] = []
    for tok in line.split():
        t = tok.strip()
        if t in _WITHHELD_TOK:
            out.append(("W", None))
            continue
        if t in _NONE_TOK:
            out.append(("none", None))
            continue
        if t in _NA_TOK:
            out.append(("NA", None))
            continue
        m = re.fullmatch(r"e?\(?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\)?", t)
        if m:
            try:
                out.append(("num", float(m.group(1).replace(",", ""))))
            except Exception:
                pass
            continue
        # Footnote superscripts merge into the following number in pdfplumber text
        # ("8290,000" = footnote 8 + 290,000). Real comma-grouped numbers never have
        # 4+ digits before the first comma, so strip the leading footnote digit.
        m = re.fullmatch(r"e?(\d)(\d{1,3}(?:,\d{3})+(?:\.\d+)?)", t)
        if m:
            try:
                out.append(("num", float(m.group(2).replace(",", ""))))
            except Exception:
                pass
    return out


def _row_status(tokens: List[Tuple[str, Optional[float]]]) -> str:
    """Classify a row from its value tokens: ok / withheld / none / na / empty."""
    kinds = [k for k, _ in tokens]
    if any(k == "num" for k in kinds):
        return "ok"
    if kinds.count("W") >= 2:
        return "withheld"
    if kinds.count("none") >= 2:
        return "none"
    if kinds.count("NA") >= 2:
        return "na"
    return "empty"  # a parent label row ("Production:") or a wrapped label


def _parse_unit(text: str) -> str:
    # USGS prints "(Data in ...)" or "[Data in ...]"; the unit may itself contain
    # parens ("potassium oxide (K2O) equivalent"), so capture up to the "unless"
    # clause first, then fall back to the bare bracketed form.
    m = re.search(r"[\(\[]Data in\s+(.+?),?\s+unless\b", text)
    if not m:
        m = re.search(r"[\(\[]Data in\s+([^\)\]]+)[\)\]]", text)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip().rstrip(",")
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


def _align_to_years(tokens: List[Tuple[str, Optional[float]]],
                    years: List[int]) -> List[Tuple[Optional[int], str, Optional[float]]]:
    """Right-align value tokens to the year columns (rows sometimes carry fewer
    tokens when a label wraps). Returns [(year|None, kind, value), ...]."""
    n = len(years)
    tail = tokens[-n:] if len(tokens) >= n else tokens
    offset = n - len(tail)
    out = []
    for i, (kind, val) in enumerate(tail):
        yr = years[offset + i] if 0 <= offset + i < n else None
        out.append((yr, kind, val))
    return out


def _latest_value(aligned: List[Tuple[Optional[int], str, Optional[float]]]
                  ) -> Optional[Tuple[Optional[int], float]]:
    """Most recent (year, value) with a real number; 'none' rows count as 0.0."""
    for yr, kind, val in reversed(aligned):
        if kind == "num" and val is not None:
            return (yr, val)
        if kind == "none":
            return (yr, 0.0)
    return None


def _find_metric(lines: List[str], start: int, patterns: List[str],
                 years: List[int], subrow_patterns: Optional[List[str]] = None
                 ) -> Dict[str, Any]:
    """Locate a metric row in the salient block, following parent rows into their
    sub-rows when the parent carries no values. Returns a dict:
      status: ok|withheld|none|na|not_found
      aligned: [(year, kind, value)]   row_text: verbatim line   label: matched text
    Statuses from a matched-but-valueless row propagate (withheld/none/na)."""
    end = min(start + 30, len(lines))
    fallback: Optional[Dict[str, Any]] = None
    for pat in patterns:
        rx = re.compile(pat, re.IGNORECASE)
        for i in range(start, end):
            line = lines[i]
            if not rx.search(line):
                continue
            toks = _value_tokens(line)
            status = _row_status(toks)
            if status == "ok":
                return {"status": "ok", "aligned": _align_to_years(toks, years),
                        "row_text": line.strip(), "label": pat}
            if status == "none":
                return {"status": "none", "aligned": _align_to_years(toks, years),
                        "row_text": line.strip(), "label": pat}
            if status in ("withheld", "na"):
                fallback = fallback or {"status": status, "aligned": [],
                                        "row_text": line.strip(), "label": pat}
                continue
            # status == "empty": a parent row like "Production:" — scan its sub-rows.
            if subrow_patterns:
                for sub_pat in subrow_patterns:
                    sub_rx = re.compile(sub_pat, re.IGNORECASE)
                    for j in range(i + 1, min(i + 8, end)):
                        sub = lines[j]
                        if not sub_rx.search(sub):
                            continue
                        stoks = _value_tokens(sub)
                        sstat = _row_status(stoks)
                        if sstat in ("ok", "none"):
                            return {"status": sstat,
                                    "aligned": _align_to_years(stoks, years),
                                    "row_text": sub.strip(), "label": sub_pat}
                        if sstat in ("withheld", "na"):
                            fallback = fallback or {"status": sstat, "aligned": [],
                                                    "row_text": sub.strip(), "label": sub_pat}
            break  # matched this pattern; don't rescan the block with the same one
    return fallback or {"status": "not_found", "aligned": [], "row_text": "", "label": ""}


def _world_production(text: str, years: List[int]) -> Optional[Dict[str, Any]]:
    """The 'World total' row of the World Production table (page 2; USGS titles it
    'World Mine Production and Reserves', 'World Refinery Production ...',
    'World Low-Purity Production and Production Capacity', ...). The row's last
    column is Reserves/Capacity when the table has one; the value before it is the
    latest-year world production. Returns None when no clean row exists."""
    lines = [l for l in text.split("\n") if l.strip()]
    world_start = None
    for i, l in enumerate(lines):
        # Case-SENSITIVE on purpose: the table header is title-case ("World Mine
        # Production and Reserves:", "World Production:"), while narrative
        # sentences ("World annual potash production capacity was...") are not.
        if re.search(r"World\s+.{0,30}Production", l):
            world_start = i
            break
    if world_start is None:
        return None
    # Production year columns LEAD the row; Reserves/Capacity/Resources columns
    # (variable in number) trail it — so anchor from the FRONT: the 2nd number is
    # the latest-year (estimate) production. When the header's FIRST year repeats
    # ("2024 2025 2024 2025"), the column sequence restarts, i.e. the table
    # carries multiple sub-metrics (silicon: ferrosilicon + silicon metal) and no
    # single world total exists — skip rather than mislabel. A single trailing
    # extra year ("2024 2025e 2025" = capacity column, gallium) is fine.
    world_year = years[-1] if years else None
    for l in lines[world_start + 1:world_start + 5]:
        yrs = _years(l)
        if yrs:
            if yrs.count(yrs[0]) > 1:
                return None  # multi-metric world table — ambiguous, degrade honestly
            world_year = yrs[1] if len(yrs) >= 2 else yrs[0]
            break
    for l in lines[world_start:world_start + 60]:
        if re.search(r"World total", l, re.IGNORECASE):
            nums = [v for k, v in _value_tokens(l) if k == "num" and v is not None]
            if not nums:
                return None
            value = nums[1] if len(nums) >= 2 else nums[0]
            return {"value": value, "year": world_year, "quote": l.strip()}
    return None


def _cagr_aligned(aligned: List[Tuple[Optional[int], str, Optional[float]]]) -> Optional[float]:
    """Trailing CAGR across the first and last real data points, using the real
    year span between them (PART 3a). 'none' rows are 0 -> no CAGR from zero."""
    pts = [(yr, val) for yr, kind, val in aligned
           if kind == "num" and val is not None and yr is not None]
    if len(pts) < 2:
        return None
    (y0, v0), (y1, v1) = pts[0], pts[-1]
    span = y1 - y0
    if span <= 0 or v0 <= 0 or v1 <= 0:
        return None
    return (v1 / v0) ** (1.0 / span) - 1.0


def parse_mcs_text(text: str) -> Optional[Dict[str, Any]]:
    """Parse the salient-statistics table out of MCS page-1(+2) text. Returns
    production/consumption metric dicts + unit + years + world production, or
    None when no salient-statistics block exists at all."""
    lines = [l for l in text.split("\n") if l.strip()]
    blk = _find_block(lines)
    if blk is None:
        return None
    years = _years(lines[blk])
    if not years:
        years = _years(lines[blk + 1]) if blk + 1 < len(lines) else []
    if not years:
        return None
    unit = _parse_unit(text)
    prod = _find_metric(lines, blk, [r"^\s*Production\b"], years,
                        subrow_patterns=_PROD_SUBROWS)
    cons = _find_metric(lines, blk, _CONS_PATTERNS, years)
    world = _world_production(text, years)
    return {"years": years, "unit": unit, "production": prod, "consumption": cons,
            "world": world}


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
            # sha256 of the exact bytes parsed — recorded in the extract so any
            # number can be traced to (and re-verified against) the source PDF
            # (Part 6b data-integrity guard, 2026-07-01).
            import hashlib as _hl
            return {"year": yr, "url": url, "content": r.content,
                    "sha256": _hl.sha256(r.content).hexdigest(),
                    "is_latest": (yr == year)}
    return None


def _metric_block(metric: Dict[str, Any], unit: str, source: str, pub_date: str,
                  url: str, text: str, with_growth: bool) -> Optional[Dict[str, Any]]:
    """Convert a _find_metric result into the cited supply/demand block, using the
    most recent non-missing value (dated to its actual year)."""
    if metric["status"] not in ("ok", "none"):
        return None
    latest = _latest_value(metric["aligned"])
    if latest is None:
        return None
    yr, value = latest
    blk: Dict[str, Any] = {
        "value": value, "unit": unit,
        "source": source, "date": pub_date, "url": url,
        "quote": metric["row_text"], "quote_verified": _verify(metric["row_text"], text),
        "scope": "United States",
    }
    if yr is not None:
        blk["data_year"] = yr
    if metric["status"] == "none":
        blk["note"] = "US production: none reported (row is '—' in the MCS table)"
    if with_growth:
        cagr = _cagr_aligned(metric["aligned"])
        blk["growth_rate_pct"] = round(cagr * 100.0, 2) if cagr is not None else 0.0
        pts = [(y, v) for y, k, v in metric["aligned"] if k == "num" and v is not None]
        if pts:
            blk["growth_calc"] = "CAGR of %s (%d-%d)" % (
                [v for _, v in pts], pts[0][0] or 0, pts[-1][0] or 0)
    return blk


def extract(commodity: str, current_year: int) -> Optional[Dict[str, Any]]:
    """Full pipeline: fetch the MCS PDF, parse the salient-statistics table, and
    return supply (Production + trailing CAGR) and demand (apparent/reported/
    estimated consumption) blocks with verbatim citation rows, per-metric
    diagnostics, world production, and the publication date.

    Returns None only when the PDF itself cannot be fetched/parsed (pdfplumber
    missing, network down, no such sheet). Withheld/none/NA metrics return a dict
    with the metric None and diagnostics saying exactly why."""
    if commodity in NOT_PUBLISHED:
        fetched = fetch_mcs_pdf(commodity, current_year)
        return {
            "source": "USGS Mineral Commodity Summaries %d" % (fetched["year"] if fetched else current_year),
            "url": (fetched or {}).get("url", USGS_URL.format(year=current_year, slug=slug_for(commodity))),
            "publication_date": date((fetched["year"] if fetched else current_year), 1, 1).isoformat(),
            "unit": "", "commodity": commodity, "scope": "United States",
            "supply": None, "demand": None, "world_production": None,
            "parser_version": PARSER_VERSION,
            "diagnostics": {"production": "not_published", "consumption": "not_published",
                            "detail": NOT_PUBLISHED[commodity]},
        }

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
    unit = parsed["unit"]
    note_year = "" if fetched["is_latest"] else " (%d data — latest available)" % pub_year
    source = "USGS Mineral Commodity Summaries %d%s" % (pub_year, note_year)

    prod, cons = parsed["production"], parsed["consumption"]
    out: Dict[str, Any] = {
        "source": source, "url": fetched["url"], "publication_date": pub_date,
        "unit": unit, "commodity": commodity, "scope": "United States",
        "supply": _metric_block(prod, unit, source, pub_date, fetched["url"], text, with_growth=True),
        "demand": _metric_block(cons, unit, source, pub_date, fetched["url"], text, with_growth=False),
        "world_production": None,
        "parser_version": PARSER_VERSION,
        "pdf_sha256": fetched.get("sha256"),
        "diagnostics": {"production": prod["status"], "consumption": cons["status"]},
    }
    world = parsed.get("world")
    if world and world.get("value") is not None:
        out["world_production"] = {
            "value": world["value"], "unit": unit, "year": world.get("year"),
            "scope": "World", "source": source, "date": pub_date, "url": fetched["url"],
            "quote": world["quote"], "quote_verified": _verify(world["quote"], text),
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
