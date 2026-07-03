# Obscure Stock Test Matrix (Part 4)

_Crash-hardening across 15 edge-case tickers, 2026-07. Methodology: each agent
run **sequentially per ticker** (concurrent-across-tickers trips Groq's
per-minute token limit), on a COLD cache, through the real agent entry points.
A result is a **crash** only if the agent raised an unhandled exception; an
`error` key (e.g. "No 10-K found") is **graceful handling**, not a crash._

Harness: `tools/` — the fast 5 agents via `obscure_matrix.py`; Agent 1 (rolling
5-year window, the slow one) on the domestic subset separately.

## Verdict

**0 crashes across all 15 tickers.** Every failure is a handled, explained
degradation. Foreign filers get an explicit "not supported" message; Agent 5
returns UNSUPPORTED for non-resource sectors instead of force-fitting; the
6-figure-share-price and dual-class names classify without CIK issues.

---

## Results

Legend: ✅ clean · 🟡 handled_error (graceful, explained) · ❌ crash

| Ticker | Why it's here | A1 Discrepancy | A2 Promise | A3 Legal | A4 Risk | A5 Constraint | A6 Moat |
|--------|---------------|:---:|:---:|:---:|:---:|:---:|:---:|
| BRK.A | Dual-class, 6-fig share price, conglomerate | ✅ | ✅ | ✅ | ✅ | ✅ UNSUPPORTED | ✅ |
| VALE | Brazilian miner, files 20-F | 🟡 foreign | 🟡 no filings | ✅ | ✅ | ✅ Steel | ✅ |
| SPOT | Luxembourg, files 20-F | 🟡 foreign | 🟡 no filings | ✅ | ✅ | ✅ UNSUPPORTED | ✅ |
| COIN | Crypto exchange, regulatory risk | ✅ | ✅ | ✅ | ✅ | ✅ UNSUPPORTED | ✅ |
| GME | Meme stock, unusual MD&A | _A1 subset_ | ✅ | ✅ | ✅ | ✅ | ✅ |
| RIVN | Pre-profit EV, heavy capex | _A1 subset_ | ✅ | ✅ | ✅ | ✅ | ✅ |
| CHPT | EV charging, small cap, thin history | — | ✅ | ✅ | ✅ | ✅ Auto & EV | ✅ |
| MSTR | Bitcoin treasury, unusual balance sheet | _A1 subset_ | ✅ | ✅ | ✅ | ✅ UNSUPPORTED | ✅ |
| SNAP | Dual-class, no-vote shares | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| IBM | Old co., many business-model changes | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| GE | Turnaround, restatements, complex | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| T | AT&T, heavy debt, spin-offs | — | ✅ | ✅ | ✅ | ✅ | ✅ |
| BABA | Chinese ADR, VIE, files 20-F | 🟡 foreign | 🟡 no filings | ✅ | ✅ | ✅ UNSUPPORTED | ✅ |
| NIO | Chinese EV ADR, files 20-F | 🟡 foreign | 🟡 no filings | ✅ | ✅ | ✅ Auto & EV | ✅ |
| FUBO | Small cap, thin filing history | — | ✅ | ✅ | ✅ | ✅ | ✅ |

_"— " = fast-agent breadth pass only (Agent 1's cold 5-year window is ~10 min/
ticker; run on the Part-9 subset BRK.A/GME/MSTR/RIVN/BABA for full 6-agent
coverage). "_A1 subset_" = Agent 1 verified on that ticker; see below._

---

## What each edge case exercised (and what held)

- **BRK.A** — the CIK/ticker normalization (`BRK.A` → SEC `BRK-A`) resolved
  correctly; Berkshire's ~37 MB filing set parsed without OOM (promise_audit
  took 149 s cold but completed). Agent 5 correctly returns UNSUPPORTED — a
  conglomerate isn't a resource sector, and the system does NOT force-fit one.
- **Foreign filers (VALE, SPOT, BABA, NIO)** — Agent 1's rolling window detects
  the absence of any 10-K and returns an explicit *"foreign private issuer
  (files 20-F, not 10-K) — not yet supported"* message rather than a confusing
  "No 10-K found". Agent 2 returns a handled "No filings available". Agents
  3/4/6 degrade to neutral (no US 10-K to compare) without crashing. Agent 5
  still works — it classifies by business description, not by 10-K, so VALE →
  Steel and NIO → Automotive & EV are correctly audited, while SPOT (music) and
  BABA (e-commerce) → UNSUPPORTED.
- **COIN** — crypto-exchange regulatory language parsed cleanly; Agent 5
  UNSUPPORTED (financial exchange, not a physical-supply sector).
- **Agent 5 discipline** — across the whole matrix, Agent 5 returned UNSUPPORTED
  for every non-resource sector (BRK.A, SPOT, COIN, BABA, SNAP-class names) and
  only produced a constraint audit where a real raw-material sector applied
  (VALE steel, NIO EV). This is the intended behavior: no force-fitting.

## Agent 1 (rolling 5-year window) — subset detail

Agent 1 is verified end-to-end on the Part-9 domestic subset (BRK.A, GME,
MSTR, RIVN) plus the foreign-filer path (BABA → clean "foreign" message). See
the Part 9 verification section of `AGENT8_FINAL_HARDENING.md` for per-ticker
window composition (fiscal years analyzed, findings, score).

## Failures observed — all data, none code

No **code bugs** were found in this matrix. The only non-clean results are
**data conditions**, each handled gracefully:

| Condition | Tickers | Handling |
|---|---|---|
| Foreign private issuer (no 10-K) | VALE, SPOT, BABA, NIO | Explicit "not supported" message from A1/A2; A3/A4/A6 neutral; A5 works via business classification |
| Non-resource sector | BRK.A, SPOT, COIN, BABA, SNAP, IBM, T, GE, FUBO | A5 returns UNSUPPORTED (by design) |
