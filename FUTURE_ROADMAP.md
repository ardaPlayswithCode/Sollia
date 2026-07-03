# Sollia — Future Roadmap

_Part 8 of the 2026-07 final bulletproofing pass. Recommendations, with
architecture sketches and a build-order call for each. Written against the
system as it stands after this pass: 6 agents live, adaptive LLM routing,
rolling 5-year Agent-1 window, per-run cost/fetch/rate abuse guards._

---

## 8a. The next wave

### Agent 7 — Capital Allocation Auditor  ·  **Build next**

**Thesis.** Track management's capital-deployment promises (capex, R&D,
buybacks, dividends, debt paydown) against what the cash-flow statement
actually shows. Every input is already in the SEC filings Sollia fetches, so
this is the highest-leverage next agent: it reuses the whole fetch/cache/cite
stack and adds a genuinely new signal ("they said $10B of buybacks; the
financing-activities line shows $3B").

**Architecture.** Mirror Agent 2 (Promise Audit), which already does
promise→outcome matching:
1. **Extract commitments** from MD&A + earnings calls (Stage-1 extraction, the
   cheap Groq-8B tier): "we plan ~$X capex", "we intend to return $Y via
   buybacks", "we expect R&D to grow N%".
2. **Extract actuals** deterministically from the cash-flow statement (XBRL
   `us-gaap:PaymentsToAcquirePropertyPlantAndEquipment`,
   `PaymentsForRepurchaseOfCommonStock`, `PaymentsOfDividends`,
   `ResearchAndDevelopmentExpense`). XBRL Financial Statements datasets make
   this exact and citable — no LLM needed for the actuals.
3. **Match + verdict** (Stage-2, Fable 5 only when the prompt is genuinely
   large, per the adaptive router): DEPLOYED / PARTIAL / UNDERDELIVERED, each
   with the verbatim promise, the XBRL fact, and both EDGAR links.
4. Score via a new `score_capital_allocation` in `src/utils/scoring.py`
   (weight it into the composite like Agents 2-4).

**Cost.** Extraction on the free tier, one large matching call → Fable 5 only
when warranted. Well under the $0.50/run envelope this pass established.

**Effort.** ~1 week — most of the plumbing is Agent-2 shaped. The new piece is
the XBRL Financial Statement dataset reader, which Agent 7 would share with a
future fundamentals layer.

### Meta-Agent — Platform Monitor  ·  **Build once there are paying customers, not before**

**Thesis.** Sollia depends on external data shapes that drift: USGS renames MCS
table rows (this pass already carries `PARSER_VERSION=4` because of one such
break), SEC deprecates endpoints, FRED retires series. A monitor that probes
each data source on a schedule and alerts on a shape change catches breakage
before a customer sees a blank agent.

**Architecture.** A scheduled job (cron / `tools/health_check.py` on a timer),
NOT an agent in the pipeline:
- Reuse `tools/health_check.py`'s probes (already checks SEC endpoints, keys,
  cache integrity, imports) and add golden-fixture assertions: parse a known
  USGS commodity and assert the extracted numbers equal a pinned expected set;
  pull a known FRED series and assert schema; parse a known filing and assert
  Item 1A extraction lands.
- On mismatch → PagerDuty/email + open a ticket. Store a rolling history so a
  gradual drift is visible.

