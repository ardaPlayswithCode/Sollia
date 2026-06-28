# Infrastructure Hardening — Final Report
**Date:** 2026-06-26 · **Scope:** SEC sources (Part 1), prompt optimization (Part 2), cache integrity (Part 3), edge cases (Part 4). All work in the **real** project (`Desktop - Arda's Mac Studio/Sollia`); the CWD `~/Desktop/Sollia` is the stale Streamlit duplicate and was untouched. Companion reports: `API_SOURCE_EVALUATION_REPORT`, `FINAL_API_PRIORITY_LIST`, `PROMPT_OPTIMIZATION_REPORT`, `CACHE_INTEGRITY_REPORT`, `EDGE_CASE_TEST_MATRIX`, `QUOTE_VERIFICATION_FIX_REPORT`.

## Headline outcomes
| Area | Before | After | Change |
|---|---|---|---|
| **Quote verification (aggregate)** | 97.82% (899/919) | **98.26% (904/920)** | Agent-3 8-K confabulation eliminated |
| **Agent-3 8-K citations** | 8 absent, cited wrong/empty docs | **4 fixed → 100%** on GE/GME/MSTR/JPM | real narrative + exact-exhibit citation |
| **Ticker robustness** | `BRK.A`, `" AAPL "` → no result | **resolve** | normalization fix (4 files) |
| **Cache integrity** | unaudited | **0 issues**, secrets clean, byte-identical re-download | full audit |
| **Edge cases** | unknown | **0 crashes / 25+ inputs** | graceful everywhere |

## The one substantive bug found & fixed — Agent-3 8-K citations
Agent 3 diffed the two most-recent 8-Ks like 10-K-vs-10-K, but consecutive 8-Ks are unrelated events, and `get_filing_text()` returned the **iXBRL cover stub** (`primaryDocument`) instead of the body+exhibits. Result: the model confabulated event "quotes" from cover metadata and cited them to the wrong accession. **Fixed** by fetching real narrative via `index.json` (skipping XBRL renders), citing each quote to the exact exhibit (`_cite_doc`), and verifying the per-proceeding URL. Validated live on 6 tickers. Full detail: `QUOTE_VERIFICATION_FIX_REPORT.md`.

## Part 2 — prompt A/B: a clean negative result
With the funded key ($10, confirmed), an isolated A/B (input fixed, prompt varied) on Agent 4 (worst agent, 89.9%) showed **the current prompt (V0) wins**; anti-concatenation/self-check/negative-example variations did **not** help and sometimes hurt (−21pp on TSLA). **No prompt change shipped.** The residual ~10% is model-level summarization of bulleted risk lists; the system already **flags** unverified quotes (`verified=false`) rather than hiding them — the correct compliance behavior. Model comparison (Groq vs funded OpenRouter, same prompt) ≈ a wash (~85-90% both); Groq shows rate-limit fragility under rapid calls (the funded OpenRouter key is the right overflow tier). Detail: `PROMPT_OPTIMIZATION_REPORT.md`.

## Part 1 — SEC sources: current list validated
All primary endpoints 100% reliable (<250ms) on real accessions: submissions JSON → `index.json` → Archives `.htm`; atom RSS a solid discovery fallback; **efts full-text confirmed slow (3-4s) + flaky (500s on punctuated phrases)** → last-resort. `index.json` promoted to first-class (it's what makes 8-K exhibit resolution work). No dead sources in code. Detail: `FINAL_API_PRIORITY_LIST.md`.

## Part 3 — cache: clean
459 history records reconcile exactly with `index.jsonl`; **secret-leak scan clean**; all filings caches honor the raw-submissions contract; embeddings uniform 768-dim; immutable re-download byte-identical. Two brief premises were wrong (no `index.json` — it's `index.jsonl`; history is cumulative so it can't "equal" last-run logs) and are corrected. Detail: `CACHE_INTEGRITY_REPORT.md`.

## Part 4 — edge cases: graceful, with documented gaps
0 crashes across 25+ malformed inputs. Fixed: dotted/whitespace ticker normalization. Documented (MEDIUM): foreign 20-F issuers fetch data but agents don't request 20-F → 0 findings; renamed/delisted tickers don't resolve. Detail: `EDGE_CASE_TEST_MATRIX.md`.

## Files changed (all in the real project)
- `src/agents/legal_proceedings.py` — `_fetch_8k_text`, `_cite_doc`, 8-K branch rewrite, per-quote citation, ticker normalization
- `src/agents/corporate_discrepancy.py`, `src/agents/risk_factors.py`, `src/providers/filings/sec_edgar.py` — ticker normalization
- `tools/verify_quotes.py` — per-proceeding URL verification

## Honest limitations
- Quote target ">99%" **not reached** (98.26%) — the gap is genuine LLM non-verbatim quoting, which prompt-tuning empirically does **not** fix; mitigated by honest `verified` flags, not masked.
- Agents 1/3 not separately A/B'd; foreign-filer (20-F) support is a documented gap; embedding vectors verified structurally (not recomputed through FinBERT).
- 8-K current/prior *labeling* can still be imperfect (unrelated events); citations now always point to the doc that contains the quote regardless.
