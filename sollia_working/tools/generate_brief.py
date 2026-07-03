#!/usr/bin/env python3.11
"""Sollia meeting brief generator (production polish, Part 6).

Turns a clean JSON export (the "Download JSON" payload) into a 1-2 page
executive brief for a family-office analyst: signal first, attention items
with citations, what's stable, per-agent detail, trust footer. Deterministic
template prose — zero LLM calls, zero cost, instant.

Usage:
  python3.11 tools/generate_brief.py export.json            # brief -> stdout
  python3.11 tools/generate_brief.py export.json -o out.txt

The frontend mirrors this exact logic client-side (src/utils/brief.js) for the
"Download Brief" button; keep the two in sync when changing section rules.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

RULE = "━" * 46

AGENT_ORDER = ["corporate_discrepancy", "promise_audit", "risk_factors",
               "constraint_auditor", "moat_auditor", "legal_proceedings"]

AGENT_HEADLINES = {
    "corporate_discrepancy": "Disclosure Consistency",
    "promise_audit": "Management Credibility",
    "risk_factors": "Risk Factors",
    "constraint_auditor": "Physical Constraints",
    "moat_auditor": "Competitive Position",
    "legal_proceedings": "Legal Proceedings",
}


def _fmt_signal(score, signal):
    if score is None:
        return signal or "AUDIT"
    return "%s/100 (%s)" % (score, signal or "—")


def _first_url(f):
    for c in f.get("citations", []):
        if c.get("url"):
            return c["url"]
    return ""


def _cite_label(c):
    doc = c.get("document", "") or "source"
    pg = c.get("page_number")
    return "%s, p.%s" % (doc, pg) if pg else doc


def _is_adverse(f: dict) -> bool:
    """Attention items must be ADVERSE. A kept promise or a removed risk can be
    severity=high (high MATERIALITY) — surfacing it under 'needs attention'
    reads as a judgment error to an analyst."""
    if f.get("type") in ("promise_kept", "kept", "no_change"):
        return False
    if f.get("change_direction") == "positive":
        return False
    return True


def collect_attention(agents: dict) -> list:
    """High-severity ADVERSE findings across agents, most urgent first, cap 5."""
    items = []
    for aid in AGENT_ORDER:
        block = agents.get(aid) or {}
        for f in block.get("top_findings", []):
            sev = f.get("severity", "info")
            if sev not in ("high", "medium") or not _is_adverse(f):
                continue
            # constraint findings: urgency beats severity labels
            urgency = f.get("urgency")
            rank = {"high": 0, "medium": 1}.get(sev, 2)
            if urgency == "immediate":
                rank = -1
            items.append((rank, aid, f))
    items.sort(key=lambda x: x[0])
    return items[:5]


def collect_stable(agents: dict) -> list:
    """Positive/no-change signals worth telling the analyst about."""
    out = []
    lp = agents.get("legal_proceedings") or {}
    lp_findings = lp.get("top_findings", [])
    if lp_findings and all(f.get("type") == "no_change" for f in lp_findings):
        out.append("Legal proceedings: no material changes")
    moats = agents.get("moat_auditor") or {}
    safe_moats = [f.get("moat_type") for f in moats.get("top_findings", [])
                  if f.get("verdict") == "NO THREAT" and f.get("moat_type")]
    for m in safe_moats[:3]:
        out.append("%s moat: no credible competitor challenge" % m)
    cons = agents.get("constraint_auditor") or {}
    unconstrained = [f for f in cons.get("top_findings", [])
                     if f.get("urgency") in ("none", "low")]
    for f in unconstrained[:2]:
        mat = (f.get("summary") or "").split(" — ")[0]
        if mat:
            out.append("%s: no near-term supply constraint" % mat)
    rf = agents.get("risk_factors") or {}
    positives = [f for f in rf.get("top_findings", [])
                 if f.get("change_direction") == "positive"]
    for f in positives[:2]:
        out.append("Risk factors: %s" % (f.get("summary") or "a risk was removed/softened"))
    pa = agents.get("promise_audit") or {}
    kept = [f for f in pa.get("top_findings", []) if f.get("type") == "kept"]
    if kept:
        out.append("%d tracked management promise%s verified KEPT" %
                   (len(kept), "s" if len(kept) != 1 else ""))
    return out


def agent_detail(aid: str, block: dict) -> list:
    findings = block.get("top_findings", [])
    n = len(findings)
    lines = ["%s: %s" % (AGENT_HEADLINES.get(aid, block.get("name", aid)),
                         _fmt_signal(block.get("score"), block.get("signal")))]
    if aid == "corporate_discrepancy":
        hi = sum(1 for f in findings if f.get("severity") == "high")
        lines.append("%d discrepanc%s flagged%s." % (
            n, "y" if n == 1 else "ies",
            (", %d high-severity" % hi) if hi else ""))
    elif aid == "promise_audit":
        kinds = {}
        for f in findings:
            k = f.get("type", "?").replace("promise_", "")
            kinds[k] = kinds.get(k, 0) + 1
        order = ["kept", "partial", "broken", "pending", "unresolved"]
        parts = ["%d %s" % (kinds[k], k) for k in order if k in kinds]
        parts += ["%d %s" % (v, k) for k, v in sorted(kinds.items()) if k not in order]
        lines.append(("%d promises tracked: %s." % (n, ", ".join(parts))) if parts
                     else "No promises surfaced.")
    elif aid == "risk_factors":
        esc = sum(1 for f in findings if "escalat" in f.get("type", ""))
        new = sum(1 for f in findings if f.get("type") == "risk_new")
        pos = sum(1 for f in findings if f.get("change_direction") == "positive")
        lines.append("%d change%s — %d escalation%s, %d new risk%s, %d positive." % (
            n, "" if n == 1 else "s", esc, "" if esc == 1 else "s",
            new, "" if new == 1 else "s", pos))
    elif aid == "constraint_auditor":
        binding = [f for f in findings if f.get("urgency") == "immediate"]
        near = [f for f in findings if f.get("urgency") == "high"]
        lines.append("%d material%s evaluated: %d binding now, %d crossing within 2 years." % (
            n, "" if n == 1 else "s", len(binding), len(near)))
    elif aid == "moat_auditor":
        for f in findings:
            if f.get("moat_type"):
                lines.append("  %s: %s" % (f["moat_type"], f.get("verdict", "?")))
    elif aid == "legal_proceedings":
        changed = [f for f in findings if f.get("type") != "no_change"]
        lines.append("%d proceeding update%s." % (len(changed), "" if len(changed) == 1 else "s")
                     if changed else "No material changes across compared filings.")
    return lines


def trust_footer(agents: dict) -> str:
    total = verified = 0
    for block in agents.values():
        for f in (block or {}).get("top_findings", []):
            for c in f.get("citations", []):
                total += 1
                verified += 1 if c.get("verified") else 0
    if not total:
        return ("All findings are generated from primary sources (SEC EDGAR, USGS, FRED). "
                "Full audit trail in the JSON export.")
    rate = 100.0 * verified / total
    return ("All findings link to original SEC filings, USGS data, and earnings "
            "transcripts. %d/%d citations (%.1f%%) verbatim-verified against source "
            "documents. Full audit trail in the JSON export." % (verified, total, rate))


def generate_brief(export: dict) -> str:
    ticker = export.get("ticker", "?")
    agents = export.get("agents", {})
    ts = export.get("timestamp", "")
    try:
        pretty_date = datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%B %-d, %Y")
    except Exception:
        pretty_date = ts[:10]
    window = export.get("analysis_window")
    if isinstance(window, dict):
        years = window.get("years") or []
        window_txt = "FY%s–FY%s" % (min(years), max(years)) if years else "last 5 fiscal years"
    else:
        window_txt = str(window or "last 5 fiscal years")

    L = []
    L.append(RULE)
    L.append("SOLLIA BRIEF — %s (%s)" % (ticker, pretty_date))
    L.append("Prepared for Investment Committee Review")
    L.append("Analysis window: %s | %d agents" % (window_txt, len(agents)))
    L.append(RULE)
    L.append("")

    # ── executive summary ──
    L.append("EXECUTIVE SUMMARY")
    summary_bits = []
    attention = collect_attention(agents)
    if attention:
        heads = []
        for _, aid, f in attention[:3]:
            h = (f.get("summary") or f.get("type", "")).rstrip(".").strip()
            # exec summary wants the headline, not the whole finding
            h = h.split(". ")[0]
            if len(h) > 90:
                h = h[:87] + "..."
            heads.append(h)
        summary_bits.append("Attention items: " + "; ".join(h for h in heads if h) + ".")
    for aid in ("corporate_discrepancy", "promise_audit", "risk_factors"):
        b = agents.get(aid)
        if b and b.get("score") is not None:
            summary_bits.append("%s %s." % (AGENT_HEADLINES[aid], _fmt_signal(b["score"], b.get("signal"))))
    summary_bits.append("Overall signal: %s (%s/100)." % (export.get("signal", "HOLD"),
                                                          export.get("score", 50)))
    L.append(" ".join(summary_bits))
    L.append("")

    # ── attention ──
    L.append("⚠️  WHAT NEEDS ATTENTION (%d item%s)" % (len(attention), "" if len(attention) == 1 else "s"))
    L.append("")
    if not attention:
        L.append("Nothing rose to high severity in this run.")
    for i, (_, aid, f) in enumerate(attention, 1):
        badge = f.get("urgency_badge") or f.get("severity", "").upper()
        L.append("%d. [%s] %s — %s" % (i, AGENT_HEADLINES.get(aid, aid), badge,
                                       (f.get("summary") or f.get("type", "")).strip()))
        if f.get("severity_reason"):
            L.append("   → %s" % f["severity_reason"])
        for c in f.get("citations", [])[:2]:
            if c.get("excerpt"):
                exc = c["excerpt"].strip()
                if len(exc) > 160:
                    exc = exc[:157] + "..."
                L.append("   \"%s\"" % exc)
            if c.get("url"):
                L.append("   [View in %s ↗] %s" % (_cite_label(c), c["url"]))
        L.append("")

    # ── stable ──
    stable = collect_stable(agents)
    L.append("✅ WHAT'S STABLE")
    L.append("")
    if stable:
        for s in stable:
            L.append("• %s" % s)
    else:
        L.append("• No unchanged areas to report (see agent detail).")
    L.append("")

    # ── per-agent detail ──
    L.append("📊 DETAIL BY AGENT")
    L.append("")
    for aid in AGENT_ORDER:
        block = agents.get(aid)
        if not block:
            continue
        for ln in agent_detail(aid, block):
            L.append(ln)
        L.append("")

    L.append(RULE)
    L.append(trust_footer(agents))
    L.append(RULE)
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("export_json", help="clean JSON export from a pipeline run")
    ap.add_argument("-o", "--out", default="", help="output path (default stdout)")
    args = ap.parse_args()
    export = json.load(open(args.export_json))
    brief = generate_brief(export)
    if args.out:
        with open(args.out, "w") as f:
            f.write(brief + "\n")
        print("brief -> %s" % args.out)
    else:
        print(brief)
    return 0


if __name__ == "__main__":
    sys.exit(main())
