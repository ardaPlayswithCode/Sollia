# Sollia Optimization Report

**Date:** 2026-06-15
**Scope:** 6 workstreams — alignment quality, Agent 1/2/3 output quality, live logging, clean export, and parallelism/performance.
**Test subject:** Ford (`F`), fiscal year 2025.
**Runtime:** Python 3.11 (the homebrew interpreter the server actually runs on; the `sollia_web/venv` is 3.9 and unused). Every edited file was verified with `python3.11 -m py_compile`.

All changes preserve the core architecture: agent separation, the `.cache` / `.llm_cache` structure, the LLM provider config, the deterministic Agent‑2 fallback, the SEC camelCase field reads, and no threading lock was added to `call_llm`.

---

## Headline results (F 2025, all 3 agents)

| Metric | Before | After |
|---|---|---|
| Agent 1 aligned passage pairs | 337 | **108** (noise removed) |
| Agent 1 alignment false‑positive rate @0.86 | **40%** | **0%** |
| Agent 1 chunk labels | `10-Q_0`, `10-Q_1` | **`10-Q (Q2 2025)`, `10-Q (Q3 2025)`** |
| Agent 1 reasoning | sometimes the literal word `"short"` | full, specific sentences |
| Agent 2 top‑5 promises | 5 identical promise+evidence | **5/5 unique promises**, 4/5 unique evidence |
| Agent 3 10‑Q section when no content | silently omitted | **"No legal proceedings disclosed in 10‑Q"** |
| Agent 3 links per row | 1 (current only) | **2 (prior + current)** |
| Live logs | one merged dump at the end | **per‑agent, streamed live, qualitative** |
| JSON export | raw internal format | **clean typed schema, zero internal fields** |
| Embedding pass on re‑run | ~90–180 s every time | **disk cache → <1 s** |
| Agents run | sequential | **parallel** (A2/A3 overlap A1) |

---

## Workstream 1 — Embedding alignment quality

**Problem:** the cosine layer matched passages that merely shared boilerplate, producing false‑positive discrepancies.

**Approaches tried** (offline A/B harness `sollia_working/ws1_align_test.py`, embed‑once then sweep — embeddings cached so each config is instant). False‑positive proxy = an aligned pair with cosine ≥ threshold but content‑word Jaccard < 0.06 (high similarity, almost no shared vocabulary = the signature of a boilerplate/anisotropy match).

| Threshold | Raw FinBERT (baseline) | Boilerplate‑opener strip | **Mean‑centered** |
|---|---|---|---|
| 0.80 | 358 aligned, **51% FP** | 358, 51% FP | 112, **2% FP** |
| **0.86** | 319 aligned, **40% FP** | 319, 40% FP | 92, **0% FP** |
| 0.90 | 221, 23% FP | 222, 23% FP | 71, 0% FP |
| 0.95 | 106, 0% FP | 106, 0% FP | 50, 0% FP |

1. **Boilerplate‑prefix stripping before embedding** — implemented and measured. *Ineffective*: only ~5% of cleaned chunks open with boilerplate, and stripping left alignment essentially unchanged (319 → 319 pairs). The HTML/XBRL cleaning already removes most boilerplate, so the opener is not the real driver.
2. **Threshold tuning** — has a large effect on volume but trades away true matches; on raw embeddings even 0.80→0.95 never reaches acceptable precision until you've discarded most pairs.
3. **TF‑IDF weighting** — `scikit-learn` is not installed in the runtime; would require a hand‑rolled implementation and replaces the semantic model. Not pursued given a better option existed.
4. **Mean‑centering (anisotropy correction) — CHOSEN.** FinBERT embeddings are highly anisotropic: a single dominant component shared by *all* finance text inflates every cosine, so topically‑unrelated passages that share generic phrasing still score high. Subtracting the corpus‑mean embedding before cosine removes that shared component. On F 2025 this cut the false‑positive rate from **40% → 0%** at the existing 0.86 threshold while keeping the genuine, vocabulary‑sharing matches.

**Implementation:** `_apply_mean_centering()` runs once over all chunks in `align_all_pairs`; `_compute_similarity_matrix` uses the centered vector (`_emb_c`) when present. Threshold kept at 0.86. Opener‑stripping is retained as an opt‑in (`STRIP_BOILERPLATE_FOR_EMBEDDING = False`) and a boilerplate‑awareness instruction was added to the LLM prompt as defense‑in‑depth.

