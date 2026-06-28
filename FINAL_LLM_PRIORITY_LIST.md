# Sollia Universal LLM Priority List — FINAL (2026-06-25)

Selected by benchmarking the top 50 OpenRouter models against Sollia's 5 REAL pipeline
prompts (`tools/model_benchmark.py`). Live in `sollia_working/src/config/llm_config.py`.

**Design constraints met:** 3 independent providers in the top 5 · all 8 tiers free ·
Fable 5 configured #1 with auto-promote · zero "All providers failed" in final health checks.

> **Operating reality:** the active OpenRouter key is a **$0 free-tier** key. Frontier paid
> models are reachable but the free balance caps affordable output to ~1–64 tokens, so the
> OpenRouter tiers below use `:free` slugs (also the project's standing "free providers only"
> policy). The chain is therefore **100% free to run** and fully resilient.

---

### #1 — PRIMARY  ⚠️ GEOBLOCKED (auto-promotes when unbanned)
**Provider:** Anthropic via OpenRouter · **Model:** `anthropic/claude-fable-5`
**Status:** `blocked` (`404 "Claude Fable 5 is not available"` — US access restriction; the model **is** in the catalog) · `auto_promote: true`
**Context:** 1M · **Deterministic:** TBD · **Speed:** TBD · **Cost:** est. $5/$25 per 1M (blocked)
**Score:** TBD/100 · **Best for:** all agents, all stages — the future #1.
`call_llm` skips it without spending a request and re-probes every 30 min (`SOLLIA_FABLE_REPROBE`); the first `200` flips it active for the process.

### #2 — PRIMARY (ACTIVE)
**Provider:** Groq · **Model:** `llama-3.3-70b-versatile`
**Context:** 131k · **Deterministic:** Yes (temp 0) · **Speed:** ~300 ms · **Cost:** FREE (dev)
**Score:** proven in production · **Best for:** Agent 1/3/4 classification + Agent 2 both stages. The workhorse that served every prior pipeline run; clean JSON at temp 0.

### #3 — FALLBACK
**Provider:** Groq · **Model:** `llama-3.1-8b-instant`
**Context:** 131k · **Deterministic:** Yes · **Speed:** ~250 ms · **Cost:** FREE
**Best for:** Agent 2 Stage 1 batching, high volume. (Shares Groq's single account with #2.)

### #4 — FALLBACK
**Provider:** Google / Gemini (INDEPENDENT account) · **Model:** `gemini-2.5-flash`
**Context:** 1M · **Deterministic:** Partial (~90%) · **Speed:** ~0.6–1.3 s · **Cost:** FREE tier
**Best for:** real resilience when Groq's single account rate-limits — separate quota.

### #5 — BACKUP  ⭐ benchmark winner of the free set
**Provider:** OpenRouter (INDEPENDENT) · **Model:** `google/gemma-4-31b-it:free`
**Context:** 262k · **Deterministic:** 80 (stable classifications, minor field diffs) · **Speed:** ~0.8 s · **Cost:** FREE
**Score:** **94.5/100** · Quality **100/100** on all 5 real prompts (valid JSON, verbatim quotes, correct classifications). **Best for:** Agent 2 Stage 2, overflow, emergency.

### #6 — BACKUP
**Provider:** OpenRouter · **Model:** `google/gemma-4-26b-a4b-it:free`
**Context:** 262k · **Deterministic:** est. 80 · **Speed:** ~0.6 s · **Cost:** FREE
**Score:** **94.5/100** · Quality **100/100**. Second-best free pick; kept adjacent to #5.

### #7 — BACKUP
**Provider:** OpenRouter · **Model:** `nvidia/nemotron-3-super-120b-a12b:free`
**Context:** 1M · **Deterministic:** est. 80 · **Speed:** ~0.6 s · **Cost:** FREE
**Score:** 73.5/100 · Quality 40/100 (solid on test1/test3; weaker JSON on test2/4/5). Large independent fallback.

### #8 — BACKUP
**Provider:** OpenRouter · **Model:** `meta-llama/llama-3.3-70b-instruct:free`
**Context:** 131k · **Deterministic:** Yes (temp 0) · **Speed:** ~0.3 s · **Cost:** FREE
**Note:** currently 429-rate-limited under load (transient); a llama-70b instance **independent** of Groq's account — true last resort.

---

## Provider independence (resilience)

| Independent account | Tiers |
|---|---|
| Groq | #2, #3 |
| Google / Gemini | #4 |
| OpenRouter | #1 (Fable, blocked), #5, #6, #7, #8 |

→ **3 independent providers** in the top 5 (Groq, Gemini, OpenRouter). All 8 tiers free.

## Auto-promotion of Fable 5
`LLM_PROVIDERS[0]` = `{... "status": "blocked", "auto_promote": True}`. In `call_llm`:
1. blocked providers are skipped **without** an API call, re-probed every `FABLE_REPROBE_SECONDS` (default 1800);
2. a `200` from a blocked provider flips `status → "active"` → it serves as #1 for the rest of the process.
No redeploy needed when the geo-ban lifts.

## If/when OpenRouter credits are added
Frontier models (opus-4.8, gpt-5.x, gemini-3-pro, grok-4.3) become full-length-capable. Re-run:
```
python3.11 tools/model_benchmark.py --models <tier-A list> --tests all --runs 3 --determinism 3
```
and re-rank with `scoring.py`. Promote the measured winner into tier #2 ahead of Groq if it beats it on the composite.
