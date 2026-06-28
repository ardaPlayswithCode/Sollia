# Quote Verification Fix Report — Agent 3 8-K Citations
**Date:** 2026-06-26 · **Outcome:** aggregate quote-verification **97.82% → 98.26%**; Agent-3 8-K absents **8 → 4**; four tickers (GE, GME, MSTR, JPM) **0 → 100%**. Remaining 4 are LLM paraphrase (Part 2 domain), not citation bugs.

## The 20 failures, root-caused
Of 919 quotes whose cited source could be re-downloaded, 20 were absent (97.82%). Diagnosis (`verify_quotes.py` caches every download in `.cache/_verify_downloads/`, so failures are reproducible offline):

| Bucket | Count | Cause |
|---|--:|---|
| **A — wrong/empty 8-K citation** | 8 | All Agent-3 8-K "current" quotes. `current_edgar_url` pointed at the **iXBRL cover stub** (`primaryDocument`, 3-9KB of `us-gaap:` tags, `longest_prefix_in_doc=0`). |
| **B — LLM non-verbatim** | 12 | Right document, partial alnum match (TSLA 43%, MSTR 61%, META chg6 **77.6%** — just under the 80% gate; NVDA "**To** want NVIDIA" vs source "**We** want…"). Concatenated bullets / paraphrase. |

> The verifier was **already** robust to smart quotes / whitespace / page-numbers (`_alnum()` strips to `[a-z0-9]`) and trailing ellipsis (80%-prefix). So Bucket A wasn't a normalization issue — the alnum sequence was genuinely **not in the cited document**.

## Proving Bucket A: wrong *document*, then wrong *accession*
1. The cited accession's `index.json` showed the needle in **none** of its docs.
2. **EDGAR full-text search** located the real source: GE's *"elected Judson Althoff to the Board"* lives in accession `…-000039` (the 06-11 board 8-K), but Agent-3 cited `…-000042` (the 06-25 by-laws 8-K). COIN's *"published a presentation…"* → `…-000053`, cited `…-000075`. **Consistent: the quote is real but attached to the wrong (latest) 8-K.**
3. Code trace: Agent-3 diffs the **two most-recent 8-Ks** (`filings[0]` vs `filings[1]`) like 10-K-vs-10-K — but consecutive 8-Ks are **unrelated events**. Worse, `get_filing_text()` returned only the cover-stub `primaryDocument`, so the model saw XBRL soup and **confabulated** event sentences from cover metadata (dates/item numbers), citing them by filing position.

## The fix (3 coordinated changes)
**`src/agents/legal_proceedings.py`:**
1. **`_fetch_8k_text()`** — for 8-Ks, fetch the real narrative: every human-readable `.htm` in the accession (via `index.json`), **skipping `R\d+.htm` XBRL renders and `-index.htm`**, body + EX-99 exhibits. Cached in a **separate namespace** `texts/{TICKER}_8k_texts.json` (no collision with the raw-HTML texts cache). 8-K prompt window widened 4000→8000 chars.
2. **`_cite_doc()`** — cite each quote to the **exact exhibit doc that actually contains it** (alnum match over the fetched docs, both sides), not the cover-stub URL.

**`tools/verify_quotes.py`:**
3. Verifier now checks the **per-proceeding** `edgar_url` (the citation the export actually uses), falling back to the finding-level URL.

### Deliberately NOT changed: the matcher
I considered loosening `quote_present` to pass the Bucket-B near-misses (anchored-window match). **Rejected** — those are genuine LLM non-verbatim errors (NVDA "To"≠"We"), and loosening the verifier would *mask the very signal Part 2 measures* (metric gaming). The strict matcher stays; Bucket B is handed to Part 2 prompt analysis.

## Validation (re-ran Agent-3 live on all 6 Bucket-A tickers, then re-verified)
| Ticker | Before | After |
|---|--:|--:|
| GE | 1 absent | **2/2 (100%)** |
| GME | 2 absent | **2/2 (100%)** |
| MSTR | 1 absent | **8/8 (100%)** |
| JPM | 2 absent | **2/2 (100%)** |
| COIN | 1 absent | 2/4 — 2 residual (LLM paraphrase: *"held its 2026 annual meeting"*) |
| META | 1 absent | 0/2 — 2 residual (LLM paraphrase: *"completed an offering of \$3,000,000,000"*) |

**Aggregate 904/920 = 98.26%.** By agent: agent_2 99.9%, agent_1 96.1%, agent_3 94.6%, **agent_4 89.9% (worst)**. The 4 residual Agent-3 + the Agent-4 cases are non-verbatim LLM quoting → addressed in `PROMPT_OPTIMIZATION_REPORT.md`.

## Smoke-tested
`_fetch_8k_text` returns the GE by-laws narrative (cover + exhibit), `_cite_doc` attributes a real snippet to its exhibit and returns `None` for a fabricated phrase; cache hit path works; namespaced cache verified (Part 3). All files compile.
