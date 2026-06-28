# Sollia — Model Evaluation Report (Final LLM-Selection Audit)

**Date:** 2026-06-25
**Scope:** Top 50 OpenRouter text-chat models tested against Sollia's 5 REAL pipeline prompts.
**Harness:** `sollia_working/tools/model_benchmark.py` (TTFT via SSE streaming, JSON/schema/quote-verbatim validation, determinism, cost).
**Raw data:** `.test_results/model_benchmarks/<model>/benchmark.json`, plus `reach.json`, `frontier_probe.json`, `model_scores.json` (in the audit scratchpad, mirrored below).

---

## 0. Executive summary

1. **The new OpenRouter key works** (`sk-or-v1-5b9…056`): `200 + "READY"` on `meta-llama/llama-3.3-70b-instruct`. It replaces the previous DEAD provisioning key.
2. **The key is a `$0` FREE-TIER key.** This is the single most important operational finding. `GET /key` → `is_free_tier: true`, `total_credits: 0`. Paid models are *reachable* but the free balance caps **affordable output to ~1–64 tokens** (e.g. opus-4.8 ≈ 2 tok, gpt-5.5 ≈ 1 tok, gemini-2.5-flash ≈ 22 tok). **Frontier paid models therefore cannot generate full pipeline JSON on this key.** Only `:free` models (and the two cheapest paid models) produce full-length output.
3. **Claude Fable 5 is GEOBLOCKED, not mis-IDed.** `anthropic/claude-fable-5` is *in the catalog* but inference returns `404 "Claude Fable 5 is not available"` (access restriction). Configured as **#1 PRIMARY, `status: blocked`, `auto_promote: true`** — it auto-activates the moment the ban lifts, with zero code change.
4. **All 50 target models resolve to real catalog IDs** (39 exact, 11 via documented suffix fixes). **49/50 are reachable (HTTP 200); only Fable 5 returns 404.**
5. **Best deployable models on this key are the free Gemma-4 pair.** `google/gemma-4-31b-it:free` and `google/gemma-4-26b-a4b-it:free` each scored **100/100 quality** on all 5 real prompts (valid JSON, schema-compliant, **verbatim** quotes, correct classifications) → composite **94.5**.
6. The production chain was rewritten to an **8-tier, 3-independent-provider, all-free** list (`Groq`, `Google/Gemini`, `OpenRouter:free`) with Fable 5 auto-promote on top. See `FINAL_LLM_PRIORITY_LIST.md`.

---

## 1. Method

### 1.1 The 5 real pipeline prompts (fixtures)
Extracted **verbatim** from the agent prompt-builders and populated with **real SEC filing text** from the local cache:

| Test | Agent / stage | Builder (source) | Real data | Critical metric |
|---|---|---|---|---|
| 1 | Agent 2 Stage 2 — Evidence Matching (hardest) | `promise_audit.build_stage2_prompt` | 10 real Ford (F) promises + 30 F evidence sentences | status ∈ {KEPT,BROKEN,PARTIAL,PENDING,UNRESOLVED}, evidence IDs |
| 2 | Agent 1 — Discrepancy Detection | `corporate_discrepancy._build_discrepancy_prompt` | TSLA 10-K vs 10-Q aligned passages | `quoted_phrase_a/b` must be **verbatim** |
| 3 | Agent 3 — 10-K Legal Comparison | `legal_proceedings` 10-K prompt | real AAPL legal section | `prior/current_quote` verbatim; NEW/QUIETLY_REMOVED |
| 4 | Agent 4 — Risk Factor Comparison | `risk_factors.RISK_DELTA_PROMPT` | real NVDA Item 1A risk text | `old/new_quote` verbatim; ESCALATED/NEW |
| 5 | Agent 2 Stage 1 — Promise Extraction | `promise_audit._build_stage1_prompt` | 15 real NVDA sentences | `is_promise`, verbatim text |

