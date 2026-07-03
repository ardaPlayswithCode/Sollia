// Sollia meeting brief — client-side generator (production polish Part 6b).
// Mirrors tools/generate_brief.py exactly; zero LLM calls, instant download.
// Keep section rules in sync with the Python tool when editing.

const RULE = '━'.repeat(46)

const AGENT_ORDER = ['corporate_discrepancy', 'promise_audit', 'risk_factors',
  'constraint_auditor', 'moat_auditor', 'legal_proceedings']

const AGENT_HEADLINES = {
  corporate_discrepancy: 'Disclosure Consistency',
  promise_audit: 'Management Credibility',
  risk_factors: 'Risk Factors',
  constraint_auditor: 'Physical Constraints',
  moat_auditor: 'Competitive Position',
  legal_proceedings: 'Legal Proceedings',
}

const fmtSignal = (score, signal) =>
  score == null ? (signal || 'AUDIT') : `${score}/100 (${signal || '—'})`

const citeLabel = (c) => {
  const doc = c.document || 'source'
  return c.page_number ? `${doc}, p.${c.page_number}` : doc
}

// Attention items must be ADVERSE: a kept promise or removed risk can be
// severity=high (materiality), but listing it under "needs attention" reads
// as a judgment error to an analyst.
function isAdverse(f) {
  if (['promise_kept', 'kept', 'no_change'].includes(f.type)) return false
  if (f.change_direction === 'positive') return false
  return true
}

function collectAttention(agents) {
  const items = []
  for (const aid of AGENT_ORDER) {
    const block = agents[aid] || {}
    for (const f of block.top_findings || []) {
      const sev = f.severity || 'info'
      if ((sev !== 'high' && sev !== 'medium') || !isAdverse(f)) continue
      let rank = sev === 'high' ? 0 : 1
      if (f.urgency === 'immediate') rank = -1
      items.push([rank, aid, f])
    }
  }
  items.sort((a, b) => a[0] - b[0])
  return items.slice(0, 5)
}

function collectStable(agents) {
  const out = []
  const lp = agents.legal_proceedings || {}
  const lpF = lp.top_findings || []
  if (lpF.length && lpF.every(f => f.type === 'no_change')) {
    out.push('Legal proceedings: no material changes')
  }
  const moats = agents.moat_auditor || {}
  const safe = (moats.top_findings || []).filter(f => f.verdict === 'NO THREAT' && f.moat_type)
  for (const f of safe.slice(0, 3)) out.push(`${f.moat_type} moat: no credible competitor challenge`)
  const cons = agents.constraint_auditor || {}
  const uncon = (cons.top_findings || []).filter(f => f.urgency === 'none' || f.urgency === 'low')
  for (const f of uncon.slice(0, 2)) {
    const mat = (f.summary || '').split(' — ')[0]
    if (mat) out.push(`${mat}: no near-term supply constraint`)
  }
  const rf = agents.risk_factors || {}
  const pos = (rf.top_findings || []).filter(f => f.change_direction === 'positive')
  for (const f of pos.slice(0, 2)) out.push(`Risk factors: ${f.summary || 'a risk was removed/softened'}`)
  const pa = agents.promise_audit || {}
  const kept = (pa.top_findings || []).filter(f => f.type === 'kept' || f.type === 'promise_kept')
  if (kept.length) out.push(`${kept.length} tracked management promise${kept.length !== 1 ? 's' : ''} verified KEPT`)
  return out
}

function agentDetail(aid, block) {
  const findings = block.top_findings || []
  const n = findings.length
  const plural = (k, one, many) => (k === 1 ? one : many)
  const lines = [`${AGENT_HEADLINES[aid] || block.name || aid}: ${fmtSignal(block.score, block.signal)}`]
  if (aid === 'corporate_discrepancy') {
    const hi = findings.filter(f => f.severity === 'high').length
    lines.push(`${n} discrepanc${plural(n, 'y', 'ies')} flagged${hi ? `, ${hi} high-severity` : ''}.`)
  } else if (aid === 'promise_audit') {
    const kinds = {}
    for (const f of findings) {
      const k = (f.type || '?').replace('promise_', '')
      kinds[k] = (kinds[k] || 0) + 1
    }
    const order = ['kept', 'partial', 'broken', 'pending', 'unresolved']
    const parts = order.filter(k => kinds[k]).map(k => `${kinds[k]} ${k}`)
    for (const k of Object.keys(kinds).sort()) if (!order.includes(k)) parts.push(`${kinds[k]} ${k}`)
    lines.push(parts.length ? `${n} promises tracked: ${parts.join(', ')}.` : 'No promises surfaced.')
  } else if (aid === 'risk_factors') {
    const esc = findings.filter(f => (f.type || '').includes('escalat')).length
    const nw = findings.filter(f => f.type === 'risk_new').length
    const pos = findings.filter(f => f.change_direction === 'positive').length
    lines.push(`${n} change${plural(n, '', 's')} — ${esc} escalation${plural(esc, '', 's')}, ${nw} new risk${plural(nw, '', 's')}, ${pos} positive.`)
  } else if (aid === 'constraint_auditor') {
    const binding = findings.filter(f => f.urgency === 'immediate').length
    const near = findings.filter(f => f.urgency === 'high').length
    lines.push(`${n} material${plural(n, '', 's')} evaluated: ${binding} binding now, ${near} crossing within 2 years.`)
  } else if (aid === 'moat_auditor') {
    for (const f of findings) if (f.moat_type) lines.push(`  ${f.moat_type}: ${f.verdict || '?'}`)
  } else if (aid === 'legal_proceedings') {
    const changed = findings.filter(f => f.type !== 'no_change')
    lines.push(changed.length
      ? `${changed.length} proceeding update${plural(changed.length, '', 's')}.`
      : 'No material changes across compared filings.')
  }
  return lines
}

