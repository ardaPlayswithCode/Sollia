# JSON Export Upgrade — Enterprise Integration Layer

**Date:** 2026-06-18
**Scope:** Add consumer-facing context to the `/api/analyze` JSON export so a fund's
risk system can act on findings without opening the UI — while keeping every
internal field, model name, threshold, and pipeline detail out of the payload.

**Verified end-to-end on a live `F` 2025 run** (sequential harness, deterministic
unit tests, and a real `POST /api/analyze` call against the restarted server).
Result: `signal=HOLD`, `score=56.0` — corporate 96 (BEARISH), promise 71.8
(BULLISH), legal 48.3 (NEUTRAL). All validations passed.

---

## Files changed

| File | Change |
|---|---|
| `sollia_web/backend/runs.py` | Export enrichment. The export builder is **`build_clean_export`** (the brief called it `_build_export`; that name does not exist). Rewrote `_findings_corporate` / `_findings_promise` / `_findings_legal` and added enrichment helpers. |
| `sollia_working/src/agents/legal_proceedings.py` | Added `_quote_in_text()` and call it after `_dedup_proceedings` to confirm each LLM-quoted legal excerpt is actually present in the source filing text — the only honest way to populate `verified` for Agent 3 (see below). Pure string match: no model call, no cache write, no change to the filings-cache contract. |

No top-level schema change. Adding Agents 4–8 still just adds keys under `agents: {}`.

---

## What was added (and exactly how each value is derived)

Every value below comes from data the pipeline already produces. **Nothing is
fabricated** — where a magnitude or fact was not actually computed, the wording
stays at the level we can defend.

### 1. Citation roles — `citations[].role`
- Agent 1: `source_a` / `source_b` (which document in the compared pair).
- Agent 2: `promise` / `evidence` (the claim vs. the verification).
- Agent 3: `prior` / `current` (older filing vs. newer).

Derived structurally from which slot the citation occupies — no inference.

### 2. Pair context — Agent 1 `pair`
`"10-K vs 10-Q (Q3 2025)"`. Reuses the agent's existing `pair_display` label (built
from the human-readable document types), falling back to the raw `pair` key with
`_vs_` → ` vs ` if `pair_display` is absent. No new computation.

### 3. Severity reasoning — `severity_reason` (high/medium findings only)
One sentence keyed to the concrete reason a finding is severe. Added **only** when
`severity` is `high` or `medium`; low-severity findings omit it.
- Corporate: keyed to discrepancy type (numerical / narrative / omission /
  qualitative). We do **not** invent a "differ by >X%" number — we never computed
  one — so the wording says figures "conflict between the two filings."
- Promise: domain (revenue / product / margin / R&D / market / strategic) + outcome
  (kept / broken / partial / pending / unresolved).
- Legal: keyed to status. Covers both changes (new/escalated/settled/…) **and**
  proceedings carried forward `UNCHANGED`/`NO_UPDATE`, so the sentence never
  contradicts the status (an early version called an `UNCHANGED` proceeding a
  "change" — fixed).

### 4. Verification flag — `citations[].verified`
"Was this excerpt confirmed to exist in the cited document?" Implemented honestly
**by provenance**, which differs per agent:

- **Corporate** `sentence_text` is pulled verbatim from the parsed filing chunk
  (`sents[idx]`). A non-empty excerpt is, by construction, present in the document
  → `verified: true`. An unresolved chunk yields an empty excerpt → `false`.
- **Promise** `text` survives extraction only if it matches a real filing sentence
  (verbatim or fuzzy-replaced with the matched sentence) or it is dropped;
  `evidence_text` is a real matched filing sentence. Both are document-backed →
  `verified: true` when non-empty.
- **Legal** `prior_quote` / `current_quote` are the LLM's *claim* of "exact text"
  and were never re-checked. Marking them `true` on faith would be fabricating a
  trust signal. So `legal_proceedings.py` now string-matches each quote against the
  extracted legal section (`_quote_in_text`, with smart-quote/whitespace/case
  normalization and a prefix match for long quotes). `verified` reflects that real
  check — on the F run all 8 legal quotes matched (`true`); a fabricated quote
  returns `false` (covered by a unit test).

