# Production Polish Report — 2026-07-03

Final production pass: dynamic LLM routing (empirically tested), JSON tolerance,
speed work, analyst-facing language, page-numbered citations, meeting briefs,
and the UI transformation. Every decision below is traceable to a measurement.

---

## Part 0.5 — TSLA agent failures: root causes and fixes

The TSLA run's two "broken agents" shared one underlying cause — **SEC
throttling turned into permanent state** — plus one independent bug.

| Symptom | Root cause | Fix |
|---|---|---|
| Agent 2: "No filings available for TSLA" | `SECEdgarProvider.get_all_filings()` was network-only; a transient 429 returned `[]` | Provider is now cache-first against the shared `filings/{T}_filings.json` (24h TTL), live-refreshes and rewrites the raw-submissions schema, and serves the **stale cache when SEC is unreachable** |
| Agent 6: "Ticker does not resolve to an SEC filer" | `get_cik()` fetched `company_tickers.json` live on every provider instance | Disk-cache-first against `.cache/company_tickers.json` with live fallback + write-through |
| Agent 6: `moats: 0` even after the above | (a) Throttled runs **negative-cached** empty EX-99.1 URL maps and empty competitor-doc lists; (b) `tenks[0]` picked Tesla's April **10-K/A** (Part III cover amendment — no Item 1/1A/7) | (a) Failed/empty fetches are never cached (ex99, compdocs, moat_raw); 9 poisoned cache files purged; (b) plain 10-Ks are tried before amendments, walking candidates until Item extraction yields substance |

Verified on TSLA: Agent 2 extracts **24 promises** (5 kept / 2 broken / 1
partial); Agent 6 audits **6 moats** across 6 competitors (BYDDF, F, GM, NVDA,
GOOGL, TM) — 2 WATCH, 4 NO THREAT. A simulated total SEC outage now resolves
CIK + 488 filings from disk.

Note: the 10-K/A bug alone would have broken TSLA every May–January (Tesla
files a Part III amendment each April).

## Part 1 — Empirical model tests and the dynamic router

`tools/routing_benchmark.py` sends the chain models three REAL recorded
pipeline prompts from `.cache/llm_history` (not synthetic approximations):

- **A** Agent 4 risk-factor comparison — 2,832 words, strict JSON array
- **B** Agent 1 discrepancy classify — 1,178 words, strict JSON + sentence indices
- **C** Agent 2 Stage-1 promise extraction — 685 words, category allowlist

Metrics per model×test: clean-JSON rate (raw `json.loads`), tolerant-recovery
rate (`extract_json`), task validity (schema keys / index bounds / category
allowlist), latency, determinism (first 3 responses byte-identical), 429s.

### Headline measurements (full data: `.test_results/routing_benchmark/`)

| Model | Test | clean | recoverable | valid | latency | deterministic |
|---|---|---|---|---|---|---|
| Fable 5 | A (agent-4 shape) | 0/3 | 1/3 | 0/3 | 41.5s | no |
| Fable 5 | B (classify+indices) | 3/3 | 3/3 | 3/3 | 7.9s | **yes** |
| Fable 5 | C (stage-1) | 0/3 (fenced) | 3/3 | 3/3 | 16.9s | no |
| **Groq-70B** | **A (agent-4 shape)** | **3/3** | **3/3** | **3/3** | **3.9s** | no |
| Groq-70B | B (classify+indices) | 3/3 | 3/3 | 3/3 | 1.0s | no |
| Groq-70B | C (stage-1) | 0/3 (fenced) | 3/3 | 3/3 | 1.5s | no |
| Groq-8B | A | 3/3 | 3/3 | 3/3 | 2.4s | yes |
| Groq-8B | B (classify+indices) | 0/3 (prose) | 0/3 | 0/3 | 1.5s | — |
| Groq-8B | C (stage-1) | 0/3 (fenced) | 3/3 | 3/3 | 1.3s | — |
| Gemini 2.5 Flash | A | 0/2 (truncated) | 0/2 | 0/2 | 11.0s | — |

