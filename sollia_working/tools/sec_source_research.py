"""Part 1b — empirical research of SEC EDGAR backup sources.

Tests every candidate source for filing METADATA and DOCUMENT content across
3 tickers (NVDA large, TSLA large, FUBO small-cap) x 3 eras (recent, 3y, 10y).
Measures success, latency, format, and coverage. Read-only, ~40 small
requests total, throttled to SEC fair-access guidelines (<10 rps).
"""
import json, re, sys, time
import requests

UA = {"User-Agent": "Sollia contact@sollia.app", "Accept-Encoding": "gzip, deflate"}
CIKS = {"NVDA": "0001045810", "TSLA": "0001318605", "FUBO": "0001484769"}
RESULTS = []

def probe(name, url, timeout=15, headers=UA, want_json=False):
    t0 = time.time()
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        ms = (time.time() - t0) * 1000
        body = r.text or ""
        parsed = None
        if want_json and r.status_code == 200:
            try:
                parsed = r.json()
            except Exception:
                parsed = None
        rec = {"source": name, "url": url[:120], "status": r.status_code,
               "ms": round(ms), "bytes": len(body), "json_ok": parsed is not None}
        RESULTS.append(rec)
        return r, parsed
    except Exception as e:
        RESULTS.append({"source": name, "url": url[:120],
                        "status": f"EXC {type(e).__name__}", "ms": round((time.time()-t0)*1000)})
        return None, None

print("=" * 70)
print("A. AWS 'SEC Public Dataset' (s3://sec-datasets/) — existence check")
print("=" * 70)
for u in ("https://sec-datasets.s3.amazonaws.com/?list-type=2&max-keys=5",
          "https://sec-datasets.s3.us-east-1.amazonaws.com/",
          "https://registry.opendata.aws/sec-edgar/"):
    r, _ = probe("aws-s3", u)
    if r is not None:
        print(f"  {u[:70]} -> {r.status_code} ({r.text[:100].strip()!r})")
    time.sleep(0.3)

print()
print("=" * 70)
print("B. SEC bulk archives (www.sec.gov, daily/full index + bulk submissions)")
print("=" * 70)
# daily index (metadata for ALL filers on a date), full index (quarterly)
r, _ = probe("daily-index", "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/")
print(f"  daily-index dir listing: {getattr(r, 'status_code', 'EXC')}")
time.sleep(0.3)
r, _ = probe("full-index", "https://www.sec.gov/Archives/edgar/full-index/2016/QTR1/master.idx")
if r is not None and r.status_code == 200:
    lines = [l for l in r.text.splitlines() if "|10-K|" in l][:2]
    print(f"  full-index 2016Q1 master.idx: 200, {len(r.text)//1024}KB, sample: {lines[:1]}")
time.sleep(0.3)
# bulk submissions.zip — HEAD only (it is >1GB)
try:
    t0 = time.time()
    h = requests.head("https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip",
                      headers=UA, timeout=15, allow_redirects=True)
    print(f"  bulkdata/submissions.zip HEAD: {h.status_code}, "
          f"size={int(h.headers.get('Content-Length', 0))/1e9:.2f}GB, {(time.time()-t0)*1000:.0f}ms")
    RESULTS.append({"source": "bulk-submissions", "status": h.status_code,
                    "bytes": int(h.headers.get("Content-Length", 0))})
except Exception as e:
    print(f"  bulkdata/submissions.zip HEAD: EXC {e}")

print()
print("=" * 70)
print("C. efts.sec.gov full-text search API — metadata per ticker/era")
print("=" * 70)
for tk, cik in CIKS.items():
    for era, (frm, to) in {"recent": ("2025-07-01", "2026-07-03"),
                           "3y": ("2023-01-01", "2023-12-31"),
                           "10y": ("2016-01-01", "2016-12-31")}.items():
        u = (f"https://efts.sec.gov/LATEST/search-index?q=%22{tk}%22&forms=10-K,10-Q"
             f"&dateRange=custom&startdt={frm}&enddt={to}")
        r, js = probe(f"efts-{tk}-{era}", u, want_json=True)
        n = 0
        if js:
            n = js.get("hits", {}).get("total", {}).get("value", 0)
        # the real filer-scoped query form:
        u2 = (f"https://efts.sec.gov/LATEST/search-index?q=*&ciks={cik}"
              f"&forms=10-K,10-Q&startdt={frm}&enddt={to}")
        r2, js2 = probe(f"efts-cik-{tk}-{era}", u2, want_json=True)
        n2 = 0
        hits = []
        if js2:
            n2 = js2.get("hits", {}).get("total", {}).get("value", 0)
            for h in js2.get("hits", {}).get("hits", [])[:2]:
                src = h.get("_source", {})
                hits.append((src.get("file_type"), src.get("file_date"),
                             (h.get("_id") or "")[:30]))
        print(f"  {tk} {era}: q-search={getattr(r,'status_code',None)}({n} hits) "
              f"cik-search={getattr(r2,'status_code',None)}({n2} hits) {hits}")
        time.sleep(0.4)

print()
print("=" * 70)
print("D. RSS/Atom browse-edgar (www.sec.gov) — metadata per ticker")
print("=" * 70)
for tk, cik in CIKS.items():
    u = (f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
         f"&type=10-K&dateb=&owner=include&count=40&output=atom")
    r, _ = probe(f"atom-{tk}", u)
    if r is not None and r.status_code == 200:
        accs = re.findall(r"accession-nu?mber>([\d-]+)<", r.text)
        dates = re.findall(r"filing-date>([\d-]+)<", r.text)
        print(f"  {tk}: 200, {len(accs)} accessions, date span "
              f"{dates[-1] if dates else '?'} .. {dates[0] if dates else '?'}")
    else:
        print(f"  {tk}: {getattr(r, 'status_code', 'EXC')}")
    time.sleep(0.4)

print()
print("=" * 70)
print("E. Document fetch: primary .htm vs complete-submission .txt (3 eras, NVDA)")
print("=" * 70)
# recent 10-K, ~3y, ~10y accessions for NVDA (from known history)
DOCS = [
    ("recent 10-K FY2026", "1045810", "000104581026000029", "nvda-20260125.htm"),
    ("3y 10-K FY2023", "1045810", "000104581023000017", "nvda-20230129.htm"),
    ("10y 10-K FY2016", "1045810", "000104581016000205", "nvda-2016x10k.htm"),
]
for label, cik, acc_nd, doc in DOCS:
    u1 = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nd}/{doc}"
    r1, _ = probe(f"doc-htm-{label}", u1, timeout=30)
    acc_dash = f"{acc_nd[:10]}-{acc_nd[10:12]}-{acc_nd[12:]}"
    u2 = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nd}/{acc_dash}.txt"
    t0 = time.time()
    try:  # HEAD for the giant .txt
        h2 = requests.head(u2, headers=UA, timeout=30)
        txt_info = f"{h2.status_code}, {int(h2.headers.get('Content-Length', 0))/1e6:.1f}MB, {(time.time()-t0)*1000:.0f}ms"
    except Exception as e:
        txt_info = f"EXC {e}"
    print(f"  {label}: .htm={getattr(r1,'status_code',None)} "
          f"({RESULTS[-1].get('bytes',0)//1024}KB {RESULTS[-1].get('ms')}ms) | .txt HEAD={txt_info}")
    time.sleep(0.4)

print()
print(json.dumps(RESULTS, indent=1)[:1500])
