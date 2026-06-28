# SEC API Source Evaluation — Part 1
**Date:** 2026-06-26 · **Method:** live probing of the **allowed** SEC endpoints across AAPL/GE/TSLA with **real** accessions resolved from submissions JSON (no dead sources tested — they are forbidden by policy; no fabricated composite scores). Harness: `scratchpad/sec_sources.py` + the real-accession pass.

## Summary
Empirical probing **validates the current priority list** and adds two evidence-based refinements: (1) **`index.json` is promoted to a first-class step** (it is essential for resolving the correct document inside an accession — proven critical by the Agent-3 8-K fix this pass), and (2) **EDGAR full-text search (efts) is confirmed slow + flaky** and stays a last-resort only.

## Measured endpoint behavior (real accessions, latest 10-K each)

| Endpoint | AAPL | GE | TSLA | Reliability | Latency | Notes |
|---|:--:|:--:|:--:|:--:|:--:|---|
| **`data.sec.gov/submissions/CIK{cik}.json`** | 200 | 200 | 200 | **3/3** | ~200ms | Authoritative filing index. Needs `Host: data.sec.gov` header. |
| **`/Archives/.../{primaryDocument}.htm`** | 200 | 200 | 200 | **3/3** | ~110-150ms | The filing text. **text yield only 16-20%** (rest is inline-XBRL/markup) → must be cleaned. |
| **`/Archives/.../index.json`** | 200 | 200 | 200 | **3/3** | ~60-70ms | Lists **every** doc in the accession (105/19/… docs). **Essential** to find EX-99 exhibits & avoid the iXBRL cover stub. |
| **`browse-edgar?...&output=atom`** | 200 | 200 | 200 | **3/3** | ~170ms | Reliable secondary for filing **discovery** by form type (22-35 entries). |
| **`efts.sec.gov/LATEST/search-index`** | 200 | 200 | **500** | flaky | **3.7-4.4s** | Full-text search. **Returns HTTP 500 on phrases containing commas/dates/`$`**; 10-20× slower than archives. Last-resort only. |

> The 404s seen in the first probe pass were **my harness's invented accession numbers**, not endpoint failures — every endpoint returns 200 when fed a real accession resolved from submissions JSON.

### Format-quality findings (raw 10-K HTML)
- **Text yield 16-20%** — modern filings are ~80% inline-XBRL/markup. Confirms the need for `extract_risk_factors` / `extract_legal_section` cleaning (regex strip → the exact text the verifier searches).
- **XBRL contamination:** ~6,000 `ix:/us-gaap:/xbrli:` markers per 10-K; the inline-XBRL *cover* renders as `us-gaap:…` token soup with **no narrative** — this is exactly the trap that broke Agent-3's 8-K branch (cover `primaryDocument` ≠ event text). Resolved via `index.json` → exhibit selection.
- **Section markers:** `Item 7`/`Item 1A`/`Item 3` reliably present and regex-locatable in 10-K/10-Q.
- **8-K reality:** the narrative lives in the **body + EX-99 exhibits**, not the cover. `index.json` is the only reliable way to enumerate them.

## efts full-text search — known failure modes & workarounds
- **500 on punctuated phrases:** strip `$` and `,` before quoting (verified: sanitized phrases return 200). Generic phrases (e.g. an 8-K item title) match 10,000+ docs → useless for disambiguation; only distinctive ≥8-word phrases are reliable.
- **Use case that works:** finding the *true source accession* of a real quote (used this pass to prove Agent-3's GE/COIN citations pointed at the wrong 8-K). Keep it as a **diagnostic / last-resort**, never primary.

## Final priority list (per document type) — see `FINAL_API_PRIORITY_LIST.md`
No dead sources are referenced anywhere in code (verified: `sec_edgar.py`, `transcripts/sec_transcript.py` use only `sec.gov`/`data.sec.gov`/`efts.sec.gov`). The current list stands, with `index.json` elevated and efts demoted to last-resort.
