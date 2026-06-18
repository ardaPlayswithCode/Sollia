# Sollia Regression Fix Report

**Date:** 2026-06-18
**Branch:** `optimization-pass`
**Context:** The previous "Output quality fix" pass (commit `fa4df1e`) resolved 7 output-quality
issues but introduced 3 regressions. This pass diagnoses and fixes all of them, verified
end-to-end on **F (Ford) 2025** with real cached SEC data.

**Files changed (3):**
- `sollia_working/src/agents/promise_audit.py` (Agent 2)
- `sollia_working/src/agents/corporate_discrepancy.py` (Agent 1)
- `sollia_working/src/agents/legal_proceedings.py` (Agent 3)

---

## REGRESSION 1 — CRITICAL: Agent 2 flipped all-KEPT → all-BROKEN, evidence text empty

### Symptom
Before the fix pass: 21/21 KEPT (over-crediting). After: 18/21 BROKEN, 0 KEPT, every row's
evidence text missing. The `clean_document(text)` call added at the Stage-2 pre-tokenization
step (promise_audit.py ~line 637) was blamed for "stripping too much."

### Root cause (NOT what it looked like)
`clean_document` was not returning empty — it was **silently deleting the body of the document**
inside `strip_safe_harbour()`. Step-by-step trace on Ford's 5.5 MB 10-K (acc `…25-000013`):

```
raw                       5,505,782 chars   1,888 sentences
after strip_html            883,496 chars   2,814 sentences   ✓
after strip_xbrl            883,496 chars   2,814 sentences
after strip_boilerplate     883,496 chars   2,814 sentences
after strip_safe_harbour    151,630 chars       5 sentences   ✗ deleted 730K chars
after normalize_*           136,301 chars      15 sentences
```

`strip_safe_harbour` used `this\s+(report|filing|…).*?forward[- ]?looking.*?(?=\n\n|\n\s*\n|$)`
with `re.DOTALL`. After HTML-stripping, an SEC filing has its whitespace collapsed and contains
**almost no `\n\n` paragraph breaks** (8 newlines in 883K chars). With no paragraph terminator,
the lazy `.*?` after "forward-looking" ran all the way to **EOF**, deleting the entire rest of
the document. 10-Ks always contain a "this report … forward-looking" disclaimer, so every raw
filing was gutted.

**Why it only surfaced now:** `clean_document` was previously only ever applied to the already
short, focused 30,000-char MD&A extracts (Stage 1). The fix pass started feeding it raw,
full-length filings at Stage 2 — exposing the latent landmine. This is also a **cache-format
collision**: the shared `.cache/texts/{ticker}_texts.json` holds MD&A extracts written by Agent 2
*and* raw HTML/iXBRL written by Agents 1/3 under the same accession key. The recent filings
(2025/2026) — exactly where evidence confirming older promises lives — were the raw ones.

Chain to the symptom: garbage tokenization → evidence search finds nothing → promises stay
`UNRESOLVED` → the "deadline in the past → BROKEN" rule (promise_audit.py ~line 833) flipped them
all to BROKEN with empty `evidence_text`.

### Fix
Rewrote `strip_safe_harbour` to remove only the disclaimer **sentence**, with length- and
boundary-bounded spans (`[^.\n]{0,N}`) that can never cross a sentence boundary or run to EOF:

```python
pattern  = r'(?i)\bthis\s+(?:report|filing|document|annual\s+report|quarterly\s+report|'
           r'presentation|transcript|call)\b[^.\n]{0,150}?forward[- ]?looking[^.\n]{0,400}?\.'
pattern2 = r'(?i)\bsafe\s+harbor?\b[^.\n]{0,200}?under\s+the\s+private\s+securities[^.\n]{0,400}?\.'
```

