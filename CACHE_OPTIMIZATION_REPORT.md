# Cache Verification & Run-Archive Report (PART 3)

**Date:** 2026-06-27
**Code:** `sollia_working/src/utils/run_archive.py` (new),
`sollia_working/tools/backfill_run_archive.py` (new), archiving wired into all 4 agents.

---

## 1. Cache verification (Part 3a) — all four caches hit

| Cache | Path | Verified | Evidence |
|---|---|---|---|
| **Filings** | `.cache/filings/{T}_filings.json` | ✅ hits | `get_filings("NVDA","10-K")` 1st=194 ms (JSON parse) / 2nd=0.9 ms; **no network** on either — raw SEC submissions JSON read locally. 15 tickers cached. |
| **LLM** | `.cache/llm/` + `.cache/llm_history/` | ✅ hits | Agent 2 NVDA: **227 s first run → 4 s second run** (Stage-1 prompts served from cache). 258 `.cache/llm` files, 616 history records, `llm_history/index.jsonl` present. |
| **Texts** | `.cache/texts/{T}_texts.json` + `{T}_8k_texts.json` | ✅ present | 26 `*_texts.json` files; 8-K bodies cached separately (Agent 3's `_fetch_8k_text`, no collision with raw-HTML texts). See §3 for a content-collision caveat. |
| **Embeddings** | `.cache/embeddings/*.json` | ⚠️ present but **not hit on re-run** | 3,751 SHA-named vectors, yet Agent 1 still spends ~50 s re-embedding F. The embedding step does not short-circuit on the cache → **Part 4 speed item**. |

**Determinism check:** immutable filing JSON re-reads byte-identical; the LLM cache is keyed by SHA-256 of the prompt, so byte-identical prompts hit (any prompt edit, e.g. the Part 1 prompt strengthening, correctly misses and re-fetches).

---

## 2. Run archive (Part 3b/3c)

### 2.1 Format decision — Option B (Hybrid)

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **A. One fat human-readable JSON/run** (everything inline incl. embeddings) | inspectable, git-diffable | 2–10 MB/run; embeddings as float arrays are unreadable; slow | ❌ |
| **C. SQLite** | compact, natively queryable | opaque without tools; can't eyeball a run while debugging | ❌ |
| **B. Hybrid** | human-readable manifest for everything an analyst reads; large binaries referenced, not copied; `index.jsonl` for O(runs) lookup/backtest | slightly more moving parts | ✅ **chosen** |

**Key efficiency choice:** embeddings already live in `.cache/embeddings/` as
**SHA-addressed** files and LLM prompts/responses in `.cache/llm_history/` (its
own index). The manifest **references** these stores instead of copying 3,751
vectors or multi-KB prompts into every run — the SHA *is* Option B's binary
pointer, with free dedup across runs.

### 2.2 Layout
```
.cache/runs/
  index.jsonl                              one line/run: run_id, agent, ticker, year,
                                           timestamp, score, signal, n_findings, duration_ms, manifest
  {agent}/{TICKER}/{year}/run_NNN.json     human-readable manifest (full result + findings + perf)
  {agent}/{TICKER}/{year}/run_NNN_embeddings/   (only if caller passes raw vectors)
```

### 2.3 API (`run_archive.py`)
- `save_run(agent_id, ticker, year, result, *, duration_ms, llm_calls, embeddings)` → run_id (atomic write; exception-safe; `SOLLIA_ARCHIVE=0` disables).
- `load_run(run_id)` → full manifest.
- `list_runs(ticker=, agent=, year=)` → index rows, newest first.
- `backtest_query(agent=, ticker=, year=, signal=, min_score=, max_score=, finding_contains=)` → matching runs; `finding_contains` scans candidate manifests' findings.

Wired into the end of every agent's run (`corporate_discrepancy`, `promise_audit`,
`legal_proceedings`, `risk_factors`), each in a `try/except` so archiving can
never break a run.

### 2.4 Verification
- **Live Agent 4 on AAPL → manifest at `.cache/runs/risk_factors/AAPL/2026/run_001.json`** (score 29.6, dur 561 ms), indexed. ✓ (registry id `risk_factors` = "agent 4".)
- **Backfill:** `tools/backfill_run_archive.py` migrated **48 runs** (12 tickers × 4 agents) from `.test_results/` → 2.1 MB total (~44 KB/manifest; GME's 92-promise run = 467 KB).
- **Backtest demo** — the Part 6 target query *"Agent 2 broken promises about gross margins"*:
  ```
  backtest_query(agent="promise_audit", finding_contains="margin")  # then filter status==BROKEN
  → NVDA: "For fiscal year 2025, we expect gross margins to be in the mid-70% range." [BROKEN]
  ```
  Also `backtest_query(agent="risk_factors", max_score=35)` → AAPL, COIN, F, META, MSTR, NVDA. ✓

(Backfilled pre-redesign runs show `signal=None` — correct: those historical runs predate the Part 2 signal field. New runs carry score+signal+rationale.)

---

## 3. Known issue (documented, not changed this pass): texts-cache content collision

**Finding.** `.cache/texts/{T}_texts.json` is keyed by *plain accession* and has
**four writers with two content types**: Agents 1/3/4 store **raw HTML**;
Agent 2 Stage 1 stores **extracted MD&A**. Last writer wins. When Agent 1 or 3
runs first (or concurrently — the backend runs all four together), Agent 2's
Stage 1 reads raw HTML, treats the **whole filing (incl. financial-statement
notes)** as "MD&A", and mines note boilerplate ("Future minimum lease
obligations", "unrecognized tax benefit", "performance obligation") as promises.
This is the root cause of the Part 1b symptom.

**Why it is safe right now.** Part 1b's `EXCLUDE_PATTERNS` + Stage-1 prompt were
verified to remove **all** such boilerplate from candidates *even when fed the
whole polluted filing* (12→0 boilerplate candidates; 0 in the final 37 promises).
So output correctness does not depend on fixing the cache.

**Why it was not rewired here.** Agent 2's two stages have *opposite* cache needs:
Stage 1 wants MD&A; Stage 2 (evidence search) deliberately wants the full raw
HTML and cleans it itself. A correct fix changes Stage 1's candidate set
(MD&A-only), which **busts the LLM prompt cache** and demands a full, rate-limit-
fragile re-verification — out of proportion to a symptom already neutralized, and
not a prescribed deliverable.

**Recommended fix (next dedicated change, with tests).** Add an optional
`content_type` param to `get_cached_text`/`cache_text` (default = current
behavior, so Agents 1/3/4 are untouched); have Agent 2 Stage 1 read/write MD&A
under `content_type="mda"` (key `"{acc}::mda"`) and *also* write the raw HTML
under the plain key so Stage 2 keeps finding full text. This removes the leak at
the source and makes Agent 2 ~3× lighter (≈300 MD&A sentences vs ≈1,500
whole-filing), which also helps Part 4. Verify with a fresh full Agent 2 run on a
ticker no other agent has touched.
