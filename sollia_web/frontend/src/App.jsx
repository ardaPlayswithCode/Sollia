import { useState, useEffect, useReducer, useCallback } from 'react'
import { motion, AnimatePresence, useScroll, useTransform } from 'framer-motion'
import Background3D from './components/Background3D'
import TickerInput from './components/TickerInput'
import AgentSelector from './components/AgentSelector'
import ScoreCard from './components/ScoreCard'
import AgentCard from './components/AgentCard'
import LogStream from './components/LogStream'
import JsonExport from './components/JsonExport'

const initialState = {
  loading: false,
  result: null,
  logs: [],
  error: null,
  activeTab: 'log'
}

function reducer(state, action) {
  switch (action.type) {
    case 'START':
      return { ...initialState, loading: true }
    case 'LOG':
      return { ...state, logs: [...state.logs, action.payload] }
    case 'RESULT':
      return { ...state, loading: false, result: action.payload }
    case 'ERROR':
      return { ...state, loading: false, error: action.payload }
    case 'SET_TAB':
      return { ...state, activeTab: action.payload }
    case 'RESET':
      return initialState
    default:
      return state
  }
}

const fadeUp = {
  hidden: { opacity: 0, y: 40 },
  show: (i) => ({
    opacity: 1,
    y: 0,
    transition: { delay: i * 0.1, duration: 0.7, ease: [0.22, 1, 0.36, 1] }
  })
}