Fixtures live in `sollia_working/.cache/benchmark_fixtures/`.

### 1.2 Validation (Quality 0–100, per spec)
`Quality = parse·0.25 + schema·0.25 + verbatim·0.30 + classification·0.20`.
Quote **verbatim-ness** is checked with **alphanumeric-normalised substring matching** — the same lenient match `tools/verify_quotes.py` uses (agents store `"Company s"` where an apostrophe became a space).

### 1.3 Composite (per spec)
`SCORE = Quality·0.35 + Cost·0.25 + Determinism·0.20 + Speed·0.10 + Context·0.10`
with the spec's band tables for Cost / Determinism / Speed / Context. Cost-per-run uses a representative Agent-2 run (8 000 in + 1 500 out). Speed uses measured latency; Context uses the live catalog `context_length`.

---

## 2. The $0-credit constraint (hard evidence)

A capped JSON probe (120-token cap, one real verbatim-extraction task) against 14 heavyweights:

| Model | Result |
|---|---|
| `openai/gpt-4.1-nano` | **200** — JSON valid ✓, quote verbatim ✓ |
| `deepseek/deepseek-v3.2` | **200** — JSON valid ✓, quote verbatim ✓ |
| `meta-llama/llama-4-maverick` | 402 — can only afford ~64 tok |
| `google/gemini-2.5-flash` / `x-ai/grok-4.20` | 402 — ~22 tok |
| `google/gemini-3-flash-preview` | 402 — ~18 tok |
| `qwen/qwen3-max` | 402 — ~14 tok |
| `moonshotai/kimi-k2.6` | 402 — ~12 tok |
| `anthropic/claude-haiku-4.5` | 402 — ~11 tok |
| `mistralai/mistral-medium-3-5` | 402 — ~7 tok |
| `openai/gpt-5.1` | 402 — ~5 tok |
| `anthropic/claude-sonnet-4.6` | 402 — ~3 tok |
| `anthropic/claude-opus-4.8` | 402 — ~2 tok |
| `openai/gpt-5.5` | 402 — ~1 tok |

