# Agent 4 — 10-Q Support (Risk Factor Delta Scanner)

Adds **10-Q comparison** to Agent 4 on top of the existing 10-K vs 10-K delta.
After the annual comparison, the agent now checks for a 10-Q filed **after** the
most recent 10-K, classifies its format, compares it against the correct
baseline, and folds those findings into the score. The 10-Q path is **purely
additive** — the 10-K vs 10-K path is unchanged.

## The four rules (as implemented)

1. **Detect format** — is the 10-Q's Item 1A a full *reprint* or *update-only*?
   (deterministic, no LLM — see below).
2. **Reprint** → compare the latest 10-Q against the **previous 10-Q in this
   cycle** (cumulative, like 10-K vs 10-K). If this is the cycle's first 10-Q,
   compare against the **10-K**.
3. **Update-only** → compare the 10-Q against the **10-K baseline** (OLD is
   always the most recent 10-K).
4. **10-K vs 10-K** → always compared directly (both full reprints — unchanged).

"This cycle" = 10-Qs filed strictly after the most recent 10-K. The baseline 10-K
is the most recent one (the `new_filing` of the annual comparison).

## Format detection (`detect_format`, deterministic)

Any **one** check is sufficient to classify as `reprint`; otherwise the default
is the safer `update_only`.

1. **Length** — 10-Q risk section > 70% of the 10-K's length (chars) → reprint.
   (Update-only sections are 1–3 pages vs 15–30 for the full section.)
2. **Structure** — bolded risk-title count within ±30% of the 10-K's
   (via the existing `_estimate_risk_count`) → reprint.
3. **Content** — > 80% word overlap on the first ~200 words (first 500 chars,
   HTML already stripped) → reprint. (Intros aren't rewritten for updates.)

## Files changed

| File | Change |
| --- | --- |
| `sollia_working/src/agents/risk_factors.py` | Module-level `detect_format()`; `_normalize_filing(..., form="10-K")` now form-aware; `_all_filings_from_cache()` + `_get_all_filings()` (cache-first 10-K **and** 10-Q); `_build_comparison_prompt()` (same template, dynamic INPUT labels); `_build_top_changes()` (shared parse→dedup→reclassify→rank→verify→link pipeline, extracted from `run()`); `_run_10q_phase()`; `run()` wires the 10-Q phase + combined score. |
| `sollia_web/backend/runs.py` | `_findings_risk()` now emits a `source` field (`"10-K"`/`"10-Q"`) on every finding and appends `ten_q_findings` with 10-Q-aware document labels (extracted shared `_risk_finding()` helper). |
| `sollia_web/frontend/src/App.jsx` | Risk Factors card renders a `ten_q_findings` subsection below the 10-K findings (same `Row`/`Quote` components). |

## Output additions

`result["ten_q_findings"]` (or `None` when no usable 10-Q exists):

```json
{
  "format": "update_only",            // or "reprint"
  "filing_date": "2026-04-30",
  "filing_url": "https://www.sec.gov/.../f-20260331.htm",
  "compared_against": "10-K",          // or "10-Q"
  "compared_date": "2026-02-11",
  "compared_url": "https://www.sec.gov/.../f-20251231.htm",
  "changes": [ /* same schema as top_changes */ ],
  "changes_found": 9
}
```

**Scoring** — the 10-Q is weighted 1.5× (more recent):
`final = (ten_k_score + 1.5 * ten_q_score) / 2.5`, clamped to `[10, 90]`.
With no 10-Q, the score is exactly the 10-K score as before.

## Test — Ford (F), verified end-to-end

Standalone (`python3.11 -m src.agents.risk_factors F`) and via the restarted
web backend (`POST /api/analyze {"ticker":"F","agents":["risk_factors"]}`):

- **10-K vs 10-K unchanged** — 5 changes, 10-K-only score `10`.
- **10-Q fetched** — `2026-04-30`; format detection in the log:
  `Found 10-Q from 2026-04-30 — format: update_only`
  (risk section 3,547 chars vs 93,085 for the 10-K = 3.8% → update-only).
- **Correct baseline** — compared against the 10-K `2026-02-11`.
- **Findings** — 9 changes (4 NEW, 3 ESCALATED, 1 REMOVED, 1 REWORDED),
  8/9 quote-verified; NEW quotes link to the 10-Q (`f-20260331.htm`), OLD
  quotes to the 10-K (`f-20251231.htm`).
- **JSON export** — 14 findings total (`source`: 5 × `10-K`, 9 × `10-Q`);
  10-Q citation labels `10-Q (2026-04-30)` / `10-K (2026-02-11)`.
- **Combined score** — `(10 + 1.5 × 16) / 2.5 = 13.6 → 14` (BEARISH). ✓
- **Cache-first** — filings metadata and both 10-K and 10-Q texts served from
  `.cache/` on subsequent runs (verified present after first fetch).
- **Frontend** — `ten_q_findings` subsection added; `eslint` passes (0 errors).

## Constraints respected (unchanged)

- LLM via `call_llm` only (Groq-70B → Groq-8B → OpenRouter); `llm_config.py`,
  `document_priority.py`, and the cache provider untouched.
- SEC EDGAR provider only; 3-tier fallback; cache checked before every download.
- `MAX_SECTION_CHARS = 8000`, `LLM_MAX_TOKENS = 1800` unchanged.
- Same LLM prompt template — only the INPUT-section labels change per comparison.
- All do-not-modify functions (`extract_risk_factors`, `_dedup_changes`,
  `_reclassify_escalations`, `_calculate_score`, `_signal`, `_is_identical_pair`,
  `_collapse_global_changes`, `_estimate_risk_count`, `_verify_quote`) untouched
  and still drive the 10-K path; the 10-Q path reuses them.
- Every quote verified against its source before inclusion.
- `py_compile` clean on both changed Python files after every edit.