**Validated in the real pipeline** (not just the harness): `align_all_pairs` returns **337 pairs without centering vs 108 with** — the ~229 removed are the low‑overlap noise; the residual difference from the harness's 92 is the intended same‑section boost.

---

## Workstream 2 — Agent 1 output quality

- **Quarter labels:** `_quarter_label()` maps each 10‑Q `period_of_report` to its fiscal quarter relative to the fiscal‑year end (handles non‑calendar years). `_display_doc_type()` renders `10-Q (Q2 2025)`. Confirmed in output: `10-K vs 10-Q (Q2 2025)`.
- **Reasoning:** the prompt's example placeholder `"reasoning":"short"` (which the LLM was copying) is now a descriptive instruction, and `_validate_llm_response` regenerates a specific sentence from the discrepancy type + both quotes whenever the model returns `<20` chars or a placeholder. Confirmed: every discrepancy now has a full sentence.
- **Same‑filing guard:** `rank_discrepancies` resolves each side's SEC accession from chunk metadata; if both sides are the same filing it penalizes the score (×0.4) and downgrades severity *before* the top‑5 selection, so false positives fall out of the list. A `same_filing` flag is surfaced for the UI.

---

## Workstream 3 — Agent 3 legal proceedings completeness

- **Empty findings:** `track()` now appends a placeholder finding for every doc type with no legal content (not enough filings / text unavailable / no legal section), so the UI shows e.g. *"No legal proceedings disclosed in 10‑Q"* instead of silence. Confirmed: F 2025 returns 3 findings (10‑K real, **10‑Q empty**, 8‑K real).
- **Prior + current provenance:** findings now store `prior_accession`/`prior_primary_doc` alongside the current ones.
- **Two links per row:** a single consolidated block builds `current_edgar_url` and `prior_edgar_url` for each finding and proceeding (replacing the old broken block that referenced a non‑existent `primary_document` field). Confirmed: every finding/proceeding has both URLs.
- **Frontend:** quotes widened to 8 words; prior and current quotes each render their own "View ↗" link.

---

## Workstream 4 — Agent 2 promise deduplication

`select_top_promises` now enforces diversity: pass 1 requires both a unique promise text (first 100 chars) and a unique evidence URL; pass 2 relaxes the evidence constraint to still return N distinct promises. Confirmed on F 2025: top‑5 went from 5 identical entries to **5/5 unique promise texts** (4/5 unique evidence URLs). The deterministic fallback is untouched.

---

## Workstream 5 — Live per‑agent logs + clean JSON export

**Live logs.** Each agent's entry function takes an optional `progress(message, kind)` callback and emits qualitative, human‑readable milestones — never internal terms ("FinBERT", "cosine", "Gaussian" do not appear). A thread‑safe run registry (`backend/runs.py`) collects them; the backend streams them over **Server‑Sent Events** (`GET /api/analyze/stream/{run_id}`), with a polling fallback (`GET /api/runs/{run_id}`). The frontend renders one **collapsible log section per agent** (default collapsed, latest line shown live in the header) with emoji/colour by kind (🔍 search, 📑 fetch, 🧭 align, ⚖️ analyze, ⚠️ flag, ✅ done).

Live sample (F 2025):
```
🔍 Pulling FY2025 filings for F...
📑 Found 10-K, 3 10-Qs, 8-K, DEF 14A, 4 earnings transcripts
🧭 Reading and aligning passages across the filing set...
•  Matched 108 related passages to cross-examine
⚖️ Cross-referencing claims across the 10-K, 10-Qs, and earnings calls...
⚠️ Flagged 5 potential discrepancies for review
✅ Corporate discrepancy scan complete (89s)
```

