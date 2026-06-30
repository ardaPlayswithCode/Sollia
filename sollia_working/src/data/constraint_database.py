"""
Sollia Agent 5 — Constraint Auditor: static SECTOR_DATABASE (PART 1).

Maps each of the 14 supported resource-intensive sectors to:
  * its constraining raw materials (with the USGS commodity name, unit, HS code,
    FRED/EIA series where applicable, and the supply source to use when a material
    is NOT tracked by USGS),
  * the raw-material supply/demand data sources,
  * the forward-CAGR source (the industry body whose forecast pressure-tests the
    management number).

This is a STATIC library — no network, no LLM. The sector a ticker maps to is
decided by the one classification LLM call in constraint_auditor.py; this module
only answers "given a sector, what do we audit and where do we read it".

Schema (per material)::

    {
      "name":           human-readable material name (verbatim in output),
      "usgs_commodity": USGS Mineral Commodity Summaries chapter, or None,
      "unit":           canonical unit for this material across all sources,
      "hs_code":        Harmonized System code for USA Trade / USITC, or None,
      "fred_series":    FRED series id (price/IP index), or None,
      "eia_series":     EIA series id, or None,
      "supply_source":  which registry source is PRIMARY for supply when USGS is
                        absent ("usgs" | "usa_trade" | "eia" | "usitc" | "doe" | "fda"),
      "notes":          why this material constrains the sector,
    }

The SUPPORTED_SECTORS ordering below is IDENTICAL to the classifier prompt's list
(constraint_auditor._classify_sector) — do not reorder without updating both.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Any


# Canonical sector order — MUST match the classification prompt exactly.
SUPPORTED_SECTORS: List[str] = [
    "Advanced Semiconductor Manufacturing",
    "Automotive & EV Manufacturing",
    "EV Battery & Charging Infrastructure",
    "Renewable Energy Equipment",
    "Aerospace & Defense Manufacturing",
    "Steel Manufacturing",
    "Aluminum Production",
    "Nuclear Energy & Fuel",
    "Chemicals — Commodity & Base",
    "Chemicals — Specialty & Advanced Materials",
    "Agricultural Inputs — Fertilizers",
    "Cement & Concrete",
    "Pharmaceutical Manufacturing — Generics & API",
    "Biotech Manufacturing & CDMOs",
]


def _m(name: str, *, usgs: Optional[str] = None, unit: str = "metric tons",
       hs: Optional[str] = None, fred: Optional[str] = None,
       eia: Optional[str] = None, supply_source: str = "usgs",
       notes: str = "") -> Dict[str, Any]:
    """Compact material constructor; keeps the big table below readable."""
    return {
        "name": name, "usgs_commodity": usgs, "unit": unit, "hs_code": hs,
        "fred_series": fred, "eia_series": eia, "supply_source": supply_source,
        "notes": notes,
    }


SECTOR_DATABASE: Dict[str, Dict[str, Any]] = {
    # ───────────────────────────────────────────────────────────────────────
    "Advanced Semiconductor Manufacturing": {
        "fred_sector_index": "IPG3344S",  # Industrial Production: Semiconductor & Other Electronic Component
        "materials": [
            _m("High-purity quartz", usgs="Silicon", hs="2506.10",
               notes="Crucibles for silicon ingots"),
            _m("Neon gas", unit="million liters", hs="2804.29", supply_source="usa_trade",
               notes="Lithography lasers; not USGS-tracked — net imports proxy"),
            _m("Gallium", usgs="Gallium", hs="8112.92",
               notes="Compound semiconductors (GaN, GaAs)"),
            _m("Germanium", usgs="Germanium", hs="8112.39",
               notes="Fiber optics, IR optics"),
            _m("Hafnium", usgs="Hafnium", hs="8112.99",
               notes="High-k gate dielectrics"),
            _m("Cobalt", usgs="Cobalt", hs="8105.20",
               notes="Interconnects, barrier layers"),
        ],
        "raw_material_sources": {
            "supply": ["USGS Mineral Commodity Summaries — annual PDF",
                       "USA Trade Online — monthly HS imports"],
            "demand": ["USGS apparent consumption — annual PDF"],
        },
        "cagr_sources": {
            "industry_body": "WSTS Semiconductor Market Forecast",
            "industry_body_url": "https://www.wsts.org/",
            "industry_body_frequency": "quarterly",
        },
    },
    # ───────────────────────────────────────────────────────────────────────
    "Automotive & EV Manufacturing": {
        "fred_sector_index": "IPG3361T3S",  # Industrial Production: Motor Vehicles & Parts
        "materials": [
            _m("Lithium (battery-grade)", usgs="Lithium", unit="metric tons LCE",
               hs="2836.91", notes="Battery cathode material"),
            _m("Cobalt", usgs="Cobalt", hs="8105.20",
               notes="Battery cathode; DRC supply concentration risk"),
            _m("Nickel (Class 1 battery-grade)", usgs="Nickel", hs="7501.10",
               notes="Battery cathode; Class 1 shortage"),
            _m("Rare Earths (Nd, Dy)", usgs="Rare Earths", hs="2805.30",
               notes="EV traction-motor magnets"),
            _m("Copper", usgs="Copper", hs="7403.11", fred="PCU3273273",
               notes="Wiring, motors, busbars — ~4x more per EV vs ICE"),
        ],
        "raw_material_sources": {
            "supply": ["USGS Mineral Commodity Summaries — annual PDF",
                       "USA Trade Online — monthly HS imports",
                       "FRED copper/nickel prices — monthly CSV"],
            "demand": ["USGS apparent consumption — annual PDF",
                       "IEA Global EV Outlook — annual"],
        },
        "cagr_sources": {
            "industry_body": "IEA Global EV Outlook",
            "industry_body_url": "https://www.iea.org/reports/global-ev-outlook",
            "industry_body_frequency": "annual",
        },
    },
    # ───────────────────────────────────────────────────────────────────────
    "EV Battery & Charging Infrastructure": {
        "fred_sector_index": None,
        "materials": [
            _m("Graphite (natural & synthetic)", usgs="Graphite",
               notes="Anode active material"),
            _m("Lithium (battery-grade)", usgs="Lithium", unit="metric tons LCE",
               hs="2836.91", notes="Cathode material"),
            _m("Cobalt", usgs="Cobalt", hs="8105.20", notes="Cathode material"),
            _m("Nickel (Class 1 battery-grade)", usgs="Nickel", hs="7501.10",
               notes="High-nickel cathodes"),
            _m("Manganese", usgs="Manganese", notes="LMO/NMC cathodes"),
            _m("Copper", usgs="Copper", hs="7403.11",
               notes="Current collectors, charging infrastructure"),
        ],
        "raw_material_sources": {
            "supply": ["USGS Mineral Commodity Summaries — annual PDF",
                       "USA Trade Online — monthly HS imports"],
            "demand": ["USGS apparent consumption — annual PDF",
                       "EIA electricity demand — monthly CSV"],
        },
        "cagr_sources": {
            "industry_body": "IEA Global EV Outlook",
            "industry_body_url": "https://www.iea.org/reports/global-ev-outlook",
            "industry_body_frequency": "annual",
        },
    },
    # ───────────────────────────────────────────────────────────────────────
    "Renewable Energy Equipment": {
        "fred_sector_index": None,
        "materials": [
            _m("Polysilicon", usgs="Silicon", notes="Solar PV wafers"),
            _m("Silver", usgs="Silver", notes="PV cell metallization paste"),
            _m("Rare Earths (Nd, Pr, Dy)", usgs="Rare Earths", hs="2805.30",
               notes="Wind-turbine permanent magnets"),
            _m("Copper", usgs="Copper", hs="7403.11", fred="PCU3273273",
               notes="Wiring, transformers, grid interconnect"),
            _m("Electrical steel", unit="metric tons", fred="WPU101707",
               supply_source="usa_trade",
               notes="Grain-oriented steel for transformers; FRED price proxy"),
        ],
        "raw_material_sources": {
            "supply": ["USGS Mineral Commodity Summaries — annual PDF",
                       "USA Trade Online — monthly HS imports",
                       "FRED copper/silver/steel prices — monthly CSV"],
            "demand": ["USGS apparent consumption — annual PDF",
                       "EIA renewable capacity — monthly CSV"],
        },
        "cagr_sources": {
            "industry_body": "IEA Renewables Market Update",
            "industry_body_url": "https://www.iea.org/reports/renewables-2025",
            "industry_body_frequency": "quarterly",
        },
    },
    # ───────────────────────────────────────────────────────────────────────
    "Aerospace & Defense Manufacturing": {
        "fred_sector_index": "IPG3364S",  # Industrial Production: Aerospace & Misc Transportation
        "materials": [
            _m("Titanium (sponge)", usgs="Titanium", notes="Airframes, engine fan blades"),
            _m("Nickel superalloys", usgs="Nickel", hs="7501.10",
               notes="Hot-section turbine components"),
            _m("Carbon fiber (PAN precursor)", unit="metric tons", supply_source="usitc",
               notes="Composite airframes; not USGS-tracked — USITC trade data"),
            _m("Rhenium", usgs="Rhenium", notes="Single-crystal turbine blades"),
            _m("Hafnium", usgs="Hafnium", hs="8112.99", notes="Superalloy additive"),
        ],
        "raw_material_sources": {
            "supply": ["USGS Mineral Commodity Summaries — annual PDF",
                       "USITC DataWeb — monthly HS imports",
                       "DLA Strategic Materials stockpile report — annual PDF"],
            "demand": ["USGS apparent consumption — annual PDF"],
        },
        "cagr_sources": {
            "industry_body": "AIA Aerospace Industry Outlook",
            "industry_body_url": "https://www.aia-aerospace.org/",
            "industry_body_frequency": "quarterly",
        },
    },
    # ───────────────────────────────────────────────────────────────────────
    "Steel Manufacturing": {
        "fred_sector_index": "IPN331S",  # Industrial Production: Primary Metals (proxy)
        "materials": [
            _m("Graphite electrodes", usgs="Graphite", notes="EAF electrodes"),
            _m("Metallurgical coal", unit="metric tons", supply_source="eia",
               eia="COAL.PRODUCTION.TOT-US.A", notes="Coking coal for BF; EIA coal data"),
            _m("Ferromanganese", usgs="Manganese", notes="Deoxidizer / alloying"),
            _m("Ferrovanadium", usgs="Vanadium", notes="HSLA strengthening"),
            _m("Niobium", usgs="Niobium", notes="Microalloying (HSLA)"),
            _m("Iron ore", usgs="Iron Ore", notes="Primary feedstock (BF/BOF)"),
        ],
        "raw_material_sources": {
            "supply": ["USGS Mineral Commodity Summaries — annual PDF",
                       "FRED steel production & prices — monthly CSV",
                       "EIA coal production — monthly CSV"],
            "demand": ["USGS apparent consumption — annual PDF"],
        },
        "cagr_sources": {
            "industry_body": "World Steel Association Short Range Outlook",
            "industry_body_url": "https://worldsteel.org/data/short-range-outlook/",
            "industry_body_frequency": "semi-annual",
        },
    },
    # ───────────────────────────────────────────────────────────────────────
    "Aluminum Production": {
        "fred_sector_index": None,
        "materials": [
            _m("Alumina", usgs="Bauxite and Alumina", notes="Smelter feedstock"),
            _m("Petroleum coke", unit="metric tons", supply_source="eia",
               eia="PET.MCRRIUS1.M", notes="Calcined anode coke; EIA petroleum data"),
            _m("Coal tar pitch", unit="metric tons", supply_source="usa_trade",
               notes="Anode binder; not USGS-tracked — trade data"),
            _m("Electricity", unit="gigawatt-hours", supply_source="eia",
               eia="ELEC.GEN.ALL-US-99.M", notes="~14 MWh/t Al — power is the binding input"),
        ],
        "raw_material_sources": {
            "supply": ["USGS Mineral Commodity Summaries (Bauxite & Alumina) — annual PDF",
                       "EIA petroleum coke & electricity prices — monthly CSV",
                       "FRED aluminum prices — monthly CSV"],
            "demand": ["USGS apparent consumption — annual PDF"],
        },
        "cagr_sources": {
            "industry_body": "IAI (International Aluminium Institute) Statistics",
            "industry_body_url": "https://international-aluminium.org/statistics/",
            "industry_body_frequency": "monthly",
        },
    },
    # ───────────────────────────────────────────────────────────────────────
    "Nuclear Energy & Fuel": {
        "fred_sector_index": None,
        "materials": [
            _m("Uranium (U3O8)", usgs="Uranium", unit="metric tons U3O8",
               eia="NUC_STATUS.U3O8", supply_source="eia",
               notes="Reactor fuel feed; EIA uranium marketing data"),
            _m("Zirconium", usgs="Zirconium", notes="Fuel cladding (zircaloy)"),
            _m("Hafnium", usgs="Hafnium", hs="8112.99", notes="Control rods"),
            _m("HALEU", unit="metric tons", supply_source="doe",
               notes="High-Assay Low-Enriched Uranium for advanced reactors; DOE reports"),
        ],
        "raw_material_sources": {
            "supply": ["EIA uranium & nuclear generation — monthly/annual CSV",
                       "USGS Mineral Commodity Summaries — annual PDF",
                       "DOE HALEU availability reports — semi-annual PDF"],
            "demand": ["EIA nuclear generation & fuel demand — annual CSV"],
        },
        "cagr_sources": {
            "industry_body": "EIA Annual Energy Outlook",
            "industry_body_url": "https://www.eia.gov/outlooks/aeo/",
            "industry_body_frequency": "annual",
        },
    },
    # ───────────────────────────────────────────────────────────────────────
    "Chemicals — Commodity & Base": {
        "fred_sector_index": "IPG325S",  # Industrial Production: Chemicals
        "materials": [
            _m("Natural gas", unit="billion cubic feet", supply_source="eia",
               eia="NG.N9050US2.M", notes="Feedstock + energy for crackers/ammonia"),
            _m("Ethane", unit="thousand barrels/day", supply_source="eia",
               eia="PET.METFPUS2.M", notes="Ethylene cracker feedstock"),
            _m("Phosphate rock", usgs="Phosphate Rock", notes="Phosphoric acid"),
            _m("Sulfur", usgs="Sulfur", notes="Sulfuric acid"),
            _m("Industrial salt", usgs="Salt", notes="Chlor-alkali feedstock"),
        ],
        "raw_material_sources": {
            "supply": ["EIA natural gas & ethane — monthly CSV",
                       "USGS Mineral Commodity Summaries — annual PDF",
                       "FRED chemical IP index — monthly CSV"],
            "demand": ["USGS apparent consumption — annual PDF"],
        },
        "cagr_sources": {
            "industry_body": "ACC Chemical Activity Barometer",
            "industry_body_url": "https://www.americanchemistry.com/chemistry-in-america/data-industry-statistics/resources/chemical-activity-barometer",
            "industry_body_frequency": "monthly",
        },
    },
    # ───────────────────────────────────────────────────────────────────────
    "Chemicals — Specialty & Advanced Materials": {
        "fred_sector_index": "IPG325S",
        "materials": [
            _m("Silicon metal", usgs="Silicon", notes="Silicones, polysilicon precursor"),
            _m("Fluorspar", usgs="Fluorspar", notes="HF / fluorochemicals"),
            _m("Titanium dioxide feedstocks", usgs="Titanium", notes="TiO2 pigment / coatings"),
            _m("Rare earths (catalysts)", usgs="Rare Earths", hs="2805.30",
               notes="FCC and auto catalysts"),
            _m("Helium", usgs="Helium", unit="million cubic meters",
               notes="Electronics, cryogenics, leak detection"),
        ],
        "raw_material_sources": {
            "supply": ["USGS Mineral Commodity Summaries — annual PDF",
                       "FRED chemical prices — monthly CSV",
                       "USA Trade Online — monthly HS imports"],
            "demand": ["USGS apparent consumption — annual PDF"],
        },
        "cagr_sources": {
            "industry_body": "ACC Specialty Chemicals Outlook",
            "industry_body_url": "https://www.americanchemistry.com/",
            "industry_body_frequency": "quarterly",
        },
    },
    # ───────────────────────────────────────────────────────────────────────
    "Agricultural Inputs — Fertilizers": {
        "fred_sector_index": "PCU325311325311",  # PPI: Nitrogenous Fertilizer Manufacturing
        "materials": [
            _m("Natural gas", unit="billion cubic feet", supply_source="eia",
               eia="NG.N9050US2.M", notes="Ammonia synthesis feedstock (~70-90% of N cost)"),
            _m("Phosphate rock", usgs="Phosphate Rock", notes="DAP/MAP feedstock"),
            _m("Potash", usgs="Potash", notes="K fertilizer"),
            _m("Sulfur", usgs="Sulfur", notes="Sulfuric acid for phosphate processing"),
        ],
        "raw_material_sources": {
            "supply": ["USGS Mineral Commodity Summaries — annual PDF",
                       "EIA natural gas prices — monthly CSV",
                       "USDA fertilizer use data — annual CSV"],
            "demand": ["USGS apparent consumption — annual PDF",
                       "USDA fertilizer use — annual CSV"],
        },
        "cagr_sources": {
            "industry_body": "USDA Agricultural Projections to 2035",
            "industry_body_url": "https://www.usda.gov/oce/commodity/projections",
            "industry_body_frequency": "annual",
        },
    },
    # ───────────────────────────────────────────────────────────────────────
    "Cement & Concrete": {
        "fred_sector_index": "IPN3273S",  # Industrial Production: Cement & Concrete Product Mfg
        "materials": [
            _m("Cement (clinker)", usgs="Cement", notes="Primary binder"),
            _m("Fly ash", unit="metric tons", supply_source="eia",
               eia="COAL.CONSUMPTION.TOT-US.A",
               notes="SCM; coal-combustion byproduct — EIA coal burn proxy"),
            _m("Ground granulated blast-furnace slag", usgs="Iron and Steel Slag",
               notes="Supplementary cementitious material"),
            _m("Limestone (crushed stone)", usgs="Stone, Crushed", notes="Aggregate + clinker feed"),
            _m("Natural sand", usgs="Sand and Gravel, Construction", notes="Fine aggregate"),
        ],
        "raw_material_sources": {
            "supply": ["USGS Mineral Commodity Summaries — annual PDF",
                       "EIA coal production (fly-ash proxy) — monthly CSV"],
            "demand": ["USGS apparent consumption — annual PDF"],
        },
        "cagr_sources": {
            "industry_body": "PCA (Portland Cement Association) Cement Forecast",
            "industry_body_url": "https://www.cement.org/",
            "industry_body_frequency": "quarterly",
        },
    },
    # ───────────────────────────────────────────────────────────────────────
    "Pharmaceutical Manufacturing — Generics & API": {
        "fred_sector_index": "IPG3254S",  # Industrial Production: Pharmaceutical & Medicine
        "materials": [
            _m("API starting materials", unit="metric tons", hs="2941.90",
               supply_source="usa_trade", notes="Key intermediates; import-dependent"),
            _m("Acetonitrile", unit="metric tons", hs="2926.90", supply_source="usa_trade",
               notes="HPLC/synthesis solvent; acrylonitrile byproduct, supply-shock prone"),
            _m("Penicillin precursors (6-APA)", unit="metric tons", hs="2941.10",
               supply_source="usa_trade", notes="Beta-lactam fermentation feedstock"),
            _m("Iodine", usgs="Iodine", notes="Contrast media, antiseptics, synthesis"),
        ],
        "raw_material_sources": {
            "supply": ["USA Trade Online — monthly HS imports",
                       "USGS Mineral Commodity Summaries (Iodine) — annual PDF",
                       "FDA Orange Book — annual XML"],
            "demand": ["USA Trade apparent consumption (production+imports-exports)"],
        },
        "cagr_sources": {
            "industry_body": "EvaluatePharma World Preview",
            "industry_body_url": "https://www.evaluate.com/thought-leadership/pharma/",
            "industry_body_frequency": "annual",
        },
    },
    # ───────────────────────────────────────────────────────────────────────
    "Biotech Manufacturing & CDMOs": {
        "fred_sector_index": None,
        "materials": [
            _m("Chromatography resins (Protein A)", unit="liters", hs="3824.99",
               supply_source="usa_trade", notes="mAb downstream purification; capacity-bound"),
            _m("Single-use bioreactor films", unit="square meters", hs="3920.99",
               supply_source="usa_trade", notes="SUB bags/films; multilayer-film bottleneck"),
            _m("Cell culture media", unit="metric tons", hs="3821.00",
               supply_source="usa_trade", notes="Upstream growth media"),
            _m("Plasmid DNA", unit="grams", supply_source="fda",
               notes="Cell & gene therapy starting material; FDA DMF capacity"),
        ],
        "raw_material_sources": {
            "supply": ["USA Trade Online — monthly HS imports",
                       "FDA Drug Master File (DMF) listings — structured text",
                       "SEC EDGAR supplier filings — quarterly"],
            "demand": ["USA Trade apparent consumption (production+imports-exports)"],
        },
        "cagr_sources": {
            "industry_body": "EvaluatePharma World Preview",
            "industry_body_url": "https://www.evaluate.com/thought-leadership/pharma/",
            "industry_body_frequency": "annual",
        },
    },
}


# ── public accessors ───────────────────────────────────────────────────────

def get_sector(sector_name: str) -> Optional[Dict[str, Any]]:
    """Return the database entry for a sector name (exact match), or None."""
    return SECTOR_DATABASE.get(sector_name)


def list_sectors() -> List[str]:
    """The canonical sector list (same order as the classifier prompt)."""
    return list(SUPPORTED_SECTORS)


def materials_for(sector_name: str) -> List[Dict[str, Any]]:
    entry = SECTOR_DATABASE.get(sector_name)
    return list(entry["materials"]) if entry else []


def cagr_source_for(sector_name: str) -> Optional[Dict[str, Any]]:
    entry = SECTOR_DATABASE.get(sector_name)
    return entry["cagr_sources"] if entry else None


def _self_check() -> None:
    """Invariant: every sector in SUPPORTED_SECTORS has a complete DB entry, and
    vice-versa. Run via `python -m src.data.constraint_database`."""
    assert set(SUPPORTED_SECTORS) == set(SECTOR_DATABASE), (
        "SUPPORTED_SECTORS and SECTOR_DATABASE keys diverge: "
        f"{set(SUPPORTED_SECTORS) ^ set(SECTOR_DATABASE)}"
    )
    for name, entry in SECTOR_DATABASE.items():
        assert entry.get("materials"), f"{name}: no materials"
        assert entry.get("cagr_sources", {}).get("industry_body"), f"{name}: no CAGR source"
        for mat in entry["materials"]:
            assert mat.get("name"), f"{name}: material missing name"
            assert mat.get("unit"), f"{name}: {mat.get('name')} missing unit"
            # A material must have *some* supply route: USGS commodity, HS code, or EIA series.
            assert mat.get("usgs_commodity") or mat.get("hs_code") or mat.get("eia_series") \
                or mat.get("supply_source") in ("doe", "fda", "usitc", "usa_trade"), \
                f"{name}: {mat['name']} has no supply route"
    n_mats = sum(len(e["materials"]) for e in SECTOR_DATABASE.values())
    print(f"OK: {len(SECTOR_DATABASE)} sectors, {n_mats} materials, all invariants hold.")


if __name__ == "__main__":
    _self_check()
