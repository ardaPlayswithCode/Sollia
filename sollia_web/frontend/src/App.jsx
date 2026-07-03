import { useState, useEffect, useRef } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import AgentGrid from './components/AgentGrid'
import { generateBrief } from './utils/brief'
import {
  GlowCard, GridBackground, NumberTicker, TextReveal,
  AnimatedList, AnimatedItem, CardEntrance, Tip,
} from './components/effects'

const API_BASE = 'http://localhost:8000'

const defaultAgents = [
  { id: 'corporate_discrepancy', name: 'Corporate Discrepancy' },
  { id: 'promise_audit', name: 'Promise Audit' },
  { id: 'legal_proceedings', name: 'Legal Proceedings' },
  { id: 'risk_factors', name: 'Risk Factors' },
  { id: 'constraint_auditor', name: 'Constraint Auditor' },
  { id: 'moat_auditor', name: 'Moat Auditor' },
]
const AGENT_LABELS = Object.fromEntries(defaultAgents.map(a => [a.id, a.name]))

const AGENT_BLURBS = {
  corporate_discrepancy: 'Cross-checks 10-K, 10-Q, proxies and earnings calls for statements that contradict each other',
  promise_audit: 'Tracks management promises and verifies follow-through in later filings',
  legal_proceedings: 'Diffs legal proceedings across filings — new cases, settlements, quiet removals',
  risk_factors: 'Compares Item 1A year over year — escalations, new risks, softened language',
  constraint_auditor: 'Pressure-tests growth projections against physical supply (USGS/FRED)',
  moat_auditor: 'Audits claimed competitive moats against competitor SEC filings',
}

// Milestone styling — qualitative kinds emitted by the backend (never internals)
const KIND_EMOJI = { search: '🔍', fetch: '📑', analyze: '⚖️', align: '🧭', flag: '⚠️', done: '✅', info: '•' }
const KIND_COLOR = {
  search: 'text-blue-300', fetch: 'text-cyan-300', analyze: 'text-violet-300',
  align: 'text-indigo-300', flag: 'text-amber-300', done: 'text-emerald-300', info: 'text-slate-400',
}