**Clean export.** `build_clean_export()` produces an analyst‑ready, reverse‑engineering‑resistant schema (delivered in the SSE `complete` event and via the export panel):
```json
{ "ticker": "F", "year": 2025, "timestamp": "...", "signal": "BUY", "score": 61.6,
  "agents": { "<agent_id>": { "name": "...", "score": 0, "signal": "...",
    "top_findings": [ { "type": "...", "severity": "high|medium|low",
      "summary": "...", "citations": [ { "document": "...", "excerpt": "...", "url": "..." } ] } ] } } }
```
The shape is identical for every agent, so adding 5 more agents just adds keys under `agents` — no schema change. Verified: **no internal fields leak** (`agent_weights`, `agent_scores`, `similarity`, `embedding`, `_emb_c`, `batch_key`, `chunk_id` all absent).

---

## Workstream 6 — Parallelism & performance

- **Embedding disk cache** (`.cache/embeddings/`, keyed by a hash of the chunk text, atomic writes): the ~90–180 s FinBERT pass becomes a <1 s cache load on warm re‑runs. The FinBERT model itself is now memoized so it loads once per process instead of per run.
- **Parallel agents:** `run_selected_agents` runs the three agents on a `ThreadPoolExecutor`. Because Agent 2 (LLM/HTTP) and Agent 3 (HTTP) are I/O‑bound, they overlap Agent 1's work. Measured on F 2025: Agent 3 finished at +1 s and Agent 2 at +12 s, both fully inside Agent 1's 89 s — sequential would be ≈ 102 s, so ≈ 13% wall‑clock saved here, and more on a cold cache where Agent 2 takes 30–60 s. The endpoint offloads the blocking pipeline via `asyncio.to_thread` so the event loop stays free for concurrent streams.
- **Concurrency test:** two simultaneous analyses (F + GM) both completed cleanly in 60.9 s with **no errors or tracebacks**. Shared cache writers (`_save_json`, the legal texts cache) were made atomic (temp + `os.replace`) so concurrent agents can't observe a half‑written file.
- **Thread‑safety notes:** FinBERT inference releases the GIL during the torch forward pass and is safe to call from a worker thread; the memoized model is shared read‑only. `call_llm`'s time‑based rate limiter remains lock‑free (per the constraint) — under contention the worst case is an occasional 429 + retry, not corruption, and its cache write is idempotent.
- **Remaining bottleneck (not in scope to change):** Agent 1's document **fetch** (~58 s) dominates a warm run because earnings‑transcript exhibits (`_fetch_ex99`) are fetched from the network each time rather than cached. Caching EX‑99 bodies by accession would cut a warm F 2025 run from ~89 s to ~30 s. A CPU‑bound `ProcessPoolExecutor` for FinBERT would only help if embeddings weren't already cached; with the disk cache it is not the bottleneck.

---

## Files changed

- `sollia_working/src/agents/corporate_discrepancy.py` — mean‑centering, embedding disk cache + model memoization, quarter labels, display labels, reasoning guard, same‑filing penalty, progress milestones, boilerplate prompt awareness.
- `sollia_working/src/agents/promise_audit.py` — diversity dedup in `select_top_promises`, progress milestones.
- `sollia_working/src/agents/legal_proceedings.py` — empty findings, prior accession/doc storage, prior+current URLs, atomic texts‑cache write, progress milestones.
- `sollia_working/src/providers/filings/sec_edgar.py` — atomic `_save_json`.
- `sollia_web/backend/agents.py` — parallel `run_selected_agents` with live emit.
- `sollia_web/backend/runs.py` *(new)* — run registry, SSE snapshots, clean export schema, parallel pipeline runner.
- `sollia_web/backend/main.py` — `/api/analyze` (non‑blocking), `/api/analyze/start`, `/api/analyze/stream/{id}` (SSE), `/api/runs/{id}`.
- `sollia_web/frontend/src/App.jsx` — SSE consumption, per‑agent collapsible live logs, clean JSON export panel, 8‑word quotes, 2 links per legal row, clean labels.
- `sollia_working/ws1_align_test.py`, `ws1_validate.py` *(new, analysis artifacts)* — the WS1 A/B methodology above.

## How to run

```bash
cd "sollia_web" && python3.11 backend/main.py     # serves API + built UI on :8000
# or for live frontend dev:  cd sollia_web/frontend && npm run dev
```
The UI is also pre‑built to `sollia_web/frontend/dist` and served at `http://localhost:8000/`.

## Security note

The git remote URL in `.git/config` embeds a GitHub personal‑access token in plaintext. It lives only locally (not pushed), but rotating it is recommended.
