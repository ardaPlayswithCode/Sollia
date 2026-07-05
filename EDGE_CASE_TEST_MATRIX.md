# Edge-Case Test Matrix — 15 Tickers × 6 Agents

**Date:** 2026-07-04 (bulletproofing pass) · **Harness:** `.test_results/edge_case_matrix/`
(each ticker = one real `run_selected_agents` call, all 6 agents concurrent,
exactly like the backend). The 2026-06-26 input-handling matrix is preserved
as an appendix below.

## Results

| Ticker | Edge case | Result | Notes |
|--------|-----------|:------:|-------|
| OPEN | Unusual structure — the int.get crash | **6/6** | Agent 2 fixed (type guard); 157s, $0.00 |
| BRK.A | Dual-class, massive filings, dotted ticker | **6/6** | 58 min — filing volume, no errors |
| SNAP | Dual-class, no-vote shares | **6/6** ² | was 5/6 — ex99 URL bug (below) |
| MSTR | Bitcoin treasury balance sheet | **6/6** | $0.067 |
| COIN | Crypto exchange, regulatory risk | **6/6** ² | was 5/6 — same ex99 URL bug |
| SPOT | Luxembourg 20-F filer | **4/6 + 2 clean degradations** ¹ | foreign private issuer — see below |
| BABA | Chinese ADR, VIE, 20-F | **4/6 + 2 clean degradations** ¹ | foreign private issuer — see below |
| IBM | Decades of format changes | **6/6** ² | was 5/6 — same ex99 URL bug |
| GE | Restatements, complex history | **6/6** | 311s |
| RIVN | Pre-profit EV, heavy capex | **6/6** | $0.054 |
| GME | Meme-stock MD&A language | **6/6** | $0.062 |
| CHPT | Small cap, thin filings | **6/6** ² | was 5/6 — same ex99 URL bug |
| FUBO | Very small cap | **6/6** ² | was 5/6 — same ex99 URL bug |
| UBER | Gig-economy risk factors | **6/6** ² | was 5/6 — breaker collateral (below) |
| PLTR | Recent IPO, limited history | **6/6** ² | was 5/6 — breaker collateral |

**Zero process crashes in all 15 runs.** ¹ = by-design scope limit, not a bug.
² = fixed this pass and re-verified individually (see root causes).

## Root causes found & fixed (2026-07-04)

### 1. Malformed `-index.htm` URL in Agent 1's ex99 discovery (the big one)
`corporate_discrepancy._fetch_ex99` built
`.../{acc_nodash}/{acc_nodash}-index.htm` — EDGAR requires the **dashed**
accession in the index filename and answers the malformed form with **503**
(proven live: 503 vs 200 on the same SNAP 8-K). Consequences:
- ex99 earnings-transcript discovery had silently failed on EVERY call, and
- with a ticker's document texts already cached (no interleaved successful
  www.sec.gov fetches to reset the counter), five straight 503s opened the
  SEC outage breaker → the next uncached fetch raised `SECUnavailable`
  through unguarded call sites → **killed Agent 1 on SNAP/COIN/IBM/CHPT/FUBO**,
  and (agents run concurrently) **took down UBER's Agent 4 and PLTR's Agent 3
  as collateral**.
Fixes: correct URL format in `_fetch_ex99`; `_fetch_htm`,
`legal_proceedings.get_filings` / `get_filing_text`, and
`corporate_discrepancy._fetch_filings_from_sec` now catch fetch failures
(including breaker fail-fasts) and degrade per the breaker's design contract
instead of dying. Audited every other `-index.htm` builder
(sec_transcript, sec_edgar, legal, moat): all already use the dashed form.

### 2. SPOT / BABA — foreign private issuers (by design, message fixed)
Both file 20-F/6-K, which the domestic-form pipeline deliberately does not
support (documented limitation #1 of the 2026-06-26 matrix). Agent 1 already
said so; Agent 2 said the misleading "No filings available". `sec_edgar` now
diagnoses the empty result (`NO_FILINGS_REASON` / `no_filings_reason()`) and
Agent 2 reports: *"SPOT is a foreign private issuer (files 20-F, not
10-K/10-Q). Foreign filers are not yet supported."*

### 3. OPEN — `'int' object has no attribute 'get'` (the original Part-2a bug)
Stage-1 responses occasionally contain bare ints/strings in the promises
array; `_parse_stage1_response` called `.get()` on them. Type guards added at
every LLM-array boundary: promise_audit (stage 1 + stage 2),
corporate_discrepancy (`_parse_discrepancy_array` + severity-audit verdicts),
legal_proceedings (proceedings list). Reproduced deterministically from the
byte-cache before the fix; OPEN now 6/6.

### 4. PLTR wall-time anomaly (3.4h, overnight unattended run)
LLM time was only 9s — the stall was SEC-side (cold cache + 2s retry sleeps
on the ex99 503s + SEC nightly throttling). The ex99 fix removes the 503
source; PLTR re-runs clean in minutes.

### 5. FUBO second failure — backup-cache fidelity (found during re-verification)
A transient data.sec.gov breaker-open routed FUBO's metadata through the Atom
backup (no `reportDate` in that schema); the persisted backup cache then made
Agent 1's period-of-report 10-K selection come up empty ("No 10-K found")
indefinitely. Fixed: Agent 1 treats `_backup_source` caches as a miss and
refetches the primary (healing the file), degrading onto the backup only if
the refetch fails mid-outage. Details: SEC_BACKUP_SOURCES.md §4. FUBO
re-verified 6/6.

