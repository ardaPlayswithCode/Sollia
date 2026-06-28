# Scoring System Redesign Report (PART 2)

**Date:** 2026-06-27
**Scope:** All 4 agents + composite. Pure post-processing — **no new LLM calls.**
**Code:** `sollia_working/src/utils/scoring.py` (new single source of truth),
wired into each agent + `backend/runs.py` + `synthesizer.py`.

---

## 1. Why the old scores failed

Measured on the 11 cached tickers in `.test_results/`:

| Symptom | Evidence (old scores) |
|---|---|
| **Not magnitude-aware** | Agent 1: F (1 high discrepancy) = **10**, GME (8 discrepancies, 6 high) = **10**. Identical despite 8× the problem — because the formula used *only the single worst* discrepancy (`100 − top.final_score`). |
| **"Clean" mis-scored as neutral** | Agent 1: AAPL, COIN, MSFT, NVDA (0 discrepancies) = **50** — a clean company looked the same as "no opinion". |
| **Thin-sample extremes** | Agent 2: AAPL = **90** on *3* promises; JPM = **10** on *1* promise. A 90 from 3 promises is not comparable to a 90 from 90 verdicts. |
| **No agent-level legal score** | Agent 3 emitted only per-finding `agent3_score`; the backend logged a hard-coded 50 (`result.get('agent3_score', 50)` never existed at top level). |
| **Opaque composite** | "44.2 · HOLD" came from a Gaussian-similarity blend pulling toward 50, clamped to [20,80]. No analyst could reconstruct it from the four agent scores. |

---

## 2. Unified design (shared by all 4 agents)

One scale, read the same way everywhere:

- **Direction:** higher = more bullish/healthier. Always.
- **Range:** clamped to **[10, 95]** — real companies are rarely perfectly clean (100) or maximally damning (0).
- **Signal bands (identical across agents):** `BULLISH ≥ 65`, `BEARISH ≤ 35`, else `NEUTRAL`. So "65" means the same thing for every agent.
- **Two question families, one scale:**
  - *State* agents (1 consistency, 2 credibility): score describes a standing condition. Agent 1's healthy default is **high** (clean = 90); Agent 2's no-data default is **50**.
  - *Trend* agents (3 legal, 4 risk): score describes the **direction of change** since the prior filing. "Nothing changed" ≈ neutral (50–55); improvements push up, deteriorations push down.

Every function is pure (no I/O, no LLM) → deterministic and offline-re-scorable.

---

## 3. Per-agent decisions

### Agent 1 — Corporate Discrepancy ("How consistent are disclosures?")

**Approaches weighed** (task A–D):
- *B (worst-offense)* — rejected: that *is* the old behavior; ignores count.
- *C (density)* — rejected: needs `aligned_pairs` count, noisy for small docs; hard to interpret.
- *A (flat weighted count)* / *D (tiered weighted)* — both magnitude-aware. **Winner: a refinement of D** that *reuses the per-discrepancy `final_score`* the agent already computes (it blends severity × type-objectivity × doc-tier × confidence, with same-filing false-positives pre-discounted ×0.4). Re-deriving severity/type/tier weights from scratch (literal D) would duplicate that logic and ignore confidence.

**Formula:** start at clean baseline **90**; subtract a **rank-decayed** penalty over *all* discrepancies:
```
penalty = Σ_i (final_score_i / 100) × 45 × 0.70^i      # i = 0,1,2… worst-first
score   = clamp(90 − penalty, 10, 95)
```
- *Magnitude-aware:* additional discrepancies lower the score; the worst dominates.
- *Noise-robust:* the 0.70 decay stops a long tail of minor tensions from flooring it.
- *Nice property:* a **single** discrepancy can never push below 45 (max single penalty = 45) — one contradiction = NEUTRAL at worst; only **multiple** reach BEARISH.

### Agent 2 — Promise Audit ("Does management do what they say?")

**Approaches weighed:** B (plain ratio) and C (severity-weighted ratio) ignore sample size; A (refined current) is severity-and-recency weighted and was already sound. **Winner: A + Bayesian shrinkage.** The only real defect was thin-sample extremes, so we shrink the weighted fulfilment ratio toward neutral with a small pseudo-prior:
```
fulfilled = weighted_kept + 0.5 × weighted_partial      # severity × recency-decay
score = clamp( (fulfilled + 0.5×8) / (weighted_total + 8) × 100 , 10, 95)
```
Prior mass **8** ≈ two average verdicts. Large records are essentially unaffected; thin records sit near neutral.

### Agent 3 — Legal Proceedings ("Are legal risks rising, stable, or falling?")

**Constraint discovered:** the model never emits a `priority` — only `status`. So the task's priority-weighted approaches (A/C) are not feasible as written. **Winner: A adapted to status-direction**, aggregated over **every** proceeding/event across 10-K + 10-Q + 8-K into a single agent score:
```
score = clamp(55 + Σ status_weight, 10, 95)
QUIETLY_REMOVED −16  ESCALATED −13  NEW −9  AMENDED −3
NO_UPDATE 0  UNCHANGED +1  TERMINATED +5  SETTLED +8  DISMISSED +12
```
Baseline **55**: ongoing litigation is normal and "no legal news" is mildly reassuring. `QUIETLY_REMOVED` is the most penalized — a matter that vanishes *without* a stated settlement/dismissal is a disclosure-quality red flag.