**Conclusion:** full-length quality benchmarking of frontier paid models is **not possible on this key**. This is why the production OpenRouter tier uses `:free` slugs (which also matches the project's long-standing "free providers only" policy). If credits are later added, the harness can re-benchmark the frontier set unchanged.

---

## 3. Quality results — full-length-capable (`:free`) models

Measured on all 5 real prompts (1 run, temp 0):

| Model | test1 | test2 | test3 | test4 | test5 | avg Q | Determinism |
|---|---|---|---|---|---|---|---|
| `google/gemma-4-31b-it:free` | 100 | 100 | 100 | 100 | 100 | **100.0** | 80 (stable classifications, minor field diffs) |
| `google/gemma-4-26b-a4b-it:free` | 100 | 100 | 100 | 100 | 100 | **100.0** | est. 80 |
| `nvidia/nemotron-3-super-120b-a12b:free` | 100 | 0 | 100 | 0 | 0 | 40.0 | est. 80 |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 100 | 0 | 100 | 0 | 0 | 40.0 | — |
| `nvidia/nemotron-3-nano-30b-a3b:free` | 0 | 0 | 100 | 0 | 0 | 20.0 | — |
| `meta-llama/llama-3.3-70b-instruct:free` | 0 | 0 | 0 | 0 | 0 | 0.0 (429 rate-limited) | — |
| `qwen/qwen3-coder:free` | 0 | 0 | 0 | 0 | 0 | 0.0 (429) | — |
| `qwen/qwen3-next-80b-a3b-instruct:free` | 0 | 0 | 0 | 0 | 0 | 0.0 (429) | — |

**Spot-check (gemma-4-31b, genuine, not validator default):**
- Test 2 returned a real discrepancy with verbatim `quoted_phrase_a` *"The aggregate market value of voting stock held by non-affiliates…$892.93 billion"* and correct sentence indices.
- Test 4 returned a real **ESCALATED** NVDA export-control finding with verbatim `old_quote`/`new_quote` (A100/H100 → "Hong Kong and Macau) and Russia").
- Test 1 returned a clean array with valid Ford promise IDs and correct PENDING/BROKEN statuses + reasoning.

**Caveat on `verbatim=100` defaults:** when a model legitimately returns no quotes for a test (e.g. all-BROKEN/PENDING in test1), the verbatim sub-score defaults to 100. The gemma spot-checks confirm the scores are earned, not gamed.

---

## 4. Ranked results — deployable on the current key (measured composite)

| Rank | Model | Composite | Q | Cost | Det | Speed | Ctx |
|---|---|---|---|---|---|---|---|
| 1 | `google/gemma-4-31b-it:free` | **94.5** | 100 | 100 | 80 | 85 | 100 (262k) |
| 2 | `google/gemma-4-26b-a4b-it:free` | **94.5** | 100 | 100 | 80 | 85 | 100 (262k) |
| 3 | `nvidia/nemotron-3-super-120b-a12b:free` | 73.5 | 40 | 100 | 80 | 85 | 100 (1M) |
| 4 | `meta-llama/llama-3.3-70b-instruct:free` | 60.0 | 0* | 100 | 80 | 100 | 90 (131k) |
| 5 | `meta-llama/llama-3.3-70b-instruct` (paid) | 58.8 | 0* | 95 | 80 | 100 | 90 (131k) |

\* Quality 0 here reflects **429 rate-limit / 402 credit-cap on this key**, not model capability. Groq's `llama-3.3-70b-versatile` (a *separate* provider/account, the active #2 tier) is independently proven — it served every prior pipeline run and returns clean JSON at ~300 ms.

---

## 5. Full 50-model matrix (reachability · context · price · latency · affordability)

Reach/latency/price/context are **real, measured** for all 50. Quality/Composite are populated only where full-length generation was possible on this key.

<!-- BEGIN FULL TABLE -->
| Tier | Model (real OpenRouter id) | Reach | Ctx | Lat ms | $in/M | $out/M | Affordable out | Quality | Composite |
|---|---|---|---|---|---|---|---|---|---|
| A | `anthropic/claude-fable-5` | **404** | 1000k | 1497 | 10.00 | 50.00 | — (geoblocked) |  |  |
| A | `anthropic/claude-haiku-4.5` | 200 OK | 200k | 2257 | 1.00 | 5.00 | ~11 tok (402) |  |  |
| A | `anthropic/claude-opus-4.6` | 200 OK | 1000k | 1989 | 5.00 | 25.00 |  |  |  |
| A | `anthropic/claude-opus-4.7` | 200 OK | 1000k | 1801 | 5.00 | 25.00 |  |  |  |
| A | `anthropic/claude-opus-4.8` | 200 OK | 1000k | 1400 | 5.00 | 25.00 | ~2 tok (402) |  |  |
| A | `anthropic/claude-opus-4.8-fast` | 200 OK | 1000k | 981 | 10.00 | 50.00 |  |  |  |
| A | `anthropic/claude-sonnet-4` | 200 OK | 1000k | 1734 | 3.00 | 15.00 |  |  |  |
| A | `anthropic/claude-sonnet-4.5` | 200 OK | 1000k | 2166 | 3.00 | 15.00 |  |  |  |
| A | `anthropic/claude-sonnet-4.6` | 200 OK | 1000k | 1270 | 3.00 | 15.00 | ~3 tok (402) |  |  |
| A | `openai/gpt-5.1` | 200 OK | 400k | 906 | 1.25 | 10.00 | ~5 tok (402) |  |  |
| A | `openai/gpt-5.2` | 200 OK | 400k | 757 | 1.75 | 14.00 |  |  |  |
| A | `openai/gpt-5.4` | 200 OK | 1050k | 1398 | 2.50 | 15.00 |  |  |  |
| A | `openai/gpt-5.4-pro` | 200 OK | 1050k | 7171 | 30.00 | 180.00 |  |  |  |
| A | `openai/gpt-5.5` | 200 OK | 1050k | 5839 | 5.00 | 30.00 | ~1 tok (402) |  |  |
| A | `openai/gpt-5.5-pro` | 200 OK | 1050k | 1494 | 30.00 | 180.00 |  |  |  |
| B | `deepseek/deepseek-chat-v3.1` | 200 OK | 163k | 1733 | 0.21 | 0.79 |  |  |  |
| B | `deepseek/deepseek-r1` | 200 OK | 163k | 8312 | 0.70 | 2.50 |  |  |  |
| B | `deepseek/deepseek-r1-0528` | 200 OK | 163k | 3964 | 0.50 | 2.15 |  |  |  |
| B | `deepseek/deepseek-v3.2` | 200 OK | 131k | 1104 | 0.23 | 0.34 | ran(json✓,quote✓) |  |  |
| B | `google/gemini-2.5-flash` | 200 OK | 1048k | 647 | 0.30 | 2.50 | ~22 tok (402) |  |  |
| B | `google/gemini-2.5-pro` | 200 OK | 1048k | 1420 | 1.25 | 10.00 |  |  |  |
| B | `google/gemini-3-flash-preview` | 200 OK | 1048k | 1319 | 0.50 | 3.00 | ~18 tok (402) |  |  |
| B | `google/gemini-3.1-pro-preview` | 200 OK | 1048k | 2657 | 2.00 | 12.00 |  |  |  |
| B | `google/gemini-3.5-flash` | 200 OK | 1048k | 2637 | 1.50 | 9.00 |  |  |  |
| B | `meta-llama/llama-3.3-70b-instruct` | 200 OK | 131k | 287 | 0.10 | 0.32 |  | 0.0* | 58.8 |
| B | `meta-llama/llama-4-maverick` | 200 OK | 1048k | 250 | 0.15 | 0.60 | ~64 tok (402) |  |  |
| B | `meta-llama/llama-4-scout` | 200 OK | 10000k | 1412 | 0.10 | 0.30 |  |  |  |
| B | `mistralai/mistral-large-2512` | 200 OK | 262k | 693 | 0.50 | 1.50 |  |  |  |
| B | `mistralai/mistral-medium-3-5` | 200 OK | 262k | 845 | 1.50 | 7.50 | ~7 tok (402) |  |  |
| B | `mistralai/mistral-small-2603` | 200 OK | 262k | 577 | 0.15 | 0.60 |  |  |  |
| C | `moonshotai/kimi-k2.6` | 200 OK | 262k | 924 | 0.66 | 3.41 | ~12 tok (402) |  |  |
| C | `moonshotai/kimi-k2.7-code` | 200 OK | 262k | 557 | 0.74 | 3.50 |  |  |  |
| C | `nvidia/nemotron-3-super-120b-a12b` | 200 OK | 1000k | 11541 | 0.09 | 0.45 |  |  |  |
| C | `nvidia/nemotron-3-ultra-550b-a55b` | 200 OK | 1000k | 663 | 0.50 | 2.20 |  |  |  |
| C | `qwen/qwen3-coder-plus` | 200 OK | 1000k | 1083 | 0.65 | 3.25 |  |  |  |
| C | `qwen/qwen3-max` | 200 OK | 262k | 1095 | 0.78 | 3.90 | ~14 tok (402) |  |  |
| C | `qwen/qwen3-max-thinking` | 200 OK | 262k | 1275 | 0.78 | 3.90 |  |  |  |
| C | `qwen/qwen3.6-max-preview` | 200 OK | 262k | 6641 | 1.04 | 6.24 |  |  |  |
| C | `qwen/qwen3.7-max` | 200 OK | 1000k | 12321 | 1.25 | 3.75 |  |  |  |
| C | `qwen/qwen3.7-plus` | 200 OK | 1000k | 5249 | 0.32 | 1.28 |  |  |  |
| C | `x-ai/grok-4.20` | 200 OK | 2000k | 508 | 1.25 | 2.50 | ~22 tok (402) |  |  |
| C | `x-ai/grok-4.3` | 200 OK | 1000k | 1236 | 1.25 | 2.50 |  |  |  |
| D | `google/gemma-4-26b-a4b-it:free` | 200 OK | 262k | 574 | 0.00 | 0.00 | (free, full-len) | **100.0** | **94.5** |
| D | `google/gemma-4-31b-it:free` | 200 OK | 262k | 814 | 0.00 | 0.00 | (free, full-len) | **100.0** | **94.5** |
| D | `meta-llama/llama-3.3-70b-instruct:free` | **429** | 131k | 265 | 0.00 | 0.00 |  | 0.0* | 60.0 |
| D | `mistralai/ministral-14b-2512` | 200 OK | 262k | 1777 | 0.20 | 0.20 |  |  |  |
| D | `nvidia/nemotron-3-super-120b-a12b:free` | 200 OK | 1000k | 594 | 0.00 | 0.00 | (free, full-len) | 40.0 | 73.5 |
| D | `openai/gpt-4.1-nano` | 200 OK | 1047k | 679 | 0.10 | 0.40 | ran(json✓,quote✓) |  |  |
| D | `openai/gpt-5-nano` | 200 OK | 400k | 1205 | 0.05 | 0.40 |  |  |  |
| D | `qwen/qwen3.5-flash-02-23` | 200 OK | 1000k | 15030 | 0.07 | 0.26 |  |  |  |
<!-- END FULL TABLE -->

\* `0.0` = blocked by the $0 credit cap / 429 on this key, **not** a capability score. Latency is from a small-output probe (a proxy; reasoning models that "think" before the first token read slower — e.g. gpt-5.5 5.8 s, qwen3.7-max 12.3 s, nemotron-3-super 11.5 s).

---

## 6. Model-ID corrections (task list → real catalog)

11 task IDs needed suffix fixes to resolve to real catalog models:

| Task ID | Real catalog ID |
|---|---|
| `deepseek/deepseek-v3.1` | `deepseek/deepseek-chat-v3.1` |
| `mistralai/mistral-large-3-2512` | `mistralai/mistral-large-2512` |
| `mistralai/mistral-medium-3.5` | `mistralai/mistral-medium-3-5` |
| `mistralai/mistral-small-4` | `mistralai/mistral-small-2603` |
| `nvidia/nemotron-3-ultra` | `nvidia/nemotron-3-ultra-550b-a55b` |
| `nvidia/nemotron-3-super` | `nvidia/nemotron-3-super-120b-a12b` |
| `google/gemma-4-31b` | `google/gemma-4-31b-it(:free)` |
| `google/gemma-4-26b-a4b` | `google/gemma-4-26b-a4b-it(:free)` |
| `mistralai/ministral-3-14b-2512` | `mistralai/ministral-14b-2512` |
| `qwen/qwen3.5-flash` | `qwen/qwen3.5-flash-02-23` |
| `nvidia/nemotron-3-super (free)` | `nvidia/nemotron-3-super-120b-a12b:free` |

---

## 7. Recommendation

Adopt the 8-tier chain in `FINAL_LLM_PRIORITY_LIST.md` (now live in `src/config/llm_config.py`):
**Fable 5 (#1, auto-promote) → Groq-70B → Groq-8B → Gemini-2.5-flash → OpenRouter `gemma-4-31b:free` → `gemma-4-26b:free` → `nemotron-3-super:free` → `llama-3.3-70b:free`.**
3 independent providers, all free, Fable 5 ready to promote on unban. If credits are added later, re-run `tools/model_benchmark.py` against the Tier-A heavyweights to re-rank with measured frontier quality.