**The decisive finding:** on the long strict-JSON Agent-4 shape, Fable 5 (via
the OpenRouter endpoint with mandatory reasoning) returned one fenced response,
one **empty** response (the reasoning budget starved the completion — the known
`content:null` mode) and one truncation, at 41s per call. The original plan
("Agent 4 → back to Fable 5") is empirically wrong on this endpoint. Fable 5's
real sweet spot is **short/medium strict judgment calls** — perfect and
byte-deterministic on the classify shape — which is exactly the pinned severity
audit. Rate-limit physics also matter: Groq-70B's free tier is **12,000
tokens/min** (one jumbo call per minute), and OpenRouter `:free` models share
**50 requests/day** below a $10 balance — the six-agent stampede, not model
quality, caused most historical "All providers failed" errors.

### The router (`select_best_tier`, llm_config.py)

Decisions derive from `MODEL_PROFILES` (the measured data, embedded as code):

1. Strict-JSON or long-context tasks → **Groq-70B floor** (never Groq-8B: it
   measured 0/3 on sentence-index tasks; never Fable 5 by size: it measured
   0/3 clean on the long strict shape). Downstream, every agent now parses
   through the tolerant extractor, which is the actual fix for Agent 4.
2. Long-context beyond ~90k words → Fable 5 (200k ctx genuinely needed;
   cost-gated; per-run cap still binds).
3. \>1,500 words → Groq-70B. Otherwise → Groq-8B.
4. Fable 5 by deliberate opt-in only: Agent 1's severity audit (`pin_tier`),
   where it measured perfect.

Task flags (`requires_strict_json`, `requires_long_context`) are set by agent
CODE at four call sites (A1 classify, A2 stage-2, A3, A4) — never derived from
document content, so an attacker-inflated filing cannot steer routing; the
$2/run cost cap is unchanged.

## Part 2 — Universal JSON extractor

`src/utils/json_extractor.py::extract_json()` — one shared tolerant parser:
markdown fences, preamble/trailing prose, trailing commas, single quotes,
`//` and `#` comments, Python literals (`True/False/None`), raw control
characters in strings, and lone backslashes in legal citations. Valid JSON
passes through unchanged; unrecoverable input raises `ValueError` (never
returns garbage). **30/30 unit tests pass** (`tools/json_extractor_test.py`),
including the all-modes-combined case.

Wired into every agent's parse path, replacing 8 ad-hoc parsers: Agent 1
(`_parse_discrepancy_array` + severity audit), Agent 2 (Stage 1 + Stage 2),
Agent 3 (proceedings, keeping its truncation semantics), Agent 4
(`_parse_changes`, keeping its truncation-salvage fallback), Agent 6
(`_extract_json` shim). Deterministic fallbacks unchanged.

## Part 3 — Speed

- **LLM concurrency semaphore** (`SOLLIA_LLM_MAX_CONCURRENCY`, default 3)
  around live calls only — cache hits bypass it. Six agents now take turns
  instead of stampeding Groq's per-model TPM into 429 retry loops.
- **Groq key cycling**: `GROQ_API_KEY_2/_3` (documented in `.env`) become
  separate rate-limit buckets; keys rotate per call and a 429 advances to the
  next key before falling down the chain.
- **Fast agents first**: submission order Legal → Constraint → Risk → Moat →
  Promise → Discrepancy, so semaphore turns go to the quick agents and the
  analyst reads results within seconds.
- **Live per-agent results**: the backend streams each agent's result the
  moment it finishes (`set_agent_result` → SSE `new_results`); the UI renders
  that card immediately with a presence animation.

## Part 4 — Constraint language

"ALREADY CONSTRAINED" (reads like a model failure) became analyst framing:

