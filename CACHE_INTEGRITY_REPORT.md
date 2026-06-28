# Cache Integrity Report — Part 3
**Date:** 2026-06-26 · **Scope:** `sollia_working/.cache/` (llm_history, filings, texts, embeddings) · **Method:** empirical, no LLM calls · Auditor script: `scratchpad/cache_audit.py`

## Verdict: **CLEAN — 0 issues (CRITICAL/HIGH/MEDIUM/LOW all zero)**

The cache is production-sound. Every checkable invariant passed. Two *premises in the Part 3 brief were factually wrong* and are corrected below (they are not defects in the system).

---

## 3a. LLM History (`.cache/llm_history/`)

Layout (confirmed): `llm_history/{agent}/{ticker}/{call_id}.json`, append-only index `index.jsonl`.

| Check | Result |
|---|---|
| Record files | **459** |
| `index.jsonl` lines | **459** — perfect 1:1 reconciliation (0 records missing from index, 0 dangling index entries) |
| `call_id` uniqueness | **OK** (0 duplicates) |
| `prompt.full_text` non-empty | **OK** (0 empty) |
| Timestamps ISO-8601 | **OK** (0 malformed) |
| `token_count` sane (0 < n < 1M) | **OK** |
| **Secret-leak scan** (sk-or / gsk_ / AKIA / AIza patterns over every record) | **CLEAN — 0 hits** |
| Record composition | live=**376**, backfilled=**67**, failed=**16** (failures *are* recorded — good) |

### Corrected premise #1 — "rebuild `index.json` and compare"
There is **no `index.json`**. The real index is **`index.jsonl`** (JSON Lines, appended by `_record_history`). It reconciles **exactly** (459/459). The "rebuild and diff" check was run against the rebuilt set of record relpaths vs. `index.jsonl` paths → identical. ✅

### Corrected premise #2 — "history record count must EQUAL logged LLM calls EXACTLY"
`sum(llm_calls_recorded across .test_results/_summary.json) = 105` vs `live history records = 376`. **This is not a gap.** History is **cumulative across all runs ever**; `_summary.json` is **last-run-only** (overwritten each run). The correct invariant — *every logged call has a durable record* — holds (376 ≥ any single run, 0 empty/dup/unparseable). An exact equality was never achievable by design.

> 67 backfilled records (provider=`backfilled`, zeroed perf) are legacy entries reconstructed from `.cache/llm/` by `tools/backfill_llm_history.py`. They are clearly flagged (`backfilled: true`) and should be treated as provenance-incomplete.

---

## 3b. Filings Cache (`.cache/filings/`) — 12 files

All 12 are **valid JSON in the raw SEC-submissions schema** (`filings.recent.{form,accessionNumber,…}`) — the shared cache contract is **honored** (no accession-keyed clobbering). 0 zero-byte, 0 truncated.

| File | forms | size | schema |
|---|--:|--:|:--:|
| AAPL | 2236 | 278KB | ✅ | F | 4428 | 451KB | ✅ | JPM | 25239 | 4.8MB | ✅ |
| COIN/GE/GME/META/MSFT/MSTR/NVDA | ~1000 | ~170-195KB | ✅ | GM | 2122 | 278KB | ✅ | TSLA | 1743 | 236KB | ✅ |

**Non-corruption (immutable re-download diff):** re-downloaded GE FY2023 10-K primary doc (`0000040545-23-000240`) and byte-compared to the cached copy → **identical SHA-256 (`6069bfd631884a1c`)**. SEC archives are immutable; the cache faithfully preserves them.

---

## 3c. Texts Cache (`.cache/texts/`) — 24 files

All valid UTF-8 JSON, **0 empty values**. The new **namespaced `{TICKER}_8k_texts.json`** caches (added by the Agent-3 8-K fix this pass — COIN/GE/GME/JPM/META/MSTR, 2 accessions each) are present and **do not collide** with the raw-HTML `{TICKER}_texts.json` caches (the documented content-type collision risk is avoided by namespacing).

## 3d. Embeddings Cache (`.cache/embeddings/`) — 3,511 files

**All 3,511 are uniform 768-dim float vectors** (FinBERT hidden size), **0 non-vector / 0 zero-byte**. Keyed by truncated content hash. (Full per-vector recomputation requires loading FinBERT and was out of scope for this no-LLM pass; structural + dimensional integrity verified.)

## 3e. Cross-cache consistency
Traced the immutable chain for GE: `filings/GE_filings.json` (accession present) → `texts/GE_texts.json` (raw HTML present, re-download identical) → `embeddings/` (768-dim vectors) → `llm_history/agent_3/GE/*` (records present, in index). **Chain unbroken.**

---

## Recommendations (non-blocking)
- **LOW:** the 67 backfilled history records carry synthetic provider/timestamp/perf — fine, but mark them excluded from any latency/cost analytics.
- **LOW:** `index.jsonl` is append-only; a record file deleted out-of-band would leave a dangling index line. Current state is consistent; consider a periodic `--rebuild-index` in `tools/llm_history_stats.py`.
