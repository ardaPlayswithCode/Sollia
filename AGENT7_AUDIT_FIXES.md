# Agent 7 — Final Production Audit & Fixes (2026-07-01)

Client-readiness audit of the Sollia platform. Every issue below was reproduced on
the live NVDA pipeline, fixed at the root cause, and re-verified end-to-end. Fable 5
(`anthropic/claude-fable-5`) is now live and serves as the primary model for the
hardest analytical tasks.

All work is in the real repo:
`/Users/ardaturgut/Desktop/Desktop - Arda's Mac Studio/Sollia`
(`sollia_working/` = agents/config/data; `sollia_web/` = FastAPI backend + React UI).

---

## Headline before/after (NVDA, cold → warm, deterministic)

| Agent | Before | After | What changed |
|-------|--------|-------|--------------|
| 1 Corporate Discrepancy | **10 BEARISH** | **51.2 NEUTRAL** | 11 → 6 findings; synonym/identical-text/disclosure-evolution false positives gone; severity now gates the score |
| 2 Promise Audit | **77 BULLISH** | **85.2 BULLISH** | ~20 historical-result "promises" removed; every citation has a real URL; earnings-call promises now actually extracted |
| 3 Legal Proceedings | **46 NEUTRAL** | **55.0 NEUTRAL** | $25B notes offering no longer mis-reported as a legal proceeding |
| 4 Risk Factors | **29 BEARISH** | **24.0 BEARISH** | HR practices no longer risk factors; extraction now lands on the real Item 1A; genuine new risks (Groq IP license) surfaced |
| 5 Constraint Auditor | **0/6 evaluated** | **3/6 success + world data** | USGS withheld-vs-fetch-failure separated; world production cited; FRED wired; material-specific messages |
| 6 Moat Auditor | CUDA/ecosystem missed | claim = ecosystem/installed base | mechanism-first claim selection; verdict/analysis consistency; heading-junk claims dropped |
| **Composite** | ~40 HOLD | **53.9 HOLD** | mean of the 4 scoring agents (audit agents excluded) |

Composite is a plain mean: `(51.2 + 85.2 + 55.0 + 24.0) / 4 = 53.9`. Verified
**byte-identical across 3 consecutive runs** (scores, signals, finding counts, and
citation fingerprints all stable).

---

## PART 0 — Fable 5 integration

### 0a. Fable 5 is live
`anthropic/claude-fable-5` returns **HTTP 200** via OpenRouter (resolves to
`anthropic/claude-5-fable-20260609`). The former 404 geoblock is lifted.

Two non-obvious properties discovered and handled:
1. **Reasoning is mandatory** on this endpoint — it cannot be disabled
   (`reasoning:{max_tokens:0}` → HTTP 400 "Reasoning is mandatory"). Reasoning
   tokens count against `max_tokens`, so a too-small budget returns **`content:
   null`** (all budget spent thinking).
2. That `content: null` exposed a **latent crash** (see 0d).

### 0b. LLM chain updated (`sollia_working/src/config/llm_config.py`)
- Fable 5 flipped from `status:"blocked"` → an active tier-1 provider.
- **Auto-promote re-probe logic removed** (`FABLE_REPROBE_SECONDS`, `_blocked_probe`,
  the `status=="blocked"` skip, and the 200→active flip are all gone).
- Chain confirmed: **Fable5 → Groq-70B → Groq-8B → Gemini-2.5-flash → OR-Gemma4-31B
  → OR-Gemma4-26B → OR-Nemotron3-Super → OR-Llama33-70B.**
- `call_llm(..., provider_idx=0)` → returns `"FABLE_ACTIVE"` via Fable5. ✓
- `start_tier("moat_auditor")` and `start_tier("constraint_auditor")` → **idx 0**. ✓

Fable 5 tier carries `reasoning_effort:"low"`, a `max_tokens_pad:1500` (so the
caller's requested output survives the reasoning phase — the pad does **not** change
the byte-cache key), and a 180s timeout.

### 0c. Strategic routing (`AGENT_START_TIER`)
Hardest analytical tasks → Fable 5 (idx 0); high-volume batching stays on free Groq:

