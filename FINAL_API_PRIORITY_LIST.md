# Sollia Universal API Priority List — FINAL (2026-06-26)
Empirically validated (Part 1). Headers: `User-Agent: "Sollia contact@sollia.app"` everywhere; add `Host: data.sec.gov` for `data.sec.gov`. Rate limit 10 req/s. **No dead sources** (Last10K, FMP, StockAnalysis, Seeking Alpha, Motley Fool, TipRanks, BusinessWire, PR Newswire, CNN, Reuters, Benzinga) appear in code.

### Resolution model (all document types)
1. **`https://data.sec.gov/submissions/CIK{CIK10}.json`** — authoritative list of forms/accessions/`primaryDocument`/dates. *Reliability 3/3, ~200ms.*
2. **`https://www.sec.gov/Archives/edgar/data/{CIK}/{ACC_nodash}/index.json`** — enumerate **all** documents in the accession (REQUIRED to find the real narrative/exhibit, not the iXBRL cover). *3/3, ~65ms.*
3. **`https://www.sec.gov/Archives/edgar/data/{CIK}/{ACC_nodash}/{doc}.htm`** — the document text (clean: strip tags/XBRL; ~16-20% text yield). *3/3, ~130ms.*

---

### 10-K
1. submissions JSON → Archives `.htm` (primary). Section regex: `Item\s+1A` (risk), `Item\s+3\.\s+Legal\s+Proceedings.*?Item\s+4`, `Item\s+7` (MD&A).
2. **Fallback (discovery):** `browse-edgar?action=getcompany&CIK={CIK}&type=10-K&output=atom` (3/3, ~170ms).
3. **Last resort:** efts full-text (slow 3-4s, 500s on punctuation).
> Pre-2016 10-Ks: `primaryDocument` may be a `.txt` — fall back to the accession's largest `.htm` via `index.json`.

### 10-Q
1. submissions JSON → Archives `.htm`. Sections: `PART\s+II.*?Item\s+1\.\s+Legal\s+Proceedings.*?Item\s+2`, `Item\s+1A`.
2. Fallback: browse-edgar atom (`type=10-Q`).

### 8-K  *(corrected this pass)*
1. submissions JSON → **`index.json`** → fetch **body + EX-99 exhibits** (skip `R\d+.htm` XBRL renders & `-index.htm`). **Do NOT use `primaryDocument` alone** — it is the iXBRL cover stub with no event text. Cite each quote to the exact exhibit doc that contains it.
2. Fallback: browse-edgar atom (`type=8-K`).
3. Last resort: efts full-text (only distinctive ≥8-word phrases; strip `$`/`,`).

### DEF 14A
1. submissions JSON → Archives `.htm` (`type=DEF 14A`). Prefer **DEF 14A** over **PRE 14A** (definitive vs preliminary).
2. Fallback: browse-edgar atom.

### Earnings Call Transcripts
1. **SEC 8-K → EX-99.1** (Item 2.02): submissions JSON → `index.json` → EX-99.1 → strip HTML. (Same exhibit-resolution path the 8-K fix now uses.)
2. Fallback: SEC Atom feed filtered for Item 2.02.
3. Backup: Nasdaq.com earnings page (best-effort; structure-fragile).
> Forbidden (dead): Seeking Alpha, Motley Fool — never used.

---
### Known failure modes
- `data.sec.gov` without `Host` header → intermittent failures. Always send it.
- 8-K `primaryDocument` = iXBRL cover (3-9KB `us-gaap:` soup) → **always** resolve via `index.json`.
- efts: HTTP 500 on phrases with `,` `$` or dates → sanitize; generic phrases match 10k+ → only use distinctive long phrases.
- Foreign issuers file **20-F** (not 10-K) — currently unsupported (see `EDGE_CASE_TEST_MATRIX.md`).