**Timing.** The value is proportional to customer count (an alert only matters
if someone's watching a dashboard). Pre-revenue, the twice-weekly manual
`health_check.py` run is enough. Build the automated monitor with the first
cohort of paying customers — it is cheap once `health_check.py` exists.

### Agent-to-Agent Communication  ·  **Design now, build after Agent 7**

**Thesis.** Today each agent is independent and the synthesizer only *averages*
scores. The real product unlock is *cross-agent inference*: if Agent 5
(Constraint Auditor) finds a growth target is physically unsupported by
raw-material supply, Agent 2 (Promise Audit) should flag the matching growth
promise as **AT RISK** *before* its deadline, not mark it UNRESOLVED and wait.

**Architecture.** Keep agents independently runnable; add a post-pass
**cross-agent reconciler** (a synthesizer stage, not inter-thread chatter —
that keeps the concurrent executor and determinism intact):
1. Each agent already emits structured findings with metric + horizon.
2. The reconciler joins findings on `(metric family, horizon)`: an Agent-5
   "lithium supply supports only ~15%/yr vs the 40% plan" constraint links to
   an Agent-2 "we will grow deliveries 40%/yr through 2027" promise.
3. It annotates the Agent-2 finding with `at_risk_reason` + a link to the
   Agent-5 evidence. No new LLM call needed for the join; one optional Fable-5
   call to phrase the combined verdict.

This is a **deterministic join over existing outputs**, so it adds a signal
without touching the parallel-execution or cost model. Design the shared
finding schema (metric family taxonomy, horizon normalization) alongside
Agent 7 so both emit join-ready findings.

### Real-Time Filing Monitoring  ·  **Explore; not near-term**

**Thesis.** Sollia is batch/on-demand. An event-driven mode that watches EDGAR
and alerts on material new filings (8-K item 2.02/1.03/5.02, a fresh 10-K)
would move the product from "research tool" to "monitoring service".

**Architecture.** EDGAR publishes a real-time submissions feed
(`https://www.sec.gov/cgi-bin/browse-edgar?...&output=atom`) and full-text
search. A poller (respecting the 10 req/s ceiling) diffs the latest accession
per watched CIK against a seen-set; on a new material filing it enqueues a
targeted single-agent run (e.g. new 8-K → Agent 3 legal + Agent 2 promise) and
pushes the delta. This is additive: the agents already run on demand; the new
parts are a scheduler, a per-customer watchlist, and a notification channel.

**Timing.** Worth a spike after Agent 7 + reconciler, because the alerts are
only compelling once cross-agent "AT RISK" inference exists — otherwise it is
just a filing firehose.

### Non-US Market Expansion  ·  **Large TAM, large lift — gate on demand**

Foreign filers are today handled by an explicit, honest "not yet supported"
(verified this pass on VALE/SPOT and every 20-F ADR). Expanding:
- **SEDAR+ (Canada)** and **20-F filers on EDGAR** are the cheapest first step:
  20-F risk factors + MD&A are already on EDGAR in the same fetch path — Agents
  3/4/6 could support 20-F with a section-map change and no new data source.
  This alone unlocks every ADR (BABA, VALE, SPOT, NIO).
- **ESEF (EU)** is iXBRL-tagged and machine-friendly but multi-jurisdiction and
  multi-language — a per-country section taxonomy and a translation layer.
- **TAM.** US-listed 20-F ADRs: hundreds of large caps, low incremental cost →
  do first. Full SEDAR/ESEF: multiplies the addressable universe but needs
  per-regime document maps, filing-calendar logic, and language handling.

**Call.** Ship 20-F support (small, high-ROI) with Agent 7. Defer SEDAR/ESEF
until customer demand names a specific market.

---

## 8b. Technology radar

### On-device models for Stage-1 extraction

Agent 2's Stage-1 and Agent 5's classify are simple extraction/selection tasks
already routed to the cheapest tier. When a Llama-70B-class model runs locally
on Apple Silicon at usable latency, those stages could run **on-device at zero
API cost and zero rate limit** — which directly attacks the one scaling limit
this pass leaves standing (provider queue time). The adaptive router already
isolates exactly the calls that qualify (`<500 words` → tier-2), so adoption is
a one-line provider addition (a local `llama.cpp`/MLX endpoint as tier 2.5),
not a rewrite. **Watch; adopt when local TTFT < ~2s for 8B-class.**

### Fine-tuning on the recorded call history

Every LLM call is already logged to `.cache/llm_history/` with prompt,
response, provider, and parse success (600+ calls recorded, and growing every
run). That is a ready-made supervised dataset for the classification tasks
(discrepancy type/severity, promise extraction, moat classification, sector
selection). A fine-tuned Llama-8B on Sollia's own moat/severity labels could
plausibly match Fable 5 on those narrow tasks at a tiny fraction of the cost.
**Path:** export via `tools/export_llm_history.py` → filter to high-confidence
verdicts (parse_success, verified quotes) → LoRA fine-tune → benchmark against
the existing `tools/model_benchmark.py` fixtures before trusting it. **Revisit
at ~5k labeled calls per task**; today's volume is enough to prototype the
severity-audit model but not yet the general classifier.

### Vector search for evidence (Agent 2 Stage 2)

Agent 2's Stage-2 evidence step uses keyword search. Sollia already computes and
caches FinBERT embeddings for Agent 1 (`.cache/embeddings`, content-addressed),
so the infrastructure to switch Stage-2 to **semantic** evidence retrieval
exists at near-zero marginal cost. Expected win: catch evidence that restates a
promise's outcome in different words (keyword misses it). **Worth an A/B**:
embed promise + candidate sentences, retrieve top-k by cosine, compare
evidence recall against the keyword baseline on a labeled set. Low risk (additive
to the existing retriever), medium reward. Good first "quality" project after
Agent 7.

---

## Recommended build order

1. **Agent 7 (Capital Allocation)** + the shared join-ready finding schema.
2. **Cross-agent reconciler** ("AT RISK" inference) as a synthesizer stage.
3. **20-F support** for ADRs (small change, unlocks a whole ticker class).
4. **Automated Platform Monitor** (with the first paying customers).
5. **Vector-search A/B** for Agent 2 evidence; **fine-tune** the severity/moat
   classifiers as history volume crosses ~5k labeled calls per task.
6. **Real-time monitoring** spike, then **on-device tier** when local models
   mature.
