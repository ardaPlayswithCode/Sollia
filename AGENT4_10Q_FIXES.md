# Agent 4 — 10-Q Comparison Output-Quality Fixes

Two output-quality bugs in Agent 4's (Risk Factor Delta Scanner) **update-only
10-Q** comparison path, found in the Ford (`F`) output, are fixed. Both are
addressed with **deterministic post-processing** — the shared LLM prompt template
is **not** touched, so the 10-K-vs-10-K and 10-Q-reprint paths are byte-for-byte
unaffected.

All changes are in `sollia_working/src/agents/risk_factors.py`.

## Background: the update-only data flow

For an update-only 10-Q (a short "Updates to Risk Factors" delta), Agent 4
compares it against the full 10-K baseline:

- **OLD** = the full 10-K (Ford: ~93,000 chars)
- **NEW** = the short 10-Q update (Ford: ~3,547 chars)

This asymmetry — a partial NEW document against a complete OLD one — is the root
of both bugs.

## Bug 1 — false-positive REMOVED findings

**Symptom.** The 10-Q output flagged `REMOVED | Global Political Instability`.
That risk appears in the 10-K baseline but not in the short update.

**Root cause.** The classifier defines REMOVED as "a risk in OLD with no match in
NEW." Because NEW is only a partial update, *any* 10-K risk not re-stated this
quarter trivially satisfies that condition. A removal cannot be reliably detected
unless the NEW filing is a full reprint.

**Fix (post-process, chosen over the prompt option).** When the comparison is
update-only, drop every REMOVED finding. See `_filter_update_only`.

Why not the prompt option (Option A):
1. **Determinism.** The acceptance criterion is absolute ("ZERO REMOVED"). A
   prompt instruction is only a request the model may ignore — especially the
   `llama-3.1-8b` fallback that the `MAX_SECTION_CHARS` comment documents as the
   only safety net when the 70B's daily quota is spent. The filter guarantees the
   outcome on every model and every run.
2. **No 10-K-path risk.** Leaving the shared template untouched means the
   10-K-vs-10-K path is provably unchanged.
3. **The suggested wording was inverted.** Option A's sentence describes the OLD
   document as the partial update, but in this path the **NEW** document is the
   partial one. Adopting it verbatim would have mis-described the data flow to the
   model.

## Bug 2 — forced match between unrelated risks

**Symptom.** `ESCALATED | Regulatory Compliance` paired an OLD quote about
**warranty accounting** ("We accrue the estimated cost of both base warranty
coverages and field service actions…") with a NEW quote about **regulatory
compliance agreements** being terminated/renegotiated. Different risks; the model
forced a match and mislabeled it ESCALATED.

**Root cause.** A genuinely new 10-Q risk often has no counterpart in the 10-K
baseline, but the model defaults to finding *some* match rather than emitting NEW.

**Fix (post-process, chosen over the prompt / context-window options).** When an
OLD/NEW quote pair shares **zero** significant (topical) words, the pairing is
spurious: reclassify the finding as **NEW** with an empty OLD quote, so the new
risk stands on its own. See `_quotes_unrelated` / `_filter_update_only`.

The heuristic is deliberately **conservative** — it fires only on a complete
topical disjoint, and only when both quotes carry ≥2 content words. This cleanly
separates the two paired findings in Ford's output:

| Finding | OLD ∩ NEW significant words | Action |
|---|---|---|
| #3 Regulatory Compliance (the bug) | `∅` (0 words) | reclassify → NEW |
| #4 Warranty & Field Service (legit reword) | `{financial}` (1 word) | **kept** as REWORDED |

Why not the alternatives:
- **Prompt strengthening (Option A):** model-dependent; same determinism argument
  as Bug 1.
- **Raise `MAX_SECTION_CHARS` (Option C):** the in-code comment documents hard
  Groq per-minute token budgets; raising it would break the 8B fallback and is an
  architectural change the task forbids.

Significant words = 4+ letter tokens minus a small grammatical stopword set
(`_TOPIC_STOPWORDS`). Domain nouns (warranty, regulatory, financial, …) are
intentionally **not** stopwords, so real topical overlap is preserved.

## What changed in code

- **`_TOPIC_STOPWORDS`** (new module constant) — grammatical glue words ignored
  when judging topical overlap.
- **`_topic_words(quote)`** (new) — significant words of a quote.
- **`_quotes_unrelated(old, new)`** (new) — True iff the pair shares zero
  significant words (with a ≥2-content-word guard).
- **`_filter_update_only(changes)`** (new) — drops REMOVED findings (Bug 1) and
  un-forces topically-unrelated matches → NEW (Bug 2).
- **`_build_top_changes(...)`** — added an `update_only: bool = False` parameter;
  when set, it runs `_filter_update_only` after the existing dedup/reclassify
  passes and before ranking. Default `False` → the 10-K and 10-Q-reprint callers
  are unchanged.
- **`_run_10q_phase`** — passes `update_only=(fmt == "update_only")` to
  `_build_top_changes`.

The LLM prompt template (`RISK_DELTA_PROMPT`) and `_build_comparison_prompt` are
**unchanged**.

## Verification (ticker `F`)

Run: `python3.11 -m src.agents.risk_factors F` from `sollia_working/`.

**10-Q update-only path (vs 10-K 2026-02-11):**

| | Before | After |
|---|---|---|
| REMOVED findings | 1 (`Global Political Instability`) | **0** ✓ |
| Regulatory Compliance | `ESCALATED`, OLD = warranty quote | **`NEW`**, OLD empty ✓ |
| total findings | 9 | 8 |

**Quotes verified against cached source text** (independent of the agent's own
flag):
- #3 NEW quote — present in the 10-Q source (`True`), absent from the 10-K
  baseline (`False`) → genuinely new, correctly NEW.
- Bug 1 dropped quote — present in the 10-K (93,085 chars), absent from the 10-Q
  update (3,547 chars) → confirmed it was *not re-stated*, not removed.

**10-K-vs-10-K path:** still runs and produces 5 quote-verified findings
(`BEARISH`). Unaffected, as expected (it never calls the new code path).

`python3.11 -m py_compile src/agents/risk_factors.py` passes after every edit.
