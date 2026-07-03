"""Startup dependency report (Part 5c, 2026-07-01 hardening).

check_dependencies() probes every OPTIONAL dependency and reports which
features are fully available and which run degraded. It never raises and
never imports anything heavier than necessary — safe to call at backend
startup and from tools/health_check.py.

Degradation contract (verified in the 2026-07-01 bulletproofing pass):
  * spaCy missing            -> Agent 1 uses the regex sentence tokenizer.
  * transformers/torch missing -> Agent 1 runs only from the embedding cache;
                                  uncached chunks abort THAT run with install
                                  guidance (other agents unaffected).
  * pdfplumber missing       -> Agent 5's USGS supply/demand extraction is
                                unavailable; the agent still runs (FRED +
                                LLM sector classification work).
  * lxml missing             -> BeautifulSoup falls back to html.parser
                                (slower, same output).
"""
from __future__ import annotations

import importlib.util
from typing import Dict


def _probe(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def check_dependencies() -> Dict[str, dict]:
    """Return {feature: {available: bool, detail: str}} for every optional
    dependency, plus the hard-required ones (so a broken venv is visible)."""
    report: Dict[str, dict] = {}

    for mod in ("requests", "bs4", "numpy", "pydantic"):
        report[mod] = {
            "available": _probe(mod), "required": True,
            "detail": "core dependency" if _probe(mod) else
                      "MISSING CORE DEPENDENCY — pipeline cannot run; pip install -r requirements.txt"}

    spacy_ok = _probe("spacy")
    model_ok = False
    if spacy_ok:
        try:
            import spacy as _sp
            model_ok = _sp.util.is_package("en_core_web_sm")
        except Exception:
            model_ok = False
    report["spacy"] = {
        "available": spacy_ok and model_ok, "required": False,
        "detail": ("sentence tokenization: spaCy + en_core_web_sm" if (spacy_ok and model_ok)
                   else "DEGRADED: regex sentence tokenizer (Agent 1). "
                        "pip install spacy && python -m spacy download en_core_web_sm")}

    torch_ok = _probe("torch") and _probe("transformers")
    report["finbert"] = {
        "available": torch_ok, "required": False,
        "detail": ("FinBERT embeddings available (Agent 1)" if torch_ok
                   else "DEGRADED: Agent 1 runs only from the embedding cache; "
                        "uncached chunks error with guidance. pip install transformers torch")}

    pdf_ok = _probe("pdfplumber")
    report["pdfplumber"] = {
        "available": pdf_ok, "required": False,
        "detail": ("USGS PDF parsing available (Agent 5)" if pdf_ok
                   else "DEGRADED: Agent 5 USGS supply/demand -> data_unavailable. "
                        "pip install pdfplumber")}

    lxml_ok = _probe("lxml")
    report["lxml"] = {
        "available": lxml_ok, "required": False,
        "detail": "fast HTML parsing" if lxml_ok else
                  "DEGRADED: bs4 falls back to html.parser (slower, same output)"}

    return report


def format_report(report: Dict[str, dict] = None) -> str:
    report = report or check_dependencies()
    lines = []
    for name, info in report.items():
        mark = "OK " if info["available"] else ("!! " if info.get("required") else "~~ ")
        lines.append(f"  [{mark}] {name:12s} {info['detail']}")
    return "\n".join(lines)
