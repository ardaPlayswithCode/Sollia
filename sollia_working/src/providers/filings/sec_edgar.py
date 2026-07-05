"""SEC EDGAR provider — pulls 10-K filings and parses MD&A."""
from __future__ import annotations
import re
import time
import logging
from typing import Dict, List, Optional, Tuple
import requests

logger = logging.getLogger(__name__)
# ── Unified cache paths (shared across all agents) ─────────────────────────
import os as _os
_CACHE_ROOT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', '..', '..', '.cache')
_FILINGS_CACHE_DIR = _os.path.join(_CACHE_ROOT, 'filings')
_TEXTS_CACHE_DIR = _os.path.join(_CACHE_ROOT, 'texts')
_LLM_CACHE_DIR = _os.path.join(_CACHE_ROOT, 'llm')
_COMPANY_TICKERS_CACHE = _os.path.join(_CACHE_ROOT, 'company_tickers.json')
_FILINGS_TTL_SECONDS = 24 * 3600  # refresh the shared filings list at most daily
_BACKUP_TTL_SECONDS = 3600        # backup-sourced metadata revalidates hourly
for _d in (_CACHE_ROOT, _FILINGS_CACHE_DIR, _TEXTS_CACHE_DIR, _LLM_CACHE_DIR):
    _os.makedirs(_d, exist_ok=True)


SEC_BASE = "https://data.sec.gov"
EDGAR_BASE = "https://www.sec.gov"
EFTS_BASE = "https://efts.sec.gov"
_HEADERS = {"User-Agent": "Sollia contact@sollia.app", "Accept-Encoding": "gzip, deflate"}
_DATA_HEADERS = {"User-Agent": "Sollia contact@sollia.app", "Accept-Encoding": "gzip, deflate", "Host": "data.sec.gov"}

# ── EDGAR backup-source reality (empirical research, 2026-07-03 Part 1b) ───
# Tested with NVDA/TSLA/FUBO across recent/3y/10y eras:
#   * data.sec.gov (submissions API)  — PRIMARY metadata. Own host.
#   * www.sec.gov browse-edgar Atom   — BEST metadata backup: different host
#     path, complete history (NVDA 1999→2026 in one response), all form
#     types, ~200-500ms, 3/3 tickers. No reportDate/primaryDocument fields
#     (primary doc resolves lazily via index.json when the doc is fetched).
#   * efts.sec.gov search-index       — THIRD independent host. ciks= +
#     forms= with EMPTY q returns full filing metadata INCLUDING the primary
#     document name (_id = "accession:doc"), 2001+ coverage. Observed
#     intermittent 500s (3 in one research run) → last-resort tier.
#   * s3://sec-datasets/              — DOES NOT EXIST (NoSuchBucket, both
#     virtual-host styles + registry.opendata.aws 404). Dead source; do not
#     re-add without new evidence.
#   * full-index master.idx           — works back to 1994 but 27MB/quarter
#     on the SAME host as the Atom feed → adds weight, not independence.
#   * Documents: NO free cross-host mirror exists (data.sec.gov returns 404
#     for Archives paths). Backup for documents is the complete-submission
#     .txt on www.sec.gov; a FULL sec.gov outage serves from disk cache only.
_BACKUP_ATOM_FORMS = ("10-K", "10-Q", "8-K", "DEF 14A")
# Which source served the last metadata fetch, per ticker (observability +
# outage tests): disk-cache | edgar-live | atom-backup | efts-backup |
# stale-cache | unavailable
LAST_METADATA_SOURCE: Dict[str, str] = {}
# WHY a ticker produced zero usable filings (set alongside an empty
# get_all_filings result). Agents use it for accurate degradation messages —
# "foreign private issuer" is actionable; "No filings available" is not.
NO_FILINGS_REASON: Dict[str, str] = {}


def no_filings_reason(ticker: str) -> str:
    """Human-readable reason the last get_all_filings(ticker) was empty."""
    return NO_FILINGS_REASON.get(
        ticker.strip().upper().replace(".", "-"),
        f"No filings available for {ticker}")



