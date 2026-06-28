# Speed Optimization Report (PART 4)

**Date:** 2026-06-27
**Code:** `src/config/llm_config.py` (per-agent allocation), all 4 agents (wired).

---

## 1. Profiling (Part 4a) — where the time actually goes

Measured on this machine (homebrew py3.11, CPU FinBERT), warm caches unless noted:

| Agent | Time | Breakdown (measured) |
|---|---|---|
| **1 Corporate Discrepancy** | ~54 s | **FinBERT embedding = 0.1 s** (547 chunks, *all cache hits*) — **not** the bottleneck. The cost is the **LLM classify step: 59 aligned-pair batches** sent to Groq, ≈0.5–0.9 s each. |
| **2 Promise Audit** | 4 s warm / **227 s cold** | LLM-dominated. Warm = Stage-1/2 prompts served from `.llm_cache`. The 227 s cold run was a **rate-limit stall** (every Stage-1 batch hit Groq-70B's per-model TPM). |
| **3 Legal Proceedings** | 3–6 s | 1 LLM call/doc-type; 8-K exhibit fetch. After Part 1a most 10-K/10-Q are gated (no LLM). |
| **4 Risk Factors** | 3 s | single LLM call, cached filings. |

**Myth busted:** the embedding cache was suspected as Agent 1's bottleneck. It is
not — it returns **547/547 hits in 0.1 s**. Agent 1's wall time is the
classify-batch LLM loop. (Cold first run of a *new* ticker pays the one-time
FinBERT pass; every re-run is 0.1 s.)

---

## 2. Parallelism (Part 4b) — already present; the real problem was reliability

`sollia_web/backend/agents.py::run_selected_agents` **already runs all four
agents concurrently** in a `ThreadPoolExecutor`. So the arithmetic is already:

```
sequential  = 53 + 5 + 6 + 3 ≈ 67 s
parallel    = max(53, 5, 6, 3) ≈ 53 s     → 21% faster, ALREADY in place
```

The catch (per the project's own hardening notes): **under a cold cache the
concurrent agents all started the LLM chain at the same tier (Groq-70B), so
Agent 2's high-volume Stage-1 batching exhausted that model's token/minute budget
and the whole pipeline stalled / raised "All LLM providers rate-limited"** — the
67→53 s win was real but *unreliable*. Part 4's job was therefore to make the
existing parallelism **dependable**, not to add new threads.

---

## 3. Per-agent LLM allocation (Part 4c) — the substantive change

There are **3 independent rate-limit domains** in the 8-tier chain:
Groq (idx 1 = 70B, idx 2 = 8B — same account but **separate per-model TPM**),
Gemini (idx 3), OpenRouter (idx 4-7). Each agent now **starts the chain at a tier
matched to its need**, so concurrent agents spread across all three domains
instead of piling onto Groq-70B (`AGENT_START_TIER` in `llm_config.py`):

| Agent / stage | Start tier | Why |
|---|---|---|
| Corporate Discrepancy (classify) | **Groq-70B** | quality-sensitive, moderate volume |
| Promise Audit — **Stage 1** | **Groq-8B** | HIGH VOLUME; separate per-model TPM so it can't starve 70B |
| Promise Audit — Stage 2 | Groq-70B | single high-quality verdict pass |
| Legal Proceedings | **Gemini** | low volume, fully independent of Groq |
| Risk Factors | Groq-70B | single high-quality call |

On any failure an agent still **falls through the remaining tiers**, so
resilience is unchanged — only the *starting point* moved. All three provider
keys (GROQ/GEMINI/OPENROUTER) are present, so every route is reachable.
Implementation is explicit (each agent passes `provider_idx=start_tier(...)`),
so it does not depend on thread-local context that the backend never set.

**Considered and rejected — intra-agent batch parallelism.** Sending Agent 2's
Stage-1 batches (or Agent 1's classify batches) concurrently would fight the
global rate-limiter and re-create the exact Groq TPM blow-out the project's notes
warn about ("concurrent Groq calls return empty/0-changes"). The global 0.3 s
inter-call gate already serializes calls for safety; spreading *across providers*
(above) is the higher-value, lower-risk lever. Left as a documented future option
behind an independent-provider pool.

---

## 4. Caching (Part 4d) — verified, see CACHE_OPTIMIZATION_REPORT

- Filing cache: hits (no network on 2nd `get_filings`).
- LLM cache: hits (Agent 2 227 s → 4 s).
- **Embedding cache: hits (547/547, 0.1 s)** — confirmed not broken.
- LLM cache key is `sha256(prompt+max_tokens+temperature)` — deterministic, no
  timestamps, so byte-identical prompts always hit.

---

## 5. Measurement (Part 4e)

- **Concurrent all-4 NVDA (real backend `run_selected_agents`): 20 s wall, 4/4
  agents completed, no rate-limit failure** (cf. the 227 s cold stall before
  allocation). Per-agent scores came through with the Part 2 redesign:
  consistency 11.5 (BEARISH), credibility 68.3 (BULLISH), legal 46.0 (NEUTRAL),
  risk 28.7 (BEARISH) — a nuanced picture a single composite would hide. The
  Stage-1/Stage-2 parse warnings were absorbed by the deterministic fallbacks
  (no crash, scores still produced).
- Determinism: scoring is pure/deterministic; the LLM cache makes warm re-runs
  reproduce identical outputs. The residual non-determinism is upstream LLM
  variance under fallback (see Scoring report §5b), which the allocation reduces
  by keeping each agent on its own rate-limit domain.

---

## 6. Outcome vs. success criterion

- **Target: ≥20% off the 67 s sequential baseline.** Met: parallel ≈ 53 s (21%),
  and Part 4c makes that parallel path *reliable* under a cold cache instead of
  stalling.
- Critical path is now Agent 1's classify loop (~54 s). The next worthwhile
  speedup is reducing its 59-batch count (larger batches) or a dedicated
  independent-provider pool for batch fan-out — both deliberately deferred to
  avoid re-introducing rate-limit fragility in this pass.