export default function App() {
  const [ticker, setTicker] = useState('F')
  const [selected, setSelected] = useState(['corporate_discrepancy', 'promise_audit', 'legal_proceedings', 'risk_factors', 'constraint_auditor', 'moat_auditor'])
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [exportData, setExportData] = useState(null)
  const [error, setError] = useState(null)
  const [agentLogs, setAgentLogs] = useState({})
  const [resetToken, setResetToken] = useState(0)
  const esRef = useRef(null)

  // Tidy up any open stream on unmount
  useEffect(() => () => { if (esRef.current) esRef.current.close() }, [])

  function toggleAgent(id) {
    setSelected(prev => prev.includes(id) ? prev.filter(a => a !== id) : [...prev, id])
    setResult(null); setExportData(null); setError(null)
  }

  function runAnalysis() {
    if (!ticker.trim() || selected.length === 0 || loading) return
    if (esRef.current) esRef.current.close()
    setLoading(true); setError(null); setResult(null); setExportData(null)

    const seed = {}
    selected.forEach(id => { seed[id] = { name: AGENT_LABELS[id] || id, status: 'queued', logs: [] } })
    setAgentLogs(seed)

    fetch(`${API_BASE}/api/analyze/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker: ticker.toUpperCase(), agents: selected }),
    })
      .then(r => { if (!r.ok) throw new Error('Server error: ' + r.status); return r.json() })
      .then(({ run_id }) => {
        const es = new EventSource(`${API_BASE}/api/analyze/stream/${run_id}`)
        esRef.current = es
        es.onmessage = (e) => {
          let ev
          try { ev = JSON.parse(e.data) } catch { return }
          if (ev.type === 'progress') {
            setAgentLogs(prev => {
              const next = { ...prev }
              for (const [aid, a] of Object.entries(ev.agents || {})) {
                const cur = next[aid] || { name: a.name, status: a.status, logs: [] }
                next[aid] = {
                  name: a.name || cur.name,
                  status: a.status || cur.status,
                  logs: a.new_logs && a.new_logs.length ? [...cur.logs, ...a.new_logs] : cur.logs,
                }
              }
              return next
            })
            // LIVE per-agent results (Part 7d): render each card the moment
            // its agent finishes — the analyst reads while the rest still run.
            if (ev.new_results) {
              setResult(prev => ({ ...(prev || { ticker: ticker.toUpperCase(), live: true }), ...ev.new_results }))
            }
          } else if (ev.type === 'complete') {
            setResult(ev.result); setExportData(ev.export); setLoading(false); es.close()
          } else if (ev.type === 'error') {
            setError(ev.message || 'Analysis error')
            if (ev.result) setResult(ev.result)
            if (ev.export) setExportData(ev.export)
            setLoading(false); es.close()
          }
        }
        es.onerror = () => { setError('Stream connection lost'); setLoading(false); es.close() }
      })
      .catch(e => { setError(e.message); setLoading(false) })
  }

  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Enter' && ticker.trim() && selected.length > 0 && !loading) runAnalysis()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [ticker, selected, loading])

  const hasActivity = Object.keys(agentLogs).length > 0
  const presentAgents = result
    ? defaultAgents.map(a => a.id).filter(id => result[id])
    : []

  function renderAgentCard(id) {
    const data = result?.[id]
    if (!data) return null
    switch (id) {
      case 'corporate_discrepancy': return <DiscrepancyCard data={data} />
      case 'promise_audit': return <PromiseCard data={data} />
      case 'legal_proceedings': return <LegalCard data={data} />
      case 'risk_factors': return <RiskCard data={data} />
      case 'constraint_auditor': return <ConstraintAuditorCard data={data} />
      case 'moat_auditor': return <MoatAuditorCard data={data} />
      default: return null
    }
  }

  return (
    <div className="min-h-screen bg-[#06080D] text-white font-sans relative">
      <GridBackground />
      <div className="max-w-6xl mx-auto px-6 py-8 relative z-10">
        {/* HEADER */}
        <div className="flex flex-wrap gap-4 items-end mb-8">
          <div>
            <label className="text-xs text-slate-500 uppercase tracking-widest mb-2 block">
              Ticker
            </label>
            <div className="flex gap-3">
              <input
                type="text" value={ticker}
                onChange={e => setTicker(e.target.value.toUpperCase().slice(0, 10))}
                placeholder="NVDA" maxLength={10}
                className="bg-[#0A0B14] border border-white/10 rounded-xl px-5 py-3.5 text-white text-2xl font-black tracking-[0.2em] placeholder-slate-700 focus:outline-none focus:border-blue-400/50 w-44 text-center"
              />
            </div>
            <div className="text-[11px] text-slate-500 mt-2">
              Analyzing last 5 fiscal years + current partial year
            </div>
          </div>
          <button
            onClick={runAnalysis}
            disabled={loading || !ticker.trim() || selected.length === 0}
            className={`px-8 py-3.5 rounded-xl font-bold text-sm tracking-wide transition-all hover:scale-[1.02] active:scale-[0.99] ${
              loading || !ticker.trim() ? 'bg-white/5 text-slate-700 cursor-not-allowed' : 'bg-gradient-to-r from-blue-500 to-violet-500 text-white hover:shadow-lg hover:shadow-blue-500/20'
            }`}
          >
            {loading ? 'Running...' : 'Analyze'}
          </button>
          {result && !loading && (
            <button
              onClick={() => setResetToken(t => t + 1)}
              className="ml-auto px-4 py-2.5 rounded-xl text-xs text-slate-500 hover:text-slate-300 bg-white/[0.02] border border-white/5 hover:border-white/20 transition-all"
              title="Restore the default card arrangement"
            >
              Reset Layout
            </button>
          )}
        </div>

        {/* AGENTS */}
        <div className="mb-6">
          <label className="text-xs text-slate-500 uppercase tracking-widest mb-2 block">Agents</label>
          <div className="flex flex-wrap gap-2">
            {defaultAgents.map(agent => (
              <Tip key={agent.id} label={AGENT_BLURBS[agent.id]}>
                <button
                  onClick={() => toggleAgent(agent.id)}
                  className={`px-5 py-2.5 rounded-xl text-sm font-semibold transition-all border hover:scale-[1.02] ${
                    selected.includes(agent.id) ? 'bg-blue-500/15 border-blue-400 text-blue-300' : 'bg-white/[0.02] border-white/5 hover:border-white/30 text-slate-400'
                  }`}
                >
                  {agent.name}
                </button>
              </Tip>
            ))}
          </div>
        </div>

        {/* ERROR */}
        {error && (
          <div className="bg-red-500/10 border border-red-500/20 rounded-xl p-4 mb-6">
            <p className="text-red-400 font-medium">Error: {error}</p>
          </div>
        )}

        {/* EMPTY STATE */}
        {!result && !loading && !error && !hasActivity && (
          <div className="text-center py-20">
            <p className="text-slate-600 text-lg">Enter a ticker, select agents, and click Analyze</p>
            <p className="text-slate-800 text-sm mt-2">or press Enter</p>
          </div>
        )}

        {/* AGENT ACTIVITY — per-agent collapsible live logs (default collapsed) */}
        {hasActivity && (
          <div className="mb-6">
            <label className="text-xs text-slate-500 uppercase tracking-widest mb-2 block">
              Agent Activity {loading && <span className="text-blue-400 normal-case tracking-normal ml-1">· live</span>}
            </label>
            <div className="space-y-2">
              {selected.filter(id => agentLogs[id]).map(id => (
                <AgentLog key={id} agent={agentLogs[id]} />
              ))}
            </div>
          </div>
        )}

        {/* RESULTS — cards stream in live as each agent finishes */}
        {result && (
          <div className="space-y-6">
            {/* SCORE CARDS */}
            <motion.div
              className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4"
              initial="hidden" animate="show"
              variants={{ hidden: {}, show: { transition: { staggerChildren: 0.1 } } }}
            >
              {result.corporate_discrepancy && (
                <ScoreBox label="Corporate Discrepancy" score={result.corporate_discrepancy.score ?? 50} signal={signalFor(result.corporate_discrepancy.score)} />
              )}
              {result.promise_audit && (
                <ScoreBox label="Promise Audit" score={result.promise_audit.score ?? 50} signal={signalFor(result.promise_audit.score)} />
              )}
              {result.legal_proceedings && (
                <ScoreBox label="Legal Proceedings" score={result.legal_proceedings.agent3_score ?? 50} signal={signalFor(result.legal_proceedings.agent3_score)} />
              )}
              {result.risk_factors && (
                <ScoreBox label="Risk Factors" score={result.risk_factors.score ?? 50} signal={signalFor(result.risk_factors.score)} />
              )}
            </motion.div>

            {/* AGENT CARDS — draggable/resizable grid, presence-animated */}
            <AnimatePresence>
              <AgentGrid
                ids={presentAgents}
                resetToken={resetToken}
                renderCard={(id) => (
                  <CardEntrance key={id} className="h-full">
                    {renderAgentCard(id)}
                  </CardEntrance>
                )}
              />
            </AnimatePresence>

            {/* JSON EXPORT — clean, analyst-ready schema (+ meeting brief) */}
            {exportData && <JsonPanel data={exportData} ticker={result.ticker} />}
          </div>
        )}
      </div>
    </div>
  )
}

// ── Inline components ─────────────────────────────────────────────────────────

function AgentLog({ agent }) {
  const [open, setOpen] = useState(false)
  const logs = agent.logs || []
  const last = logs[logs.length - 1]
  const dot = agent.status === 'running' ? 'text-blue-400' : agent.status === 'complete' ? 'text-emerald-400' : agent.status === 'error' ? 'text-red-400' : 'text-slate-600'
  const glyph = agent.status === 'running' ? '●' : agent.status === 'complete' ? '✓' : agent.status === 'error' ? '✗' : '○'
  return (
    <div className="border border-white/[0.06] rounded-xl overflow-hidden bg-[#0A0B14]">
      <button onClick={() => setOpen(o => !o)} className="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-white/[0.02] text-left">
        <span className={`text-[10px] ${dot} ${agent.status === 'running' ? 'animate-pulse' : ''}`}>{glyph}</span>
        <span className="text-sm font-semibold text-slate-300">{agent.name}</span>
        <span className="text-slate-600 text-[10px] uppercase tracking-wide">{agent.status}</span>
        <span className="text-slate-500 text-xs ml-auto truncate max-w-[55%]">{last ? `${KIND_EMOJI[last.kind] || '•'} ${last.message}` : '…'}</span>
        <span className="text-slate-600 text-xs">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <div className="px-4 py-2 bg-[#04060C] max-h-56 overflow-y-auto space-y-1 font-mono text-xs border-t border-white/5">
          {logs.length === 0 && <p className="text-slate-600">No activity yet.</p>}
          {logs.map((l, i) => (
            <p key={i} className={KIND_COLOR[l.kind] || 'text-slate-400'}>
              <span className="text-slate-600">[{l.ts}]</span> {KIND_EMOJI[l.kind] || '•'} {l.message}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}

function JsonPanel({ data, ticker }) {
  const [view, setView] = useState(false)
  const [copied, setCopied] = useState(false)
  if (!data) return null
  const text = JSON.stringify(data, null, 2)
  const downloadBlob = (content, name, type) => {
    const blob = new Blob([content], { type })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = name; a.click()
    URL.revokeObjectURL(url)
  }
  const download = () => downloadBlob(text, `sollia_${(ticker || 'export').toLowerCase()}.json`, 'application/json')
  // Meeting brief (Part 6b): generated client-side from the export — no LLM,
  // no server round-trip, instant.
  const downloadBrief = () => downloadBlob(
    generateBrief(data), `sollia_brief_${(ticker || 'export').toLowerCase()}.txt`, 'text/plain')
  const copy = () => { navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500) }
  const btn = 'px-5 py-2.5 rounded-xl text-sm text-slate-400 hover:text-white bg-white/[0.03] border border-white/10 transition-all font-medium hover:scale-[1.02]'
  return (
    <div className="bg-[#0A0B14] border border-white/5 rounded-2xl p-6 lift-card">
      <h3 className="text-xl font-bold mb-1">Export</h3>
      <p className="text-slate-500 text-sm mb-4">Analyst-ready JSON + a 1-page meeting brief for your PM</p>
      <div className="flex gap-3 mb-3 flex-wrap">
        <Tip label="1-page executive brief: attention items, stable areas, per-agent detail">
          <button onClick={downloadBrief} className={`${btn} border-blue-400/30 text-blue-300`}>Download Brief</button>
        </Tip>
        <button onClick={download} className={btn}>Download JSON</button>
        <button onClick={copy} className={btn}>{copied ? 'Copied!' : 'Copy'}</button>
        <button onClick={() => setView(v => !v)} className={btn}>{view ? 'Hide' : 'View JSON'}</button>
      </div>
      {view && (
        <pre className="bg-[#04060C] rounded-xl p-4 max-h-96 overflow-auto text-xs text-emerald-400/80 whitespace-pre-wrap border border-white/5 font-mono">{text}</pre>
      )}
    </div>
  )
}

// ── Agent 1: Corporate Discrepancy ────────────────────────────────────────────
function DiscrepancyCard({ data }) {
  const findings = data.top_discrepancies || []
  const tone = findings.some(d => d.severity === 'high') ? 'bearish' : findings.length ? 'neutral' : 'bullish'
  return (
    <Card title="Corporate Discrepancy" tone={tone} active={findings.length > 0}
      blurb={AGENT_BLURBS.corporate_discrepancy}
      subtitle={`Score: ${data.score ?? '—'} · ${data.total_aligned_pairs ?? 0} aligned passages`}>
      {findings.length === 0 && (
        <p className="text-slate-600 text-sm">No material cross-document discrepancies surfaced.</p>
      )}
      <AnimatedList className="space-y-3">
        {findings.slice(0, 5).map((d, i) => (
          <AnimatedItem key={i}>
            <Row badge={d.severity === 'high' ? 'RED' : d.severity === 'medium' ? 'YELLOW' : 'GREEN'} badgeText={d.severity} meta={d.pair_display || d.pair?.replace(/_/g, ' ')}>
              {d.same_filing && <span className="inline-block mb-2 px-2 py-0.5 rounded text-[10px] font-bold bg-slate-500/10 text-slate-400">SAME FILING — DOWNRANKED</span>}
              <p className="text-slate-400 text-xs mb-2">{d.reasoning}</p>
              <Quote text={d.source_a?.sentence_text} url={d.source_a?.edgar_url || d.source_a?.htm_link} label={d.source_a?.document_type} page={d.source_a?.page_number} />
              <Quote text={d.source_b?.sentence_text} url={d.source_b?.edgar_url || d.source_b?.htm_link} label={d.source_b?.document_type} page={d.source_b?.page_number} />
            </Row>
          </AnimatedItem>
        ))}
      </AnimatedList>
    </Card>
  )
}

// ── Agent 2: Promise Audit ────────────────────────────────────────────────────
function PromiseCard({ data }) {
  const broken = data.total_broken || 0
  const tone = broken > (data.total_kept || 0) ? 'bearish' : 'bullish'
  return (
    <Card title="Promise Audit" tone={tone} active={(data.top_promises || []).length > 0}
      blurb={AGENT_BLURBS.promise_audit}
      subtitle={`${data.total_promises_extracted || 0} promises · ${data.total_kept || 0} kept · ${broken} broken`}>
      <AnimatedList className="space-y-3">
        {(data.top_promises || []).map((p, i) => (
          <AnimatedItem key={i}>
            <Row badge={p.status} badgeText={p.status} meta={`${p.category} · severity ${p.severity}/10`}>
              <Quote text={p.text} url={p.promise_url} label={p.promise_source || 'Promise'} />
              <Quote text={p.evidence_text} url={p.evidence_url} label={p.evidence_filing || 'Evidence'} />
              {p.reasoning && <p className="text-slate-500 text-xs mt-1">{p.reasoning}</p>}
            </Row>
          </AnimatedItem>
        ))}
      </AnimatedList>
    </Card>
  )
}

// ── Agent 3: Legal Proceedings ────────────────────────────────────────────────
function LegalCard({ data }) {
  const findings = data.findings || []
  const hasChanges = findings.some(f => (f.proceedings || []).length > 0)
  return (
    <Card title="Legal Proceedings" tone={hasChanges ? 'neutral' : 'bullish'} active={hasChanges}
      blurb={AGENT_BLURBS.legal_proceedings}
      subtitle={`${findings.length} filing types compared`}>
      {findings.map((f, i) => (
        <div key={i} className="mb-4">
          <p className="text-slate-400 text-xs font-semibold mb-2">
            {f.doc_type}{f.prior_date ? `: ${f.prior_date} → ${f.current_date}` : ''} · Score: {f.agent3_score}
          </p>
          {(!f.proceedings || f.proceedings.length === 0) ? (
            <p className="text-slate-600 text-xs italic">{f.summary || `No legal proceedings in ${f.doc_type}`}</p>
          ) : (
            <>
              {f.summary && <p className="text-slate-500 text-xs mb-2">{f.summary}</p>}
              <AnimatedList className="space-y-3">
                {f.proceedings.slice(0, 5).map((p, j) => (
                  <AnimatedItem key={j}>
                    <Row badge={p.status || p.change_type} badgeText={p.status || p.change_type} meta={p.case_name || p.description}>
                      <Quote text={p.prior_quote || p.prior_text} url={p.prior_edgar_url} label="Prior" page={p.prior_quote_page} />
                      <Quote text={p.current_quote || p.current_text} url={p.edgar_url} label="Current" page={p.current_quote_page} />
                      {p.liability_change && <p className="text-slate-500 text-xs mt-1">Liability: {p.liability_change}</p>}
                    </Row>
                  </AnimatedItem>
                ))}
              </AnimatedList>
            </>
          )}
        </div>
      ))}
    </Card>
  )
}

// ── Agent 4: Risk Factors ─────────────────────────────────────────────────────
function RiskCard({ data }) {
  const changes = data.top_changes || []
  const bearish = changes.some(c => ['ESCALATED', 'NEW'].includes(String(c.category).toUpperCase()))
  return (
    <Card title="Risk Factors" tone={bearish ? 'bearish' : changes.length ? 'neutral' : 'bullish'} active={changes.length > 0}
      blurb={AGENT_BLURBS.risk_factors}
      subtitle={`Item 1A delta · ${data.old_filing_date || '—'} → ${data.new_filing_date || '—'} · ${data.total_risks_old ?? 0}→${data.total_risks_new ?? 0} risks · ${data.changes_found || 0} changes`}>
      {changes.length === 0 && (
        <p className="text-slate-600 text-sm">{data.summary || 'No material changes in risk factors between the two 10-Ks.'}</p>
      )}
      <AnimatedList className="space-y-3">
        {changes.map((c, i) => (
          <AnimatedItem key={i}>
            <Row badge={c.category} badgeText={c.category} meta={c.risk_title}>
              {c.summary && <p className="text-slate-400 text-xs mb-2">{c.summary}</p>}
              <Quote text={c.old_quote} url={c.old_url} label="Old 10-K" page={c.old_quote_page} />
              <Quote text={c.new_quote} url={c.new_url} label="New 10-K" page={c.new_quote_page} />
              {c.verified === false && <p className="text-amber-400/70 text-[10px] mt-1">⚠ quote not verified verbatim</p>}
            </Row>
          </AnimatedItem>
        ))}
      </AnimatedList>
      {data.ten_q_findings && (
        <div className="mt-4 pt-4 border-t border-white/10">
          <p className="text-slate-500 text-xs font-semibold mb-3">
            10-Q Update · {data.ten_q_findings.filing_date} · vs {data.ten_q_findings.compared_against} ({data.ten_q_findings.compared_date}) · {data.ten_q_findings.changes_found} changes
          </p>
          <div className="space-y-3">
            {(data.ten_q_findings.changes || []).length === 0 && (
              <p className="text-slate-600 text-sm">No material changes in the latest 10-Q.</p>
            )}
            {(data.ten_q_findings.changes || []).map((c, i) => (
              <Row key={i} badge={c.category} badgeText={c.category} meta={c.risk_title}>
                {c.summary && <p className="text-slate-400 text-xs mb-2">{c.summary}</p>}
                <Quote text={c.old_quote} url={c.old_url} label={`Old ${data.ten_q_findings.compared_against}`} page={c.old_quote_page} />
                <Quote text={c.new_quote} url={c.new_url} label="New 10-Q" page={c.new_quote_page} />
                {c.verified === false && <p className="text-amber-400/70 text-[10px] mt-1">⚠ quote not verified verbatim</p>}
              </Row>
            ))}
          </div>
        </div>
      )}
    </Card>
  )
}

// ── Agent 5: Constraint Auditor ───────────────────────────────────────────────
// Urgency → badge (production polish Part 4c): BINDING NOW red, <2yr orange,
// <5yr yellow, beyond-5yr green, none gray. Backend computes urgency/badge.
const URGENCY_BADGE = {
  immediate: 'bg-red-500/15 text-red-400 border border-red-500/40',
  high: 'bg-orange-500/15 text-orange-400 border border-orange-500/40',
  medium: 'bg-amber-500/15 text-amber-400 border border-amber-500/40',
  low: 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/40',
  none: 'bg-slate-500/15 text-slate-400 border border-slate-500/40',
}

function CagrLine({ label, c }) {
  if (!c || c.value == null) return (
    <p className="text-slate-500 text-xs"><span className="uppercase font-semibold">{label} CAGR</span> · unavailable</p>
  )
  return (
    <div className="text-slate-400 text-sm">
      <span className="text-slate-500 text-[10px] uppercase font-semibold">{label} CAGR</span>{' '}
      <span className="text-white font-semibold">{c.value}%</span>
      <span className="text-slate-600 text-[10px]"> · {c.source} · {c.date}</span>
      {c.quote_verified === false && <span className="text-amber-400/70 text-[10px]"> ⚠ unverified</span>}
      <Quote text={c.quote} url={c.url} label="" />
    </div>
  )
}

function ConstraintAuditorCard({ data }) {
  if (data.status === 'unsupported') {
    return (
      <Card title="Constraint Auditor" tone="audit" blurb={AGENT_BLURBS.constraint_auditor}
        subtitle="Supply-constraint audit — 14 resource-intensive sectors">
        <p className="text-slate-500 text-sm">{data.message}</p>
      </Card>
    )
  }
  const pc = data.projected_cagr || {}
  const constraints = data.constraints || []
  const evaluated = constraints.filter(c => c.status === 'success').length
  const binding = constraints.some(c => c.urgency === 'immediate')
  const prov = data.data_provisioning
  return (
    <Card
      title="Constraint Auditor" tone="audit" active={binding}
      blurb={AGENT_BLURBS.constraint_auditor}
      subtitle={`${data.sector || '—'} · ${constraints.length} materials · ${evaluated} with live supply/demand · AUDIT (no bull/bear score)`}
    >
      <div className="border border-white/[0.06] rounded-xl p-4 space-y-2">
        <p className="text-slate-500 text-xs font-semibold uppercase tracking-wide">Projected CAGR being pressure-tested</p>
        <CagrLine label="Management" c={pc.management} />
        <CagrLine label="Industry" c={pc.industry} />
      </div>

      <AnimatedList className="space-y-3">
        {constraints.map((c, i) => {
          const ok = c.status === 'success'
          return (
            <AnimatedItem key={i}>
              <div className="border border-white/[0.04] rounded-xl p-4">
                <div className="flex items-center gap-2 mb-2 flex-wrap">
                  <Tip label={c.model_note || ''}>
                    <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${URGENCY_BADGE[c.urgency] || URGENCY_BADGE.none}`}>
                      {ok ? (c.urgency_badge || '—') : 'NO DATA'}
                    </span>
                  </Tip>
                  <span className="text-sm font-semibold text-slate-200">{c.material}</span>
                </div>
                {ok ? (
                  <>
                    <p className="text-slate-400 text-xs mb-2 leading-relaxed">
                      {c.crossover_detail || `Demand exceeds supply — management CAGR ${c.crossover_management}, industry CAGR ${c.crossover_industry}`}
                    </p>
                    {c.supply && (
                      <Quote text={c.supply.quote} url={c.supply.url}
                        label={`Supply ${c.supply.value ?? ''} ${c.supply.unit ?? ''} @ ${c.supply.growth_rate_pct ?? '?'}%`} />
                    )}
                    {c.demand && (
                      <Quote text={c.demand.quote} url={c.demand.url}
                        label={`Demand ${c.demand.value ?? ''} ${c.demand.unit ?? ''}`} />
                    )}
                    {c.date_note && <p className="text-slate-600 text-[11px] mt-1 italic">⚠ {c.date_note}</p>}
                  </>
                ) : (
                  <p className="text-slate-500 text-xs">{c.reason || c.status}</p>
                )}
              </div>
            </AnimatedItem>
          )
        })}
      </AnimatedList>

      {prov && (prov.missing_keys?.length > 0 || prov.needs_pdf_parser?.length > 0) && (
        <p className="text-slate-600 text-[10px] mt-2">
          To enable live supply/demand &amp; industry-CAGR data: provision {[...(prov.missing_keys || []), ...(prov.needs_pdf_parser || []).map(s => s + ' parser')].join(', ')}.
        </p>
      )}
    </Card>
  )
}

