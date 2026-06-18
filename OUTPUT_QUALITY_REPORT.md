# Sollia — Output Quality Report

**Date:** 2026-06-17
**Scope:** Fix all 7 output-quality issues across the 3 SEC-filing agents without changing the core architecture (LLM provider priority, document-fetch tiers, shared cache, deterministic fallback safety net).
**Test subject:** Ford Motor Company (`F`), FY2025 — fully cached under `sollia_working/.cache/`.
**Runtime:** Homebrew Python 3.11 (`/opt/homebrew/bin/python3.11`), `PYTHONPATH=sollia_working`.

Entry points used for verification:
- Agent 1 — `corporate_discrepancy.run_discrepancy_agent("F", 2025)`
- Agent 2 — `promise_audit.PromiseAuditAgent().run("F")`
- Agent 3 — `legal_proceedings.track_legal("F")`

> Every edited file was re-checked with `python3.11 -m py_compile`; all three agents compile.

---

## Summary

| # | Sev | Issue | Status |
|---|-----|-------|--------|
| 1 | CRITICAL | Agent 1 citation/excerpt mismatch | ✅ Fixed (two root causes) |
| 2 | HIGH | Agent 2 evidence shows raw XBRL/HTML | ✅ Fixed |
| 3 | HIGH | Agent 2 marks everything KEPT | ✅ Fixed |
| 4 | MEDIUM | Agent 1 forecast-vs-actual false positives | ✅ Fixed |
| 5 | MEDIUM | Agent 2 same-text circular matches | ✅ Fixed |
| 6 | LOW | Agent 3 8-K duplicate findings | ✅ Fixed |
| 7 | — | Dead files removed | ✅ Verified (already absent) |

---

## Issue 1 — CRITICAL: Agent 1 citation / excerpt mismatch

**Symptom.** Reasoning referenced one topic (tariffs, EBIT, EV incentives) but the displayed excerpt showed unrelated text (depreciation tables, equity-method investments).

**Root cause — two compounding bugs in `corporate_discrepancy.py`:**

1. **First-word index matching** in `_validate_llm_response`. The old code re-derived the cited sentence from only the **first word** of the quote and took the first hit:
   ```python
   key_a = qa.split()[0]              # e.g. "In", "The", "During"
   for i, s in enumerate(sents_a):
       if key_a.lower() in s.lower():  # first sentence containing "in" wins
           entry["sentence_index_a"] = i; break
   ```
   A quote beginning with a common word landed on the wrong sentence.

2. **`chunk_id` collisions** (the deeper bug). Chunk IDs were built as
   `f"{doc_type}_{section_name[:30]}_{pi}_{si}"`. Ford's MD&A produces many subsections that all truncate to `"Item 7. Management's Discussio"`, with `pi`/`si` resetting per section → **non-unique IDs**. `build_citations` builds `all_chunks[chunk_id] = chunk`, so colliding IDs **overwrote each other** and citations resolved to the *wrong chunk*. The same collision **merged distinct batches** (batches are keyed by anchor `chunk_id`), collapsing 82 real comparison batches down to 12.

**Fixes.**
- `_validate_llm_response` now matches the **full quote** (whitespace-normalised substring, then significant-word overlap) via `_locate_sentence`, and only overrides the LLM index on a confident match — otherwise it keeps the LLM's own index.
- `_locate_sentence` now also reports whether the quote was **grounded**; a discrepancy whose substantive quote cannot be found in the cited passage's sentences is **rejected** (hallucinated/misaligned claim).
- A small credibility guard drops `numerical_contradiction` rows whose "conflicting figures" are **bare reference integers** (e.g. `"53"` vs `"39"`).
- `build_chunks` now appends a running counter `gi` → **globally unique `chunk_id`**.