### 5. Agent 2 evidence quality — `evidence_quality`
`strong | partial | weak`, on every promise finding. Significant-word overlap
between the promise and its evidence, with the matcher's own confidence as a
tie-break. Thresholds (≥5 → strong, ≥3 → partial, else weak) deliberately mirror
Agent 2's own KEPT/PARTIAL/UNRESOLVED fallback tiers. No evidence text → `weak`.

### 6. Agent 3 change direction — `change_direction`
`positive | negative | neutral`, on every legal finding. settled/dismissed →
positive; new/escalated/quietly_removed → negative; unchanged/no_update/amended/
terminated → neutral. This **mirrors Agent 3's own scoring polarity**
(`_calculate_score`), so the export can't disagree with the agent that produced it.

---

## Internal-field firewall (confirmed clean)

A recursive walk of the live export asserts **none** of these appear, no key starts
with `_` or contains `internal`, and no banned token appears in any value:

`agent_weights, agent_scores, similarity, batch_key, chunk_id, embedding, _emb_c,
score_components, final_score, same_filing, rank, weighted_importance, decay,
severity_base, type_multiplier, historical_correlation, promise_id,
document_type_raw, page_number, sentence_index, htm_link, *_accession, cik,
current_quote_verified, prior_quote_verified` — and tokens `finbert, llama-3, groq,
openrouter, cosine, gaussian, score_components, align_all_pairs,
_validate_llm_response`.

The two intermediate flags added to the agent (`current_quote_verified` /
`prior_quote_verified`) are consumed by the export builder and collapsed into the
clean `verified` boolean — they never reach the export themselves (explicitly
asserted).

## Legacy Synthesizer fields — already absent from the export

`agent_weights`, `similarity`, and `historical_correlation` do **not** appear in the
export and required no removal. They live only in the separate rich **UI** payload
(`_build_rich_result`), which is a different object from the archived export
(`build_clean_export`). The enterprise/compliance artifact — the `export` field — is
and stays clean. (No code change needed; documented here so a future pass doesn't
"remove" them from the export looking for something that was never there.)

> Note: `document` labels are left as the agent's existing format (`"10-K"`,
> `"10-Q (Q3 2025)"`). The brief's sample showed `"10-K FY2025"`; I did not rewrite
> document labels (out of scope, and the UI shares them). The `pair` field already
> carries the full period context.

---

## Verified schema (live `F` 2025, excerpts trimmed)

```json
{
  "ticker": "F", "year": 2025, "signal": "HOLD", "score": 56.0,
  "agents": {
    "corporate_discrepancy": { "name": "Corporate Discrepancy", "score": 96.0, "signal": "BEARISH",
      "top_findings": [{
        "type": "numerical_contradiction", "severity": "high",
        "severity_reason": "Reported financial figures conflict between the two filings.",
        "pair": "10-K vs 10-Q (Q3 2025)",
        "summary": "The 10-K passage lists 'contributions to funded pension plans'…",
        "citations": [
          {"role": "source_a", "document": "10-K", "excerpt": "Non-operating items include…",
           "url": "https://www.sec.gov/Archives/edgar/data/37996/000003799626000015/f-20251231.htm", "verified": true},
          {"role": "source_b", "document": "10-Q (Q3 2025)", "excerpt": "Non-operating items include…",
           "url": "https://www.sec.gov/Archives/edgar/data/37996/000003799625000186/f-20250930.htm", "verified": true}
        ]}]},
    "promise_audit": { "name": "Promise Audit", "score": 71.8, "signal": "BULLISH",
      "top_findings": [{
        "type": "promise_broken", "severity": "high",
        "severity_reason": "Material market-expansion commitment with no or contradicting follow-through.",
        "evidence_quality": "weak",
        "summary": "No evidence of material impact from conflict or economic downturn.",
        "citations": [
          {"role": "promise", "document": "10-Q", "excerpt": "Our guidance does not include…",
           "url": "https://www.sec.gov/Archives/edgar/data/37996/000003799626000086/f-20260331.htm", "verified": true}
        ]}]},
    "legal_proceedings": { "name": "Legal Proceedings", "score": 48.3, "signal": "NEUTRAL",
      "top_findings": [{
        "type": "new", "severity": "medium",
        "severity_reason": "New legal proceeding or material agreement disclosed.",
        "change_direction": "negative",
        "summary": "Submission of Matters to a Vote of Security Holders",
        "citations": [
          {"role": "prior", "document": "8-K (prior 2026-05-19)", "excerpt": "On May 14, 2026…",
           "url": "https://www.sec.gov/Archives/edgar/data/37996/000003799626000090/f-20260514.htm", "verified": true},
          {"role": "current", "document": "8-K (current 2026-05-21)", "excerpt": "As previously disclosed…",
           "url": "https://www.sec.gov/Archives/edgar/data/37996/000003799626000093/f-20260520.htm", "verified": true}
        ]}]}
  }
}
```