// ── Agent 6: Moat Auditor ─────────────────────────────────────────────────────
// Verdict → color (PART 7c): DIRECT THREAT red, WATCH orange, NO THREAT green.
const MOAT_VERDICT = {
  'DIRECT THREAT': 'bg-red-500/10 text-red-400 border-red-500/30',
  'WATCH': 'bg-orange-500/10 text-orange-400 border-orange-500/30',
  'NO THREAT': 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30',
}

function MoatQuote({ label, sub, text, url, page }) {
  if (!text) return null
  return (
    <div className="mt-2">
      <p className="text-slate-500 text-[10px] uppercase font-semibold tracking-wide">
        {label}{sub ? <span className="text-slate-600 normal-case"> · {sub}</span> : null}
        {page ? <span className="text-slate-600 normal-case"> · p.{page}</span> : null}
        {url && url.startsWith('http') && (
          <a href={url} target="_blank" rel="noopener noreferrer" className="text-blue-400 ml-2 hover:underline">View ↗</a>
        )}
      </p>
      <p className="text-slate-300 text-sm italic leading-snug">"{text}"</p>
    </div>
  )
}

function MoatAuditorCard({ data }) {
  if (!data) return null
  if (data.status === 'error') {
    return (
      <Card title="Moat Auditor" tone="audit" blurb={AGENT_BLURBS.moat_auditor}
        subtitle="Competitive-moat audit vs competitor SEC filings">
        <p className="text-slate-500 text-sm">{data.message || 'Moat audit could not complete.'}</p>
      </Card>
    )
  }
  const competitors = data.competitors_analyzed || []
  const moats = data.moats_audited || []
  const threats = moats.filter(m => m.verdict === 'DIRECT THREAT').length
  const watches = moats.filter(m => m.verdict === 'WATCH').length
  return (
    <Card
      title="Moat Auditor" tone="audit" active={threats + watches > 0}
      blurb={AGENT_BLURBS.moat_auditor}
      subtitle={`${data.sector || '—'} · ${competitors.length} competitors · ${moats.length} moats · ${threats} direct threat${threats === 1 ? '' : 's'}, ${watches} watch · AUDIT (no bull/bear score)`}
    >
      <div className="border border-white/[0.06] rounded-xl p-4">
        <p className="text-slate-500 text-xs font-semibold uppercase tracking-wide mb-2">Competitors analyzed</p>
        <div className="flex flex-wrap gap-2">
          {competitors.map((c, i) => (
            <Tip key={i} label={c.rationale || ''}>
              <span className="px-2 py-0.5 rounded text-[11px] bg-white/[0.04] text-slate-300 border border-white/5">
                {c.ticker} <span className="text-slate-600">· {String(c.source || '').startsWith('10-K') ? '10-K' : 'LLM'}</span>
              </span>
            </Tip>
          ))}
          {competitors.length === 0 && <span className="text-slate-600 text-xs">None identified.</span>}
        </div>
      </div>

      {data.status === 'no_moats' && (
        <p className="text-slate-500 text-sm">No moat claims identified in company filings.</p>
      )}

      <AnimatedList className="space-y-3">
        {moats.map((m, i) => {
          const cc = m.company_claim || {}
          const ch = m.strongest_challenge
          return (
            <AnimatedItem key={i}>
              <div className="border border-white/[0.04] rounded-xl p-4">
                <div className="flex items-center gap-2 mb-1">
                  <span className={`px-2 py-0.5 rounded text-[10px] font-bold border ${MOAT_VERDICT[m.verdict] || MOAT_VERDICT['NO THREAT']}`}>{m.verdict}</span>
                  <span className="text-sm font-semibold text-slate-200">{m.moat_type}</span>
                </div>
                <MoatQuote label="Company Claim" sub={cc.source} text={cc.sentence} url={cc.url} page={cc.page_number} />
                {ch
                  ? <MoatQuote label="Competitor Challenge" sub={`${ch.competitor} · ${ch.source}${ch.date ? ' · ' + ch.date : ''}`} text={ch.sentence} url={ch.url} />
                  : <p className="text-slate-500 text-xs mt-2"><span className="uppercase font-semibold text-[10px]">Competitor Challenge</span> · None found.</p>}
                {m.analysis && <p className="text-slate-400 text-xs mt-2 leading-relaxed">{m.analysis}</p>}
              </div>
            </AnimatedItem>
          )
        })}
      </AnimatedList>
    </Card>
  )
}

