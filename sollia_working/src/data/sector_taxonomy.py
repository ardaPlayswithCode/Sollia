"""Universal Sector Taxonomy — the single source of truth for sector
classification across ALL market-style Sollia agents (Agent 6 Moat Auditor and
any future agent that needs a coarse, exhaustive sector grouping).

This is DISTINCT from Agent 5's constraint sectors (`constraint_database.
SUPPORTED_SECTORS`): those 14 are a curated set of *resource-intensive* sectors
for which Sollia models physical supply/demand. The 55 sectors here are a
general-purpose business taxonomy used to (a) classify a company and (b) validate
LLM-suggested competitors (a competitor in the same sector — or with a stated
cross-sector overlap — is plausible; one in an unrelated sector is a likely
hallucination). It is a VALIDATION FILTER, not the competitor source.

Determinism: the list and the matcher are pure data + pure functions. Sector
classification is one cached LLM call per ticker in the agent layer.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# ── The 55 sectors, grouped (the grouping is for humans/UI; agents use SECTORS) ──
SECTOR_GROUPS: Dict[str, List[str]] = {
    "Technology (Hardware)": [
        "Advanced Semiconductor Manufacturing",
        "Hyperscale AI Infrastructure",
        "Networking & Telecom Equipment",
        "Consumer Electronics",
        "Enterprise Hardware & Storage",
    ],
    "Technology (Software & Services)": [
        "Enterprise Software / SaaS",
        "Cybersecurity",
        "Cloud Infrastructure & Platform",
        "Fintech & Payments",
        "AI & ML Software",
        "Gaming & Interactive Media",
    ],
    "Industrials & Manufacturing": [
        "Aerospace & Defense",
        "Industrial Manufacturing",
        "Automotive & EV",
        "Logistics & Supply Chain",
        "Building Materials & Construction",
    ],
    "Energy & Resources": [
        "Oil & Gas (Upstream)",
        "Oil & Gas (Midstream & Services)",
        "Renewable Energy & Clean Tech",
        "Utilities & Power Generation",
        "Metals & Mining",
        "Chemicals",
    ],
    "Healthcare": [
        "Biotech (Therapeutics)",
        "Pharmaceuticals (Large Cap)",
        "Medical Devices & Diagnostics",
        "Healthcare Services & Managed Care",
        "Life Sciences Tools & Services",
    ],
    "Financial Services": [
        "Banking (Large Cap)",
        "Banking (Regional)",
        "Asset Management & Wealth",
        "Private Equity & Alternative Asset Managers",
        "Insurance (P&C)",
        "Capital Markets & Exchanges",
        "Mortgage & Specialty Finance",
    ],
    "Consumer": [
        "Retail (Brick & Mortar)",
        "E-Commerce",
        "Restaurants & Food Service",
        "Consumer Packaged Goods",
        "Apparel & Luxury",
        "Travel & Hospitality",
        "Media & Entertainment",
        "Alcoholic Beverages",
        "Tobacco & Cannabis",
    ],
    "Real Estate": [
        "Data Center REITs",
        "Residential REITs",
        "Commercial & Office REITs",
        "Industrial & Logistics REITs",
        "Retail REITs",
        "Healthcare REITs",
    ],
    "Other": [
        "Telecom Services",
        "Agriculture & Ag Tech",
        "Transportation (non-logistics)",
        "Crypto & Blockchain Infrastructure",
        "Education Technology",
        "Space & Satellite",
    ],
}

# Flat, ordered list of all 55 sector names (the canonical surface for agents).
SECTORS: List[str] = [s for group in SECTOR_GROUPS.values() for s in group]

# Map a sector name -> its parent group (used by the cross-sector overlap check).
SECTOR_TO_GROUP: Dict[str, str] = {
    s: g for g, names in SECTOR_GROUPS.items() for s in names
}

assert len(SECTORS) == 55, "Universal taxonomy must have exactly 55 sectors, got %d" % len(SECTORS)


# ── normalization + matching (tolerant of noisy LLM output) ────────────────────
def _norm(s: Optional[str]) -> str:
    """Normalize a sector string for robust comparison: lowercase, '&'->'and',
    drop punctuation/parentheses, collapse hyphens/dashes and whitespace."""
    if not s:
        return ""
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[‘’“”\"'()/\\.,:;–—-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_NORM_SECTORS = {_norm(s): s for s in SECTORS}


def match_sector(raw: Optional[str]) -> Optional[str]:
    """Map a (possibly noisy) string to an exact canonical sector, or None.

    Recognizes the literal "UNSUPPORTED" / "NONE" sentinel (returns None). Tries
    an exact normalized match, then a both-directions substring match so
    "Technology: Cybersecurity" or "the Cybersecurity sector" still resolve. The
    longest canonical name wins on substring ties (so "Banking (Large Cap)" is
    preferred over a bare "Banking" partial)."""
    if not raw:
        return None
    cleaned = raw.strip().strip(".").strip()
    low = cleaned.lower()
    if not cleaned or "unsupported" in low or low in ("none", "n/a", "unknown"):
        return None
    norm = _norm(cleaned)
    if norm in _NORM_SECTORS:
        return _NORM_SECTORS[norm]
    # substring tolerance — pick the longest canonical name that matches
    best = None
    for ns, canonical in _NORM_SECTORS.items():
        if ns in norm or norm in ns:
            if best is None or len(ns) > len(_norm(best)):
                best = canonical
    return best


def sector_list_text(bullet: str = "* ") -> str:
    """Render the 55 sectors as a newline-bulleted list for an LLM prompt."""
    return "\n".join(bullet + s for s in SECTORS)


def same_group(sector_a: Optional[str], sector_b: Optional[str]) -> bool:
    """True if two sectors fall in the same top-level group (a softer notion of
    'same space' than identical sector — e.g. both Technology (Hardware))."""
    if not sector_a or not sector_b:
        return False
    return SECTOR_TO_GROUP.get(sector_a) is not None and \
        SECTOR_TO_GROUP.get(sector_a) == SECTOR_TO_GROUP.get(sector_b)


if __name__ == "__main__":  # quick self-check
    print("%d sectors in %d groups" % (len(SECTORS), len(SECTOR_GROUPS)))
    for probe in ["Cybersecurity", "advanced semiconductor manufacturing",
                  "Technology: AI & ML Software", "Banking (Large Cap)",
                  "e commerce", "UNSUPPORTED", "Defense contractor"]:
        print("  %-45s -> %s" % (probe, match_sector(probe)))