| Task | Tier | Rationale |
|------|------|-----------|
| Agent 1 discrepancy classify | **Fable 5** | hardest cross-document reasoning |
| Agent 2 Stage 1 (high volume) | Groq-8B | many batches — kept on free Groq per spec |
| Agent 2 Stage 2 evidence match | **Fable 5** | multi-promise reasoning |
| Agent 3 legal analysis | **Fable 5** | nuanced legal reading |
| Agent 4 risk comparison | **Fable 5** | long-document comparison |
| Agent 5 sector classify | **Fable 5** | one cached call/run — cost negligible |
| Agent 6 moat analysis/verdict | **Fable 5** | competitive reasoning |

`PRICING["anthropic/claude-fable-5"] = {"in": 10.0, "out": 50.0}` — the **verified
OpenRouter list price** (from `GET /models`, not an estimate). `estimate_cost()` and
the returned `cost_usd` meta now bill Fable 5 correctly.

**Cost note (honest):** Fable 5 is materially more expensive. Agent 1's ~33-batch
cold classify is the dominant cost (~$1.8/cold run). The byte-identical prompt cache
makes **repeat runs free**, and `AGENT_START_TIER` is the single tuning knob if a
deployment wants Agent 1 back on Groq-70B for cost. During this audit OpenRouter
spend rose from ~$0.05 → ~$8.12 (funded key).

### 0d. Latent crash fixed (surfaced by the injection audit)
When Fable 5 returns `content: null`, `content.strip()` raised
`AttributeError` — which was **not** in the caught `(KeyError, IndexError,
TypeError)` tuple, so it propagated out of `call_llm` and crashed the whole run
instead of falling through to the next provider. Fixed: `result = (content or
"").strip()` — a `null`/empty 200 now degrades to the next tier exactly like any
other empty response. This matters specifically because Fable 5 (a reasoning model)
is now primary.

---

## PART 1 — Constraint Auditor: 0/6 → real data

### Root causes (three, compounding)
1. **`pdfplumber` was not installed** in the homebrew py3.11 interpreter — every
   live USGS extract returned `None`, so all six materials degraded. Installed.
2. **The parser only understood flat `Production` rows.** USGS prints production as
   parent/subrow tables (`Production:` → `Mine`/`Total`/`Mineral concentrates`) and
   consumption under several labels (`apparent` / `reported` / `estimated`). Gallium,
   germanium, cobalt, nickel, graphite all parsed to `None` despite having data.
3. **No distinction between "withheld" and "fetch failed."** Every failure produced
   the same catch-all string `USGS withheld the value ("W") or the MCS PDF was
   unavailable` — which was often simply false.

### Fixes
**`sollia_working/src/data/usgs_parser.py`** (rewritten, `PARSER_VERSION = 4`):
- Parent/subrow production parsing + `apparent > reported > estimated` consumption
  fallback; footnote-superscript de-merge (`8290,000` → `290,000`); unit parsing
  that tolerates nested parens (`potassium oxide (K2O) equivalent`).
- **Per-metric diagnostics**: `ok | withheld | none | na | not_found |
  not_published`. `withheld` (US "W") is now distinct from `none` (US produces
  none, value 0) from a genuine fetch failure.
- **World production extraction** (`World total` row) — USGS usually publishes world
  figures even when the US figure is withheld, so a withheld material still gets
  cited context.
- **Hafnium** is correctly reported as *not published* (it's a zirconium byproduct;
  only import/export tonnage exists) — with the citable zirconium-sheet URL.