### 6. Router field fix — congestion-memo TTL cap
Groq's rolling RPD dipped mid-testing and returned an hours-scale
Retry-After; the network monitor honored it up to 3600s, parking every call
on dry Gemini/OR-`:free` starts long after Groq recovered (observed 39s/call
crawl). `CONGESTED_TTL_MAX=300s` now bounds the memo — details:
LLM_ROUTING_REPORT.md §9.7.

### Note on re-run wall times
The ex99 fix makes Agent 1 download years of 2.02 8-K transcript exhibits it
had silently skipped forever — a one-time backfill per ticker (SNAP 732s →
IBM 1561s → FUBO 2.4h under quota-thin overnight conditions), now cached
(`::ex99` namespace, verified 0.16s warm reads). Steady-state runs are
unaffected.

---

# Appendix — Input-Handling Matrix (2026-06-26, still current)

**Method:** real fetch/parse layer exercised directly (no LLM) · Harness: `scratchpad/edge_cases.py`

## 4a. Ticker input handling (`get_cik` × agent_1 / agent_3 / provider)

| Input | Before | Severity | Status |
|---|---|---|---|
| `ZZZZ` (invalid) | `None` | — | ✅ graceful |
| `""` (empty) | `None` | — | ✅ graceful |
| `"A"` (single-letter, real) | `0001090872` (Agilent) | — | ✅ correct |
| `"000"` (numeric) | `None` | — | ✅ graceful |
| `1000-char string` | `None` | — | ✅ graceful |
| `"AAPL'--"` (injection-ish) | `None` | — | ✅ graceful (exact-match lookup, no injection surface) |
| `"aapl"` (lowercase) | `0000320193` | — | ✅ uppercased internally |
| **`"BRK.A"` (dotted)** | `None` ❌ | **MEDIUM** | **FIXED** → `0001067983` |
| **`" AAPL "` (whitespace)** | `None` ❌ | **MEDIUM** | **FIXED** → `0000320193` |
| `"FB"` (renamed→META) | `None` | MEDIUM | documented (see below) |
| `"GOOGL"`/`"GOOG"` (multi-class) | both resolve | — | ✅ |

**Fix applied** (4 files: `corporate_discrepancy.py`, `legal_proceedings.py`, `risk_factors.py`, `providers/filings/sec_edgar.py`):
`key = ticker.upper()` → `key = ticker.strip().upper().replace(".", "-")`
Rationale: SEC `company_tickers.json` lists class shares with a hyphen (`BRK-A`, `BRK-B`); users type a dot. Verified post-fix: `BRK.A`, `BRK.B`, `" AAPL "` all resolve; `ZZZZ` still `None`. All four files compile.

## 4b. Non-standard issuers (filings fetch — no 10-K expected)

| Issuer | `get_10k` | `get_all` | `get_filings(20-F)` | Behavior |
|---|--:|--:|--:|---|
| **SPY** (ETF) | `[]` | `[]` | `[]` | ✅ graceful empty — ETFs file N-CSR, not 10-K |
| **VALE** (Brazil) | `[]` | `[]` | **25** | ⚠️ foreign filer files **20-F** → now surfaced via `no_filings_reason()` (2026-07-04) |
| **BABA** (China) | `[]` | `[]` | **12** | ⚠️ same as VALE — now surfaced |
| **BRK.A** (pre-fix) | `[]` | `[]` | `[]` | now resolves post-fix 4a |
| **ZZZZ** | `[]` | `[]` | `[]` | ✅ graceful |

## Documented limitations (with workarounds)

| # | Gap | Severity | Workaround / recommendation |
|---|---|---|---|
| 1 | **Foreign filers (20-F) yield 0 findings.** Data is present (VALE 25, BABA 12 20-Fs fetchable) but the doc-driven agents only request 10-K/10-Q/8-K. | MEDIUM | ~~surface "20-F issuer not yet supported"~~ **DONE 2026-07-04** (`no_filings_reason`). Full 20-F support: FUTURE_ROADMAP.md. |
| 2 | **Renamed/delisted tickers** (`FB`→META, `TWTR`, `LEHMQ`) don't resolve — `company_tickers.json` lists only *current* tickers. | MEDIUM | Maintain a small alias map (`FB→META`, `TWTR→ (delisted)`), or accept CIK directly. |
| 3 | **Dotless class tickers** (`BRKA`) still `None` (only `BRK.A`/`BRK-A` work). | LOW | The dot→hyphen fix covers the common case; full alias resolution is out of scope. |

## Other edge classes (assessed from code + the 8-K fix work this pass)
- **8-K exhibit-only / cover-stub filings** (the Agent-3 root cause): the iXBRL cover `primaryDocument` carries no narrative. **Fixed** — Agent 3 pulls body+EX-99 exhibits (see `QUOTE_VERIFICATION_FIX_REPORT.md`). Verified on GME/GE/JPM/MSTR.
- **Future / pre-EDGAR years:** `get_filings` returns `[]` for years with no electronic filings (graceful). Agents require ≥2 filings to compare and emit an explicit "not enough filings" finding rather than crashing.
- **Invalid CIK / wrong-company:** lookup is exact-match on `company_tickers.json`; a miss returns `None` (no silent wrong-company resolution observed).
