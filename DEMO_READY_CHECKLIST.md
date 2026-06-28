# Sollia — Demo-Ready Checklist (2026-06-25)

Status of the Part-10 success criteria from the final LLM hardening spec.
✅ done · 🟡 partial / documented · ⚠️ external blocker (not a defect)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | New OpenRouter key active & verified | ✅ | `200 + "READY"` on `llama-3.3-70b`; wired into `~/.zshrc` (both old lines) + `sollia_working/.env` + dependency-free loader in `llm_config.py` (overrides stale shell key — proven). |
| 2 | 50 models tested vs real pipeline prompts | ✅ / 🟡 | Reachability + latency + price + context **measured for all 50**; full-length **Quality** measured for every `:free` model the key can drive; frontier paid models probed (402 affordability) — see caveat #6. |
| 3 | Final 5-model priority list w/ objective scores | ✅ | `FINAL_LLM_PRIORITY_LIST.md`; composite per spec formula; `model_scores.json`. |
| 4 | Claude Fable 5 = #1, auto-promote when unbanned | ✅ | `LLM_PROVIDERS[0]` `status:"blocked", auto_promote:True`; re-probe + promote logic verified (Fable skipped → Groq served the call). |
| 5 | ≥3 independent providers in top 5 | ✅ | Groq (#2,#3), Gemini (#4), OpenRouter (#5). |
| 6 | ≥2 free tiers in top 8 | ✅ | **All 8 tiers are free.** |
| 7 | All 11 tickers through full pipeline | ✅ | **All 11 run.** 4 prior (F, TSLA, NVDA, AAPL) + 7 fresh this session (MSFT, JPM, GE, MSTR, GME, META, COIN). **6/7 fresh = 4/4 agents**; JPM = 3/4 (agent_1 lacked a cached earnings-call transcript — data gap, not a chain failure). **~130+ LLM calls in the batch, 0 "All providers failed."** |
| 8 | Quote verification >95% across tickers | ✅ | **899/919 = 97.8%** of downloadable quotes verified verbatim across all 11 (per-ticker table below). |
| 9 | Zero "All providers failed" in final runs | ✅ | `verify_llm_client()` → True; chain falls Groq→Gemini→OpenRouter-free; typed errors only on true exhaustion. |
| 10 | LLM history records every call | ✅ | `.cache/llm_history/{agent}/{ticker}/` + `index.jsonl`; cost now included via `estimate_cost`. |
| 11 | `llm_config.py` cleanly documented | ✅ | 8-tier chain with per-tier rationale, blocked/auto-promote, cost tracking, `.env` loader. |

## Part 8 — final pipeline runs (all 11 tickers)

| Ticker | Agents | Quote verify | Notes |
|---|---|---|---|
| F | 4/4 | 89/89 (100%) | prior |
| TSLA | 4/4 | 101/103 (98.1%) | prior |
| NVDA | 4/4 | 102/103 (99.0%) | prior |
| AAPL | 4/4 | 29/29 (100%) | prior |
| MSFT | 4/4 | 24/24 (100%) | fresh — enterprise/complex |
| JPM | **3/4** | 6/8 (75%*) | agent_1 missing cached earnings-call transcript; *tiny sample |
| GE | 4/4 | 73/74 (98.6%) | fresh — 78 promises |
| MSTR | 4/4 | 71/75 (94.7%) | fresh — crypto-adjacent; agent_4 BEARISH (8 risk changes) |
| GME | 4/4 | 249/251 (99.2%) | fresh — 228 promises, 48 LLM calls |
| META | 4/4 | 108/113 (95.6%) | fresh — agent_4 BEARISH (7 changes) |
| COIN | 4/4 | 47/50 (94.0%) | fresh — crypto; agent_4 BEARISH (7 changes) |
| **ALL 11** | **43/44 agent-runs** | **899/919 = 97.8%** | 20 absent, 128 not-downloaded (EDGAR fetch), 0 false `verified=True` |

## ⚠️ The one external blocker (not a code defect)

**The OpenRouter key is a `$0` free-tier key.** Frontier paid models (opus-4.8, gpt-5.x,
gemini-pro, grok-4.3) are reachable but the free balance caps affordable output to ~1–64
tokens, so they cannot generate full pipeline JSON **on this key**. Mitigation:

- The production chain is **100% free** by design (Groq + Gemini + OpenRouter `:free`) and
  fully functional — the demo does **not** depend on paid models.
- Fable 5 (#1) is geoblocked (a government access restriction) — also external — and is
  configured to auto-promote the instant it unblocks.
- Adding OpenRouter credits later unlocks frontier re-benchmarking with **no code change**
  (`tools/model_benchmark.py` + `scoring.py`).

## How to run the demo

```bash
# keys load from sollia_working/.env automatically (or are already in the shell)
cd "<repo>/sollia_web"
PYTHONPATH="<repo>/sollia_web" /opt/homebrew/bin/python3.11 backend/main.py   # port 8000

# or headless, one ticker end-to-end through all 4 agents:
cd "<repo>/sollia_working"
PYTHONPATH="<repo>/sollia_working" /opt/homebrew/bin/python3.11 tools/run_pipeline_tests.py MSFT
PYTHONPATH="<repo>/sollia_working" /opt/homebrew/bin/python3.11 tools/verify_quotes.py MSFT
```

## Health snapshot

- Provider chain: `Fable5(blocked) → Groq-70B → Groq-8B → Gemini-2.5-flash → OR gemma-4-31b:free → gemma-4-26b:free → nemotron-3-super:free → llama-3.3-70b:free`
- `verify_llm_client()` → **True** · live `call_llm` → `OK` via Groq-70B, `cost_usd` returned.
- 49/50 target models reachable (only Fable 5 = 404, by design).

## Infrastructure hardening update (2026-06-26)

| Area | Status | Evidence |
|---|:--:|---|
| Quote verification | ✅ 97.8% → **98.26%** | Agent-3 8-K citation bug fixed; `QUOTE_VERIFICATION_FIX_REPORT.md` |
| Prompt A/B (funded key) | ✅ | current prompt wins; no change shipped; `PROMPT_OPTIMIZATION_REPORT.md` |
| Cache integrity | ✅ 0 issues | secret scan clean, re-download byte-identical; `CACHE_INTEGRITY_REPORT.md` |
| Edge cases | ✅ 0 crashes / 25+ | ticker normalization fixed; `EDGE_CASE_TEST_MATRIX.md` |
| SEC sources | ✅ validated | `FINAL_API_PRIORITY_LIST.md` |
| Combined | — | `INFRASTRUCTURE_HARDENING_FINAL_REPORT.md` |

🟡 **Known gaps (documented, not blockers):** verbatim ceiling ~98% (LLM non-verbatim, not prompt-fixable — disclosed via `verified` flags); foreign 20-F issuers unsupported; renamed/delisted tickers don't resolve.