function trustFooter(agents) {
  let total = 0, verified = 0
  for (const block of Object.values(agents)) {
    for (const f of (block || {}).top_findings || []) {
      for (const c of f.citations || []) {
        total += 1
        if (c.verified) verified += 1
      }
    }
  }
  if (!total) {
    return 'All findings are generated from primary sources (SEC EDGAR, USGS, FRED). Full audit trail in the JSON export.'
  }
  const rate = (100 * verified / total).toFixed(1)
  return `All findings link to original SEC filings, USGS data, and earnings transcripts. ${verified}/${total} citations (${rate}%) verbatim-verified against source documents. Full audit trail in the JSON export.`
}

export function generateBrief(exportData) {
  const ticker = exportData.ticker || '?'
  const agents = exportData.agents || {}
  let prettyDate = (exportData.timestamp || '').slice(0, 10)
  try {
    prettyDate = new Date(exportData.timestamp).toLocaleDateString('en-US',
      { year: 'numeric', month: 'long', day: 'numeric' })
  } catch { /* keep ISO date */ }
  const w = exportData.analysis_window
  let windowTxt = 'last 5 fiscal years'
  if (w && typeof w === 'object' && Array.isArray(w.years) && w.years.length) {
    windowTxt = `FY${Math.min(...w.years)}–FY${Math.max(...w.years)}`
  } else if (typeof w === 'string') windowTxt = w

  const L = []
  L.push(RULE)
  L.push(`SOLLIA BRIEF — ${ticker} (${prettyDate})`)
  L.push('Prepared for Investment Committee Review')
  L.push(`Analysis window: ${windowTxt} | ${Object.keys(agents).length} agents`)
  L.push(RULE, '')

  L.push('EXECUTIVE SUMMARY')
  const bits = []
  const attention = collectAttention(agents)
  if (attention.length) {
    const heads = attention.slice(0, 3).map(([, , f]) => {
      let h = (f.summary || f.type || '').replace(/\.$/, '').trim().split('. ')[0]
      if (h.length > 90) h = h.slice(0, 87) + '...'
      return h
    }).filter(Boolean)
    bits.push('Attention items: ' + heads.join('; ') + '.')
  }
  for (const aid of ['corporate_discrepancy', 'promise_audit', 'risk_factors']) {
    const b = agents[aid]
    if (b && b.score != null) bits.push(`${AGENT_HEADLINES[aid]} ${fmtSignal(b.score, b.signal)}.`)
  }
  bits.push(`Overall signal: ${exportData.signal || 'HOLD'} (${exportData.score ?? 50}/100).`)
  L.push(bits.join(' '), '')

  L.push(`⚠️  WHAT NEEDS ATTENTION (${attention.length} item${attention.length === 1 ? '' : 's'})`, '')
  if (!attention.length) L.push('Nothing rose to high severity in this run.')
  attention.forEach(([, aid, f], i) => {
    const badge = f.urgency_badge || (f.severity || '').toUpperCase()
    L.push(`${i + 1}. [${AGENT_HEADLINES[aid] || aid}] ${badge} — ${(f.summary || f.type || '').trim()}`)
    if (f.severity_reason) L.push(`   → ${f.severity_reason}`)
    for (const c of (f.citations || []).slice(0, 2)) {
      if (c.excerpt) {
        let exc = c.excerpt.trim()
        if (exc.length > 160) exc = exc.slice(0, 157) + '...'
        L.push(`   "${exc}"`)
      }
      if (c.url) L.push(`   [View in ${citeLabel(c)} ↗] ${c.url}`)
    }
    L.push('')
  })

  const stable = collectStable(agents)
  L.push("✅ WHAT'S STABLE", '')
  if (stable.length) stable.forEach(s => L.push(`• ${s}`))
  else L.push('• No unchanged areas to report (see agent detail).')
  L.push('')

  L.push('📊 DETAIL BY AGENT', '')
  for (const aid of AGENT_ORDER) {
    const block = agents[aid]
    if (!block) continue
    agentDetail(aid, block).forEach(ln => L.push(ln))
    L.push('')
  }

  L.push(RULE)
  L.push(trustFooter(agents))
  L.push(RULE)
  return L.join('\n')
}