### Agent 4 — Risk Factors ("Are corporate risks rising, stable, or falling?")

**Winner: C (current refined).** The category+rank model was already the most mature scorer and is magnitude-aware (more changes + higher rank = more impact). We kept the model and the 10-K + 1.5×10-Q recency blend; only **unified the band to [10,95]**, aligned the signal thresholds, and **added a rationale**. Switching to flat per-category weights (task A) would have *lost* the "top change matters more" nuance for no benefit — the empirical scores were already sensible (clean = 50, escalating = 10–30).

---

## 4. Empirical calibration (offline, all 11 cached tickers)

### Agent 1 — magnitude restored
| Ticker | discrepancies | OLD | **NEW** | signal |
|---|---|---|---|---|
| AAPL / COIN / JPM / MSFT / NVDA | 0 | 50 / None | **90.0** | BULLISH |
| GE | 1 high (FS 81) | 19 | **53.4** | NEUTRAL |
| F | 1 high (FS 94) | 10 | **47.7** | NEUTRAL |
| TSLA | 3 (2 high,1 med) | 18 | **14.7** | BEARISH |
| GME | 8 (6 high) | 10 | **10.0** | BEARISH |
| META | 11 (5 high) | 10 | **10.0** | BEARISH |
| MSTR | 26 (14 high) | 10 | **10.0** | BEARISH |

Clean → 90; one issue → NEUTRAL (≥45); several high-severity → floor. **3 minor (low) discrepancies score ~75; 1 major scores ~48 — magnitude reflected** (design goal #4). ✓

### Agent 2 — thin-sample extremes removed
| Ticker | K/B/P | OLD | **NEW** | note |
|---|---|---|---|---|
| AAPL | 3/0/0 | 90.0 | **63.9** | thin record pulled to NEUTRAL |
| JPM | 0/1/0 | 10.0 | **40.8** | one broken promise no longer = 10 |
| GME | 80/1/12 | 89.3 | **86.7** | large record barely moves |
| GE | 14/13/2 | 51.4 | **55.0** | ~50/50 stays neutral |
| NVDA | 18/8/2 | 58.2 | **57.8** | stable |

### Agent 3 — post-Part-1a, live
| Ticker | matters | **NEW** | signal |
|---|---|---|---|
| NVDA | 1 NEW (8-K notes offering) | **46.0** | NEUTRAL |
| F | UNCHANGED + NEW + TERMINATED | **52.0** | NEUTRAL |
| GME | 1 TERMINATED | **60.0** | NEUTRAL |

(Pre-fix NVDA legal was 10/10/35 driven by leaked risk-factor text — Part 1a + Part 2 together now yield a truthful 46.)

### Agent 4 — essentially unchanged (model was sound)
AAPL 30→29.6, COIN 10→10, F 11→10.5, GE/GME/JPM/MSFT 50→50, META 26→26, MSTR 10→10, NVDA 29→28.7. Only the band/rationale changed.

---

## 5. Composite — transparent simple average

Replaced the Gaussian-similarity blend with the **equal-weight mean** of the four agent scores (each is independently meaningful on the same scale, so a plain mean is the only defensible aggregate):
```
composite = clamp(mean(agent_scores), 10, 95)
BUY ≥ 65   |   SELL ≤ 35   |   HOLD otherwise
```
Worked example: agents [90, 58, 46, 29] → **55.8 HOLD** = exactly `(90+58+46+29)/4`. An analyst can reconstruct it instantly. The legacy `similarity`/`agent_weights` fields are kept as UI-only diagnostics (they never reach the clean export — see JSON-export memory).

The individual scores tell the story the composite cannot, e.g. NVDA:
> *Disclosure consistency 90 (BULLISH) · Management credibility 58 (NEUTRAL) · Legal trend 46 (NEUTRAL) · Risk-factor trend 29 (BEARISH).*

---

## 5b. Stability caveat (design goal #5)

The four scoring **formulas are fully deterministic** given their inputs. A live
re-run of Agent 1 confirmed magnitude (F: 19 discrepancies, 13 high → **10.0
BEARISH**, "penalty 144 from baseline 90"; NVDA: 3 → 11.5). It also exposed that
Agent 1's *input* is not perfectly stable: the cached run found 1 discrepancy for
F, the live run 19 — because under rate-limit pressure a different tier of the LLM
chain answered the classify step. That variance lives **upstream** of scoring
(embedding → align → LLM classify) and is addressed by the Part 4 per-agent LLM
allocation, not by the score formula. Agents 2–4 showed stable inputs/scores
across runs.

## 6. Output schema additions

Every agent result now carries `score` (10–95), `signal` (BULLISH/NEUTRAL/BEARISH),
and `score_rationale` (a one-line plain-English explanation of how the number was
derived). Agent 3 additionally sets a top-level `score`/`agent3_score`.
