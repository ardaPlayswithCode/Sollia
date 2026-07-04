// Severity distributions for the per-agent header boxes (Part 4c).
// MIRRORS backend/runs.py so the UI counts always agree with the JSON export:
//   _sev_label()      -> sevLabel()
//   _RISK_SEVERITY    -> RISK_SEVERITY
// If runs.py's mapping changes, change this file with it.

export const SCORING_AGENTS = [
  'corporate_discrepancy', 'promise_audit', 'legal_proceedings', 'risk_factors',
]

// runs.py _sev_label: numeric >=7 high / >=4 medium / else low; strings pass
// through; anything else (bool, null, junk) reads medium.
export function sevLabel(sev) {
  if (typeof sev === 'boolean') return 'medium'
  if (typeof sev === 'number') return sev >= 7 ? 'high' : sev >= 4 ? 'medium' : 'low'
  const s = String(sev ?? '').toLowerCase()
  return ['high', 'medium', 'low'].includes(s) ? s : 'medium'
}

// runs.py _RISK_SEVERITY
export const RISK_SEVERITY = {
  ESCALATED: 'high', NEW: 'high', REMOVED: 'medium',
  'DE-ESCALATED': 'low', REWORDED: 'low',
}

export function riskSeverity(category) {
  return RISK_SEVERITY[String(category || '').toUpperCase()] || 'medium'
}

// Rank for sorting: higher = more severe.
export const SEV_RANK = { high: 2, medium: 1, low: 0 }

// One {high, medium, low} distribution per scoring agent, computed from the
// same rich-result fields each card renders (so counts match what's on screen).
export function severityCounts(agentId, data) {
  const counts = { high: 0, medium: 0, low: 0 }
  if (!data) return counts
  const bump = (label) => { counts[label] = (counts[label] || 0) + 1 }
  switch (agentId) {
    case 'corporate_discrepancy':
      for (const d of data.top_discrepancies || []) bump(sevLabel(d.severity))
      break
    case 'promise_audit':
      for (const p of data.top_promises || []) bump(sevLabel(p.severity))
      break
    case 'legal_proceedings':
      for (const f of data.findings || []) {
        for (const p of f.proceedings || []) bump(sevLabel(p.priority))
      }
      break
    case 'risk_factors':
      for (const c of data.top_changes || []) bump(riskSeverity(c.category))
      for (const c of data.ten_q_findings?.changes || []) bump(riskSeverity(c.category))
      break
    default:
      break
  }
  return counts
}

// Part 4d finding sort: severity first; the tie-break preserves the backend's
// ordering, which is already importance-ranked. getSeverity maps an item to
// 'high'|'medium'|'low'; getRecency (optional) returns a sortable value where
// LARGER = more recent.
export function sortFindings(items, getSeverity, getRecency) {
  return items
    .map((item, i) => ({ item, i }))
    .sort((a, b) => {
      const sev = (SEV_RANK[getSeverity(b.item)] ?? 1) - (SEV_RANK[getSeverity(a.item)] ?? 1)
      if (sev !== 0) return sev
      if (getRecency) {
        const rec = (getRecency(b.item) || 0) - (getRecency(a.item) || 0)
        if (rec !== 0) return rec
      }
      return a.i - b.i // stable: keep backend importance order
    })
    .map(({ item }) => item)
}
