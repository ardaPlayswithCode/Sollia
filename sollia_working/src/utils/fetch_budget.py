"""Per-run SEC fetch budget (Part 8d, 2026-07-01 security hardening).

Sollia's agents issue SEC requests from ~20 call sites across 6 modules, so the
abuse guard lives at the ONE choke point they all share: requests.Session.request.
install() wraps it with a counter scoped to *.sec.gov hosts; when a single
pipeline run exceeds SOLLIA_MAX_SEC_FETCHES_PER_RUN (default 50) live SEC
fetches, further SEC requests raise SECFetchBudgetExceeded.

Design notes:
  * CACHE HITS DON'T COUNT — agents consult their caches before fetching, so a
    warm run uses a handful of fetches. Only genuine network requests count.
  * Failure is graceful by construction: every agent fetch site already wraps
    its request in try/except and degrades (missing doc -> skipped/partial with
    a logged reason), so a budget breach degrades the run instead of crashing it.
  * The backend calls reset_fetch_budget() at the start of each pipeline run
    (same lifecycle as the LLM call budget in llm_config).
"""
from __future__ import annotations

import os
import time as _time
import threading

import requests

# 200 (was 50): Agent 1's rolling 5-year window fetches ~25-30 documents per
# fiscal year on a fully cold cache; this stays a hard abuse ceiling.
MAX_SEC_FETCHES_PER_RUN = int(os.environ.get("SOLLIA_MAX_SEC_FETCHES_PER_RUN", "200"))
_SEC_HOST_MARKERS = ("sec.gov",)  # www.sec.gov + data.sec.gov (+ any subdomain)

_lock = threading.Lock()
_count = 0
_installed = False
_orig_request = requests.sessions.Session.request


class SECFetchBudgetExceeded(RuntimeError):
    """A pipeline run exceeded its live SEC fetch budget (abuse guard)."""


def reset_fetch_budget() -> None:
    """Reset the per-run SEC fetch counter. Call at pipeline start."""
    global _count
    with _lock:
        _count = 0


def get_fetch_count() -> int:
    with _lock:
        return _count


# ── EDGAR-resilience layer (Part 5b, 2026-07-01; per-HOST 2026-07-03) ───────
# All sec.gov traffic flows through this wrapper, so EDGAR-outage handling
# lives here too:
#   * one automatic retry (2 s) on 502/503/504 — absorbs EDGAR's transient
#     maintenance blips without every call site needing retry logic;
#   * an outage breaker: after SEC_DOWN_THRESHOLD consecutive failures,
#     further requests fail FAST for SEC_DOWN_COOLDOWN seconds instead of
#     each burning a 15-30 s timeout.
# The breaker is PER-HOST (2026-07-03 bulletproofing): sec.gov runs three
# separate services — data.sec.gov (submissions API), www.sec.gov (Archives +
# Atom), efts.sec.gov (full-text search) — and the Part-1 backup chain falls
# from one host to the next. A global breaker defeated that chain: 5 failures
# on data.sec.gov+www.sec.gov opened it for efts.sec.gov too, fast-failing
# exactly the backup the outage needed.
SEC_DOWN_THRESHOLD = int(os.environ.get("SOLLIA_SEC_DOWN_THRESHOLD", "5"))
SEC_DOWN_COOLDOWN = float(os.environ.get("SOLLIA_SEC_DOWN_COOLDOWN", "60"))
_sec_consecutive_failures: dict = {}   # host -> consecutive failure count
_sec_down_until: dict = {}             # host -> fail-fast deadline


class SECUnavailable(RuntimeError):
    """An SEC host looks down (consecutive failures); failing fast until cooldown."""


def _sec_host(url) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(str(url)).netloc.lower() or "sec.gov"
    except Exception:
        return "sec.gov"


def _note_sec_result(host: str, ok: bool) -> None:
    with _lock:
        if ok:
            _sec_consecutive_failures[host] = 0
        else:
            n = _sec_consecutive_failures.get(host, 0) + 1
            _sec_consecutive_failures[host] = n
            if n >= SEC_DOWN_THRESHOLD:
                _sec_down_until[host] = _time.time() + SEC_DOWN_COOLDOWN


def _counting_request(self, method, url, *args, **kwargs):
    global _count
    try:
        is_sec = any(m in str(url) for m in _SEC_HOST_MARKERS)
    except Exception:
        is_sec = False
    if not is_sec:
        return _orig_request(self, method, url, *args, **kwargs)

    host = _sec_host(url)
    with _lock:
        if _time.time() < _sec_down_until.get(host, 0.0):
            raise SECUnavailable(
                "%s appears to be down (%d consecutive failures); failing fast "
                "for %.0fs. Other SEC hosts and cached tickers keep working; "
                "fresh fetches to this host resume after the cooldown." % (
                    host, _sec_consecutive_failures.get(host, 0),
                    _sec_down_until[host] - _time.time()))
        _count += 1
        if _count > MAX_SEC_FETCHES_PER_RUN:
            raise SECFetchBudgetExceeded(
                "Run exceeded its SEC fetch budget (%d live fetches). This is an "
                "abuse guard; raise SOLLIA_MAX_SEC_FETCHES_PER_RUN if the workload "
                "is legitimate (e.g. a fully cold cache)." % MAX_SEC_FETCHES_PER_RUN)

    try:
        resp = _orig_request(self, method, url, *args, **kwargs)
    except Exception:
        _note_sec_result(host, False)
        raise
    if resp.status_code in (502, 503, 504):
        _time.sleep(2.0)  # one retry absorbs EDGAR's transient 5xx blips
        try:
            resp = _orig_request(self, method, url, *args, **kwargs)
        except Exception:
            _note_sec_result(host, False)
            raise
    _note_sec_result(host, resp.status_code < 500)
    return resp


def install() -> None:
    """Install the counting wrapper (idempotent)."""
    global _installed
    with _lock:
        if _installed:
            return
        requests.sessions.Session.request = _counting_request
        _installed = True
