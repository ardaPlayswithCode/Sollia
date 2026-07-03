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


# ── EDGAR-resilience layer (Part 5b, 2026-07-01) ────────────────────────────
# All sec.gov traffic flows through this wrapper, so EDGAR-outage handling
# lives here too:
#   * one automatic retry (2 s) on 502/503/504 — absorbs EDGAR's transient
#     maintenance blips without every call site needing retry logic;
#   * an outage breaker: after SEC_DOWN_THRESHOLD consecutive sec.gov
#     failures, further sec.gov requests fail FAST for SEC_DOWN_COOLDOWN
#     seconds instead of each burning a 15-30 s timeout. Agents already treat
#     fetch failures gracefully (skip/degrade), and their caches are checked
#     BEFORE any network request, so a warm ticker keeps working from stale
#     cache through a full EDGAR outage.
SEC_DOWN_THRESHOLD = int(os.environ.get("SOLLIA_SEC_DOWN_THRESHOLD", "5"))
SEC_DOWN_COOLDOWN = float(os.environ.get("SOLLIA_SEC_DOWN_COOLDOWN", "60"))
_sec_consecutive_failures = 0
_sec_down_until = 0.0


class SECUnavailable(RuntimeError):
    """EDGAR looks down (consecutive failures); failing fast until cooldown."""


def _note_sec_result(ok: bool) -> None:
    global _sec_consecutive_failures, _sec_down_until
    with _lock:
        if ok:
            _sec_consecutive_failures = 0
        else:
            _sec_consecutive_failures += 1
            if _sec_consecutive_failures >= SEC_DOWN_THRESHOLD:
                _sec_down_until = _time.time() + SEC_DOWN_COOLDOWN


def _counting_request(self, method, url, *args, **kwargs):
    global _count
    try:
        is_sec = any(m in str(url) for m in _SEC_HOST_MARKERS)
    except Exception:
        is_sec = False
    if not is_sec:
        return _orig_request(self, method, url, *args, **kwargs)

    with _lock:
        if _time.time() < _sec_down_until:
            raise SECUnavailable(
                "SEC EDGAR appears to be down (%d consecutive failures); serving "
                "from cache only for %.0fs. Cached tickers keep working; fresh "
                "fetches resume after the cooldown." % (
                    _sec_consecutive_failures, _sec_down_until - _time.time()))
        _count += 1
        if _count > MAX_SEC_FETCHES_PER_RUN:
            raise SECFetchBudgetExceeded(
                "Run exceeded its SEC fetch budget (%d live fetches). This is an "
                "abuse guard; raise SOLLIA_MAX_SEC_FETCHES_PER_RUN if the workload "
                "is legitimate (e.g. a fully cold cache)." % MAX_SEC_FETCHES_PER_RUN)

    try:
        resp = _orig_request(self, method, url, *args, **kwargs)
    except Exception:
        _note_sec_result(False)
        raise
    if resp.status_code in (502, 503, 504):
        _time.sleep(2.0)  # one retry absorbs EDGAR's transient 5xx blips
        try:
            resp = _orig_request(self, method, url, *args, **kwargs)
        except Exception:
            _note_sec_result(False)
            raise
    _note_sec_result(resp.status_code < 500)
    return resp


def install() -> None:
    """Install the counting wrapper (idempotent)."""
    global _installed
    with _lock:
        if _installed:
            return
        requests.sessions.Session.request = _counting_request
        _installed = True
