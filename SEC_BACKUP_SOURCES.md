# SEC EDGAR Backup Sources — Empirical Research & Fallback Chain

**Date:** 2026-07-03 · **Branch:** optimization-pass · **Part 1 of the bulletproofing pass**

The NVDA-run EDGAR outage killed Agent 1 for cold tickers (the breaker
fast-failed correctly, but uncached filings had no second source). This pass
researched every candidate backup empirically and wired the winners into
`sec_edgar.py`.

## 1. Empirical source research (Part 1b)

Method: `scratchpad/sec_source_research.py` — every source probed with
NVDA (mega-cap), TSLA (large), FUBO (small-cap) across recent / 3y / 10y
eras, measuring status, latency, format, and coverage.

| Source | Verdict | Evidence |
|---|---|---|
| `data.sec.gov` submissions API | **PRIMARY (metadata)** | richest schema; the outage host |
| **AWS `s3://sec-datasets/`** | **DOES NOT EXIST** | `NoSuchBucket` on both virtual-host styles; registry.opendata.aws/sec-edgar 404. The task table's entry is wrong — removed from consideration |
| `www.sec.gov` browse-edgar **Atom** | **BEST metadata backup** | 3/3 tickers, complete history in ONE request (NVDA span 1999→2026), ~200-500ms, all form types. No reportDate/primaryDocument (resolved lazily via index.json) |
| `efts.sec.gov` search-index | **LAST-RESORT metadata** | third independent host; correct query shape is `ciks=<padded>&forms=...` with EMPTY `q` — returns `_id = "accession:primary_doc"` (doc names included!), 2001+; **3 intermittent 500s observed in one research run** |
| `www.sec.gov` full-index master.idx | tested, NOT wired | works back to 1994 but 27MB/quarter on the SAME host as Atom — adds weight, not independence |
| bulk `submissions.zip` | tested, NOT wired | exists (1.55GB) — impractical per-run |
| SEC RSS feeds | subsumed by Atom | same cgi endpoint, `output=atom` |
| complete-submission `.txt` | **DOCUMENT backup** | `{accession}.txt` on www.sec.gov holds every exhibit in SGML `<DOCUMENT>` blocks; primary doc extractable; streaming capped at 12MB (stops after the primary block) |
| `data.sec.gov/Archives/...` | dead end | **404** — data.sec.gov does NOT mirror Archives; there is NO free cross-host mirror of EDGAR documents |

## 2. The implemented fallback chain (Part 1c/1d — `sec_edgar.py`)

**Metadata** (`get_all_filings`):
1. Fresh disk cache (<24h; backup-sourced entries always revalidate)
2. `data.sec.gov` submissions API (live)
3. `www.sec.gov` browse-edgar Atom (different host, complete history)
4. `efts.sec.gov` search-index (third host, carries primary-doc names)
5. Stale disk cache (any age)
6. `[]` + `LAST_METADATA_SOURCE[ticker] = "unavailable"` (graceful, flagged)

**Documents** (`fetch_html`):
1. Primary `.htm` on www.sec.gov (+1 retry)
2. Complete-submission `.txt` (same host, different artifact — recovers
   missing/renamed primary docs)
3. Graceful `""` — documents have no free cross-host mirror; a full
   www.sec.gov outage serves documents from `.cache/texts/` only

**Supporting changes:**
- `_find_primary_doc` now uses structured `index.json` first (also serves the
  Atom path, whose metadata carries no primaryDocument).
- The fetch-budget **outage breaker is now PER-HOST** (`fetch_budget.py`):
  previously 5 failures on data+www opened the breaker for efts.sec.gov too —
  fast-failing exactly the backup the outage needed.

## 3. Outage simulation results (Part 1d)

`scratchpad/outage_sim_test.py` — hosts blocked at the HTTPAdapter layer (the
fetch-budget wrapper and breaker run exactly as production; no /etc/hosts
changes, no sudo):

| Scenario | Result |
|---|---|
| Live baseline (DASH, uncached) | **PASS** — 94 filings via `edgar-live` |
| data.sec.gov DOWN (ABNB, uncached) | **PASS** — 67 filings via `atom-backup` (10-K/10-Q/8-K/DEF 14A all present) |
| data+www DOWN (UBER, uncached) | **PASS** — 149 filings via `efts-backup`, 149/149 with primary-doc names |
| ALL sec hosts DOWN (NVDA, cache aged 3 days) | **PASS** — 423 filings via `stale-cache` |
| ALL DOWN, never-cached ticker (PLTR) | **PASS** — graceful `[]`, flagged `unavailable` |
| Wrong primary-doc name (live) | **PASS** — 2.6MB 10-K recovered from `.txt`, MD&A extracted (30k chars) |

**8/8 scenarios pass.**

## 4. Field finding (2026-07-05): backup metadata is reduced-fidelity — consumers must know

During live stress testing, a transient data.sec.gov breaker-open sent FUBO's
metadata through the Atom backup, whose schema carries **no reportDate**. The
backup cache persisted (by design), but Agent 1 selects 10-Ks BY report
period and reads the shared cache file directly — it trusted the
reduced-fidelity file indefinitely and reported "No 10-K found" long after
the primary had recovered.

Fixes:
- Backup-sourced caches carry `_backup_source` and revalidate hourly in
  `get_all_filings` (vs 24h for primary data).
- `corporate_discrepancy._get_all_filings` treats a `_backup_source` cache
  as a MISS and refetches the primary (healing the file); only if that
  refetch fails (genuine outage) does it degrade onto the backup schema.
- Post-test hygiene: any lingering backup-schema caches are healed on the
  next primary fetch (verified: ABNB, FUBO, UBER all restored to full
  schema).

Rule of thumb: **Atom backup = complete history, no reportDate; EFTS backup
= reportDate present, coverage capped (~50/form group)**. Agents keying off
filing_date work on both; period-of-report logic needs the primary.
