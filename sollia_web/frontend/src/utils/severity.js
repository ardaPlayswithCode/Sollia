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

// ── Part 3b (2026-07-03 bulletproofing): header severity/urgency counts for
// ALL 6 agents, not just the 4 scoring ones. The audit agents count their own
// native buckets:
//   Constraint — BINDING NOW (urgency immediate) / crosses ≤2yr (urgency
//                high) / partial (status partial_data) / unavailable
//                (data_unavailable | insufficient_data)  [backend vocab]
//   Moat       — direct threats / watch / no threat  [verdict vocab]
export const ALL_AGENTS = [...SCORING_AGENTS, 'constraint_auditor', 'moat_auditor']

const BUCKET_COLORS = {
  high: '#EF4444', medium: '#F59E0B', low: '#22C55E',
  info: '#94A3B8', gray: '#64748B',
}

// Ordered [{key, label, count, color}] for an agent's header. Callers decide
// which zero-count buckets to keep (headerText below implements the spec).
export function headerCounts(agentId, data) {
  if (agentId === 'constraint_auditor') {
    const cs = data?.constraints || []
    const n = (fn) => cs.filter(fn).length
    return [
      { key: 'binding', label: 'BINDING NOW', color: BUCKET_COLORS.high,
        count: n(c => c.urgency === 'immediate') },
      { key: 'near', label: 'crosses ≤2yr', color: BUCKET_COLORS.medium,
        count: n(c => c.urgency === 'high') },
      { key: 'partial', label: 'partial', color: BUCKET_COLORS.info,
        count: n(c => c.status === 'partial_data') },
      { key: 'unavailable', label: 'unavailable', color: BUCKET_COLORS.gray,
        count: n(c => ['data_unavailable', 'insufficient_data'].includes(c.status)) },
    ]
  }
  if (agentId === 'moat_auditor') {
    const ms = data?.moats_audited || []
    const n = (v) => ms.filter(m => m.verdict === v).length
    const threats = n('DIRECT THREAT')
    return [
      { key: 'threat', label: threats === 1 ? 'direct threat' : 'direct threats',
        count: threats, color: threats > 0 ? BUCKET_COLORS.high : BUCKET_COLORS.gray },
      { key: 'watch', label: 'watch', count: n('WATCH'), color: BUCKET_COLORS.medium },
      { key: 'clear', label: 'no threat', count: n('NO THREAT'), color: BUCKET_COLORS.low },
    ]
  }
  const counts = severityCounts(agentId, data)
  return ['high', 'medium', 'low'].map(s => (
    { key: s, label: s, count: counts[s] || 0, color: BUCKET_COLORS[s] }))
}

// Header line per the Part-3b spec: zero buckets dropped, EXCEPT Moat keeps
// "0 direct threats" (the zero IS the signal). Legal's empty state reads
// "No material changes"; other agents read "no findings".
export function headerText(agentId, data) {
  const buckets = headerCounts(agentId, data)
  const total = buckets.reduce((s, b) => s + b.count, 0)
  if (total === 0) {
    return agentId === 'legal_proceedings' ? 'No material changes' : 'no findings'
  }
  const keep = buckets.filter(b => b.count > 0 ||
    (agentId === 'moat_auditor' && b.key === 'threat'))
  return keep.map(b => `${b.count} ${b.label}`).join(' · ')
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
