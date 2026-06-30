# Sollia Universal API Priority List — FINAL (2026-06-26)
Empirically validated (Part 1). Headers: `User-Agent: "Sollia contact@sollia.app"` everywhere; add `Host: data.sec.gov` for `data.sec.gov`. Rate limit 10 req/s. **No dead sources** (Last10K, FMP, StockAnalysis, Seeking Alpha, Motley Fool, TipRanks, BusinessWire, PR Newswire, CNN, Reuters, Benzinga) appear in code.

### Resolution model (all document types)
1. **`https://data.sec.gov/submissions/CIK{CIK10}.json`** — authoritative list of forms/accessions/`primaryDocument`/dates. *Reliability 3/3, ~200ms.*
2. **`https://www.sec.gov/Archives/edgar/data/{CIK}/{ACC_nodash}/index.json`** — enumerate **all** documents in the accession (REQUIRED to find the real narrative/exhibit, not the iXBRL cover). *3/3, ~65ms.*
3. **`https://www.sec.gov/Archives/edgar/data/{CIK}/{ACC_nodash}/{doc}.htm`** — the document text (clean: strip tags/XBRL; ~16-20% text yield). *3/3, ~130ms.*

---

### 10-K
1. submissions JSON → Archives `.htm` (primary). Section regex: `Item\s+1A` (risk), `Item\s+3\.\s+Legal\s+Proceedings.*?Item\s+4`, `Item\s+7` (MD&A).
2. **Fallback (discovery):** `browse-edgar?action=getcompany&CIK={CIK}&type=10-K&output=atom` (3/3, ~170ms).
3. **Last resort:** efts full-text (slow 3-4s, 500s on punctuation).
> Pre-2016 10-Ks: `primaryDocument` may be a `.txt` — fall back to the accession's largest `.htm` via `index.json`.

### 10-Q
1. submissions JSON → Archives `.htm`. Sections: `PART\s+II.*?Item\s+1\.\s+Legal\s+Proceedings.*?Item\s+2`, `Item\s+1A`.
2. Fallback: browse-edgar atom (`type=10-Q`).

### 8-K  *(corrected this pass)*
1. submissions JSON → **`index.json`** → fetch **body + EX-99 exhibits** (skip `R\d+.htm` XBRL renders & `-index.htm`). **Do NOT use `primaryDocument` alone** — it is the iXBRL cover stub with no event text. Cite each quote to the exact exhibit doc that contains it.
2. Fallback: browse-edgar atom (`type=8-K`).
3. Last resort: efts full-text (only distinctive ≥8-word phrases; strip `$`/`,`).

### DEF 14A
1. submissions JSON → Archives `.htm` (`type=DEF 14A`). Prefer **DEF 14A** over **PRE 14A** (definitive vs preliminary).
2. Fallback: browse-edgar atom.

### Earnings Call Transcripts
1. **SEC 8-K → EX-99.1** (Item 2.02): submissions JSON → `index.json` → EX-99.1 → strip HTML. (Same exhibit-resolution path the 8-K fix now uses.)
2. Fallback: SEC Atom feed filtered for Item 2.02.
3. Backup: Nasdaq.com earnings page (best-effort; structure-fragile).
> Forbidden (dead): Seeking Alpha, Motley Fool — never used.

---
### Known failure modes
- `data.sec.gov` without `Host` header → intermittent failures. Always send it.
- 8-K `primaryDocument` = iXBRL cover (3-9KB `us-gaap:` soup) → **always** resolve via `index.json`.
- efts: HTTP 500 on phrases with `,` `$` or dates → sanitize; generic phrases match 10k+ → only use distinctive long phrases.
- Foreign issuers file **20-F** (not 10-K) — currently unsupported (see `EDGE_CASE_TEST_MATRIX.md`).

---

## Agent 5 — Market & Economic Data Sources

These sources supplement the SEC filing APIs for Agent 5 (Constraint Auditor). All are FREE, machine-readable (CSV or structured PDF/XML tables), and from government or established non-profit sources. **No paywalls, no chart-only reports.** The machine-readable registry is `sollia_working/src/data/sector_sources.py` (slug, endpoint, format, cadence, `date_location`, `requires_key`, `status`). Every number Agent 5 uses must carry a **publication date** — the `date_location` column says where to read it (PART 2c). The publication date is the crossover clock-start, never the access date.