**`sollia_working/src/agents/constraint_auditor.py`:**
- **`_get_usgs_extract` / `_get_fred` are now cache-first** even when the parser or
  FRED client import is unavailable — the old guards returned `None` *before*
  consulting the cache (the reason a cached FRED index still didn't appear).
- **Parser-version staleness**: a cached USGS payload from an older parser is treated
  as stale, so parser upgrades auto-invalidate old null extracts.
- **New per-material status `partial_data`** (demand-only or supply-only): shows the
  side we have, with citation, instead of a blanket `data_unavailable`.
- **Material-specific messages** (`_degradation_reason`) exactly as specified:
  USGS-withheld → shows apparent consumption + world production + source link;
  CENSUS/EIA-gated → names the required key and the HS/series code; total failure →
  the honest "cannot be evaluated until the data source is available."

### Verified
| Ticker | Sector | Result |
|--------|--------|--------|
| **NVDA** | Advanced Semiconductor Mfg | Gallium/Germanium/Cobalt **success** (all ALREADY CONSTRAINED, US production 0/withheld vs real consumption) + FRED sector IP index `IPG3344S`; quartz withheld w/ world context; neon needs CENSUS key; hafnium not-published |
| **NTR** | Fertilizers | Phosphate/Potash/Sulfur **success** + world data; natural gas needs EIA key |
| **TSLA** | Automotive & EV | Cobalt/Nickel/Copper **success** + FRED price series; lithium withheld w/ world context |

The message "0/6 materials evaluated" is gone; the log now reads "3/6 with full
supply/demand, N partial."

---

## PART 2 — Agent 2: historical statements + broken citations

### Root causes
- Historical results (`"Revenue for fiscal year 2024 was $60.9 billion"`) passed the
  **metric+deadline** branch of the deterministic filter: they have a `$` amount AND
  a `fiscal year 2024` token, and nothing blocked past tense.
- The **JSON-parse fallback marked *every* batch sentence `is_promise=true`** — so any
  batch the LLM failed to parse converted its historical sentences into "promises."
- **Transcripts were silently skipped**: the provider returns dicts with a `text`
  key, but the loop only read `getattr(t,'content')` — so no earnings-call promise
  ever existed, and `"url":""`/`"document":"Promise"` came from that gap.

### Fixes (`sollia_working/src/agents/promise_audit.py`)
- **`PAST_TENSE_BLOCK`** deterministic filter (revenue/income/... `for/in FY#### was/
  were/grew/...`; `was/were up/down N%`; past-tense `grew/rose N%`; press-release
  bullet openers; customer-concentration disclosures). A past-result sentence is
  dropped **before** the metric branch — unless it *also* carries a FUTURE_KEYWORD
  (mixed past+outlook sentences survive, and only the future portion is extracted).
- **Stage-1 prompt** now carries the CRITICAL RULES block (past results are not
  promises; extract only the future portion of mixed sentences).
- **Fallback fixed**: only sentences with an explicit FUTURE_KEYWORD survive a JSON
  parse failure, and they flow through the same verbatim + source-metadata validation.
- **Transcript reader** handles dict/`text`/`content`/object shapes → earnings-call
  promises are extracted again, with real `url`.
- **Source metadata** now threads `accession` end-to-end;
  `runs.py` finding builder default changed `"Promise"` → `"SEC filing"`.

### Verified (NVDA)
- **0** historical statements classified as promises.
- **0** findings with an empty URL; **0** with `document:"Promise"` (179 citations
  audited across the full export).
- Status mix is now meaningful: 36 KEPT / 9 PARTIAL / 2 BROKEN / 31 UNRESOLVED.
- Score 77 → **85.2**. *Note the direction:* removing the noise did **not** lower the
  score. The false entries were mostly neutral UNRESOLVED clutter and
  customer-concentration facts; the clean set of genuine forward-looking commitments
  (Blackwell shipping, Data Center cadence, Earth-2) shows NVIDIA's genuinely strong
  ~90% follow-through, which an analyst would agree with. The spec's "expected lower"
  hypothesis assumed the noise inflated the score; in fact it clarified an already
  strong record.

---

## PART 3 — Agent 1: synonym / identical-text false positives

### Fixes (`sollia_working/src/agents/corporate_discrepancy.py`)
- **Identical-excerpt check**: before flagging, the two excerpts are normalized
  (lowercase, whitespace/punctuation collapsed); character-identical excerpts (the
  "certificates of deposit" bug) are dropped, as are same-word-set reorderings and
  cases where the two located source sentences are literally the same sentence.
- **Financial synonym filter** (`FINANCIAL_SYNONYMS` + `_canonicalize_excerpt`):
  `share purchases ↔ share repurchases`, `non-marketable ↔ privately-held`,
  `AIBs ↔ add-in board manufacturers` — a "discrepancy" that differs only by a known
  synonym is a terminology clarification and is skipped.
- **Prompt refinements**: an explicit "Terminology is not a discrepancy" block; a
  "disclosure evolution is not an omission" block (a later filing adding detail, or a
  10-Q incrementally updating a 10-K, is not an omission); and a note that
  "year over year" means different windows in a 10-K vs a 10-Q.
- **Scoring recalibrated** (`sollia_working/src/utils/scoring.py`): the penalty is now
  **gated by severity** (`high×1.0, medium×0.5, low×0.2`). Six medium/low findings
  used to floor the score at 10 — reading like catastrophic inconsistency for a
  "0 high, 3 medium, 3 low" docket. Now only HIGH findings can drive a bearish floor.
  Sanity: 1 high@100 → 45; 8 high@90 → 10 (floor); clean → 90; 3 low@60 → 78.

### Verified (NVDA)
11 → **6** discrepancies. The false positives (certificates of deposit, share
purchases, AIBs, employee-count duplicate, digital-twin phrasing) are gone; the real
ones (Israel employee count 4,700 vs 4,300; Graphics segment income direction;
military call-up magnitude) remain. Score 10 → **51.2 NEUTRAL**.

---

## PART 4 — Agent 3: notes offering ≠ legal proceeding

### Fix (`sollia_working/src/agents/legal_proceedings.py`)
- **8-K prompt** rewritten: "Only report legal proceedings (lawsuits, investigations,
  regulatory/enforcement actions, subpoenas, settlements). Do NOT report financing
  events (notes offerings, credit facilities, stock issuances) — a $3.5B notes
  offering is a financing event, not a legal proceeding." Empty legal content → return
  an empty list with a one-line note on what the filing actually covered.