**Before / after (F 2025, verified by dumping each cited chunk's sentence list):**

| | Before | After |
|---|--------|-------|
| Raw discrepancies found | 12 (batches merged by ID collision) | **82** (true count) |
| Top-5 citations grounded at cited index | 3/5 (2 cited a depreciation chunk for compliance-credit / zero-emission claims) | **5/5** |

Final top-5 (every quoted phrase located at the cited sentence index):

1. **Non-operating items** — 10-K *"contributions to funded pension plans"* vs 10-Q *"finance lease payments"* in the same list.
2. **Offtake commitments** — 10-K *"approximately $4.7 billion"* vs 10-Q *"approximately $4.8 billion"* of maximum purchase commitments through 2035.
3. **Ford Credit liquidity** — 10-K *"$24.6 billion of liquidity"* (year-end 2025) vs 10-Q *"$28.1 billion of liquidity"* (Q3 2025).
4. **Financing cash flows** — 10-K *"$(2,360) $(2,495) $1,650 $(3,205)"* vs 10-Q *"$(1,791) $(2,817) $1,050 $(3,558)"*.
5. **Tariff net EBIT** — 10-K *"…about $3 billion…net EBIT impact about $2 billion"* vs 10-Q *"…second quarter of 2025…about $800 million"*.

**Unit proof** (`/tmp/unit_tests.py`): given an LLM response with wrong indices `a=0, b=0`, `_validate_llm_response` corrects them to `a=2, b=1` (the sentences that actually contain the quotes); the old first-word logic would have cited *"The Company operates in many markets."*

---

## Issue 2 — HIGH: Agent 2 evidence text contains raw XBRL/HTML

**Symptom.** Evidence excerpts in UI and JSON contained `<ix:nonFraction>`, `&#160;`, and other tags.

**Root cause.** The evidence the user sees is tokenized in `PromiseAuditAgent.run` at the pre-tokenization step — `split_sentences(get_cached_text(...))` was called on **raw** cached text. (The cached `texts/{T}_texts.json` is shared, and Agents 1/3 store raw HTML there, so Agent 2 frequently read un-stripped markup.) `_search_evidence_fast` consumes that pre-tokenized cache; it never fetches text itself, so the documented "fix it inside `_search_evidence_fast`" spot was a no-op — the real fix belongs at pre-tokenization.

**Fix (`promise_audit.py`).**
- Pre-tokenization now runs `clean_document(text)` (filings) and `clean_document(content, doc_type="transcript")` (transcripts) before `split_sentences`.
- `normalize_unicode` now calls `html.unescape(...)` first, so HTML entities (`&#160;`, `&amp;`, `&#8217;`) decode to real characters before NFKC normalisation.

**Before / after (unit check):**
```
in : 'Revenue was <ix:nonFraction contextRef="c1">1,234</ix:nonFraction>&#160;million in&#160;2025.'
out: 'Revenue was 1,234 million in 2025.'      # tags gone, number kept, &#160; -> space
```
Live F run: **0/5 top-promise evidence excerpts contain tags** (e.g. *"We had a balance of $5.3 billion and $5.5 billion of unearned revenue…"*).

---

## Issue 3 — HIGH: Agent 2 marks everything KEPT

**Symptom.** Deterministic fallback marked any promise with ≥2 word overlap as KEPT → 21/21 KEPT.

**Root causes found in `PromiseAuditAgent.run`.**
- The deterministic fallback was **mis-indented inside the `else:` (no-evidence) branch**, so it only ran when there was *no* evidence — and then defaulted everything to one bucket.
- When the LLM raised (oversized prompt), the exception **propagated and crashed the whole agent** — the safety net was never reached.

**Fixes.**
- **LLM enabled** and, for `len(promises) > 5`, **always split by category before building the prompt** (`_run_llm_batch` per category).
- **3-tier honest deterministic net**, run for *any* promise the LLM left unmatched (failure, overflow, or no evidence): `≥5` significant-word overlap → **KEPT**, `3–4` → **PARTIAL**, `<3` → **UNRESOLVED**. Stop-words and ≤3-char tokens are filtered so the threshold is meaningful, and self-matches (evidence from the promise's own filing) are dropped.
- **Honest reasoning** text: *"Strong keyword overlap with later filing"* / *"Partial keyword overlap with later filing"* / *"No corroborating evidence in later filings"* (replacing *"Deterministic keyword match"*).
- **Resilience:** `_run_llm_batch` catches LLM exceptions and returns `[]` (defers to the net) — the agent no longer crashes. Evidence is length-capped (`[:400]`) and count-capped (`STAGE2_EVIDENCE_CAP=60`) so prompts fit Groq, and a **stable global `evid_id`** is assigned so LLM evidence references resolve correctly even in per-category batches.

**Before / after (F 2025):**

| Status | Before | After |
|--------|--------|-------|
| KEPT | 21 / 21 (100%) | **1 (4.8%)** |
| PARTIAL | 0 | **7 (33.3%)** |
| BROKEN | 0 | **1 (4.8%)** |
| UNRESOLVED | 0 | **12 (57.1%)** |
| Score | — | 53.4 |

Distribution checks: KEPT < 60% ✅, PARTIAL > 0 ✅, BROKEN-or-UNRESOLVED > 0 ✅. The LLM contributes where the provider is reachable (e.g. the BROKEN verdict carries an analytical reason); when a batch fails, the net guarantees a complete, honest result rather than a crash or an all-KEPT wall.

---

## Issue 4 — MEDIUM: Agent 1 forecast-vs-actual false positives

**Symptom.** A 10-Q saying *"we expect lower production"* and a later 10-K saying *"we experienced lower production"* (the Novelis fire) were flagged as a contradiction — a forecast being confirmed, not a conflict.

**Fix (`corporate_discrepancy._build_discrepancy_prompt`).** Added a rule block to the LLM prompt:
> **FORECAST vs ACTUAL — DO NOT FLAG TENSE DIFFERENCES AS CONTRADICTIONS.** If one document uses future tense (expect, anticipate, plan, will…) and another uses past tense (experienced, resulted, was…) for the **same** event/metric, it is a forecast being confirmed — not a contradiction.

**Before / after.** The Novelis "lower production" forecast-vs-actual pair is **no longer in the F 2025 top-5**. (It had previously appeared as discrepancies #3/#5.)

---

## Issue 5 — MEDIUM: Agent 2 same-text circular matches

**Symptom.** A promise and its "evidence" were the **identical sentence** copied into a later filing (different URL) — no independent confirmation.

**Fix (`promise_audit.py`, evidence-merge step).** After evidence is attached, if the first 100 chars (whitespace-normalised, lower-cased) of the evidence equal those of the promise, the row is set to **UNRESOLVED** with reasoning *"Same disclosure found in later filing — no independent confirmation."*

**Before / after (F 2025):** **0 circular matches** among the top promises (down from the reported self-referential rows).

---

## Issue 6 — LOW: Agent 3 8-K duplicate findings

**Symptom.** The 8-K section showed the same event twice (BlueOval SK DOE Loan) — one entry with prior+current, one with only current.

**Fix (`legal_proceedings.py`).** New `_dedup_proceedings`, called right after the proceedings/events list is parsed (covers 10-K/10-Q `case_name` and 8-K `description`). Entries are merged by normalised case key; the **richer** entry (one carrying both prior and current quotes) wins.

**Before / after.**
- Unit proof: 3 events → **2** (the two BlueOval entries merge into one, keeping the prior+current version).
- Live F run: 3 findings, **no duplicate case names** in any doc-type; the 8-K's two proceedings are genuinely distinct (a security-holder vote and a material definitive agreement).

---

## Issue 7 — Dead files removed

`fmp.py`, `finnhub.py`, `llm_helper.py`, and `*.save` files are **absent** from the repository, and there are **no dangling imports/references** to them (or to any of the banned dead sources) in `sollia_working/src` or `sollia_web/backend`. `src/providers/news/` contains only an empty `__init__.py`.

---

## Final F 2025 output (after all fixes)

**Agent 1 — Corporate Discrepancy:** 82 raw discrepancies; top-5 all numerical contradictions with **5/5 grounded citations** (key terms verified against the cited sentence in each chunk): non-operating-item composition, offtake commitments ($4.7B vs $4.8B), Ford Credit liquidity ($24.6B vs $28.1B), financing cash flows, and tariff net-EBIT ($2B vs $800M).

**Agent 2 — Promise Audit:** 21 promises, score 53.4 — KEPT 1 / PARTIAL 7 / BROKEN 1 / UNRESOLVED 12. Evidence excerpts are clean prose (0 tags), 0 circular matches, honest reasoning strings.

**Agent 3 — Legal Proceedings:** 3 findings (10-K Asbestos UNCHANGED; 10-Q none; 8-K two distinct NEW events) — **no duplicate case names**.

## Guardrails honored
Core architecture untouched: LLM provider priority and `llm_config.call_llm` (no threading lock re-added), 3-tier document fetching, shared `.cache/` layout, and SEC API field names are all unchanged. The deterministic fallback was **strengthened, not removed** — it remains the safety net.

## Reproduce
```bash
cd "sollia_working"
export PYTHONPATH="$PWD"
python3.11 /tmp/unit_tests.py          # deterministic proofs (dedup + citation index)
python3.11 /tmp/test_agent2_F.py       # Agent 2 distribution + cleanliness + circular
python3.11 /tmp/test_agent3_F.py       # Agent 3 dedup
python3.11 /tmp/inspect_agent1.py      # Agent 1: dump each cited chunk + locate the quote
```
