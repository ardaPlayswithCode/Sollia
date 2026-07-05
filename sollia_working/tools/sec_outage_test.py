"""Part 1d — EDGAR outage simulation against the REAL provider code.

Blocks hosts at the HTTPAdapter.send level (below Session.request), so the
fetch-budget wrapper AND its per-host outage breaker run exactly as in
production. No /etc/hosts changes needed.

Scenarios:
  1. LIVE baseline      (DASH, uncached)  -> edgar-live
  2. data.sec.gov DOWN  (ABNB, uncached)  -> atom-backup
  3. data+www DOWN      (UBER, uncached)  -> efts-backup
  4. ALL sec hosts DOWN (NVDA, cache aged)-> stale-cache
  5. ALL sec hosts DOWN (PLTR, uncached)  -> unavailable ([])
  6. LIVE .txt doc fallback: wrong primary-doc name -> complete-submission
"""
import os, sys, time

sys.path.insert(0, "/Users/ardaturgut/Desktop/Desktop - Arda’s Mac Studio/Sollia/sollia_working")

import requests
from requests.adapters import HTTPAdapter

from src.utils import fetch_budget
fetch_budget.install()
fetch_budget.reset_fetch_budget()

from src.providers.filings import sec_edgar
from src.providers.filings.sec_edgar import SECEdgarProvider, LAST_METADATA_SOURCE

BLOCKED = set()
_orig_send = HTTPAdapter.send

def _blocking_send(self, request, *a, **kw):
    from urllib.parse import urlparse
    host = urlparse(request.url).netloc.lower()
    if any(b in host for b in BLOCKED):
        raise requests.ConnectionError(f"simulated outage: {host} blocked")
    return _orig_send(self, request, *a, **kw)

HTTPAdapter.send = _blocking_send

passed = failed = 0
def check(name, cond, detail=""):
    global passed, failed
    passed += bool(cond); failed += (not cond)
    print(f"{'PASS' if cond else 'FAIL'}: {name} {detail}")

def fresh_provider():
    return SECEdgarProvider()

def reset_breaker():
    fetch_budget._sec_consecutive_failures.clear()
    fetch_budget._sec_down_until.clear()
    fetch_budget.reset_fetch_budget()

# 1. LIVE baseline — uncached ticker
BLOCKED.clear(); reset_breaker()
f = fresh_provider().get_all_filings("DASH")
check("1 live baseline (DASH)",
      len(f) > 0 and LAST_METADATA_SOURCE.get("DASH") in ("edgar-live", "disk-cache"),
      f"-> {len(f)} filings via {LAST_METADATA_SOURCE.get('DASH')}")

# 2. data.sec.gov down — Atom serves
BLOCKED.clear(); BLOCKED.add("data.sec.gov"); reset_breaker()
f = fresh_provider().get_all_filings("ABNB")
forms = {x["form"] for x in f}
check("2 data.sec.gov down (ABNB) -> atom-backup",
      len(f) > 0 and LAST_METADATA_SOURCE.get("ABNB") == "atom-backup",
      f"-> {len(f)} filings via {LAST_METADATA_SOURCE.get('ABNB')}, forms={sorted(forms)[:6]}")

# 3. data + www down — EFTS serves
BLOCKED.clear(); BLOCKED.update(("data.sec.gov", "www.sec.gov")); reset_breaker()
f = fresh_provider().get_all_filings("UBER")
check("3 data+www down (UBER) -> efts-backup",
      len(f) > 0 and LAST_METADATA_SOURCE.get("UBER") == "efts-backup",
      f"-> {len(f)} filings via {LAST_METADATA_SOURCE.get('UBER')}")
has_docs = sum(1 for x in f if x.get("primary_doc"))
check("3b efts carries primary docs", has_docs > 0, f"({has_docs}/{len(f)})")

# 4. ALL sec hosts down — stale cache serves NVDA (age the cache first)
BLOCKED.clear(); BLOCKED.add("sec.gov"); reset_breaker()  # matches all *.sec.gov
nvda_cache = os.path.join(sec_edgar._FILINGS_CACHE_DIR, "NVDA_filings.json")
old = time.time() - 3 * 86400
os.utime(nvda_cache, (old, old))
f = fresh_provider().get_all_filings("NVDA")
check("4 full outage (NVDA) -> stale-cache",
      len(f) > 0 and LAST_METADATA_SOURCE.get("NVDA") == "stale-cache",
      f"-> {len(f)} filings via {LAST_METADATA_SOURCE.get('NVDA')}")

# 5. ALL down + never-cached ticker — graceful empty
reset_breaker()
pltr_cache = os.path.join(sec_edgar._FILINGS_CACHE_DIR, "PLTR_filings.json")
if os.path.exists(pltr_cache):
    os.remove(pltr_cache)
f = fresh_provider().get_all_filings("PLTR")
check("5 full outage, cold ticker (PLTR) -> graceful []",
      f == [] and LAST_METADATA_SOURCE.get("PLTR") == "unavailable",
      f"-> {LAST_METADATA_SOURCE.get('PLTR')}")

# 6. document fallback: wrong primary doc name -> complete-submission .txt
BLOCKED.clear(); reset_breaker()
p = fresh_provider()
html = p.fetch_html("1045810", "000104581023000017", "wrong-name.htm", "NVDA")
ok6 = len(html) > 100_000 and "NVIDIA" in html[:200_000]
check("6 .txt complete-submission fallback", ok6,
      f"-> {len(html)//1024}KB recovered")
if ok6:
    mda = p.extract_mda(html)
    check("6b MD&A extractable from .txt payload", len(mda) > 5_000, f"({len(mda)} chars)")

HTTPAdapter.send = _orig_send
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