- **Deterministic backstop** (`_is_financing_event`): even if the LLM returns a
  financing event, it's dropped when its text matches financing language
  (`notes offering|indenture|credit facility|...`) and lacks legal language
  (`lawsuit|settlement|investigation|...`), and the summary is set to the honest note.

### Verified (NVDA)
The 8-K finding now reads: *"8-K filings covered quarterly earnings results (prior)
and a ~$25B multi-tranche notes offering (current) — no legal proceedings to
report."* Score 46 → **55.0 NEUTRAL** (stable legal posture; no fabricated proceeding).

---

## PART 5 — Agent 4: HR-practice severity inflation

### Root cause
The "Employee Listening Systems / Employee Referrals as HIGH-severity NEW risks" came
from **extraction landing inside Item 1**, not Item 1A. NVDA's Item 1 Human Capital
section says *see "Item 1A. Risk Factors — Risks Related to ... Matters" for a
discussion* — the old header finder matched that inline cross-reference (a quoted
subsection title follows, not a preposition), so the "risk factors" the LLM compared
were actually Item 1's HR-program text.

### Fixes (`sollia_working/src/agents/risk_factors.py`)
- **Cross-reference guards in `_find_genuine_1a_start`**: skip an "Item 1A" preceded
  by a `see|refer to|described in|...` lead-in or followed by a `for a discussion of`
  tail. Extraction now anchors on the real Item 1A header.
- **Prompt rules**: human-capital disclosures (referrals, listening systems, training,
  turnover) are operational metrics, not risk factors unless the text explicitly
  states a THREAT; a program present in both filings is REWORDED not REMOVED; a topic
  moving sections (climate → sustainability) is a restructure, not a removal.

