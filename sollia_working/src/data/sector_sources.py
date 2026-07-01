"""
Sollia Agent 5 — Market & Economic Data Source Registry (PART 0 / PART 6d).

The 18 government / established-non-profit sources Agent 5 reads, in a single
machine-readable registry. This is the source-of-truth that:
  * documents each source's endpoint/format, update cadence and what it is used for,
  * records WHERE the publication date lives in each source (PART 2c — every number
    needs a date; the date is the "clock start" for the crossover model),
  * declares each source's auth requirement so the fetch layer can degrade
    gracefully (and so ops knows which API keys must be present to go fully live),
  * feeds the "Agent 5 — Market & Economic Data Sources" section of
    FINAL_API_PRIORITY_LIST.md.

All sources are FREE and machine-readable (CSV or structured PDF/XML tables) from
government or established non-profit bodies. No paywalls, no chart-only reports.

`requires_key`: env var that must be set for a LIVE fetch (None = no key needed).
`status`: "live" (no key required, reachable here) | "needs_key" | "needs_parser"
          (needs a PDF/table parser, e.g. camelot/pdfplumber, not yet installed).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Any


def _s(slug, name, *, category, endpoint, fmt, frequency, used_for, date_location,
       requires_key=None, status="live", notes="") -> Dict[str, Any]:
    return {
        "slug": slug, "name": name, "category": category, "endpoint": endpoint,
        "format": fmt, "update_frequency": frequency, "used_for": used_for,
        "date_location": date_location, "requires_key": requires_key,
        "status": status, "notes": notes,
    }


# Ordered exactly as PART 0's "Agent 5 Data Sources" table.
SOURCE_REGISTRY: List[Dict[str, Any]] = [
    _s("usgs_mcs", "USGS Mineral Commodity Summaries",
       category="supply_demand",
       endpoint="https://pubs.usgs.gov/periodicals/mcs{YEAR}/mcs{YEAR}-{commodity}.pdf",
       fmt="PDF tables (annual)", frequency="annual (January)",
       used_for="Production, apparent consumption, 5-yr growth for most minerals",
       date_location="'January {YEAR}' on the PDF cover — use January 1 of {YEAR}",
       requires_key=None, status="live",
       notes="Primary supply/demand source. LIVE via pdfplumber (src/data/usgs_parser.py): "
             "parses the Salient Statistics table for Production + apparent consumption."),
    _s("fred", "FRED (Federal Reserve Economic Data)",
       category="index_price",
       endpoint="https://api.stlouisfed.org/fred/series/observations?series_id={ID}&api_key={KEY}&file_type=json",
       fmt="JSON/CSV API", frequency="monthly",
       used_for="Industrial production indices, commodity prices",
       date_location="observation_date in the JSON response",
       requires_key="FRED_API_KEY", status="live",
       notes="LIVE: FRED_API_KEY provisioned. src/data/fred_client.py fetches the "
             "verbatim series title + dated reading as a cited index/price indicator."),
    _s("eia", "EIA (U.S. Energy Information Administration)",
       category="supply_demand",
       endpoint="https://api.eia.gov/v2/seriesid/{ID}?api_key={KEY}",
       fmt="JSON/CSV API", frequency="monthly",
       used_for="Energy production, prices, capacity; uranium; natural gas; coal",
       date_location="period field in the CSV/JSON or report header",
       requires_key="EIA_API_KEY", status="needs_key"),
    _s("usa_trade", "USA Trade Online",
       category="trade",
       endpoint="https://usatrade.census.gov/ (Census API: https://api.census.gov/data/timeseries/intltrade/imports/hs)",
       fmt="CSV download / Census API", frequency="monthly",
       used_for="Import/export volumes by HS code (net-imports supply proxy)",
       date_location="month of the data (e.g. 'December 2025') — use the 1st of that month",
       requires_key="CENSUS_API_KEY", status="needs_key",
       notes="Census intltrade API key is free; without it the bulk CSV download path is used."),
    _s("usitc", "USITC DataWeb",
       category="trade",
       endpoint="https://dataweb.usitc.gov/",
       fmt="CSV download", frequency="monthly",
       used_for="Trade data for specific commodities (aerospace materials)",
       date_location="month column in the CSV",
       requires_key="USITC_TOKEN", status="needs_key"),
    _s("usda", "USDA (Economic Research / WAOB projections)",
       category="supply_demand",
       endpoint="https://www.usda.gov/oce/commodity/projections",
       fmt="CSV/JSON + PDF", frequency="annual",
       used_for="Fertilizer use, agricultural projections to 2035",
       date_location="report cover / first CSV row",
       requires_key=None, status="needs_parser"),
    _s("fda_orange_book", "FDA Orange Book",
       category="capacity",
       endpoint="https://www.fda.gov/drugs/drug-approvals-and-databases/approved-drug-products-therapeutic-equivalence-evaluations-orange-book",
       fmt="Structured XML/zip", frequency="annual (monthly deltas)",
       used_for="Drug approvals, API sources (generics)",
       date_location="<edition> / file date in the data package",
       requires_key=None, status="live"),
    _s("fda_dmf", "FDA Drug Master File (DMF) listings",
       category="capacity",
       endpoint="https://www.fda.gov/drugs/drug-master-files-dmfs/list-drug-master-files-dmfs",
       fmt="Structured text / CSV", frequency="continuous (quarterly file)",
       used_for="Active drug master files (API/biotech production capacity)",
       date_location="quarterly file date in the header/filename",
       requires_key=None, status="live"),
    _s("dla_strategic", "DLA Strategic Materials stockpile report",
       category="supply_demand",
       endpoint="https://www.dla.mil/Strategic-Materials/",
       fmt="PDF (annual)", frequency="annual",
       used_for="National Defense Stockpile inventories (aerospace/defense)",
       date_location="report cover / fiscal-year header",
       requires_key=None, status="needs_parser"),
    _s("wsts", "WSTS Semiconductor Market Forecast / press releases",
       category="cagr",
       endpoint="https://www.wsts.org/",
       fmt="Free text / HTML (quarterly)", frequency="quarterly",
       used_for="Semiconductor market growth forecast (industry CAGR)",
       date_location="date in the press-release text or HTML <time> tag",
       requires_key=None, status="live"),
    _s("iea", "IEA Reports (Global EV Outlook, Renewables Market Update)",
       category="cagr",
       endpoint="https://www.iea.org/reports/",
       fmt="PDF tables / free summary", frequency="annual/quarterly",
       used_for="EV, renewables, energy growth forecasts (industry CAGR)",
       date_location="publication date on the report cover page",
       requires_key=None, status="needs_parser"),
    _s("wsa", "World Steel Association Short Range Outlook",
       category="cagr",
       endpoint="https://worldsteel.org/data/short-range-outlook/",
       fmt="PDF tables / press release", frequency="semi-annual",
       used_for="Steel demand growth forecast (industry CAGR)",
       date_location="press-release date / report cover",
       requires_key=None, status="live"),
    _s("iai", "IAI (International Aluminium Institute) Statistics",
       category="supply_demand",
       endpoint="https://international-aluminium.org/statistics/",
       fmt="Web tables", frequency="monthly",
       used_for="Primary aluminium production data",
       date_location="month column in the web table",
       requires_key=None, status="live"),
    _s("acc", "ACC (American Chemistry Council) press releases / CAB",
       category="cagr",
       endpoint="https://www.americanchemistry.com/",
       fmt="Free text / HTML", frequency="monthly/quarterly",
       used_for="Chemical industry outlook (industry CAGR)",
       date_location="press-release date",
       requires_key=None, status="live"),
    _s("pca", "PCA (Portland Cement Association) Cement Forecast",
       category="cagr",
       endpoint="https://www.cement.org/",
       fmt="Free text / HTML", frequency="quarterly",
       used_for="Cement consumption forecast (industry CAGR)",
       date_location="press-release date",
       requires_key=None, status="live"),
    _s("evaluatepharma", "EvaluatePharma World Preview",
       category="cagr",
       endpoint="https://www.evaluate.com/thought-leadership/pharma/",
       fmt="Free executive summary (HTML)", frequency="annual",
       used_for="Pharma/biotech market growth (industry CAGR)",
       date_location="report date in the summary page",
       requires_key=None, status="live"),
    _s("aia", "AIA Aerospace Industry Outlook",
       category="cagr",
       endpoint="https://www.aia-aerospace.org/",
       fmt="Free executive summary (HTML)", frequency="quarterly",
       used_for="Aerospace industry outlook (industry CAGR)",
       date_location="report date in the summary page",
       requires_key=None, status="live"),
    _s("doe_haleu", "DOE HALEU availability reports",
       category="supply_demand",
       endpoint="https://www.energy.gov/ne/",
       fmt="PDF (semi-annual)", frequency="semi-annual",
       used_for="HALEU nuclear-fuel availability",
       date_location="report cover date",
       requires_key=None, status="needs_parser"),
]

# Fast lookup by slug.
SOURCES_BY_SLUG: Dict[str, Dict[str, Any]] = {s["slug"]: s for s in SOURCE_REGISTRY}


def get_source(slug: str) -> Optional[Dict[str, Any]]:
    return SOURCES_BY_SLUG.get(slug)


def sources_requiring_keys() -> List[Dict[str, Any]]:
    """Sources that need an API key (or a parser) before a LIVE fetch can succeed
    — exactly what makes the difference between 'data_unavailable' and live numbers."""
    return [s for s in SOURCE_REGISTRY if s["status"] != "live"]


def missing_capabilities() -> Dict[str, List[str]]:
    """Report, for this environment, which sources are blocked and why. Used by the
    agent log and by ops to know what to provision."""
    import os
    blocked_keys: List[str] = []
    blocked_parser: List[str] = []
    for s in SOURCE_REGISTRY:
        if s["status"] == "needs_key" and s["requires_key"] and not os.environ.get(s["requires_key"]):
            blocked_keys.append("%s (set %s)" % (s["slug"], s["requires_key"]))
        elif s["status"] == "needs_parser":
            blocked_parser.append(s["slug"])
    return {"missing_keys": blocked_keys, "needs_pdf_parser": blocked_parser}


def _self_check() -> None:
    assert len(SOURCE_REGISTRY) == 18, f"expected 18 sources, got {len(SOURCE_REGISTRY)}"
    assert len(SOURCES_BY_SLUG) == 18, "duplicate slug in registry"
    for s in SOURCE_REGISTRY:
        assert s["date_location"], f"{s['slug']}: every source must declare a date_location"
    caps = missing_capabilities()
    print(f"OK: 18 sources registered. "
          f"Live-now: {sum(1 for s in SOURCE_REGISTRY if s['status']=='live')}, "
          f"need key: {len(caps['missing_keys'])}, need parser: {len(caps['needs_pdf_parser'])}.")


if __name__ == "__main__":
    _self_check()
