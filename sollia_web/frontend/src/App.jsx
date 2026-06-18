import { useState, useEffect, useRef } from 'react'

const API_BASE = 'http://localhost:8000'

const defaultAgents = [
  { id: 'corporate_discrepancy', name: 'Corporate Discrepancy' },
  { id: 'promise_audit', name: 'Promise Audit' },
  { id: 'legal_proceedings', name: 'Legal Proceedings' },
  { id: 'risk_factors', name: 'Risk Factors' },
]
const AGENT_LABELS = Object.fromEntries(defaultAgents.map(a => [a.id, a.name]))

// Milestone styling — qualitative kinds emitted by the backend (never internals)
const KIND_EMOJI = { search: '🔍', fetch: '📑', analyze: '⚖️', align: '🧭', flag: '⚠️', done: '✅', info: '•' }
const KIND_COLOR = {
  search: 'text-blue-300', fetch: 'text-cyan-300', analyze: 'text-violet-300',
  align: 'text-indigo-300', flag: 'text-amber-300', done: 'text-emerald-300', info: 'text-slate-400',
}

export default function App() {
  const [ticker, setTicker] = useState('F')
  const [year, setYear] = useState(2025)
  const [selected, setSelected] = useState(['corporate_discrepancy', 'promise_audit', 'legal_proceedings', 'risk_factors'])
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [exportData, setExportData] = useState(null)
  const [error, setError] = useState(null)
  const [agentLogs, setAgentLogs] = useState({})
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
      body: JSON.stringify({ ticker: ticker.toUpperCase(), agents: selected, year }),
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
  }, [ticker, selected, loading, year])

  const showYear = selected.includes('corporate_discrepancy')
  const hasActivity = Object.keys(agentLogs).length > 0

  return (
    <div className="min-h-screen bg-[#06080D] text-white font-sans">
      <div className="max-w-6xl mx-auto px-6 py-8">
        {/* HEADER */}
        <div className="flex flex-wrap gap-4 items-end mb-8">
          <div>
            <label className="text-xs text-slate-500 uppercase tracking-widest mb-2 block">
              Ticker{showYear ? ' · Year' : ''}
            </label>
            <div className="flex gap-3">
              <input
                type="text" value={ticker}
                onChange={e => setTicker(e.target.value.toUpperCase().slice(0, 5))}
                placeholder="NVDA" maxLength={5}
                className="bg-[#0A0B14] border border-white/10 rounded-xl px-5 py-3.5 text-white text-2xl font-black tracking-[0.2em] placeholder-slate-700 focus:outline-none focus:border-blue-400/50 w-36 text-center"
              />
              {showYear && (
                <input
                  type="number" value={year}
                  onChange={e => setYear(parseInt(e.target.value) || 2025)}
                  min={2016} max={2026}
                  className="bg-[#0A0B14] border border-white/10 rounded-xl px-4 py-3.5 text-white text-xl font-bold placeholder-slate-700 focus:outline-none focus:border-purple-400/50 w-24 text-center"
                />
              )}
            </div>
          </div>
          <button
            onClick={runAnalysis}
            disabled={loading || !ticker.trim() || selected.length === 0}
            className={`px-8 py-3.5 rounded-xl font-bold text-sm tracking-wide transition-all ${
              loading || !ticker.trim() ? 'bg-white/5 text-slate-700 cursor-not-allowed' : 'bg-gradient-to-r from-blue-500 to-violet-500 text-white hover:shadow-lg hover:shadow-blue-500/20'
            }`}
          >
            {loading ? 'Running...' : 'Analyze'}
          </button>
        </div>

        {/* AGENTS */}
        <div className="mb-6">
          <label className="text-xs text-slate-500 uppercase tracking-widest mb-2 block">Agents</label>
          <div className="flex flex-wrap gap-2">
            {defaultAgents.map(agent => (
              <button
                key={agent.id}
                onClick={() => toggleAgent(agent.id)}
                className={`px-5 py-2.5 rounded-xl text-sm font-semibold transition-all border ${
                  selected.includes(agent.id) ? 'bg-blue-500/15 border-blue-400 text-blue-300' : 'bg-white/[0.02] border-white/5 hover:border-white/30 text-slate-400'
                }`}
              >
                {agent.name}
              </button>
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

        {/* RESULTS */}
        {result && (
          <div className="space-y-6">
            {/* SCORE CARDS */}
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              {result.corporate_discrepancy && (
                <ScoreBox label="Corporate Discrepancy" score={result.corporate_discrepancy.score || 50} signal={(result.corporate_discrepancy.score || 50) >= 62 ? 'BEARISH' : (result.corporate_discrepancy.score || 50) < 39 ? 'BULLISH' : 'NEUTRAL'} />
              )}
              {result.promise_audit && (
                <ScoreBox label="Promise Audit" score={result.promise_audit.score || 50} signal={(result.promise_audit.score || 50) >= 62 ? 'BULLISH' : (result.promise_audit.score || 50) < 39 ? 'BEARISH' : 'NEUTRAL'} />
              )}
              {result.legal_proceedings && (
                <ScoreBox label="Legal Proceedings" score={result.legal_proceedings.agent3_score || 50} signal={(result.legal_proceedings.agent3_score || 50) >= 60 ? 'BULLISH' : (result.legal_proceedings.agent3_score || 50) <= 40 ? 'BEARISH' : 'NEUTRAL'} />
              )}
              {result.risk_factors && (
                <ScoreBox label="Risk Factors" score={result.risk_factors.score || 50} signal={(result.risk_factors.score || 50) >= 65 ? 'BEARISH' : (result.risk_factors.score || 50) <= 35 ? 'BULLISH' : 'NEUTRAL'} />
              )}
            </div>

            {/* AGENT 1 - Corporate Discrepancy */}
            {result.corporate_discrepancy && (
              <Card title="Corporate Discrepancy" subtitle={`Score: ${result.corporate_discrepancy.score ?? '—'} · ${result.corporate_discrepancy.total_aligned_pairs ?? 0} aligned passages`}>
                {(result.corporate_discrepancy.top_discrepancies || []).length === 0 && (
                  <p className="text-slate-600 text-sm">No material cross-document discrepancies surfaced.</p>
                )}
                {(result.corporate_discrepancy.top_discrepancies || []).slice(0, 5).map((d, i) => (
                  <Row key={i} badge={d.severity === 'high' ? 'RED' : d.severity === 'medium' ? 'YELLOW' : 'GREEN'} badgeText={d.severity} meta={d.pair_display || d.pair?.replace(/_/g, ' ')}>
                    {d.same_filing && <span className="inline-block mb-2 px-2 py-0.5 rounded text-[10px] font-bold bg-slate-500/10 text-slate-400">SAME FILING — DOWNRANKED</span>}
                    <p className="text-slate-400 text-xs mb-2">{d.reasoning}</p>
                    <Quote text={d.source_a?.sentence_text} url={d.source_a?.edgar_url || d.source_a?.htm_link} label={d.source_a?.document_type} />
                    <Quote text={d.source_b?.sentence_text} url={d.source_b?.edgar_url || d.source_b?.htm_link} label={d.source_b?.document_type} />
                  </Row>
                ))}
              </Card>
            )}

            {/* AGENT 2 - Promise Audit */}
            {result.promise_audit && (
              <Card title="Promise Audit" subtitle={`${result.promise_audit.total_promises_extracted || 0} promises · ${result.promise_audit.total_kept || 0} kept · ${result.promise_audit.total_broken || 0} broken`}>
                {(result.promise_audit.top_promises || []).map((p, i) => (
                  <Row key={i} badge={p.status} badgeText={p.status} meta={`${p.category} · severity ${p.severity}/10`}>
                    <Quote text={p.text} url={p.promise_url} label={p.promise_source || 'Promise'} />
                    <Quote text={p.evidence_text} url={p.evidence_url} label={p.evidence_filing || 'Evidence'} />
                    {p.reasoning && <p className="text-slate-500 text-xs mt-1">{p.reasoning}</p>}
                  </Row>
                ))}
              </Card>
            )}

            {/* AGENT 3 - Legal Proceedings */}
            {result.legal_proceedings && (
              <Card title="Legal Proceedings" subtitle={`${result.legal_proceedings.findings?.length || 0} filing types compared`}>
                {(result.legal_proceedings.findings || []).map((f, i) => (
                  <div key={i} className="mb-4">
                    <p className="text-slate-400 text-xs font-semibold mb-2">
                      {f.doc_type}{f.prior_date ? `: ${f.prior_date} → ${f.current_date}` : ''} · Score: {f.agent3_score}
                    </p>
                    {(!f.proceedings || f.proceedings.length === 0) ? (
                      <p className="text-slate-600 text-xs italic">{f.summary || `No legal proceedings in ${f.doc_type}`}</p>
                    ) : (
                      <>
                        {f.summary && <p className="text-slate-500 text-xs mb-2">{f.summary}</p>}
                        {f.proceedings.slice(0, 5).map((p, j) => (
                          <Row key={j} badge={p.status || p.change_type} badgeText={p.status || p.change_type} meta={p.case_name || p.description}>
                            <Quote text={p.prior_quote || p.prior_text} url={p.prior_edgar_url} label="Prior" />
                            <Quote text={p.current_quote || p.current_text} url={p.edgar_url} label="Current" />
                            {p.liability_change && <p className="text-slate-500 text-xs mt-1">Liability: {p.liability_change}</p>}
                          </Row>
                        ))}
                      </>
                    )}
                  </div>
                ))}
              </Card>
            )}

            {/* AGENT 4 - Risk Factors */}
            {result.risk_factors && (
              <Card title="Risk Factors" subtitle={`Item 1A delta · ${result.risk_factors.old_filing_date || '—'} → ${result.risk_factors.new_filing_date || '—'} · ${result.risk_factors.total_risks_old ?? 0}→${result.risk_factors.total_risks_new ?? 0} risks · ${result.risk_factors.changes_found || 0} changes`}>
                {(result.risk_factors.top_changes || []).length === 0 && (
                  <p className="text-slate-600 text-sm">{result.risk_factors.summary || 'No material changes in risk factors between the two 10-Ks.'}</p>
                )}
                {(result.risk_factors.top_changes || []).map((c, i) => (
                  <Row key={i} badge={c.category} badgeText={c.category} meta={c.risk_title}>
                    {c.summary && <p className="text-slate-400 text-xs mb-2">{c.summary}</p>}
                    <Quote text={c.old_quote} url={c.old_url} label="Old 10-K" />
                    <Quote text={c.new_quote} url={c.new_url} label="New 10-K" />
                    {c.verified === false && <p className="text-amber-400/70 text-[10px] mt-1">⚠ quote not verified verbatim</p>}
                  </Row>
                ))}
              </Card>
            )}

            {/* JSON EXPORT — clean, analyst-ready schema */}
            <JsonPanel data={exportData} ticker={result.ticker} />
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
  const download = () => {
    const blob = new Blob([text], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = `sollia_${(ticker || 'export').toLowerCase()}.json`; a.click()
    URL.revokeObjectURL(url)
  }
  const copy = () => { navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500) }
  return (
    <Card title="Export" subtitle="Clean, analyst-ready JSON — drop into Excel or a terminal">
      <div className="flex gap-3 mb-3">
        <button onClick={() => setView(v => !v)} className="px-5 py-2.5 rounded-xl text-sm text-slate-400 hover:text-white bg-white/[0.03] border border-white/10 transition-all font-medium">{view ? 'Hide' : 'View JSON'}</button>
        <button onClick={copy} className="px-5 py-2.5 rounded-xl text-sm text-slate-400 hover:text-white bg-white/[0.03] border border-white/10 transition-all font-medium">{copied ? 'Copied!' : 'Copy'}</button>
        <button onClick={download} className="px-5 py-2.5 rounded-xl text-sm text-slate-400 hover:text-white bg-white/[0.03] border border-white/10 transition-all font-medium">Download</button>
      </div>
      {view && (
        <pre className="bg-[#04060C] rounded-xl p-4 max-h-96 overflow-auto text-xs text-emerald-400/80 whitespace-pre-wrap border border-white/5 font-mono">{text}</pre>
      )}
    </Card>
  )
}

function ScoreBox({ label, score, signal }) {
  const colors = { BULLISH: '#22C55E', NEUTRAL: '#F59E0B', BEARISH: '#EF4444', HOLD: '#F59E0B' }
  const color = colors[signal] || '#F59E0B'
  return (
    <div className="bg-[#0A0B14] border border-white/5 rounded-2xl p-6 text-center">
      <div className="text-5xl font-black mb-2" style={{ color }}>{Math.round(score)}%</div>
      <div className="text-slate-400 text-sm font-medium">{label}</div>
      <div className="text-xs mt-1" style={{ color }}>{signal}</div>
    </div>
  )
}

function Card({ title, subtitle, children }) {
  return (
    <div className="bg-[#0A0B14] border border-white/5 rounded-2xl p-6">
      <h3 className="text-xl font-bold mb-1">{title}</h3>
      {subtitle && <p className="text-slate-500 text-sm mb-4">{subtitle}</p>}
      <div className="space-y-3">{children}</div>
    </div>
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

function Quote({ text, url, label }) {
  if (!text) return null
  const words = text.split(/\s+/)
  const shown = words.slice(0, 8).join(' ') + (words.length > 8 ? '…' : '')
  return (
    <div className="text-slate-400 text-sm flex items-center gap-2 flex-wrap">
      <span className="text-slate-600 text-[10px] uppercase font-semibold">{label || 'Source'}</span>
      <span className="italic">"{shown}"</span>
      {url && (
        <a href={url} target="_blank" rel="noopener noreferrer" className="text-blue-400 text-[10px] hover:underline">View ↗</a>
      )}
    </div>
  )
}