// Unified bullishness → signal. Every agent reports on the same 10-90 scale
// (higher = better prospects, 50 = neutral), so one mapping serves them all and
// the badge here always agrees with the backend export: >65 BULLISH (green),
// <35 BEARISH (red), otherwise NEUTRAL (yellow).
function signalFor(score) {
  const s = score ?? 50
  return s > 65 ? 'BULLISH' : s < 35 ? 'BEARISH' : 'NEUTRAL'
}

function ScoreBox({ label, score, signal }) {
  const colors = { BULLISH: '#22C55E', NEUTRAL: '#F59E0B', BEARISH: '#EF4444', HOLD: '#F59E0B' }
  const color = colors[signal] || '#F59E0B'
  return (
    <motion.div
      className="bg-[#0A0B14] border border-white/5 rounded-2xl p-6 text-center lift-card"
      variants={{ hidden: { opacity: 0, y: 20 }, show: { opacity: 1, y: 0, transition: { duration: 0.3, ease: 'easeOut' } } }}
    >
      <div className="text-5xl font-black mb-2" style={{ color }}>
        <NumberTicker value={Math.round(score)} />
        <span className="text-2xl font-bold text-slate-600">/100</span>
      </div>
      <div className="text-slate-400 text-sm font-medium">{label}</div>
      <div className="text-xs mt-1" style={{ color }}>{signal}</div>
    </motion.div>
  )
}