This keeps the original intent (don't extract the boilerplate disclaimer as a promise) while
making `clean_document` safe on any input — raw iXBRL included. No need to special-case the cache
format: full-document search actually gives *better* evidence recall (the strongest Ford evidence
came from Risk Factors, not just MD&A), and the XBRL-header noise never matches a promise's
keyword search terms, so it never surfaces as evidence.

### Verification (real F data)
- `clean_document` sentence yield on the raw 10-Ks: **15 → 3,054** and **22 → 749** (MD&A extracts
  unchanged).
- `_search_evidence_fast` for a Ford promise: **0 → 20** clean-prose candidates, **0 flagged as
  XBRL/URL noise**.
- Full Agent 2 run on F: distribution **KEPT=15, PARTIAL=5, BROKEN=2, PENDING=1, UNRESOLVED=37**
  (score 84.1) — a realistic mix, not all-one-status. Evidence text present and clean
  (**0/5 top promises contained XBRL/HTML tags**). Reasoning is honest and varied (e.g. "Partial
  keyword overlap with later filing"), not "deadline has passed" on every row.
- `strip_safe_harbour` sanity tests: still removes a real disclaimer sentence; never nukes a
  no-newline body; does not over-match across sentence boundaries.

---

## REGRESSION 2 — HIGH: Agent 1 ignored the cache (60s SEC download + timeout)

### Root cause
`SECFilingFetcher._get_all_filings()` and `_fetch_htm()` guard their cache reads with
`ticker = getattr(self, '_current_ticker', None)`. But **`_current_ticker` was never assigned
anywhere** — only read. `getattr` therefore always returned `None`, so:
- `_get_all_filings` took the `else` branch → `_fetch_filings_from_sec()` → re-downloaded the full
  submissions JSON **plus every historical archive** (rate-limited loop = the ~60s phase).
- `_fetch_htm` skipped the text cache → re-downloaded every filing's HTML → ran out the time
  budget → timeout.

`F_filings.json` (raw SEC submissions format) and all needed 10-K/10-Q texts were sitting in the
cache the whole time, unused. (Agent 1 reads its own `_strip_html`, not `clean_document`, so the
Agent 2 change never affected it.)

### Fix
Set `_current_ticker` so the existing cache logic actually runs:
- `__init__`: initialize `self._current_ticker = None`.
- `align_fiscal_year(self, ticker, year)`: `self._current_ticker = ticker` (covers instance #1).
- `fetch_all_texts()`: `fetcher._current_ticker = ticker` after constructing instance #2.

### Verification (real F data, network blocked to prove cache use)
- `_get_all_filings('0000037996')` returned all **4,428 filings in 0.00s with the network
  blocked** (pre-fix this raised "NETWORK CALL ATTEMPTED"). Correctly resolved the 2025 10-K.
- Full Agent 1 run on F: **no 60s download phase**, completes in ~120–160s (FinBERT embedding +
  LLM cross-referencing only), **5 grounded discrepancies** with real citation pairs, e.g.
  "$24.6 billion of liquidity" vs "$28.1 billion of liquidity". Score 96.

---

## REGRESSION 3 — MEDIUM: Agent 3 duplicate 8-K finding + lost asbestos sub-findings

### Root cause
`_dedup_proceedings` keyed on the **case_name/description label**. That single choice caused two
*opposite* failures, both confirmed from real cached LLM responses:
- **Under-merge (the duplicate BlueOval finding):** one 8-K passage was extracted under three
  different item headers — "Entry into a Material Definitive Agreement", "Creation of a Direct
  Financial Obligation", "Other Events" — all quoting the *same* BlueOval SK DOE Loan sentence.
  Different labels → kept as duplicates.
- **Over-merge (lost asbestos sub-findings):** Ford lists several distinct "Asbestos Litigation"
  disclosures with the *same* label but different quotes. Same label → the richest entry evicted
  the others, dropping "Annual payout and defense costs may become significant in the future."

Note: the suggested "dedup by URL" is unsafe here — `_dedup_proceedings` runs per-filing *before*
`edgar_url` is assigned, and all proceedings in one filing share a single URL, so a URL key would
collapse every distinct proceeding (asbestos included) into one.

### Fix
Key on the **cited passage (`current_quote`, then `prior_quote`)**, not the label — because the
identity of a legal matter is the passage it quotes. Quote-less entries fall back to the label so
genuinely empty duplicates still collapse, but a substantial quote (≥30 chars) is never merged
across two different passages.

### Verification (real cached LLM responses + full F run)
- BlueOval 8-K: **4 → 2** (the 3 same-quote items merge; the distinct "Submission of Matters to a
  Vote" item is preserved). Merged entry keeps the meaningful "Entry into a Material Definitive
  Agreement" label.
- Asbestos 10-K: **3 → 3** — all distinct sub-findings preserved, including the previously-lost
  "Annual payout and defense costs…".
- Full Agent 3 run on F: **3 filing types** (10-K, 10-Q, 8-K); no spurious same-matter duplicates;
  sub-findings intact.

(The 3 asbestos rows share the category label "Asbestos Litigation" by design — they are distinct
disclosures with distinct quotes, which is what "preserve sub-findings" requires. They are not the
same-matter duplicates the regression was about.)

---

## BONUS — robustness fix surfaced during verification

Agent 2's `_call_llm` **re-raised** when all providers failed, but its callers (the Stage 1 loop
and Stage 2 batches) branch on an *empty* response and fall through to the deterministic net. A
transient Groq 429 burst therefore crashed the whole agent instead of degrading. Changed the final
`raise` to `return "", ""`, honoring the contract and preserving the "deterministic fallback is the
safety net" architecture. Confirmed: under live rate-limiting, Agent 2 no longer crashes — it skips
failed batches and still produces a scored, evidenced result.

---

## Additional checks from the brief

| Check | Result |
|---|---|
| Agent 2 score distribution is a mix, not all-one-status | ✅ KEPT 15 / PARTIAL 5 / BROKEN 2 / PENDING 1 / UNRESOLVED 37 |
| Evidence text clean prose, no XBRL/HTML, not empty | ✅ 0/5 top promises had tags; matched promises carry non-empty evidence |
| Evidence present in clean JSON export (promise + evidence = 2 citations) | ✅ 3/5 findings now emit 2 citations (was 1); driven by populated `evidence_text` |
| Agent 2 reasoning honest, not "deadline passed" on every row | ✅ varied reasons; "deadline passed" only for genuinely past-due, evidence-less promises |
| Agent 3 10-K asbestos sub-findings preserved | ✅ 3/3 retained |
| Cache integrity / no cross-agent collision | ✅ Agent 2 now robust to raw HTML in shared text cache; Agent 1 reads its own submissions-format filings cache; writes are atomic (temp+rename) |
| SEC camelCase response field names unchanged | ✅ untouched |
| Deterministic fallback retained | ✅ retained and now reachable on LLM failure |

## Final verification summary (F 2025, real data)

- **Agent 1:** 5 grounded discrepancies, cache hit, no 60s download, no timeout. ✅
- **Agent 2:** realistic KEPT/PARTIAL/BROKEN/PENDING/UNRESOLVED mix, clean non-empty evidence,
  honest reasoning, 2 citations per matched promise, no crash under rate-limit. ✅
- **Agent 3:** 3 filing types, BlueOval duplicate merged, asbestos sub-findings preserved. ✅

### Note on environment
Groq's free-tier rate limit was hit during back-to-back verification runs. The fixes handle this
gracefully (Agent 2 degrades instead of crashing); the elevated UNRESOLVED count partly reflects
rate-limited Stage-1 batches falling to the deterministic net rather than the LLM. With a warm LLM
cache / unthrottled key, more promises resolve to KEPT/PARTIAL with semantically-matched evidence.
All successful LLM responses are cached, so subsequent runs are faster and more complete.