---

## Testing & verification

1. **`py_compile`** on both edited files after every edit — clean.
2. **Full sequential pipeline** (`/tmp/sollia_export_test.py`) — ran all three agents
   on `F` 2025 (sequentially, to avoid the documented Groq concurrent rate-limit),
   synthesized, built the export through the real `build_clean_export`, and asserted:
   field presence per agent, every high/medium finding has `severity_reason`, every
   citation has `verified`, valid roles, and zero internal leakage. **PASS.**
3. **Deterministic unit tests** (`/tmp/sollia_unit_test.py`, 38 assertions) — cover
   the branches the live data couldn't hit because of the LLM outage below:
   `evidence_quality` strong/partial/weak, `_quote_in_text` true **and false**
   (fabricated quote, smart-quote variant, empty/None), every `change_direction`,
   every `severity_reason` status (incl. the `UNCHANGED` fix), `verified: false`
   legal path, key ordering, and low-severity findings omitting `severity_reason`.
   **ALL PASSED.**
4. **Live API** — restarted the backend (killed the stale pre-edit process on :8000,
   relaunched under homebrew py3.11), then `POST /api/analyze` for `F` 2025 with all
   three agents → **HTTP 200 in 83s**. Re-ran the full validation on the response's
   `export` field. **PASS — server export carries all upgrades, zero internal leakage.**

### Known limitation observed (pre-existing, not introduced here)
On both live runs Agent 2's Stage-1/2 LLM calls hit `All LLM providers failed`
(Groq rate-limit — the documented failure mode), so it fell back to deterministic
matching and surfaced **no `evidence` citations**; every promise therefore shows
`evidence_quality: "weak"` and a `promise`-only citation. The export represents this
correctly. The `strong`/`partial` quality and the `evidence` role are proven by the
deterministic unit tests rather than this particular live run. When providers are
healthy, those paths populate automatically — no further change needed.

## Addendum — extensibility proven with a real 4th agent (`risk_factors`)

While this work was in progress a fourth agent, **`risk_factors`** (Item 1A risk-factor
changes between annual 10-Ks), was added to the codebase — registered in
`backend/agents.py`, backed by `sollia_working/src/agents/risk_factors.py`, and wired
into the export with its own `_findings_risk` builder. This is the exact "Agents 4–8"
case the schema was designed for. **My export code required no changes to absorb it**
(`build_clean_export` dispatches via `_FINDING_BUILDERS.get(aid)` / `AGENT_NAMES.get`).

Verified end-to-end on `F` (ran the agent standalone to avoid the concurrent
rate-limit; live server confirms all four agents are served):
- Export entry present: `name="Risk Factors"`, `score=40.0`, `signal=NEUTRAL`, 10 findings.
- New fields all correct: citation roles `old`/`new`, `change_direction` on every
  finding, `severity_reason` on all 3 high/medium findings, every citation has `verified`.
- **`verified`: 17 true / 1 false** — one LLM-supplied quote did not match the source
  10-K and was correctly flagged `false`. The agent computes this with the same
  honest pattern as legal (`_verify_quote` against the extracted Item 1A section,
  setting `old_quote_verified` / `new_quote_verified`), and those intermediate flags
  do **not** leak into the export.
- Recursive walk + token scan on the risk entry: **zero internal leakage.**

Sample: `{"type":"risk_new","severity":"high","severity_reason":"A new risk factor
appeared that was absent from the prior 10-K.","change_direction":"negative", … }`.

## Server state
The backend is **running on :8000 with the current code, serving all four agents**
(`corporate_discrepancy`, `promise_audit`, `legal_proceedings`, `risk_factors`).
