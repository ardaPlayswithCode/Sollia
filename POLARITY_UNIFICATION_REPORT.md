# Polarity Unification & Output-Quality Report

**Date:** 2026-06-19
**Branch:** `optimization-pass`
**Scope:** Unify all agent scores onto one bullishness scale, fix the Risk Factors
signal inversion, drop hallucinated identical-pair risk findings, clamp every
score to `[10, 90]`, and keep the analyst JSON export fund-ready.

---

## The unified scale

Every agent now reports a **bullishness** score on the same scale:

| Band | Score | Signal | UI color |
|------|-------|--------|----------|
| Bearish | `< 35` | `BEARISH` | red `#EF4444` |
| Neutral | `35–65` | `NEUTRAL` | yellow `#F59E0B` |
| Bullish | `> 65` | `BULLISH` | green `#22C55E` |

- **50 = neutral baseline** — "nothing material changed."
- **Clamped to `[10, 90]`** for every agent — no filing is perfectly clean or
  maximally damning.
- One mapping (`> 65 / < 35`) now drives **both** the backend export
  (`runs._agent_signal`) **and** the UI badge (`App.jsx signalFor`), so they can
  never disagree. It mirrors `RiskFactorAgent._signal`.

---

## Issue-by-issue

### Issue 1 — Risk Factors: identical-pair findings flagged ESCALATED
**Root cause.** The pair-key dedup only collapses *duplicate* findings (two
findings citing the same passage). A **lone** finding whose `old_quote` ==
`new_quote` (Ford's Takata recall paragraph) has nothing to collapse against, so
it survived dedup and was reported as ESCALATED — a hallucinated change.

**Fix.** Added a pre-dedup filter that drops any finding whose OLD and NEW quotes
are identical after normalizing quotes/whitespace/case.
- `risk_factors.py` — new `RiskFactorAgent._is_identical_pair()`; called as the
  first step of `_dedup_changes()` (before any key-building/collapse runs).

**Verified.** Unit test: the Ford identical pair is dropped; a genuine escalation
(different text) is kept; a NEW risk (no OLD quote) is kept; two true duplicates
(identical OLD+NEW pair) still collapse to one via the pair key. Live F 2025 run:
**0 identical-pair findings** in the export.

### Issue 2 — Risk Factors signal inverted in the UI
**Root cause.** `RiskFactorAgent._signal(10)` correctly returns `BEARISH` (the
agent's score already pushes escalations *down*). But **both** the export
(`runs._agent_signal`, old comment "higher score => more bearish") **and** the UI
(`App.jsx`, `score <= 35 ? 'BULLISH'`) used the *opposite* polarity, so score 10
rendered green/BULLISH.

**Fix.** Both now use the unified bullishness mapping (`> 65 BULLISH`,
`< 35 BEARISH`). `_agent_signal` and `RiskFactorAgent._signal` agree at every
boundary (verified across 10→90).

### Issue 3 — Agent 1 (Corporate Discrepancy): convert to bullishness
**Root cause.** The agent's score is *discrepancy severity* (higher = more/worse
contradictions). Reported as-is, 96 looked like a strong score; the synthesizer
even treated it as bullish, pulling the overall signal toward BUY — backwards.

**Fix.** Invert **at the source** so every consumer (export, UI card, per-agent
signal, **and** the synthesizer) sees one consistent bullishness number:
- `corporate_discrepancy.py` — `bullishness_score = max(10, min(90, 100 - raw_severity))`;
  no discrepancies stays neutral at 50 (`100 - 50`).
- Example: 5 high-severity discrepancies → raw ~96 → `100-96 = 4` → clamp **10** →
  BEARISH. No discrepancies → 50 → NEUTRAL.

This also corrects a **latent synthesis bug**: severe discrepancies now correctly
drag the overall score down instead of up.

### Issue 4 — Agent 3 (Legal Proceedings): verify polarity
**Finding.** Already on the bullishness scale — `_calculate_score` pushes
SETTLED/DISMISSED **up** (+1/+2), NEW/ESCALATED/QUIETLY_REMOVED **down**
(−1/−2/−1), UNCHANGED holds at 50. No inversion needed.
**Fix.** Only the clamp changed from `[0,100]` to `[10,90]` (Issue 7). NEUTRAL
maps to yellow `#F59E0B` via the unified mapping.

### Issue 5 — Unified signal colors in the frontend
**Finding.** The live `ScoreBox` (inline in `App.jsx`) already maps
`BULLISH→#22C55E`, `NEUTRAL→#F59E0B`, `BEARISH→#EF4444`. The bug was the *signal
string* computation, not the color map.
**Fix.** Replaced four divergent inline ternaries with one shared `signalFor()`
helper; all four cards pass a unified `BULLISH/NEUTRAL/BEARISH` string. No card
hardcodes a color that bypasses the mapping.
**Note.** `components/ScoreCard.jsx` and `components/AgentCard.jsx` are **dead
code** — `main.jsx` mounts only `App.jsx`, which imports nothing from
`./components`. They do not affect the live UI.

### Issue 6 — JSON export audit
Audited the full F 2025 export. **Required fields present:** `ticker`, `year`,
`timestamp`, `signal`, `score`, `agents{}`; per-agent `name`/`score`/`signal`/
`top_findings[]`; per-finding `type`/`severity`/`summary`/`citations[]`; per-citation
`role`/`document`/`excerpt`/`url`/`verified`. **No forbidden fields:** no
`agent_weights`, `agent_scores`, `similarity`, `batch_key`, `chunk_id`,
`embedding`, `final_score`, `score_components`, model names, thresholds, or any
`_`-prefixed key. Per-agent qualifiers confirmed:
- Agent 1 `pair` — e.g. `"10-K vs 10-Q (Q3 2025)"`.
- Agent 2 `evidence_quality` — one of `strong`/`partial`/`weak`.
- Agent 3 `change_direction` — `negative` NEW/ESCALATED, `positive`
  SETTLED/DISMISSED, `neutral` UNCHANGED.
- Agent 4 `change_direction` — `negative` ESCALATED/NEW, `positive`
  DE-ESCALATED/**REMOVED**, `neutral` REWORDED.

**Bug fixed during audit.** `runs._RISK_DIRECTION` mapped `REMOVED → "negative"`,
contradicting the spec, the agent's scoring (REMOVED raises the score = bullish),
and the dict's own comment. Corrected to `REMOVED → "positive"`.

### Issue 7 — Score clamping `[10, 90]`
- Agent 1: inverted **then** clamped `[10,90]`. ✓
- Agent 2 (`promise_audit.calculate_score`): added clamp — could previously hit
  0 (all broken) or 100 (all kept). ✓
- Agent 3 (`legal_proceedings._calculate_score`): clamp changed `[0,100]→[10,90]`. ✓
- Agent 4: already clamped `[10,90]`. ✓

---

## Final verification — F 2025, all 4 agents

Run against the restarted backend with all fixes loaded (real SEC + LLM calls):

| Agent | Score | Signal | UI color |
|-------|------:|--------|----------|
| Corporate Discrepancy | 10.0 | BEARISH | red `#EF4444` |
| Promise Audit | 71.8 | BULLISH | green `#22C55E` |
| Legal Proceedings | 48.3 | NEUTRAL | yellow `#F59E0B` |
| Risk Factors | 10.0 | BEARISH | red `#EF4444` |

Overall (synthesizer): score **45.4**, signal **HOLD**.

**Automated audit (all ✓):** every score in `[10,90]`; no forbidden/underscore
fields; Agent 1 `pair`, Agent 2 `evidence_quality`, Agent 3 & 4 `change_direction`
all present and spec-correct; **0 identical-pair findings** in Risk Factors; the
Risk Factors badge is **BEARISH/red** at 10 (previously green); Corporate is
**BEARISH/red** at the inverted 10 (previously 96).

---

## Files changed

| File | Change |
|------|--------|
| `sollia_working/src/agents/risk_factors.py` | `_is_identical_pair()` + pre-dedup filter (Issue 1); corrected stale CATEGORY_IMPACT comment |
| `sollia_working/src/agents/corporate_discrepancy.py` | invert agent score to bullishness + clamp `[10,90]` (Issues 3, 7) |
| `sollia_working/src/agents/legal_proceedings.py` | clamp `[0,100]→[10,90]` (Issue 7) |
| `sollia_working/src/agents/promise_audit.py` | clamp score to `[10,90]` (Issue 7) |
| `sollia_web/backend/runs.py` | unify `_agent_signal` to bullishness (Issues 2, 3, 5); fix `_RISK_DIRECTION[REMOVED]→positive` (Issue 6) |
| `sollia_web/frontend/src/App.jsx` | shared `signalFor()` helper for all four score cards (Issues 2, 5) |

**Not touched (per constraints):** core architecture, `llm_config.py`,
`document_priority.py`, the synthesizer, and Agent 2's deterministic fallback.
Frontend `dist/` rebuilt (gitignored) so the served UI reflects the fix.

Every edited Python file passes `python3.11 -m py_compile`; the frontend passes
`vite build`.
