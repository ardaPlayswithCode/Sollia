"""
FRED (St. Louis Fed) client for Agent 5 — Industrial Production indices and
commodity prices, as CITED indicator signals (PART 0 source #2 / PART 7.6b).

IMPORTANT — what FRED is and isn't for the constraint audit:
  * FRED serves INDICES (e.g. IPG3344S, "Index 2017=100") and PRICES (e.g.
    PCOPPUSDM, "$/metric ton"). These are NOT tonnage, so they do NOT share units
    with USGS production/apparent-consumption and therefore do NOT drive the
    supply-vs-demand crossover. They enrich the audit as:
      - a sector-level PRODUCTION-TREND signal (the IP index + its growth), and
      - a material-level PRICE-PRESSURE signal (a rising price = tightening supply).
  * Every datum is cited: the verbatim FRED series TITLE (independently
    re-verifiable via the /fred/series endpoint) plus the dated reading and units.

Security: the API key is read from FRED_API_KEY and used ONLY in the request. It
is NEVER written into any returned/cached field or citation URL — the citation
URL is the public, browsable series page (no key).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

FRED_BASE = "https://api.stlouisfed.org/fred"
SERIES_PAGE = "https://fred.stlouisfed.org/series/{sid}"  # public citation URL (no key)


def _load_env_once() -> None:
    """Load FRED_API_KEY from sollia_working/.env if not already in the environment.
    The pipeline loads .env via llm_config, but fred_client is also used standalone
    (tools, tests), so it stays self-sufficient. Mirrors llm_config._load_dotenv."""
    if os.environ.get("FRED_API_KEY"):
        return
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
    try:
        with open(env_path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                k, _, v = line.partition("=")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass


def _key() -> str:
    if not os.environ.get("FRED_API_KEY"):
        _load_env_once()
    return os.environ.get("FRED_API_KEY", "")


def _get(path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    key = _key()
    if not key:
        return None
    p = dict(params, api_key=key, file_type="json")
    try:
        r = requests.get("%s/%s" % (FRED_BASE, path), params=p, timeout=20)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def _series_meta(series_id: str) -> Optional[Dict[str, Any]]:
    data = _get("series", {"series_id": series_id})
    if not data or not data.get("seriess"):
        return None  # invalid/unknown series id -> caller degrades, never cites a fake
    s = data["seriess"][0]
    return {"id": s.get("id", series_id), "title": s.get("title", ""),
            "units": s.get("units_short") or s.get("units", ""),
            "frequency": s.get("frequency", ""), "last_updated": s.get("last_updated", "")}


def _observations(series_id: str, limit: int = 84) -> List[Tuple[str, float]]:
    data = _get("series/observations",
                {"series_id": series_id, "sort_order": "desc", "limit": limit})
    out: List[Tuple[str, float]] = []
    for o in (data or {}).get("observations", []) or []:
        v = o.get("value", ".")
        if v in (".", "", None):
            continue
        try:
            out.append((o.get("date", ""), float(v)))
        except Exception:
            continue
    return out  # newest-first


def _nearest_n_months_back(obs: List[Tuple[str, float]], months: int) -> Optional[Tuple[str, float]]:
    """obs is newest-first. Find the observation closest to `months` before the
    latest date (within a 2-month tolerance)."""
    if not obs:
        return None
    try:
        latest = datetime.strptime(obs[0][0], "%Y-%m-%d")
    except Exception:
        return None
    target_ord = (latest.year * 12 + latest.month) - months
    best = None
    best_gap = 99
    for d, v in obs:
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except Exception:
            continue
        gap = abs((dt.year * 12 + dt.month) - target_ord)
        if gap < best_gap:
            best_gap, best = gap, (d, v)
    return best if best and best_gap <= 2 else None


def fetch_fred(series_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a FRED series as a cited indicator. Returns None if FRED_API_KEY is
    unset, the series id is invalid, or the network call fails (caller degrades)."""
    meta = _series_meta(series_id)
    if not meta:
        return None
    obs = _observations(series_id)
    if not obs:
        return None
    latest_date, latest_val = obs[0]

    g1 = _nearest_n_months_back(obs, 12)
    g5 = _nearest_n_months_back(obs, 60)
    growth_1y = round((latest_val / g1[1] - 1.0) * 100.0, 2) if (g1 and g1[1] > 0) else None
    growth_5y = (round(((latest_val / g5[1]) ** (1.0 / 5.0) - 1.0) * 100.0, 2)
                 if (g5 and g5[1] > 0) else None)

    title = meta["title"]
    units = meta["units"]
    return {
        "series_id": meta["id"],
        "title": title,                 # verbatim FRED title — the citation quote
        "value": latest_val,
        "date": latest_date,            # observation date = publication date for this datum
        "publication_date": latest_date,
        "units": units,
        "frequency": meta["frequency"],
        "growth_1y_pct": growth_1y,
        "growth_5y_cagr_pct": growth_5y,
        "source": "FRED series %s" % meta["id"],
        "url": SERIES_PAGE.format(sid=meta["id"]),   # public page, NO api key
        "quote": title,                              # strictly verbatim (FRED metadata)
        "reading": "%.2f %s as of %s" % (latest_val, units, latest_date),
        "quote_verified": bool(title),
        "last_updated": meta["last_updated"],
        "access_date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def verify_title(series_id: str, claimed_title: str) -> Optional[bool]:
    """Independent check used by the quote-verifier: re-fetch the series metadata
    and confirm the claimed title matches FRED verbatim. None if unreachable."""
    meta = _series_meta(series_id)
    if not meta:
        return None

    def norm(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return norm(claimed_title) == norm(meta["title"]) and bool(meta["title"])


if __name__ == "__main__":
    import sys, json
    sid = sys.argv[1] if len(sys.argv) > 1 else "IPG3344S"
    r = fetch_fred(sid)
    print(json.dumps(r, indent=2) if r else "no data (key set? valid series?)")
