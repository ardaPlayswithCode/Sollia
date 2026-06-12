import { useState, useEffect } from 'react'

const API_BASE = 'http://localhost:8000'

const defaultAgents = [
  { id: 'corporate_discrepancy', name: 'Corporate Discrepancy' },
  { id: 'promise_audit', name: 'Promise Audit' },
  { id: 'legal_proceedings', name: 'Legal Proceedings' },
]

export default function App() {
  const [ticker, setTicker] = useState('F')
  const [year, setYear] = useState(2025)
  const [selected, setSelected] = useState(['legal_proceedings'])
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [log, setLog] = useState([])

  function toggleAgent(id) {
    setSelected(prev => prev.includes(id) ? prev.filter(a => a !== id) : [...prev, id])
    setResult(null)
    setError(null)
  }

  async function runAnalysis() {
    if (!ticker.trim() || selected.length === 0 || loading) return
    setLoading(true)
    setError(null)
    setResult(null)
    setLog([])
    try {
      const res = await fetch(`${API_BASE}/api/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker: ticker.toUpperCase(), agents: selected, year })
      })
      if (!res.ok) throw new Error('Server error: ' + res.status)
      const data = await res.json()
      setResult(data)
      if (data.log) {
        data.log.forEach((l, i) => setTimeout(() => setLog(prev => [...prev, l]), i * 30))
      }
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  // Keyboard shortcut
  useEffect(() => {
    function onKey(e) {
      if (e.key === 'Enter' && ticker.trim() && selected.length > 0 && !loading) runAnalysis()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [ticker, selected, loading])

  const showYear = selected.includes('corporate_discrepancy')

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
        {!result && !loading && !error && (
          <div className="text-center py-20">
            <p className="text-slate-600 text-lg">Enter a ticker, select agents, and click Analyze</p>
            <p className="text-slate-800 text-sm mt-2">or press Enter</p>
          </div>
        )}

        {/* RESULTS */}
        {result && (
          <div className="space-y-6">
            {/* SCORE CARDS */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              {result.corporate_discrepancy && (
                <ScoreBox label="Corporate Discrepancy" score={result.corporate_discrepancy.final_score || result.corporate_discrepancy.score || 50} signal={result.corporate_discrepancy.score >= 70 ? 'BEARISH' : result.corporate_discrepancy.score >= 50 ? 'NEUTRAL' : 'BULLISH'} />
              )}
              {result.promise_audit && (
                <ScoreBox label="Promise Audit" score={result.promise_audit.score || 50} signal={result.promise_audit.score >= 70 ? 'BULLISH' : result.promise_audit.score >= 40 ? 'NEUTRAL' : 'BEARISH'} />
              )}
              {result.legal_proceedings && (
                <ScoreBox label="Legal Proceedings" score={result.legal_proceedings.agent3_score || 50} signal={result.legal_proceedings.agent3_score >= 60 ? 'BULLISH' : result.legal_proceedings.agent3_score <= 40 ? 'BEARISH' : 'NEUTRAL'} />
              )}
            </div>

            {/* AGENT 1 - Corporate Discrepancy */}
            {result.corporate_discrepancy && (
              <Card title="Corporate Discrepancy" subtitle={`Score: ${result.corporate_discrepancy.final_score || result.corporate_discrepancy.score || '—'}`}>
                {(result.corporate_discrepancy.top_discrepancies || []).slice(0, 5).map((d, i) => (
                  <Row key={i} badge={d.severity === 'high' ? 'RED' : d.severity === 'medium' ? 'YELLOW' : 'GREEN'} badgeText={d.severity} meta={d.pair?.replace(/_/g, ' ')}>
                    <p className="text-slate-400 text-xs mb-2">{d.reasoning}</p>
                    <Quote text={d.source_a?.sentence_text} url={d.source_a?.htm_link || d.source_a?.edgar_url} label={d.source_a?.document_type} />
                    <Quote text={d.source_b?.sentence_text} url={d.source_b?.htm_link || d.source_b?.edgar_url} label={d.source_b?.document_type} />
                  </Row>
                ))}
              </Card>
            )}

            {/* AGENT 2 - Promise Audit */}
            {result.promise_audit && (
              <Card title="Promise Audit" subtitle={`${result.promise_audit.total_promises_extracted || 0} promises · ${result.promise_audit.total_kept || 0} kept · ${result.promise_audit.total_broken || 0} broken`}>
                {(result.promise_audit.top_promises || []).slice(0, 5).map((p, i) => (
                  <Row key={i} badge={p.status} badgeText={p.status} meta={`${p.category} · severity ${p.severity}/10`}>
                    <Quote text={p.text} url={p.promise_url} label={p.promise_source || 'Promise'} />
                    <Quote text={p.evidence_text} url={p.evidence_url} label={p.evidence_filing || 'Evidence'} />
                    <p className="text-slate-500 text-xs mt-1">{p.reasoning}</p>
                  </Row>
                ))}
              </Card>
            )}

            {/* AGENT 3 - Legal Proceedings */}
            {result.legal_proceedings && (
              <Card title="Legal Proceedings" subtitle={`${result.legal_proceedings.findings?.length || 0} filing types compared`}>
                {(result.legal_proceedings.findings || []).map((f, i) => (
                  <div key={i} className="mb-4">
                    <p className="text-slate-400 text-xs font-semibold mb-2">{f.doc_type}: {f.prior_date} → {f.current_date} | Score: {f.agent3_score}</p>
                    <p className="text-slate-500 text-xs mb-2">{f.summary}</p>
                    {(f.proceedings || []).slice(0, 5).map((p, j) => (
                      <Row key={j} badge={p.status || p.change_type} badgeText={p.status || p.change_type} meta={p.case_name}>
                        <Quote text={p.prior_quote || p.prior_text} label="Prior" />
                        <Quote text={p.current_quote || p.current_text} label="Current" url={p.edgar_url} />
                        {p.liability_change && <p className="text-slate-500 text-xs">Liability: {p.liability_change}</p>}
                        {p.obfuscation_detected && <span className="text-amber-400 text-[10px] font-bold">⚠ OBFUSCATION</span>}
                      </Row>
                    ))}
                  </div>
                ))}
              </Card>
            )}

            {/* LOG */}
            {log.length > 0 && (
              <Card title="Agent Log">
                <div className="max-h-48 overflow-y-auto space-y-1">
                  {log.map((l, i) => (
                    <p key={i} className="text-slate-600 text-xs font-mono">
                      <span className="text-slate-500">[{l.speaker}]</span> {l.message}
                    </p>
                  ))}
                </div>
              </Card>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// Simple components
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
    PENDING: 'bg-slate-500/10 text-slate-400', UNRESOLVED: 'bg-slate-500/10 text-slate-400',
    NEW: 'bg-red-500/10 text-red-400', ESCALATED: 'bg-red-500/10 text-red-400', SETTLED: 'bg-emerald-500/10 text-emerald-400',
    DISMISSED: 'bg-emerald-500/10 text-emerald-400', UNCHANGED: 'bg-slate-500/10 text-slate-400', QUIETLY_REMOVED: 'bg-amber-500/10 text-amber-400',
  }
  return (
    <div className="border border-white/[0.04] rounded-xl p-4">
      <div className="flex items-center gap-2 mb-2">
        {badge && <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${colors[badge] || 'bg-slate-500/10 text-slate-400'}`}>{badgeText || badge}</span>}
        {meta && <span className="text-slate-600 text-[10px] ml-auto">{meta}</span>}
      </div>
      {children}
    </div>
  )
}

function Quote({ text, url, label }) {
  if (!text) return null
  const words = text.split(' ').slice(0, 5).join(' ') + '...'
  return (
    <div className="text-slate-400 text-sm flex items-center gap-2">
      <span className="text-slate-600 text-[10px] uppercase font-semibold">{label || 'Source'}</span>
      <span className="italic">"{words}"</span>
      {url && (
        <a href={url} target="_blank" rel="noopener noreferrer" className="text-blue-400 text-[10px] hover:underline">View ↗</a>
      )}
    </div>
  )
}
