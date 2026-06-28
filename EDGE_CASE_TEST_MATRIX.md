# Edge Case Test Matrix — Part 4
**Date:** 2026-06-26 · **Method:** real fetch/parse layer exercised directly (no LLM, no fabricated results) · Harness: `scratchpad/edge_cases.py`

## Headline: **No crashes. The system is robustly graceful against every bad input.**
Every malformed/edge ticker returned `None` or `[]` — zero unhandled exceptions across all three `get_cik` implementations and the filings fetchers. The findings below are **coverage/normalization gaps**, not stability bugs. Two were **fixed this pass**; the rest are documented with workarounds.

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
| **VALE** (Brazil) | `[]` | `[]` | **25** | ⚠️ foreign filer files **20-F**; agents don't request it → silently 0 findings |
| **BABA** (China) | `[]` | `[]` | **12** | ⚠️ same as VALE |
| **BRK.A** (pre-fix) | `[]` | `[]` | `[]` | now resolves post-fix 4a |
| **ZZZZ** | `[]` | `[]` | `[]` | ✅ graceful |

## Documented limitations (with workarounds)

| # | Gap | Severity | Workaround / recommendation |
|---|---|---|---|
| 1 | **Foreign filers (20-F) yield 0 findings.** Data is present (VALE 25, BABA 12 20-Fs fetchable) but the 3 doc-driven agents only request 10-K/10-Q/8-K. | MEDIUM | Add `20-F` to the form list and map Item 3/Item D etc. Until then, foreign issuers are out of scope — surface "20-F issuer not yet supported" instead of an empty result. |
| 2 | **Renamed/delisted tickers** (`FB`→META, `TWTR`, `LEHMQ`) don't resolve — `company_tickers.json` lists only *current* tickers. | MEDIUM | Maintain a small alias map (`FB→META`, `TWTR→ (delisted)`), or accept CIK directly. |
| 3 | **Dotless class tickers** (`BRKA`) still `None` (only `BRK.A`/`BRK-A` work). | LOW | The dot→hyphen fix covers the common case; full alias resolution is out of scope. |

## Other edge classes (assessed from code + the 8-K fix work this pass)
- **8-K exhibit-only / cover-stub filings** (the Agent-3 root cause): the iXBRL cover `primaryDocument` carries no narrative. **Fixed** this pass — Agent 3 now pulls body+EX-99 exhibits (see `QUOTE_VERIFICATION_FIX_REPORT.md`). Verified on GME/GE/JPM/MSTR (cover-only no longer breaks extraction).
- **Future / pre-EDGAR years:** `get_filings` returns `[]` for years with no electronic filings (graceful). Agents require ≥2 filings to compare and emit an explicit "not enough filings" finding rather than crashing.
- **Invalid CIK / wrong-company:** lookup is exact-match on `company_tickers.json`; a miss returns `None` (no silent wrong-company resolution observed).

## Success criteria
- [x] 25+ edge cases exercised against real code · [x] **0 crashes** · [x] HIGH/CRITICAL: none found · [x] 2 MEDIUM normalization gaps **fixed + verified** · [x] remaining MEDIUM/LOW documented with workarounds.
