# Sollia — Final Bulletproofing Pass (2026-07)

_The last hardening pass before production. Goal: at 50+ customers the only
scaling limit should be **queue time from API rate limits** — not cost, not
crashes, not a breach. This document is the index; details live in the
per-topic reports it links._

Companion docs: `SECURITY.md` · `FUTURE_ROADMAP.md` ·
`OBSCURE_STOCK_TEST_MATRIX.md`.

---

## Part 1 — Cost optimization (was ~$1.80/cold run → target <$0.50)

**Root cause of the burn.** Every tier-0 agent sent *every* call to Fable 5,
including ~1,000-word classify batches a free 70B model handles identically.
Measured from `.cache/llm_history` (600+ calls): Agent 1's classify batches
alone were ~148 Fable-5 calls at ~$5 of the audit's $8 spend.

**Fixes (all in `src/config/llm_config.py`).**
1. **Adaptive routing** — `select_tier_for_prompt()` runs inside `call_llm` on
   every live call: `<500 words → Groq-8B`, `<2000 → Groq-70B`, `≥2000 → the
   agent's nominal tier`. `AGENT_START_TIER` is now a *suggestion*; the
   complexity check overrides it in the **cheaper direction only** — a simple
   prompt can never bill the paid tier, and a cheap agent can never be promoted
   onto it (so no oversized document — or attacker — can force paid calls).
2. **Fable-5 effort proportional to task** — `fable5_effort_level()` sets the
   reasoning budget (200/500/1000/2000) by prompt size, sent as
   `reasoning:{max_tokens}` and padded onto `max_tokens` (reasoning is billed as
   output on this endpoint). Clamped to the Anthropic 1024-token minimum.
3. **Hard cost cap** — `MAX_COST_PER_RUN_USD` ($2, `SOLLIA_MAX_COST_PER_RUN`).
   Tracked from OpenRouter's *actual* returned `usage.cost`. Once crossed, the
   paid tier is skipped inside the provider loop and calls fall through to free
   tiers. **No code path bypasses it.**
4. **Nominal tiers rebased on measured sizes** — legal_proceedings and
   moat_auditor → Groq-70B, constraint_auditor → Groq-8B (were all Fable 5).
5. **Low-volume judgment exception** — `pin_tier=True` lets a deliberately
   short, high-value call use the paid tier (Agent 1's severity audit: ~1
   call/run, ~$0.02). The cost cap still binds it.

**Cost reconciliation (important).** The spec's Part-1b table nominally kept
`promise_audit:stage2` and `risk_factors` on Fable 5. Measured, that is
**incompatible with the hard <$0.50/run ceiling**: Agent 4 issues *two*
~4,500-word calls/run (10-K delta + 10-Q phase) at ~$0.30 each, and stage-2
evidence fires several ≥2,000-word calls/run — either alone exceeds the budget.
Cost control is this pass's overriding goal, so the ceiling wins: both route to
**Groq-70B** (128k context, benchmarked 100/100 on the 5 real Sollia prompts).
Fable 5 is reserved for **one** low-volume, highest-value judgment call — Agent
1's severity audit (`pin_tier`, ~$0.037/run).

**Result:** cold NVDA **full-pipeline** billed spend ≈ **$0.037** (severity-audit
call only; every other tier is free Groq/Gemini) — down from ~$1.80. **~48×
cheaper**, with the $2 cap as a hard backstop no code path can bypass.

## Part 2 — Agent 1: year selector removed, rolling 5-year window

`run_discrepancy_agent(ticker)` no longer takes a year. It builds a window
newest-first: **current partial year → most recent COMPLETE fiscal year (the
strict anchor) → up to 4 older years (relaxed)**, aligning documents by date
**proximity** within each year and sorting findings by **recency**.

- `build_rolling_window()` walks 10-Ks newest-first; a shared `used_dates` set
  guarantees a transcript is never assigned to two adjacent fiscal years
  (verified: 22 unique transcript dates for NVDA, 0 collisions).
- Foreign private issuers (no 10-K) get an explicit *"files 20-F, not 10-K —
  not supported"* instead of "No 10-K found".
- Handles thin history (IPO'd recently → fewer years, still runs) and long
  history (20+ years → only the last 5 complete).
- **Recency-weighted scoring** — findings carry `recency_weight` (1.0 current,
  ×0.5 per older year); `score_corporate_discrepancy` scales each penalty by it,
  so a 5-year window doesn't floor every mature company at 10.
- **Cross-period false-positive guards** (deterministic, model-independent):
  different "as of" dates, annual-total-vs-quarter, and table-fragment pairs are
  dropped — these are the classic period-mismatch FPs the larger window surfaces.
- **Severity audit** — one `pin_tier` Fable-5 call reviews the high-severity
  candidates (free-tier classify over-assigns "high"); confirms/demotes each.
  NVDA: 12 raw "high" → 0 after audit; score 10 → 61 (NEUTRAL), which matches an
  analyst read of NVDA's disclosures (rewordings/disclosure-evolution, not
  contradictions).