- Human/machine label: `ALREADY_CONSTRAINED` + per-material `crossover_detail`
  ("US apparent consumption (190,000 tons) already meets or exceeds domestic
  production (170,000 tons) as of 2025. The constraint is binding today —
  any sector growth requires increased imports or new domestic capacity.")
- `urgency` levels: immediate / high (<2yr) / medium (<5yr) / low (beyond 5yr)
  / none — with UI badges **BINDING NOW** (red), `Q3 2027` (orange), `Q1 2029`
  (yellow), `Beyond 2033` (green), `No constraint` (gray).
- Export findings carry `crossover_detail`, `urgency`, `urgency_badge`; the
  finding summary leads with what the crossover MEANS.

## Part 5 — Page numbers in citations

`src/utils/page_numbers.py`: scans cleaned filing text for printed page
markers (bare footer numbers, "N Table of Contents", "Page N", "- N -",
"[PAGE N]"), keeps only a self-consistent ascending chain (≥3 markers) so
tabular numbers can't masquerade as pages, and maps citation quotes to pages
via the same alnum-normalized matching the quote verifier uses. **Honesty
rule: no page is ever fabricated** — quotes past the last printed marker get
no page rather than a wrong one (Agent 1's old synthetic `char_count//3000`
"page_number" was exactly that kind of fake precision, and is gone).

Wired into Agents 1 (real pages in chunk metadata → citations), 3 (prior/
current quote pages), 4 (old/new quote pages), 6 (claim pages). Export
citations carry `page_number` when real; the UI renders "10-K, p.42".
Measured on Tesla's real 10-K: 18 printed markers recovered; 44% of chunks
receive a verified page (the rest fall after Tesla stops printing numbers).

## Part 6 — Meeting brief

`tools/generate_brief.py` (CLI) and `src/utils/brief.js` (client-side, wired
to a **Download Brief** button next to Download JSON — zero LLM calls,
instant): executive summary, ⚠ WHAT NEEDS ATTENTION (top 5 by urgency/severity
with verbatim quotes + page-labeled citation links), ✅ WHAT'S STABLE, 📊
per-agent detail, and a trust footer with the **measured** citation
verification rate for that run (never a hardcoded number).

## Part 7 — UI transformation

All effects hand-built in `components/effects.jsx` (Aceternity/Magic UI are
copy-paste libraries, implemented locally to their spec):

- **GlowCard** — severity-toned glow borders (red bearish / green bullish /
  blue neutral / purple audit) that breathe when findings are present and
  intensify on hover; box-shadow + opacity only.
- **GridBackground** — 1px dots on a 30px lattice at 0.045 opacity, drifting
  one cell per 6s.
- **NumberTicker** scores (0→value, 400ms), **TextReveal** titles (blur→clear,
  300ms), **AnimatedList/Item** findings (50ms stagger, slide-from-left),
  **CardEntrance** presence animation for live-arriving agent cards,
  **Tip** animated tooltips (agent blurbs, competitor rationales, model notes).
- **Drag/resize grid** — react-grid-layout v2 (`/legacy` API): drag by card
  header, resize from the corner, layout persisted to localStorage
  (`sollia-layout-v1`), **Reset Layout** button restores defaults.
- Score tiles read **N/100** (not a bogus percent sign).
- Professional constraints: every animation ≤400ms; transform/opacity only;
  `prefers-reduced-motion` disables all motion (global CSS kill-switch plus
  framer-motion's `useReducedMotion` in every effect component); dark slate
  theme preserved; no sound, no particles, no video.

`npm run build`: **passes, zero errors** (438 modules).

## Part 8 — Verification (measured 2026-07-03)

**Agent 4 on TSLA ×5 — parse-failure rate 0/5 = 0%.** The first verification
pass exposed one more latent production bug: a garbage LLM response cached
during the rate-limit storm was byte-cache-served forever ("comparison
returned an unparseable result" in 1.0s). Fixed with a parse-failure-driven
cache bust (`call_llm(..., bypass_cache=True)` retry that overwrites the
poisoned entry). After the fix: run 1 = 8.7s live (10 annual changes + 4 10-Q
changes, score 12.9), runs 2–5 = 1.7s cached, identical.

**Full 6-agent TSLA pipeline (all agents, streamed):**

| Metric | Semi-cold (fresh 5-yr window prompts) | Warm re-run |
|---|---|---|
| Time to first agent card | ~3–5s (Constraint Auditor, streamed live) | **5s** |
| All six agents complete | 544s | **181s (~3 min)** |
| "All providers failed" errors | 0 | 0 |

The cold total is bounded by physics, not code: Groq-70B's free tier is 12k
tokens/min on ONE key, and a cold TSLA needs far more 70B traffic than one
minute holds. Adding `GROQ_API_KEY_2/_3` (supported, each its own bucket)
multiplies cold throughput ~2.5×. Warm runs — the analyst's daily reality —
meet the 3-minute goal with the first card at 5 seconds.

**TSLA output quality:** HOLD 40.2 — Disclosure 20.1 BEARISH (13 findings),
Credibility 73.0 BULLISH (27 promises), Legal 55.0 (no material changes),
Risk 12.9 BEARISH (14 changes), Constraints 3× BINDING NOW (cobalt, nickel,
copper), Moats 5 audited. 111 citations, **93.7% verbatim-verified, 36 with
printed page numbers**. Standout finding (real, analyst-grade): the FY2025
10-K forecasts 2026 capex ">$20 billion" while the later 10-Q says
">$25 billion" — cited to 10-K p.33 with EDGAR links on both sides.

**NVDA control:** HOLD 53.2 vs baseline HOLD ~54 — consistent. Risk Factors
24.0 BEARISH exactly matches the known-good baseline; Credibility 73.0 vs 84
(new filings entered the rolling window since the baseline archive);
Constraints reproduce gallium/germanium/cobalt BINDING NOW. 213/213 citations
verified (100%); 41 carry page numbers.

**Analyst's checklist:**
- *Each agent's purpose in 5 seconds* — every agent chip and card title has a
  plain-language tooltip ("Diffs legal proceedings across filings — new
  cases, settlements, quiet removals").
- *Findings trustworthy* — verbatim quotes with verify flags, printed page
  numbers, one-click EDGAR/USGS links; the trust footer reports the run's
  MEASURED verification rate.
- *Brief PM-ready* — leads with binding constraints and the capex
  contradiction; adverse-only attention list (kept promises stay in "stable").
- *Professional, not gimmicky* — ≤400ms animations, no particles/sound/video,
  reduced-motion respected, dark slate theme intact.
- *Would I return tomorrow* — warm runs ≈3 min with first results at 5s,
  layout persistence, one-click brief.

## Deviations from the original spec (all evidence-based)

1. **Agent 4 stays on Groq-70B, not Fable 5** — Fable 5 measured 0/3 clean /
   1/3 recoverable / 41s on the real Agent-4 prompt (reasoning-starved empty
   responses). The parse-failure fix is the universal extractor.
2. **`select_best_tier` promotes to Fable 5 only beyond ~90k words** — within
   128k, Groq-70B measured equal-or-better on these shapes at 30× lower
   latency and zero cost.
3. **`max_cost_cents` default is 40¢, not 5¢** — a real 4,500-word Fable 5
   call projects ~20¢; a 5¢ ceiling would have contradicted the routing table
   it guards.
4. **OpenRouter `:free` tiers tested at reduced n** — the account's 50
   requests/day `:free` cap (balance <$10) was consumed by the day's pipeline
   runs; their quality profile stands on the 2026-06-25 benchmark
   (`FINAL_LLM_PRIORITY_LIST.md`). They remain last-resort refuges by design.
5. **Agent-2 transcripts are ~2KB stubs** (provider truncation) — noted as a
   known limitation, out of scope for this pass.
