import { useState, useReducer, useEffect, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import Background3D from './components/Background3D';
import TickerInput from './components/TickerInput';
import AgentSelector from './components/AgentSelector';
import ScoreCard from './components/ScoreCard';
import AgentCard from './components/AgentCard';
import LogStream from './components/LogStream';
import JsonExport from './components/JsonExport';

const API_BASE = 'http://localhost:8000';

const initialState = {
  loading: false,
  result: null,
  logs: [],
  error: null,
  activeTab: 'log'
};

function reducer(state, action) {
  switch (action.type) {
    case 'START': return { ...initialState, loading: true };
    case 'LOG': return { ...state, logs: [...state.logs, action.payload] };
    case 'RESULT': return { ...state, loading: false, result: action.payload };
    case 'ERROR': return { ...state, loading: false, error: action.payload };
    case 'SET_TAB': return { ...state, activeTab: action.payload };
    case 'RESET': return initialState;
    default: return state;
  }
}

const fadeUp = {
  hidden: { opacity: 0, y: 40 },
  show: (i) => ({
    opacity: 1,
    y: 0,
    transition: { delay: i * 0.1, duration: 0.7, ease: [0.22, 1, 0.36, 1] }
  })
};

export default function App() {
  const [agents, setAgents] = useState([
    { id: 'corporate_discrepancy', name: 'Corporate Discrepancy', description: 'Cross-document contradiction finder across 5 SEC filings' },
    { id: 'promise_audit', name: 'Promise Audit', description: 'Tracks management promises and verifies follow-through' },
    { id: 'legal_proceedings', name: 'Legal Proceedings', description: 'Tracks legal proceeding changes across 10-K, 10-Q, and 8-K filings' }
  ]);
  const [selected, setSelected] = useState(['corporate_discrepancy', 'promise_audit', 'legal_proceedings']);
  const [ticker, setTicker] = useState('');
  const [year, setYear] = useState(2023);
  const [state, dispatch] = useReducer(reducer, initialState);

  useEffect(() => {
    fetch(`${API_BASE}/api/agents`)
      .then(r => r.json())
      .then(data => {
        if (data.agents && data.agents.length > 0) {
          setAgents(data.agents);
          setSelected(data.agents.map(a => a.id));
        }
      })
      .catch(() => {
        // Keep default hardcoded agents on error — no change needed
      });
  }, []);  // Only run once on mount

  useEffect(() => {
    const handleEnter = (e) => {
      if (e.key === 'Enter' && ticker.trim() && selected.length > 0 && !state.loading) {
        runAnalysis();
      }
    };
    window.addEventListener('keydown', handleEnter);
    return () => window.removeEventListener('keydown', handleEnter);
  }, [ticker, selected, state.loading]);

  const runAnalysis = useCallback(async () => {
    if (!ticker || selected.length === 0) return;
dispatch({ type: 'START' }); dispatch({ type: 'ERROR', payload: null });
    try {
      const response = await fetch(`${API_BASE}/api/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ticker: ticker.toUpperCase(), agents: selected, year: year })
      });
      if (!response.ok) throw new Error('Analysis failed');
      const data = await response.json();
      if (data.log) {
        data.log.forEach((l, i) => {
          setTimeout(() => dispatch({ type: 'LOG', payload: l }), i * 15);
        });
      }
      setTimeout(() => { dispatch({ type: 'RESULT', payload: data }); }, (data.log?.length || 0) * 15 + 300);
    } catch (err) {
      dispatch({ type: 'ERROR', payload: err.message });
    }
  }, [ticker, selected]);

  const toggleAgent = (id) => {
    setSelected(prev => prev.includes(id) ? prev.filter(a => a !== id) : [...prev, id]);
    dispatch({ type: 'ERROR', payload: null });
  };

  return (
    <>
      <Background3D />
      <div className="aurora-layer" />
      <main className="relative z-10 min-h-screen px-4 sm:px-6 lg:px-8 py-6 max-w-7xl mx-auto">
        <motion.header style={{ opacity: 1 }} className="sticky top-0 z-20 pt-2 pb-4">
          <div className="glass-card p-5 flex flex-col lg:flex-row gap-4 items-stretch lg:items-end">
            <div className="flex-1">
              <label className="block text-xs text-slate-500 uppercase tracking-[0.2em] mb-2 font-medium">Ticker <span className="text-slate-700">· Year</span></label>
              <div className="flex gap-3 items-end">
                <input
                  type="text"
                  value={ticker}
                  onChange={e => setTicker(e.target.value.toUpperCase().slice(0, 5))}
                  placeholder="NVDA"
                  maxLength={5}
                  className="bg-[#06080D] border border-white/10 rounded-xl px-5 py-4 text-white text-3xl font-black tracking-[0.15em] placeholder-slate-700 transition-all w-full sm:w-44 text-center"
                />
                {selected.includes('corporate_discrepancy') && (
                  <>
                    <span className="text-xs text-slate-500 uppercase tracking-[0.2em] mt-2 block sm:hidden">Year</span>
                    <input
                      type="number"
                      value={year}
                      onChange={e => setYear(parseInt(e.target.value) || 2023)}
                      min={2016}
                      max={2026}
                      className="bg-[#06080D] border border-white/10 rounded-xl px-4 py-4 text-white text-xl font-bold placeholder-slate-700 transition-all w-full sm:w-24 text-center"
                    />
                  </>
                )}
                <motion.button
                  onClick={runAnalysis}
                  disabled={state.loading || !ticker.trim() || selected.length === 0}
                  whileHover={state.loading ? {} : { scale: 1.03, boxShadow: '0 0 40px rgba(59,130,246,0.4)' }}
                  whileTap={state.loading ? {} : { scale: 0.97 }}
                  className="px-8 py-4 rounded-xl font-bold text-base tracking-wide transition-all cursor-pointer bg-gradient-to-r from-blue-500 to-violet-500 text-white disabled:bg-white/5 disabled:text-slate-700"
                >
                  {state.loading ? "Running..." : "Analyze"}
                </motion.button>
              </div>
            </div>
            <div className="flex-1">
              <label className="block text-xs text-slate-500 uppercase tracking-[0.2em] mb-2 font-medium">Agents</label>
              <div className="flex flex-wrap gap-2">
                {agents.map(agent => (
                  <motion.button
                    key={agent.id}
                    onClick={() => toggleAgent(agent.id)}
                    whileHover={{ scale: 1.04 }}
                    whileTap={{ scale: 0.96 }}
                    className={`px-5 py-2.5 rounded-xl text-sm font-semibold transition-all cursor-pointer border ${
                      selected.includes(agent.id)
                        ? 'bg-blue-500/15 border-blue-400 text-blue-300'
                        : 'bg-white/[0.02] border-white/5 hover:border-white/30 hover:text-slate-300'
                    }`}
                  >
                    {agent.name}
                  </motion.button>
                ))}
              </div>
            </div>
          </div>
        </motion.header>

        <AnimatePresence>
          {state.result && !state.loading && (
            <div className="space-y-8 pb-16">
              {/* Score cards: one per agent, no merged score */}
              <motion.section custom={0} variants={fadeUp} initial="hidden" animate="show" className="grid grid-cols-1 md:grid-cols-2 gap-6">
                {state.result.corporate_discrepancy && (
                  <ScoreCard
                    score={state.result.corporate_discrepancy.final_score || state.result.corporate_discrepancy.score || 0}
                    label="Corporate Discrepancy"
                    signal_color={state.result.corporate_discrepancy.signal_color || (state.result.corporate_discrepancy.score >= 70 ? "#EF4444" : state.result.corporate_discrepancy.score >= 50 ? "#F59E0B" : "#22C55E")}
                    signal={state.result.corporate_discrepancy.signal || (state.result.corporate_discrepancy.score >= 70 ? 'BEARISH' : state.result.corporate_discrepancy.score >= 50 ? 'NEUTRAL' : 'BULLISH')}
                  />
                )}
                {state.result.promise_audit && (
                  <ScoreCard
                    score={state.result.promise_audit.score || 50}
                    label="Promise Audit"
                    signal_color={state.result.promise_audit.score >= 70 ? "#22C55E" : state.result.promise_audit.score >= 40 ? "#F59E0B" : "#EF4444"}
                    signal={state.result.promise_audit.score >= 70 ? 'BULLISH' : state.result.promise_audit.score >= 40 ? 'NEUTRAL' : 'BEARISH'}
                  />
                )}
                {state.result.legal_proceedings && (
                  <ScoreCard
                    score={state.result.legal_proceedings.agent3_score || 50}
                    label="Legal Proceedings"
                    signal_color={state.result.legal_proceedings.agent3_score >= 60 ? "#22C55E" : state.result.legal_proceedings.agent3_score <= 40 ? "#EF4444" : "#F59E0B"}
                    signal={state.result.legal_proceedings.agent3_score >= 60 ? 'BULLISH' : state.result.legal_proceedings.agent3_score <= 40 ? 'BEARISH' : 'NEUTRAL'}
                  />
                )}
              </motion.section>

              {/* Agent cards */}
              <motion.section custom={1} variants={fadeUp} initial="hidden" animate="show" className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                {state.result.corporate_discrepancy && (
                  <AgentCard
                    title="Corporate Discrepancy"
                    label={`Score: ${state.result.corporate_discrepancy.final_score || state.result.corporate_discrepancy.score || '—'}`}
                    conclusion={state.result.corporate_discrepancy.conclusion || 'Cross-document analysis complete'}
                    topSignals={(state.result.corporate_discrepancy.top_discrepancies || []).map(d => ({
                      severity: d.severity === 'high' ? 'RED' : d.severity === 'medium' ? 'YELLOW' : 'GREEN',
                      final_score: d.final_score,
                      discrepancy_type: d.discrepancy_type,
                      pair: d.pair,
                      description: d.reasoning,
                      transcript_citation: d.source_a?.sentence_text || '',
                      filing_citation: d.source_b?.sentence_text || '',
                      source_a_type: d.source_a?.document_type || 'Source A',
                      source_b_type: d.source_b?.document_type || 'Source B',
                      transcript_url: d.source_a?.edgar_url || '',
                      htm_link: d.source_b?.edgar_url || ''
                    }))}
                    type="corporate_discrepancy"
                    index={0}
                  />
                )}
                
                {state.result.promise_audit && (
                  <AgentCard
                    title="Promise Audit"
                    label={`${state.result.promise_audit.total_promises_extracted || 0} promises · ${state.result.promise_audit.total_kept || 0} kept · ${state.result.promise_audit.total_broken || 0} broken`}
                    conclusion={`Credibility score: ${state.result.promise_audit.score || 50}%`}
                    topSignals={(state.result.promise_audit.top_promises || []).map(p => ({
                      status: p.status || 'UNRESOLVED',
                      category: p.category || 'other',
                      severity: p.severity || 5,
                      text: p.text || '',
                      evidence_text: p.evidence_text || '',
                      promise_url: p.promise_url || '',
                      evidence_url: p.evidence_url || '',
                      promise_source: p.promise_source || 'Filing',
                      evidence_source: p.evidence_filing || '',
                      reasoning: p.reasoning || ''
                    }))}
                    type="promise_audit"
                    index={1}
                  />
                )}
                
                {state.result.legal_proceedings && (
                  <AgentCard
                    title="Legal Proceedings"
                    label={`${state.result.legal_proceedings.findings?.length || 0} filing types compared`}
                    conclusion={`Legal score: ${state.result.legal_proceedings.agent3_score || 50}%`}
                    topSignals={(state.result.legal_proceedings.findings || []).flatMap(f => 
                      (f.proceedings || []).map(p => ({
                        status: p.status || p.change_type || 'UNCHANGED',
                        case_name: p.case_name || '',
                        doc_type: f.doc_type || '',
                        prior_quote: p.prior_quote || p.prior_text || '',
                        current_quote: p.current_quote || p.current_text || '',
                        liability_change: p.liability_change || '',
                        summary: f.summary || '',
                        obfuscation_detected: p.obfuscation_detected || false,
                        priority: p.priority || 'MEDIUM'
                      }))
                    )}
                    type="legal_proceedings"
                    index={2}
                  />
                )}
              </motion.section>

              {/* Log / JSON tabs */}
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

        {!state.result && !state.loading && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.3 }} className="text-center py-20">
            {state.error ? <p className="text-red-400 text-lg font-medium">Error: {state.error}</p> : <p className="text-slate-700 text-lg font-medium">Enter a ticker and select agents to begin</p>}
            <p className="text-slate-800 text-sm mt-2">or press Enter</p>
          </motion.div>
        )}
      </main>
    </>
  );
}