Backend (`main.py`, `runs.py`, `agents.py`) and frontend (`App.jsx`) no longer
send or render a year; the UI shows *"Analyzing last 5 fiscal years + current
partial year."* Frontend rebuilds clean.

## Part 3 — Security (see SECURITY.md for the full matrix)

- **Injection**: `tools/injection_test.py` — 15/15 pass on Agents 3/4/5, 0
  breaches, 0 leaks. New `src/utils/prompt_safety.py` defangs injection
  directives in untrusted filing text (no-op on 948 real docs → caches intact).
- **Pre-commit hook** blocks API-key-shaped commits + any `.env` (tested).
- **Backend guards** (`main.py`): per-IP 10/min + 2 concurrent + 20/day, 10 KB
  body cap (413), ticker validated against SEC directory before any fetch. All
  tested via `TestClient` (422/413/429).
- **Cache-bust rate limit**: ≤5 `SOLLIA_LLM_NO_CACHE` runs/hour.
- **Secrets**: 0 keys in working tree or git history; `.env.example` added.
- **Dependencies**: pinned (`requirements.txt` + `requirements.lock`);
  `pip-audit` → 0 known vulns; unused `streamlit` dropped.

## Part 4 — Obscure stock matrix (see OBSCURE_STOCK_TEST_MATRIX.md)

15 edge-case tickers, **0 crashes**. Foreign filers handled explicitly; Agent 5
returns UNSUPPORTED for non-resource sectors (no force-fit); BRK.A's 6-figure
price / dual-class / conglomerate structure classifies without CIK issues.

## Part 5 / 6 — Resilience & operational abuse

- **Provider-down** tested: Groq dead → OpenRouter/Gemini serve; OpenRouter dead
  (Fable 5 unreachable) → Groq serves; all dead → clean typed error, <60 s, no
  hang.
- **EDGAR resilience** (`fetch_budget`): one retry on transient 5xx; fail-fast
  breaker after 5 consecutive sec.gov failures; caches checked before network so
  cached tickers survive a full outage.
- **Disk**: `cache_janitor` — 10 GB LRU cap at pipeline start, run archives
  protected (LRU order + protection unit-tested).
- **USGS integrity**: `pdf_sha256` recorded per extract.
- **Dependency degradation**: `src/utils/dependencies.py` `check_dependencies()`
  reports spaCy/FinBERT/pdfplumber/lxml availability; each degrades gracefully.
- **Stalled-connection hang (found + fixed during Part 4)**: an ESTABLISHED
  provider connection stalled 13+ min despite a socket timeout (the `requests`
  timeout is inactivity-between-bytes, defeated by a trickling server). Every
  LLM POST now runs on a daemon thread with a hard wall-clock ceiling
  (`_post_with_wallclock`) and falls through to the next provider on breach; the
  last un-timeouted Agent-3 SEC requests were given `(connect, read)` timeouts.
  This closes the one path that could block a run indefinitely.

## Part 7 — Maintainability

- `tools/health_check.py` — keys (cheap probe, no paid tokens), SEC endpoints,
  cache-schema integrity, imports, budget echo. Reports "System healthy".
- Requirements pinned + lock snapshot; `pip-audit` clean.

## Part 8 — Future roadmap (see FUTURE_ROADMAP.md)

Agent 7 (Capital Allocation) next; cross-agent "AT RISK" reconciler; 20-F
support for ADRs; automated Platform Monitor with first customers; vector-search
A/B + fine-tuning on the recorded history; real-time monitoring later.

## Part 9 — Final verification

See the "Part 9 — Verification results" section below (filled during the final
run): NVDA full pipeline, 3× determinism, security scan, cost audit.

---

## Deliverables

| Deliverable | Path |
|---|---|
| Adaptive routing, cost cap, effort scaling | `sollia_working/src/config/llm_config.py` |
| Agent 1 rolling window + period guards + severity audit | `sollia_working/src/agents/corporate_discrepancy.py` |
| Prompt-injection defang | `sollia_working/src/utils/prompt_safety.py` |
| Cache LRU janitor | `sollia_working/src/utils/cache_janitor.py` |
| Dependency degradation report | `sollia_working/src/utils/dependencies.py` |
| EDGAR resilience + SEC fetch budget | `sollia_working/src/utils/fetch_budget.py` |
| Backend rate limits + input validation | `sollia_web/backend/main.py` |
| Health check | `sollia_working/tools/health_check.py` |
| Injection regression test | `sollia_working/tools/injection_test.py` |
| Pinned deps + lock | `sollia_working/requirements.txt`, `requirements.lock` |
| Env template | `sollia_working/.env.example` |
| Pre-commit key scan | `.git/hooks/pre-commit` |
| Security summary | `SECURITY.md` |
| Roadmap | `FUTURE_ROADMAP.md` |
| Obscure stock matrix | `OBSCURE_STOCK_TEST_MATRIX.md` |