# ── Universal cache helpers ────────────────────────────────────────────────

def _load_json(path: str):
    """Load JSON file, return None if missing/corrupt."""
    import json as _json
    try:
        if _os.path.exists(path):
            with open(path) as f:
                return _json.load(f)
    except Exception:
        pass
    return None

def _save_json(path: str, data) -> bool:
    """Save data as JSON atomically (temp + rename) so concurrent agent threads
    (WS6) never observe a half-written cache file. Returns True on success."""
    import json as _json
    try:
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        tmp = "%s.%d.tmp" % (path, _os.getpid())
        with open(tmp, 'w') as f:
            _json.dump(data, f, default=str)
        _os.replace(tmp, path)
        return True
    except Exception:
        return False

def get_cached_filings(ticker: str) -> dict | None:
    """Load cached filing metadata for a ticker. Returns dict keyed by accession_number."""
    path = _os.path.join(_FILINGS_CACHE_DIR, f'{ticker.upper()}_filings.json')
    return _load_json(path)

def cache_filings(ticker: str, filings: list[dict]) -> bool:
    """Cache filing metadata keyed by accession_number."""
    cache = {}
    for f in filings:
        acc = f.get('accession_number', '')
        if acc:
            cache[acc] = {
                'cik': f.get('cik', ''),
                'form': f.get('form', ''),
                'filing_date': f.get('filing_date', ''),
                'period_of_report': f.get('period_of_report', ''),
                'primary_doc': f.get('primary_doc', ''),
                'htm_url': f.get('htm_url', ''),
                'accession_nodash': f.get('accession_nodash', ''),
            }
    path = _os.path.join(_FILINGS_CACHE_DIR, f'{ticker.upper()}_filings.json')
    return _save_json(path, cache)

def _text_cache_key(accession_number: str, content_type: Optional[str]) -> str:
    """Namespace the text-cache key by content type so different representations
    of the SAME filing never collide.

    Convention: the PLAIN accession key (content_type=None) holds the RAW filing
    HTML — every agent that fetches a filing writes it there (Agents 1/3/4 and
    Agent 2's Stage-2 evidence search all read it and clean it themselves). A
    non-None content_type appends '::{type}', e.g. '{acc}::mda' for Agent 2's
    extracted MD&A. This is what stops Agent 2 Stage 1 from reading another
    agent's raw HTML as if it were MD&A (the texts-cache content collision)."""
    return accession_number if not content_type else f"{accession_number}::{content_type}"


def get_cached_text(ticker: str, accession_number: str,
                    content_type: Optional[str] = None) -> str | None:
    """Load cached text for a filing. content_type=None reads the plain key (raw
    HTML by convention); see _text_cache_key. Backward compatible: existing
    callers that omit content_type get exactly the previous behavior."""
    path = _os.path.join(_TEXTS_CACHE_DIR, f'{ticker.upper()}_texts.json')
    all_texts = _load_json(path) or {}
    return all_texts.get(_text_cache_key(accession_number, content_type))

def cache_text(ticker: str, accession_number: str, text: str,
               content_type: Optional[str] = None) -> bool:
    """Cache text for a filing (merges into existing dict). content_type=None
    writes the plain key (raw HTML by convention); see _text_cache_key."""
    path = _os.path.join(_TEXTS_CACHE_DIR, f'{ticker.upper()}_texts.json')
    all_texts = _load_json(path) or {}
    all_texts[_text_cache_key(accession_number, content_type)] = text
    return _save_json(path, all_texts)

def get_cached_llm(prompt_hash: str) -> dict | None:
    """Load cached LLM response. Returns {prompt, response, model, timestamp} or None."""
    path = _os.path.join(_LLM_CACHE_DIR, f'{prompt_hash}.json')
    return _load_json(path)