// Card shell used by every agent: severity glow, drag handle header, internal
// scroll (the grid controls outer size), hover lift.
function Card({ title, subtitle, tone = 'neutral', active = false, blurb = '', children }) {
  return (
    <GlowCard tone={tone} active={active} className="h-full">
      <div className="bg-[#0A0B14] border border-white/5 rounded-2xl h-full flex flex-col overflow-hidden relative">
        <div className="drag-handle px-6 pt-5 pb-3 select-none">
          <h3 className="text-xl font-bold mb-1 flex items-center gap-2">
            <TextReveal>{title}</TextReveal>
            {blurb && (
              <Tip label={blurb}>
                <span className="text-slate-600 text-xs border border-white/10 rounded-full w-4 h-4 inline-flex items-center justify-center cursor-help">?</span>
              </Tip>
            )}
          </h3>
          {subtitle && <p className="text-slate-500 text-sm">{subtitle}</p>}
        </div>
        <div className="px-6 pb-5 space-y-3 overflow-y-auto flex-1">{children}</div>
      </div>
    </GlowCard>
  )
}

function Row({ badge, badgeText, meta, children }) {
  const colors = {
    RED: 'bg-red-500/10 text-red-400', YELLOW: 'bg-amber-500/10 text-amber-400', GREEN: 'bg-emerald-500/10 text-emerald-400',
    KEPT: 'bg-emerald-500/10 text-emerald-400', PARTIAL: 'bg-amber-500/10 text-amber-400', BROKEN: 'bg-red-500/10 text-red-400',
    PENDING: 'bg-slate-500/10 text-slate-400', UNRESOLVED: 'bg-slate-500/10 text-slate-400', STALE: 'bg-orange-500/10 text-orange-400',
    NEW: 'bg-red-500/10 text-red-400', ESCALATED: 'bg-red-500/10 text-red-400', SETTLED: 'bg-emerald-500/10 text-emerald-400',
    DISMISSED: 'bg-emerald-500/10 text-emerald-400', UNCHANGED: 'bg-slate-500/10 text-slate-400', QUIETLY_REMOVED: 'bg-amber-500/10 text-amber-400',
    REMOVED: 'bg-amber-500/10 text-amber-400', 'DE-ESCALATED': 'bg-emerald-500/10 text-emerald-400', REWORDED: 'bg-slate-500/10 text-slate-400',
  }
  return (
    <div className="border border-white/[0.04] rounded-xl p-4">
      <div className="flex items-center gap-2 mb-2">
        {badge && <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${colors[badge] || 'bg-slate-500/10 text-slate-400'}`}>{badgeText || badge}</span>}
        {meta && <span className="text-slate-600 text-[10px] ml-auto truncate max-w-[60%]">{meta}</span>}
      </div>
      {children}
    </div>
  )
}

function Quote({ text, url, label, page }) {
  if (!text) return null
  const words = text.split(/\s+/)
  const shown = words.slice(0, 10).join(' ') + (words.length > 10 ? '…' : '')
  const sourceLabel = [label || 'Source', page ? `p.${page}` : ''].filter(Boolean).join(', ')
  return (
    <div className="text-slate-400 text-sm flex items-center gap-2 flex-wrap">
      <span className="text-slate-600 text-[10px] uppercase font-semibold">{sourceLabel}</span>
      <span className="italic">"{shown}"</span>
      {url && (
        <a href={url} target="_blank" rel="noopener noreferrer" className="text-blue-400 text-[10px] hover:underline">View ↗</a>
      )}
    </div>
  )
}