### Verified (NVDA)
The extracted Item 1A now starts at *"Item 1A. Risk Factors The following risk factors
should be considered..."* with **0** HR-term hits (listening/referral). The findings
are now genuine: the **Groq IP license arrangement** and **counterparty risk** as real
NEW risks, DGX Cloud as REMOVED, plus REWORDED items — no HR practices. Score 29 →
**24.0** (the fake HR HIGH-risks are gone, but better extraction surfaced genuine new
risks, so the trend stays legitimately bearish — NVIDIA's risk disclosures did expand).

---

## PART 6 — Moat Auditor: claim selection + verdict consistency

### Fixes (`sollia_working/src/agents/moat_auditor.py`)
- **Claim-selection prompt** now asks for the sentence that BEST REPRESENTS the moat,
  prioritizing (1) the MECHANISM (for switching costs: the ecosystem/installed base
  that makes leaving expensive), (2) specific metrics, (3) the CORE product/platform —
  and states explicitly that a partnership/customer-win announcement is not a switching
  cost claim (kills the Hyundai-partnership pick).
- **Verdict/analysis consistency**: the analysis prompt now requires the verdict to
  agree with the analysis — "if your analysis calls the challenge peripheral or not
  material, the verdict is NO THREAT; do not hedge a peripheral challenge into WATCH."
- **Sentence hygiene** (`_clean_sentences`): strips leading page-number / "Table of
  Contents" run-ins and drops sub-8-word heading fragments — this removed the junk
  `"7 Table of Contents Leveraging our intellectual property, or IP."` claim.
- **Honest "no credible challenge"** analysis text when candidates existed but none
  survived selection (instead of falsely saying "no competitor filing contained
  language").

### Verified (NVDA)
The Network Effects claim is now the genuine ecosystem/lock-in sentence: *"The large
and growing number of developers and installed base across our platforms strengthens
our ecosystem and increases the value of our platform for our customers."* Verdicts
and analyses agree (peripheral Alphabet data-platform challenge → NO THREAT).

---

## PART 7 — UI preview: 5 → 10 words

`sollia_web/frontend/src/components/AgentCard.jsx` (`QuoteExcerpt`, the 5-word
truncation an analyst sees) and `sollia_web/frontend/src/App.jsx` (`Quote`, 8-word)
both now show **10 words + ellipsis**; the full quote stays in the expanded citation.
Frontend builds clean (`vite build` ✓).

---

## PART 8 — Security hardening

### 8a. Prompt-injection audit
Ran the specified attacks (`Ignore previous instructions…`, `[[system]]…`,
`SYSTEM: override…`, competitor names shaped like instructions, financial figures with
embedded injections) through **Agent 1 classify, Agent 2 Stage 1, and the moat
index-selector**. Results:
- **No prompt/system text leaks** in any response.
- **Schema unchanged** — Agent 1 returned valid `[]`, Agent 2 a valid promise list,
  the moat selector an in-range index bound to the inputs (it selects an index, so a
  quote **cannot** be injected — verbatim-by-construction).
- Fable 5 **refuses** the injection (returns `content:null`); the chain now falls
  through to Groq which returns valid JSON — this is exactly the 0d fix in action.

### 8b. API-key leakage audit
Scanned **2,912 files** across every output path (`.cache/runs`, `.cache/llm_history`,
`.cache/market_data`, `.llm_cache`, `.test_results`, moat/sector caches) for the actual
key values **and** generic key patterns (`sk-or-v1-…`, `gsk_…`, `AIza…`). **0 leaks.**
- `.env` is in `.gitignore`. ✓
- FRED citation URLs are **public series pages** (`fred.stlouisfed.org/series/…`), never
  the API endpoint, and never carry the key. ✓
- The clean export was scanned separately (Part 9c) — no provider/model names.

### 8c. Input validation (`sollia_web/backend/main.py`)
`_parse_request` now validates before anything reaches SEC/LLM:
- Ticker must match `^[A-Z][A-Z0-9.\-]{0,9}$` → **rejects** 10,000-char strings,
  Unicode, and SQL/injection punctuation (`NVDA; DROP TABLE--`) with HTTP 422.
- Year must be `1994 ≤ year ≤ current+1` → rejects 0, negative, 3000. Pydantic already
  rejects non-int / SQL-string years.
- Empty agent selection → 422. Valid tickers (`NVDA`, `BRK.A`) pass.

### 8d. Rate limiting & abuse prevention
- **Per-run LLM call budget** (`SOLLIA_MAX_LLM_CALLS_PER_RUN=100`): cache hits are
  exempt; the 101st live call raises `LLMBudgetExceeded`.
- **Circuit breaker**: 5 consecutive full-chain failures raise and abort the run
  (`llm_config._budget_precheck`).
- **Per-run SEC fetch budget** (`SOLLIA_MAX_SEC_FETCHES_PER_RUN=50`): a new
  `sollia_working/src/utils/fetch_budget.py` wraps `requests.Session.request` at the
  one shared choke point, counts only `*.sec.gov` hosts, and raises
  `SECFetchBudgetExceeded` on the 51st. Cache hits don't count.
- The backend calls `reset_run_budget()` + `reset_fetch_budget()` at the start of every
  pipeline run. All three guards verified to fire at the boundary and reset cleanly.

---

## PART 9 — Cross-cutting verification

- **9a Citations**: 179 citations in the NVDA export — **0** empty URLs, **0**
  `document:"Promise"`, **0** unverified, and all **44 unique URLs resolve** (41 SEC,
  3 USGS; 0 broken on live HTTP check).
- **9b Scoring**: before/after table at the top of this doc.
- **9c Export leak-check**: the only matches for internal-sounding terms are genuine
  **filing content** — "Groq, Inc." is the real IP-license counterparty in NVDA's
  10-Q, "provider" is inside a product announcement, "rationale" is a word in the
  valuation-allowance disclosure. **No internal field names** (`provider`, `model`,
  `final_score`, `weight`, `score_rationale`, `rationale`) exist as keys. Export schema
  is exactly `ticker/year/timestamp/signal/score/agents{name,score,signal,
  top_findings[]}`.
- **9d Concurrent run**: all 6 agents complete concurrently in ~28s, **no errors, no
  "All providers failed."** Composite correct (mean of 4 scoring agents; audit agents
  excluded). Budgets reset per run; SEC fetch count 38 (< 50).
- **9e Determinism**: **3/3 runs byte-identical** (composite, per-agent scores/signals/
  finding counts, citation fingerprints). Fable 5 is **byte-deterministic at temp 0**
  even on cold calls (verified 3/3 with cache bypassed) — combined with the
  byte-identical prompt cache and pure-math scoring, the pipeline is fully
  deterministic. No temp-0 non-determinism observed.

---

## PART 11 — Success criteria

| Criterion | Status |
|-----------|--------|
| Fable 5 verified working, primary for complex tasks | ✅ |
| Constraint Auditor shows FRED + honest USGS messages | ✅ 3/6 success + world data |
| Agent 2: zero historical statements as promises | ✅ |
| Agent 2: zero findings with missing URLs | ✅ 179/179 cited |
| Agent 1: zero synonym/identical-text false positives | ✅ 11 → 6 |
| Agent 3: debt offerings not legal proceedings | ✅ |
| Agent 4: HR practices not high-severity risks | ✅ structurally excluded |
| Moat Auditor: representative claims, not partnerships | ✅ ecosystem/installed base |
| UI preview 8–10 words | ✅ 10 |
| Prompts pass injection testing | ✅ no leaks, schema intact |
| No API keys / internals leak | ✅ 2,912 files + export clean |
| 6 agents run concurrently without failures | ✅ ~28s, 6/6 |
| NVDA output deterministic across 3 runs | ✅ byte-identical |
| Every finding has a working citation URL | ✅ 44/44 resolve |
| JSON export clean / API-ready | ✅ |

---

## Known limitations (honest)

1. **Cost.** Routing the hard tasks to Fable 5 is real money. Agent 1's ~33-batch
   cold classify dominates (~$1.8/cold run at $10/$50 per 1M). The byte-cache makes
   repeat runs free, and `AGENT_START_TIER` is the single knob to move Agent 1 back to
   Groq-70B if a deployment is cost-sensitive. This audit spent ~$8 of the funded key.
2. **Agent 2 Stage 1 stays on Groq-8B** (per spec's "keep Groq for high-volume
   batching"). The 8B model occasionally emits malformed JSON; the deterministic
   fallback (now future-keyword-gated) covers it, but Stage-1 extraction is not always
   LLM-clean. Moving Stage 1 to Fable 5 would fix quality at a large cost increase.
3. **Concurrent COLD load post-routing not re-stress-tested.** With 5 of 7 stages now
   on Fable 5 (OpenRouter, unlimited rate on the funded key) and only Stage 1 on Groq,
   the old all-on-Groq-70B contention that caused the historical 227s stall is largely
   gone. The concurrent run verified here was warm-cached; a fully cold concurrent run
   was not re-measured for wall-clock (determinism and correctness were).
4. **USGS `partial_data` / `insufficient_data` materials** show the data we have but no
   crossover (e.g. TSLA rare earths: production row absent from the salient-statistics
   table; consumption present). This is honest degradation, not a fabricated number.
5. **Foreign issuers (20-F) and renamed/delisted tickers** remain out of scope
   (pre-existing, documented in earlier hardening passes).
6. **Agent 2 export can carry many findings** (79 for NVDA) — it lists all tracked
   promises, not a top-N. Not a correctness issue, but a large payload; capping is a
   product decision left unchanged.

---

*Files touched:* `llm_config.py`, `usgs_parser.py`, `constraint_auditor.py`,
`promise_audit.py`, `corporate_discrepancy.py`, `legal_proceedings.py`,
`risk_factors.py`, `moat_auditor.py`, `scoring.py`, new `utils/fetch_budget.py`;
backend `main.py`, `agents.py`, `runs.py`; frontend `AgentCard.jsx`, `App.jsx`.