export default function App() {
  const [agents, setAgents] = useState([])
  const [selected, setSelected] = useState(['forensic', 'narrative'])
  const [ticker, setTicker] = useState('')
  const [state, dispatch] = useReducer(reducer, initialState)

  const { scrollY } = useScroll()
  const headerOpacity = useTransform(scrollY, [0, 200], [1, 0.3])
  const headerScale = useTransform(scrollY, [0, 200], [1, 0.95])

  useEffect(() => {
    fetch('/api/agents')
      .then(r => r.json())
      .then(data => {
        setAgents(data.agents)
        setSelected(data.agents.map(a => a.id))
      })
      .catch(() => {
        setAgents([
          { id: 'forensic', name: 'CEO vs 10-K', description: 'CEO transcript vs 10-K analysis' },
          { id: 'narrative', name: 'News Sentiment', description: 'Financial news sentiment analysis' }
        ])
        setSelected(['forensic', 'narrative'])
      })
  }, [])

  useEffect(() => {
    const handleKey = (e) => {
      if (e.key === 'Enter' && ticker && selected.length > 0 && !state.loading) {
        runAnalysis(ticker)
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [ticker, selected, state.loading])

  const runAnalysis = useCallback(async (t) => {
    const sym = t || ticker
    if (!sym || selected.length === 0) return
    dispatch({ type: 'START' })
    try {
      const response = await fetch('/api/analyze', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker: sym.toUpperCase(), agents: selected })
      })
      if (!response.ok) throw new Error('Analysis failed')
      const data = await response.json()
      if (data.log) {
        data.log.forEach((l, i) => {
          setTimeout(() => dispatch({ type: 'LOG', payload: l }), i * 15)
        })
      }
      setTimeout(() => dispatch({ type: 'RESULT', payload: data }), (data.log?.length || 0) * 15 + 300)
    } catch (err) {
      dispatch({ type: 'ERROR', payload: err.message })
    }
  }, [ticker, selected])

  const toggleAgent = (id) => {
    setSelected(prev => prev.includes(id) ? prev.filter(a => a !== id) : [...prev, id])
  }

  return (
    <>
      <Background3D />
      <div className="aurora-layer" />

      <main className="relative z-10 min-h-screen px-4 sm:px-6 lg:px-8 py-6 max-w-7xl mx-auto">
        
        {/* Fixed header */}
        <motion.header
          style={{ opacity: headerOpacity, scale: headerScale }}
          className="sticky top-0 z-20 pt-2 pb-4"
        >
          <div className="glass-card p-5 flex flex-col lg:flex-row gap-4 items-stretch lg:items-end">
            <div className="flex-1">
              <label className="block text-xs text-slate-500 uppercase tracking-[0.2em] mb-2 font-medium">Ticker</label>
              <div className="flex gap-3 items-end">
                <input
                  type="text"
                  value={ticker}
                  onChange={e => setTicker(e.target.value.toUpperCase().slice(0, 5))}
                  placeholder="NVDA"
                  maxLength={5}
                  className="bg-[#06080D] border border-white/10 rounded-xl px-5 py-4 text-white text-3xl font-black tracking-[0.15em] placeholder-slate-700 transition-all w-full sm:w-44 text-center"
                />
                <motion.button
                  onClick={() => runAnalysis()}
                  disabled={state.loading || !ticker.trim() || selected.length === 0}
                  whileHover={state.loading ? {} : { scale: 1.03, boxShadow: '0 0 40px rgba(59,130,246,0.4)' }}
                  whileTap={state.loading ? {} : { scale: 0.97 }}
                  className={`px-8 py-4 rounded-xl font-bold text-base tracking-wide transition-all cursor-pointer ${
                    state.loading || !ticker.trim() || selected.length === 0
                      ? 'bg-white/5 text-slate-700 border border-white/5'
                      : 'bg-gradient-to-r from-blue-500 to-violet-500 text-white shadow-xl shadow-blue-500/25 border border-blue-400/30'
                  }`}
                >
                  {state.loading ? (
                    <span className="flex items-center gap-2">
                      <motion.span animate={{ rotate: 360 }} transition={{ repeat: Infinity, duration: 1, ease: 'linear' }} className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full inline-block" />
                      Running
                    </span>
                  ) : 'Analyze'}
                </motion.button>
              </div>
            </div>
            <div className="flex-1">
              <div className="flex items-center justify-between mb-2">
                <label className="text-xs text-slate-500 uppercase tracking-[0.2em] font-medium">Agents</label>
                <div className="flex gap-3">
                  <button onClick={() => setSelected(agents.map(a => a.id))} className="text-xs text-slate-600 hover:text-slate-400 transition-colors cursor-pointer font-medium">All</button>
                  <button onClick={() => setSelected([])} className="text-xs text-slate-600 hover:text-slate-400 transition-colors cursor-pointer font-medium">Clear</button>
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                {agents.map(agent => (
                  <motion.button
                    key={agent.id}
                    onClick={() => toggleAgent(agent.id)}
                    whileHover={{ scale: 1.04 }}
                    whileTap={{ scale: 0.96 }}
                    className={`relative px-5 py-2.5 rounded-xl text-sm font-semibold transition-all cursor-pointer border ${
                      selected.includes(agent.id)
                        ? 'bg-blue-500/15 border-blue-400/40 text-blue-300 shadow-lg shadow-blue-500/10'
                        : 'bg-white/[0.02] border-white/5 text-slate-600 hover:border-white/10 hover:text-slate-400'
                    }`}
                  >
                    {agent.name}
                    {selected.includes(agent.id) && (
                      <motion.span layoutId="check" className="absolute -top-1.5 -right-1.5 w-4 h-4 bg-blue-400 rounded-full flex items-center justify-center">
                        <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" /></svg>
                      </motion.span>
                    )}
                  </motion.button>
                ))}
              </div>
            </div>
          </div>
        </motion.header>

        {/* Error */}
        <AnimatePresence>
          {state.error && (
            <motion.div initial={{ opacity: 0, y: -10 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="mb-6">
              <div className="glass-card border-red-500/20 p-4 flex items-center gap-3">
                <span className="text-red-400">!</span>
                <p className="text-red-300 text-sm">{state.error}</p>
                <button onClick={() => dispatch({ type: 'RESET' })} className="ml-auto text-red-400 text-sm cursor-pointer">Dismiss</button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Loading skeleton */}
        <AnimatePresence>
          {state.loading && (
            <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="space-y-4 mb-8">
              <div className="glass-card p-8 text-center">
                <motion.div animate={{ rotate: 360 }} transition={{ repeat: Infinity, duration: 1.5, ease: 'linear' }} className="w-12 h-12 border-3 border-blue-400/20 border-t-blue-400 rounded-full mx-auto mb-6" />
                <p className="text-slate-500 text-lg font-medium">Running analysis agents...</p>
              </div>
              <div className="glass-card p-6 space-y-3 animate-pulse">
                {[...Array(8)].map((_, i) => <div key={i} className="h-3 bg-white/5 rounded-full" style={{width: `${40 + Math.random()*60}%`}} />)}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Results */}
        <AnimatePresence>
          {state.result && !state.loading && (
            <div className="space-y-8 pb-16">
              
              {/* Score section */}
              <motion.section custom={0} variants={fadeUp} initial="hidden" animate="show">
                <ScoreCard result={state.result} />
              </motion.section>

              {/* Agent cards */}
              <motion.section custom={1} variants={fadeUp} initial="hidden" animate="show" className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                {state.result.forensic && <AgentCard title="CEO vs 10-K" label={state.result.forensic.discrepancy_label} conclusion={state.result.forensic.conclusion} topSignals={state.result.forensic.top_signals} type="forensic" index={0} />}
                {state.result.narrative && <AgentCard title="News Sentiment" label={state.result.narrative.sentiment_label} conclusion={state.result.narrative.conclusion} topSignals={state.result.narrative.top_signals} type="narrative" index={1} />}
                {state.result.follow_through && <AgentCard title="Management Follow-Through" label={`Score: ${state.result.follow_through.score}% — ${state.result.follow_through.promises?.length || 0} promises tracked`} conclusion={state.result.follow_through.conclusion || 'Top promises from company history'} topSignals={(state.result.follow_through.promises || []).map(p => ({
                  severity: p.status === 'EVIDENCE_FOUND' ? 'GREEN' : 'RED',
                  severity_color: p.status === 'EVIDENCE_FOUND' ? '#22C55E' : '#EF4444',
                  description: `[${p.status}] ${p.text?.slice(0, 100)}...`,
                  transcript_citation: p.evidence?.slice(0, 100) || 'No evidence found',
                  filing_citation: `Severity: ${p.severity}/10 | ${p.source || 'unknown'} | ${p.date || ''}`,
                  transcript_url: '',
                  htm_link: ''
                }))} type="follow_through" index={2} />}
              </motion.section>

              {/* Log & JSON tabs */}
              <motion.section custom={2} variants={fadeUp} initial="hidden" animate="show" className="glass-card overflow-hidden">
                <div className="flex border-b border-white/5">
                  {[
                    { id: 'log', label: 'Agent Log' },
                    { id: 'json', label: 'JSON Export' }
                  ].map(tab => (
                    <button key={tab.id} onClick={() => dispatch({ type: 'SET_TAB', payload: tab.id })}
                      className={`relative px-6 py-4 text-sm font-semibold transition-all cursor-pointer ${
                        state.activeTab === tab.id ? 'text-white' : 'text-slate-600 hover:text-slate-400'
                      }`}>
                      {tab.label}
                      {state.activeTab === tab.id && (
                        <motion.div layoutId="tab" className="absolute bottom-0 left-0 right-0 h-0.5 bg-gradient-to-r from-blue-400 to-violet-400" />
                      )}
                    </button>
                  ))}
                </div>
                <motion.div key={state.activeTab} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="p-6">
                  {state.activeTab === 'log' && <LogStream logs={state.logs} />}
                  {state.activeTab === 'json' && <JsonExport data={state.result} ticker={state.result.ticker} />}
                </motion.div>
              </motion.section>
            </div>
          )}
        </AnimatePresence>

        {/* Empty state */}
        {!state.result && !state.loading && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.3 }} className="text-center py-20">
            <p className="text-slate-700 text-lg font-medium">Enter a ticker and select agents to begin</p>
            <p className="text-slate-800 text-sm mt-2">or press <kbd className="px-2 py-0.5 bg-white/5 rounded text-slate-600 text-xs border border-white/5">Enter</kbd></p>
          </motion.div>
        )}
      </main>
    </>
  )
}
