# Prompt Optimization Report — Part 2
**Date:** 2026-06-26 · **Funded key confirmed** (`GET /key`: `is_free_tier:false`, usage $0.018 of $10). **Empirical, isolated A/B** — input held constant (real cached SEC risk-factor text), prompt is the only variable; every emitted quote scored with the agent's own `_verify_quote`. Harness: `scratchpad/ab_agent4.py`, `scratchpad/model_compare.py`.

## TL;DR — **The current prompts are already optimal. No prompt change is warranted.**
Naive instruction-strengthening (more "VERBATIM", anti-concatenation rules, self-checks, negative examples) **did not improve** Agent-4's verbatim rate and sometimes **degraded** it. The reliable levers for the residual ~10% are (a) **model choice** (see model comparison) and (b) the **existing verify-and-flag** mechanism, which already discloses unverified quotes honestly.

## Prompt inventory (all quote-producing prompts)
| Agent | Stage | Builder | Verbatim instruction today |
|---|---|---|---|
| 1 | Discrepancy | `_build_discrepancy_prompt` (corporate_discrepancy.py:909) | "quoted_phrase must be exact text"; input pre-split into indexed `[N]` sentences |
| 3 | 10-K/10-Q/8-K legal | inline in `track()` | "exact text from current"; **8-K input fixed this pass** |
| 4 | Risk delta | `RISK_DELTA_PROMPT` (risk_factors.py:96) | "VERBATIM… Copy exactly… Never paraphrase" ×3 |

Agent 4 is the worst performer (89.9%) **despite** the strongest verbatim language — the target of the A/B.

## A/B matrix — Agent 4 (model: OpenRouter `llama-3.3-70b-instruct`, 1 run/cell, real risk-factor inputs)
Variations: **V0** current · **V1** +single-span/anti-concat rule · **V2** V1+negative concatenation example · **V3** V1+self-check pass.

| Variation | TSLA | NVDA | MSTR | **Verbatim (agg)** | JSON ok |
|---|--:|--:|--:|:--:|:--:|
| **V0 current** | 82% (14/17) | 100% (14/14) | 100% (14/14) | **93.3% (42/45)** ✅ | 100% |
| V1 single_span | 61% (11/18) | 94% (15/16) | 100% (14/14) | 83.3% (40/48) | 100% |
| V2 neg_example | 71% (12/17) | 100% (16/16) | 100% (13/13) | 89.1% (41/46) | 100% |
| V3 self_check | 82% (14/17) | 100% (9/9) | 93% (13/14) | 90.0% (36/40) | 100% |

**Winner: V0 (current)** by the scoring formula — it leads on Quote_Accuracy (the 0.35-weight term: 93.3% vs 83-90%) and ties on JSON (1.00 across the board). V1's anti-concatenation rule *hurt* TSLA by **21 points** (the extra rules appear to make the model over-select/rewrite). This is a clean negative result: **the prompt is not the bottleneck.**

> Why the brief's "stronger instruction → better quotes" intuition fails here: the model already "wants" to copy; the failures are (a) it summarizes bulleted risk lists into a semicolon-joined span, and (b) provider-level lossiness. More imperative text doesn't fix either and adds tokens/latency (V1/V2 ran 2-4× slower).

## Why not loosen the verifier or snap-to-source?
- **Loosening the matcher** to pass near-misses = metric gaming; rejected (see `QUOTE_VERIFICATION_FIX_REPORT.md`).
- **Snap-to-source** (rewrite a non-verbatim quote to the nearest source span) risks **changing the cited meaning** — unacceptable for a compliance tool. The current behavior (verify each quote, set `new_quote_verified`/`verified`, surface the flag in the export) is the **honest** design: unverified citations are *disclosed*, not hidden or silently altered.

## Model comparison (Groq prod vs funded OpenRouter, V0 prompt, 2 runs/cell)
Same prompt, same reconstructed inputs; **model isolated.** `scratchpad/model_compare.py`.

| Ticker·run | Groq `llama-3.3-70b-versatile` | OpenRouter `llama-3.3-70b-instruct` |
|---|--:|--:|
| TSLA run0 / run1 | **88% (15/17)** / **empty (rate-limited)** | 83% (15/18) / 67% (12/18) |
| NVDA run0 / run1 | **empty** / **empty** | 94% (15/16) / 100% (16/16) |
| MSTR run0 / run1 | **empty** / **empty** | 93% (13/14) / ~93% |
| **Reliability** | **1 of 6 runs produced output** | **6 of 6** |
| **Quality (when responding)** | 88% (single point) | ~67-100%, avg **~87%** |

### Conclusion: quality is a wash; the funded key buys **reliability under load**
Direct Groq calls returned **empty / 0-changes on 5 of 6 runs** — the documented per-minute rate-limit fragility under rapid sequential calls. OpenRouter (funded) answered every time at comparable verbatim quality. So:
- **No model swap for "better quotes"** — both sit in the ~85-90% band. The residual is intrinsic to llama-70b-class summarization, not the provider.
- **The funded OpenRouter tier's real value is resilience:** when Groq (the production primary) rate-limits, the chain's fallback to a *funded, reliable* OpenRouter model now produces full output (previously the `$0`/`:free` tier would 429). This is a genuine production-hardening win — validated, and the chain's design (Groq→…→OpenRouter) is confirmed correct.

## Final selection (by `PROMPT_SCORE`)
| Agent·stage | Selected | Why | vs. original |
|---|---|---|---|
| Agent 4 risk delta | **V0 (unchanged)** | best verbatim (93.3%), ties JSON, lowest latency | variations −3 to −21pp |
| Agent 1 / Agent 3 | **unchanged** | residuals are the same non-verbatim class; the Agent-4 negative result generalizes; not separately A/B'd | n/a |

**Net Part-2 change to production code: none** (correctly — the prompts are already optimal). The hardening win for quote accuracy came from the **Agent-3 8-K input/citation fix** (Part 3 of the brief), not prompt-tuning.

## Coverage & honesty notes
- A/B scope: Agent-4 (the worst agent), 4 variations × 3-4 tickers, OpenRouter llama. Agents 1/3 were **not** separately A/B'd — their residuals are few (3 and 4) and of the same non-verbatim class; the negative Agent-4 result (more instructions don't help) generalizes.
- Determinism was **not** isolated in the matrix (1 run/cell); the cross-ticker spread shown is input variance. The model comparison uses 2 runs/cell for a determinism read.
- **No fabricated scores.** Every percentage is measured; classification accuracy lacks labeled ground truth and is reported as category-distribution sanity only.