def cache_llm(prompt_hash: str, prompt: str, response: str, model: str) -> bool:
    """Cache LLM prompt+response with metadata."""
    from datetime import datetime as _dt
    path = _os.path.join(_LLM_CACHE_DIR, f'{prompt_hash}.json')
    return _save_json(path, {
        'prompt': prompt,
        'response': response,
        'model': model,
        'timestamp': _dt.now().isoformat(),
    })


class SECEdgarProvider:
    def __init__(self) -> None:
        self._cik_cache: Dict[str, str] = {}
        self._name_cache: Dict[str, str] = {}

    def get_cik(self, ticker: str) -> Optional[str]:
        key = ticker.strip().upper().replace(".", "-")  # " aapl " -> AAPL; BRK.A -> BRK-A (SEC form)
        if key in self._cik_cache:
            return self._cik_cache[key]
        # Disk cache first: a transient SEC 429 must never make a known ticker
        # "not resolve to an SEC filer" (that broke Agent 6 on TSLA).
        cik = self._cik_from_map(key, _load_json(_COMPANY_TICKERS_CACHE))
        if cik:
            return cik
        try:
            resp = requests.get(
                "https://www.sec.gov/files/company_tickers.json",
                headers=_HEADERS, timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            _save_json(_COMPANY_TICKERS_CACHE, data)
            cik = self._cik_from_map(key, data)
            if cik:
                return cik
        except Exception as exc:
            logger.error("CIK lookup failed for %s: %s", ticker, exc)
        return None

    def _cik_from_map(self, key: str, data) -> Optional[str]:
        if not isinstance(data, dict):
            return None
        for item in data.values():
            if isinstance(item, dict) and str(item.get("ticker", "")).upper() == key:
                cik = str(item["cik_str"]).zfill(10)
                self._cik_cache[key] = cik
                self._name_cache[key] = item.get("title", "")
                return cik
        return None

    def get_company_name(self, ticker: str) -> str:
        key = ticker.upper()
        if key in self._name_cache:
            return self._name_cache[key]
        cik = self.get_cik(ticker)
        if not cik:
            return ticker
        try:
            resp = requests.get(f"{SEC_BASE}/submissions/CIK{cik}.json", headers=_DATA_HEADERS, timeout=15)
            resp.raise_for_status()
            name = resp.json().get("name", ticker)
            self._name_cache[key] = name
            return name
        except Exception:
            return ticker

    def get_10k_filings(self, ticker: str) -> List[dict]:
        """Get 10-K filings only (backwards compatible)."""
        return [f for f in self.get_all_filings(ticker) if f.get('form', '') in ('10-K', '10-K/A')]

    def get_10q_filings(self, ticker: str) -> List[dict]:
        """Get 10-Q filings only."""
        return [f for f in self.get_all_filings(ticker) if f.get('form', '') in ('10-Q', '10-Q/A')]

    def get_all_filings(self, ticker: str) -> List[dict]:
        """Get ALL 10-K and 10-Q filings (no year filter).

        FALLBACK CHAIN (Part 1c, 2026-07-03 — empirical priorities inline):
          1. Fresh disk cache (<24h, .cache/filings/) — no network at all.
             A backup-sourced cache entry is treated as ALWAYS stale so the
             primary is re-tried on the next call (self-healing after outage).
          2. data.sec.gov submissions API — richest schema (primary).
          3. www.sec.gov browse-edgar Atom — different host, complete history,
             best measured backup (3/3 tickers, 1999+ coverage).
          4. efts.sec.gov search-index — third independent host, 2001+,
             carries primary-doc names; intermittent 500s → last resort.
          5. Stale disk cache (any age) — a transient 429/outage can never
             turn into "No filings available" (that broke Agent 2 on TSLA).
          6. [] + LAST_METADATA_SOURCE[ticker] = "unavailable" (graceful).
        """
        key = ticker.strip().upper().replace(".", "-")
        cik = self.get_cik(ticker)
        if not cik:
            LAST_METADATA_SOURCE[key] = "unavailable"
            return []
        path = _os.path.join(_FILINGS_CACHE_DIR, f'{key}_filings.json')
        cached = _load_json(path)
        if not (isinstance(cached, dict) and cached.get("filings", {}).get("recent")):
            cached = None  # wrong schema (e.g. old keyed-by-accession writes) — refetch
        if cached is not None:
            try:
                fresh = (time.time() - _os.path.getmtime(path)) < _FILINGS_TTL_SECONDS
            except OSError:
                fresh = False
            if fresh and not cached.get("_backup_source"):
                LAST_METADATA_SOURCE[key] = "disk-cache"
                return self._filings_from_submissions(cik, cached, key)
            # Backup-sourced caches stay fresh for a SHORTER window (1h vs
            # 24h): long enough that the 6 agents of an outage run share one
            # backup fetch instead of each re-trying the dead primary, short
            # enough that the primary is revalidated soon after recovery.
            if cached.get("_backup_source"):
                try:
                    age = time.time() - _os.path.getmtime(path)
                except OSError:
                    age = float("inf")
                if age < _BACKUP_TTL_SECONDS:
                    LAST_METADATA_SOURCE[key] = str(cached["_backup_source"])
                    return self._filings_from_submissions(cik, cached, key)

        data = self._fetch_submissions(cik, ticker)
        source = "edgar-live"
        if data is None:
            data = self._fetch_filings_atom(cik, ticker)
            source = "atom-backup"
        if data is None:
            data = self._fetch_filings_efts(cik, ticker)
            source = "efts-backup"
        if data is not None:
            if source != "edgar-live":
                data["_backup_source"] = source
                logger.warning("EDGAR primary unreachable for %s — metadata served "
                               "by %s", ticker, source)
            _save_json(path, data)  # raw submissions schema, per the shared contract
            LAST_METADATA_SOURCE[key] = source
            return self._filings_from_submissions(cik, data, key)
        if cached is not None:
            logger.warning("All SEC metadata sources unreachable for %s — serving "
                           "stale filings cache", ticker)
            LAST_METADATA_SOURCE[key] = "stale-cache"
            return self._filings_from_submissions(cik, cached, key)
        LAST_METADATA_SOURCE[key] = "unavailable"
        logger.error("No filing metadata available for %s: EDGAR, Atom and EFTS "
                     "all failed and no cache exists.", ticker)
        return []

    def _fetch_submissions(self, cik: str, ticker: str) -> Optional[dict]:
        """Live-fetch CIK{cik}.json and merge paginated history into `recent`
        (heavy filers overflow the 1000-entry recent block)."""
        try:
            url = f"{SEC_BASE}/submissions/CIK{cik}.json"
            resp = requests.get(url, headers=_DATA_HEADERS, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Submissions fetch failed for %s: %s", ticker, exc)
            return None
        recent = data.get("filings", {}).get("recent", {})
        for file_info in data.get("filings", {}).get("files", []):
            try:
                time.sleep(0.12)
                r = requests.get(f"{SEC_BASE}/submissions/{file_info['name']}",
                                 headers=_DATA_HEADERS, timeout=30)
                if r.status_code == 200:
                    old = r.json()
                    for k in ("form", "accessionNumber", "filingDate", "reportDate",
                              "items", "primaryDocument"):
                        if k in old and k in recent:
                            recent[k].extend(old.get(k, []))
            except Exception:
                pass
        return data

    @staticmethod
    def _synth_submissions(rows: List[Tuple[str, str, str, str, str]]) -> dict:
        """Build a submissions-schema dict from (form, accession_dash,
        filing_date, report_date, primary_doc) rows so backup sources flow
        through the SAME parser (_filings_from_submissions) as the primary."""
        block = {"form": [], "accessionNumber": [], "filingDate": [],
                 "reportDate": [], "primaryDocument": [], "items": []}
        seen = set()
        for form, acc, fdate, rdate, pdoc in rows:
            if not acc or acc in seen:
                continue
            seen.add(acc)
            block["form"].append(form)
            block["accessionNumber"].append(acc)
            block["filingDate"].append(fdate)
            block["reportDate"].append(rdate)
            block["primaryDocument"].append(pdoc)
            block["items"].append("")
        return {"filings": {"recent": block}}

    def _fetch_filings_atom(self, cik: str, ticker: str) -> Optional[dict]:
        """BACKUP metadata source #1 — www.sec.gov browse-edgar Atom feed.
        Complete filing history per form type in one request each (measured:
        NVDA 10-K span 1999→2026). Carries form/accession/filing-date but NOT
        reportDate/primaryDocument — the primary doc resolves lazily via
        _find_primary_doc (index.json) when a document is actually fetched."""
        rows: List[Tuple[str, str, str, str, str]] = []
        got_any = False
        for form in _BACKUP_ATOM_FORMS:
            url = (f"{EDGAR_BASE}/cgi-bin/browse-edgar?action=getcompany"
                   f"&CIK={cik}&type={form.replace(' ', '+')}&dateb=&owner=include"
                   f"&count=100&output=atom")
            try:
                time.sleep(0.12)
                resp = requests.get(url, headers=_HEADERS, timeout=15)
                if resp.status_code != 200:
                    continue
                got_any = True
                for m in re.finditer(r"<entry>(.*?)</entry>", resp.text, re.DOTALL):
                    e = m.group(1)
                    def _tag(name, _e=e):
                        t = re.search(rf"<{name}>\s*([^<]+?)\s*</{name}>", _e)
                        return t.group(1) if t else ""
                    ftype = _tag("filing-type")
                    acc = _tag("accession-nu?mber")
                    fdate = _tag("filing-date")
                    if ftype and acc:
                        rows.append((ftype, acc, fdate, "", ""))
            except Exception as exc:
                logger.debug("Atom backup fetch failed for %s %s: %s", ticker, form, exc)
        if not rows:
            return None  # nothing usable — let the chain try the next source
        return self._synth_submissions(rows)

    def _fetch_filings_efts(self, cik: str, ticker: str) -> Optional[dict]:
        """BACKUP metadata source #2 (last resort) — efts.sec.gov full-text
        search API, a THIRD independent sec.gov host. Query shape verified
        2026-07-03: ciks=<padded>&forms=... with EMPTY q returns filing hits
        whose _id is "accession:primary_doc" (doc name included, 2001+).
        10 hits/page; pages are bounded to keep the fallback cheap."""
        rows: List[Tuple[str, str, str, str, str]] = []
        got_any = False
        for forms in ("10-K,10-Q", "8-K,DEF 14A"):
            for page in range(5):  # ≤50 filings per form group — outage mode
                url = (f"{EFTS_BASE}/LATEST/search-index?q=&ciks={cik}"
                       f"&forms={forms}&from={page * 10}")
                try:
                    time.sleep(0.12)
                    resp = requests.get(url, headers=_HEADERS, timeout=15)
                    if resp.status_code != 200:
                        break
                    hits = (resp.json().get("hits", {}) or {}).get("hits", [])
                except Exception as exc:
                    logger.debug("EFTS backup fetch failed for %s: %s", ticker, exc)
                    break
                got_any = True
                if not hits:
                    break
                for h in hits:
                    src = h.get("_source", {}) or {}
                    _id = h.get("_id", "") or ""
                    acc, _, pdoc = _id.partition(":")
                    form = src.get("file_type") or (src.get("root_forms") or [""])[0]
                    rows.append((form, acc, src.get("file_date", ""),
                                 src.get("period_ending", ""), pdoc))
        if not rows:
            return None
        return self._synth_submissions(rows)

    def _filings_from_submissions(self, cik: str, data: dict,
                                  ticker_key: str = "") -> List[dict]:
        block = data.get("filings", {}).get("recent", {})
        all_filings = self._parse_filing_block(cik, block)
        all_filings.sort(key=lambda x: x.get("filing_date", ""), reverse=True)
        if not all_filings and ticker_key:
            # Diagnose the empty result so agents can degrade with an accurate
            # message (SPOT/BABA-class: foreign private issuers file 20-F/40-F/
            # 6-K, which the domestic-form pipeline deliberately skips).
            forms = set(block.get("form", []) or [])
            foreign = sorted(forms & {"20-F", "40-F", "6-K"})
            if foreign:
                NO_FILINGS_REASON[ticker_key] = (
                    f"{ticker_key} is a foreign private issuer (files "
                    f"{'/'.join(f for f in foreign if f != '6-K') or '6-K'}, "
                    "not 10-K/10-Q). Foreign filers are not yet supported.")
        return all_filings

    def _parse_filing_block(self, cik: str, block: dict) -> List[dict]:
        out: List[dict] = []
        forms = block.get("form", [])
        accs = block.get("accessionNumber", [])
        dates = block.get("filingDate", [])
        periods = block.get("reportDate", [])
        primary = block.get("primaryDocument", [])
        for i, form in enumerate(forms):
            if form in ("10-K", "10-K/A", "10-Q", "10-Q/A", "8-K", "8-K/A") or "14A" in form:
                acc_dash = accs[i] if i < len(accs) else ""
                acc_nd = acc_dash.replace("-", "")
                pd = primary[i] if i < len(primary) else ""
                htm_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{pd}" if pd else ""
                out.append({
                    "cik": cik,
                    "form": form,
                    "accession_number": acc_dash,
                    "accession_nodash": acc_nd,
                    "filing_date": dates[i] if i < len(dates) else "",
                    "period_of_report": periods[i] if i < len(periods) else "",
                    "primary_doc": pd,
                    "htm_url": htm_url,
                })
        return out

    def fetch_html(self, cik: str, acc_nd: str, doc: str, ticker: str = "") -> str:
        """Fetch filing HTML with the DOCUMENT fallback chain (Part 1, 2026-07-03):

          1. Primary .htm on www.sec.gov (+ one retry — absorbs blips).
          2. Complete-submission .txt on www.sec.gov — every filing exists as
             {accession}.txt containing all exhibits in SGML <DOCUMENT> blocks;
             the primary document is extracted from it. Recovers missing or
             renamed primary docs and .htm-path 404s.
          3. Graceful "" — there is NO free cross-host mirror of EDGAR
             documents (verified 2026-07-03: data.sec.gov 404s Archives paths;
             efts.sec.gov serves search metadata, not documents; the AWS
             bucket in the old comment does not exist). During a full
             www.sec.gov outage, documents come from .cache/texts/ or not at
             all — callers already degrade per-document.

        Always uses the compliant SEC User-Agent — SEC's fair-access policy
        requires a descriptive UA with contact info on EVERY request."""
        # Source 1: SEC EDGAR direct .htm (primary, ~0.2s) + one retry
        url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{doc}"
        for attempt in range(2):
            try:
                time.sleep(0.15)
                resp = requests.get(url, headers=_HEADERS, timeout=30)
                resp.raise_for_status()
                if len(resp.text) > 5000:
                    return resp.text
                break  # a 200 that is too small won't improve on retry
            except Exception as exc:
                logger.debug("SEC .htm attempt %d failed: %s", attempt + 1, exc)

        # Source 2: complete-submission .txt (same host, different artifact)
        txt = self._fetch_from_complete_submission(cik, acc_nd, doc)
        if txt:
            logger.warning("Primary .htm unavailable for %s/%s — served from "
                           "complete-submission .txt", acc_nd, doc or "?")
            return txt
        return ""

    # Read at most this much of a complete-submission .txt: the primary
    # document is the FIRST <DOCUMENT> block; reading past it only pulls
    # exhibits and uuencoded graphics (some filings exceed 100MB).
    _TXT_FETCH_CAP = 12 * 1024 * 1024

    def _fetch_from_complete_submission(self, cik: str, acc_nd: str, doc: str) -> str:
        """DOCUMENT backup: stream {accession}.txt and extract the primary
        document's <TEXT> payload. Returns "" when unavailable."""
        acc_dash = f"{acc_nd[:10]}-{acc_nd[10:12]}-{acc_nd[12:]}" if len(acc_nd) == 18 else acc_nd
        url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{acc_dash}.txt"
        try:
            time.sleep(0.15)
            resp = requests.get(url, headers=_HEADERS, timeout=60, stream=True)
            if resp.status_code != 200:
                return ""
            chunks: List[bytes] = []
            read = 0
            doc_b = (doc or "").encode()
            seen_doc = not doc_b  # no specific doc requested -> first block wins
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                chunks.append(chunk)
                read += len(chunk)
                if doc_b and not seen_doc and doc_b in chunk:
                    seen_doc = True
                # stop once a block END arrives after the target was seen —
                # everything past the primary document is exhibits/graphics
                if seen_doc and b"</DOCUMENT>" in chunk:
                    break
                if read >= self._TXT_FETCH_CAP:
                    break
            resp.close()
            body = b"".join(chunks).decode("utf-8", errors="replace")
        except Exception as exc:
            logger.debug("complete-submission fetch failed: %s", exc)
            return ""

        # Prefer the <DOCUMENT> whose <FILENAME> matches the requested doc;
        # fall back to the first block (SEQUENCE 1 = the primary document).
        best = ""
        for m in re.finditer(r"<DOCUMENT>(.*?)(?:</DOCUMENT>|\Z)", body, re.DOTALL):
            block = m.group(1)
            fname = re.search(r"<FILENAME>([^\r\n<]+)", block)
            text = re.search(r"<TEXT>(.*?)(?:</TEXT>|\Z)", block, re.DOTALL)
            if not text:
                continue
            payload = text.group(1).strip()
            if len(payload) <= 5000:
                continue
            if doc and fname and fname.group(1).strip() == doc:
                return payload  # exact match wins immediately
            if not best:
                best = payload
        return best

    def _find_primary_doc(self, cik: str, acc_nd: str, acc_dash: str) -> str:
        """Resolve a filing's primary document name. Structured index.json
        first (2026-07-03 — also serves the Atom backup path, whose metadata
        carries no primaryDocument); legacy -index.htm regex as fallback."""
        try:
            time.sleep(0.12)
            resp = requests.get(
                f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/index.json",
                headers=_HEADERS, timeout=15)
            if resp.status_code == 200:
                items = (resp.json().get("directory", {}) or {}).get("item", []) or []
                htms = [i.get("name", "") for i in items
                        if isinstance(i, dict) and str(i.get("name", "")).endswith((".htm", ".html"))]
                # primary docs are conventionally <ticker>-<date>.htm or the
                # largest non-exhibit .htm; exhibits carry ex/exhibit in the name
                non_exhibit = [n for n in htms
                               if not re.search(r"(^|[^a-z])ex(hibit)?[-_0-9]", n, re.IGNORECASE)
                               and "index" not in n.lower()]
                if non_exhibit:
                    for n in non_exhibit:  # prefer the conventional dated name
                        if re.search(r"-20\d{6}\.htm", n):
                            return n
                    return non_exhibit[0]
                if htms:
                    return htms[0]
        except Exception:
            pass
        idx_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{acc_dash}-index.htm"
        try:
            time.sleep(0.12)
            resp = requests.get(idx_url, headers=_HEADERS, timeout=15)
            text = resp.text
            for m in re.finditer(
                r'<a href="[^"]*/([^"]+\.htm)"[^>]*>(?:[^<]*(?:10-K|Annual)[^<]*)</a>',
                text, re.IGNORECASE
            ):
                fname = m.group(1)
                if fname and fname.endswith(".htm"):
                    return fname
            m2 = re.search(r'/Archives/edgar/data/\d+/\d+/([^"]+\.htm)', text, re.IGNORECASE)
            if m2:
                return m2.group(1)
        except Exception:
            pass
        return ""

    def get_filing_result(self, ticker: str, info: dict):
        from src.schemas import FilingResult
        cik = info["cik"]
        acc_dash = info.get("accession_number", "")
        acc_nd = info.get("accession_nodash", acc_dash.replace("-", ""))
        doc = info.get("primary_doc", "")
        if not doc:
            doc = self._find_primary_doc(cik, acc_nd, acc_dash)
        htm_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nd}/{doc}" if doc else ""
        html = self.fetch_html(cik, acc_nd, doc) if doc else ""
        mda = self.extract_mda(html) if html else ""
        ceo = self.extract_ceo_name(html) if html else ""
        name = self.get_company_name(ticker)
        return FilingResult(
            ticker=ticker,
            company_name=name,
            cik=cik,
            filing_type=info.get("form", "10-K"),
            period_of_report=info.get("period_of_report", ""),
            filing_date=info.get("filing_date", ""),
            accession_number=acc_dash,
            htm_url=htm_url,
            mda_text=mda,
            full_text=html[:80000] if html else "",
            ceo_name=ceo,
        )

    @staticmethod
    def _strip_html(html: str) -> str:
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'&nbsp;', ' ', text)
        text = re.sub(r'&amp;', '&', text)
        text = re.sub(r'&lt;', '<', text)
        text = re.sub(r'&gt;', '>', text)
        text = re.sub(r'&#\d+;', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def extract_mda(self, html: str) -> str:
        clean = self._strip_html(html)
        starts = [
            r"item\s+7[\.\s:]*management[\'s]*\s+discussion\s+and\s+analysis",
            r"management[\'s]*\s+discussion\s+and\s+analysis\s+of",
            r"results\s+of\s+operations",
        ]
        start_pos = -1
        for pat in starts:
            for m in re.finditer(pat, clean, re.IGNORECASE):
                pos = m.start()
                after = clean[pos:pos + 500].strip()
                # Skip TOC entries: "Item 7... 36 Item 7A" pattern with page numbers
                if re.match(r'[^.]{0,100}\d{1,3}\s+Item\s+7A', after[:120]):
                    continue
                if len(after) > 200:
                    start_pos = pos
                    break
            if start_pos != -1:
                break
        if start_pos == -1:
            return clean[:30000]
        ends = [
            r"item\s+7a[\.\s:]*quantitative",
            r"item\s+8[\.\s:]*financial\s+statements",
        ]
        end_pos = len(clean)
        for pat in ends:
            m = re.search(pat, clean[start_pos + 200:], re.IGNORECASE)
            if m:
                end_pos = start_pos + 200 + m.start()
                break
        return clean[start_pos:end_pos][:30000]

    def extract_ceo_name(self, html: str) -> str:
        """Extract CEO name from filing signature block."""
        clean = self._strip_html(html)
        patterns = [
            r'/s/\s+\S+\s+\S+\s+([A-Z][-a-zA-Z]+\s+[A-Z][-a-zA-Z]+(?:\s+[A-Z][-a-zA-Z]+)?)\s+(?:President|Chief)',
            r'/s/\s+[A-Z][-A-Z\s]+\n\s*([A-Z][-a-zA-Z]+\s+[A-Z][-a-zA-Z]+(?:\s+[A-Z][-a-zA-Z]+)?)',
            r'/s/\s+([A-Z][-A-Za-z]+\s+[A-Z][-A-Za-z]+(?:\s+[A-Z][-A-Za-z]+)?)',
        ]
        for pat in patterns:
            m = re.search(pat, clean)
            if m:
                name = m.group(1).strip()
                if name.isupper():
                    name = name.title()
                parts = name.split()
                cleaned = []
                for p in parts:
                    if "-" in p:
                        subparts = p.split("-")
                        p = "-".join(s[0].upper() + s[1:].lower() if s else "" for s in subparts)
                    cleaned.append(p)
                name = " ".join(cleaned)
                if 2 <= len(name.split()) <= 4:
                    bad_words = ["segment", "information", "note", "item", "part", "report", "pricewaterhouse", "deloitte", "touche", "llp", "ernst", "young", "kpmg", "pricewaterhousecoopers", "pwc", "grant", "thornton", "auditor", "accounting", "audit"]
                    if not any(bad in name.lower() for bad in bad_words):
                        return name
        return ""
