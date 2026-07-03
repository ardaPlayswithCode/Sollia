"""Cache size guard with LRU eviction (Part 6c, 2026-07-01 hardening).

`.cache/` + `.llm_cache/` grow without bound (filings HTML, embeddings, LLM
history, USGS PDFs). On a busy multi-tenant box that is a disk-exhaustion
vector: this janitor caps total cache size at SOLLIA_CACHE_MAX_GB (default 10)
and evicts least-recently-USED files first (st_atime, falling back to mtime)
until usage drops below the high-water mark.

Design:
  * Called from the backend at pipeline start (cheap: one os.walk, ~50k files
    < 1 s) — no daemon to babysit.
  * PROTECTED paths are never evicted: run-archive manifests + indexes
    (.cache/runs) are the product's memory, and index.jsonl files are append
    logs that other tooling expects to exist.
  * Eviction is best-effort and exception-safe: a failed unlink never breaks
    a pipeline run. Every cache in Sollia is a re-fetchable/re-computable
    artifact by contract, so eviction is always safe.
"""
from __future__ import annotations

import os
import threading

CACHE_MAX_BYTES = int(float(os.environ.get("SOLLIA_CACHE_MAX_GB", "10")) * (1024 ** 3))
# Evict down to 90% of the cap so we don't run the janitor every single run.
LOW_WATER_FRACTION = 0.90

_SOLLIA_WORKING = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_ROOTS = (
    os.path.join(_SOLLIA_WORKING, ".cache"),
    os.path.join(_SOLLIA_WORKING, ".llm_cache"),
)
_PROTECTED_MARKERS = (os.sep + "runs" + os.sep, "index.jsonl")

_lock = threading.Lock()


def _iter_files():
    for root in CACHE_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                yield path, st


def cache_size_bytes() -> int:
    return sum(st.st_size for _p, st in _iter_files())


def enforce_cache_limit(max_bytes: int = None) -> dict:
    """Evict LRU cache files until total size < low-water mark. Returns a
    summary dict (for logs / health checks). Never raises."""
    limit = max_bytes if max_bytes is not None else CACHE_MAX_BYTES
    summary = {"limit_bytes": limit, "before_bytes": 0, "evicted_files": 0,
               "evicted_bytes": 0, "after_bytes": 0}
    try:
        with _lock:
            entries = list(_iter_files())
            total = sum(st.st_size for _p, st in entries)
            summary["before_bytes"] = total
            if total <= limit:
                summary["after_bytes"] = total
                return summary
            target = int(limit * LOW_WATER_FRACTION)
            # LRU: least-recently-accessed first (atime may equal mtime on
            # noatime mounts — still a sane recency proxy).
            evictable = [(max(st.st_atime, st.st_mtime), st.st_size, p)
                         for p, st in entries
                         if not any(m in p for m in _PROTECTED_MARKERS)]
            evictable.sort()
            for _ts, size, path in evictable:
                if total <= target:
                    break
                try:
                    os.remove(path)
                    total -= size
                    summary["evicted_files"] += 1
                    summary["evicted_bytes"] += size
                except OSError:
                    continue
            summary["after_bytes"] = total
    except Exception:
        pass
    return summary