| # | Source | Endpoint / Format | Cadence | Used for | Date location | Provisioning |
|--:|--------|-------------------|:-------:|----------|---------------|--------------|
| 1 | **USGS Mineral Commodity Summaries** | `pubs.usgs.gov/periodicals/mcs{YEAR}/mcs{YEAR}-{commodity}.pdf` (PDF tables) | annual (Jan) | Production, apparent consumption, 5-yr growth for most minerals | "January {YEAR}" cover → Jan 1 | needs PDF table parser |
| 2 | **FRED** | `api.stlouisfed.org/fred/series/observations` (JSON) | monthly | IP indices, commodity prices | `observation_date` | `FRED_API_KEY` |
| 3 | **EIA** | `api.eia.gov/v2/seriesid/{ID}` (JSON) | monthly | Energy, uranium, natural gas, coal | `period` field | `EIA_API_KEY` |
| 4 | **USA Trade Online** | Census intltrade API / CSV download | monthly | Import/export by HS code (net-imports supply proxy) | data month → 1st of month | `CENSUS_API_KEY` (or CSV) |
| 5 | **USITC DataWeb** | `dataweb.usitc.gov` CSV | monthly | Trade data (aerospace materials) | month column | session token |
| 6 | **USDA** | `usda.gov/oce/commodity/projections` CSV/PDF | annual | Fertilizer use, ag projections to 2035 | cover / first CSV row | needs PDF parser |
| 7 | **FDA Orange Book** | structured XML/zip | annual | Drug approvals, API sources | edition/file date | live |
| 8 | **FDA DMF listings** | structured text/CSV | quarterly | Active drug master files (API/biotech capacity) | file date | live |
| 9 | **DLA Strategic Materials** | PDF (annual) | annual | National Defense Stockpile inventories | cover / FY header | needs PDF parser |
| 10 | **WSTS** | wsts.org press release (HTML) | quarterly | Semiconductor market forecast (industry CAGR) | text / `<time>` tag | live |
| 11 | **IEA Reports** | iea.org PDF tables / free summary | annual/qtr | EV, renewables, energy forecasts (industry CAGR) | report cover | needs PDF parser |
| 12 | **WSA Short Range Outlook** | worldsteel.org PDF / press release | semi-annual | Steel demand forecast (industry CAGR) | press-release date | live |
| 13 | **IAI Statistics** | international-aluminium.org web tables | monthly | Primary aluminium production | month column | live |
| 14 | **ACC** | americanchemistry.com press release | monthly/qtr | Chemical industry outlook (industry CAGR) | press-release date | live |
| 15 | **PCA** | cement.org press release | quarterly | Cement consumption forecast (industry CAGR) | press-release date | live |
| 16 | **EvaluatePharma** | evaluate.com free exec summary | annual | Pharma/biotech market growth (industry CAGR) | report date | live |
| 17 | **AIA Industry Outlook** | aia-aerospace.org free exec summary | quarterly | Aerospace industry outlook (industry CAGR) | report date | live |
| 18 | **DOE HALEU reports** | energy.gov/ne PDF | semi-annual | HALEU nuclear-fuel availability | cover date | needs PDF parser |

**Caching:** `.cache/market_data/{usgs,fred,eia,usa_trade,industry_bodies}/` (PART 7.5b). USGS re-fetched only in a later year past January; EIA/FRED/USA-Trade re-fetched if >30 days old; industry bodies trust cache (manual bust). Common config: `User-Agent: "Sollia contact@sollia.app"`, rate limit 10 req/s, temperature 0 (the lone LLM call is sector classification).

### Fallback hierarchy (PART 7.6b)
- **Supply/Demand:** USGS MCS → USGS Minerals Yearbook → USA Trade net-imports proxy → FRED sector IP index → flag `data_unavailable` (skip material, never fabricate).
- **Industry CAGR:** industry-body report → recent financial-news headline → management's cited source (Gartner/IDC) → flag `unavailable` (use management CAGR alone).
- **Management CAGR:** most-recent earnings call → previous call → 10-K MD&A (Item 7) → flag `unavailable`.

> A material with no datable number is **unusable for the crossover math** — still reported (number + sentence) for reference, but excluded from the timeline (PART 7.6c).
