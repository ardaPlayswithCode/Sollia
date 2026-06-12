import { useRef, useState } from 'react'
import { motion } from 'framer-motion'

const sevBadge = {
  'RED': 'badge-red',
  'YELLOW': 'badge-amber',
  'GREEN': 'badge-green',
}


const legalStatusConfig = {
  NEW:              { icon: '+', color: '#EF4444', bg: 'bg-red-500/10', border: 'border-red-500/20', text: 'text-red-400' },
  ESCALATED:        { icon: '↑', color: '#EF4444', bg: 'bg-red-500/10', border: 'border-red-500/20', text: 'text-red-400' },
  SETTLED:          { icon: '✓', color: '#22C55E', bg: 'bg-emerald-500/10', border: 'border-emerald-500/20', text: 'text-emerald-400' },
  DISMISSED:        { icon: '✓', color: '#22C55E', bg: 'bg-emerald-500/10', border: 'border-emerald-500/20', text: 'text-emerald-400' },
  UNCHANGED:        { icon: '=', color: '#6B7280', bg: 'bg-slate-500/10', border: 'border-slate-500/20', text: 'text-slate-400' },
  QUIETLY_REMOVED:  { icon: '⚠', color: '#F59E0B', bg: 'bg-amber-500/10', border: 'border-amber-500/20', text: 'text-amber-400' },
  NO_UPDATE:        { icon: '○', color: '#6B7280', bg: 'bg-slate-500/10', border: 'border-slate-500/20', text: 'text-slate-400' },
}
const statusConfig = {
  KEPT:       { icon: '✓', color: '#22C55E', bg: 'bg-emerald-500/10', border: 'border-emerald-500/20', text: 'text-emerald-400' },
  PARTIAL:    { icon: '◐', color: '#F59E0B', bg: 'bg-amber-500/10', border: 'border-amber-500/20', text: 'text-amber-400' },
  BROKEN:     { icon: '✗', color: '#EF4444', bg: 'bg-red-500/10', border: 'border-red-500/20', text: 'text-red-400' },
  PENDING:    { icon: '○', color: '#6B7280', bg: 'bg-slate-500/10', border: 'border-slate-500/20', text: 'text-slate-400' },
  UNRESOLVED: { icon: '?', color: '#6B7280', bg: 'bg-slate-500/10', border: 'border-slate-500/20', text: 'text-slate-400' },
  STALE:      { icon: '⏤', color: '#F97316', bg: 'bg-orange-500/10', border: 'border-orange-500/20', text: 'text-orange-400' },
}

function QuoteExcerpt({ text, url, docType }) {
  if (!text) return <span className="text-slate-700 italic">—</span>
  const words = text.split(' ').slice(0, 5).join(' ')
  const truncated = text.length > words.length ? `${words}...` : words
  return (
    <div className="text-slate-300 text-sm leading-relaxed">
      <span className="text-slate-500 mr-1 text-[11px] uppercase tracking-wider font-semibold">{docType}</span>
      <span className="italic">"{truncated}"</span>
      {url && (
        <a href={url} target="_blank" rel="noopener noreferrer" 
           className="text-blue-400 text-[11px] hover:underline ml-2 inline-block">
          View ↗
        </a>
      )}
    </div>
  )
}

export default function AgentCard({ title, label, conclusion, topSignals, type, index = 0 }) {
  const cardRef = useRef(null)
  const [tilt, setTilt] = useState({ x: 0, y: 0 })
  const isForensic = type === 'forensic' || type === 'corporate_discrepancy'
  const isPromise = type === 'promise_audit'
  const isLegal = type === 'legal_proceedings'
  const accent = isForensic ? 'from-blue-400 to-cyan-400' : isPromise ? 'from-emerald-400 to-teal-400' : 'from-purple-400 to-violet-400'

  const handleMouseMove = (e) => {
    if (!cardRef.current) return
    const rect = cardRef.current.getBoundingClientRect()
    setTilt({ x: ((e.clientY - rect.top) / rect.height - 0.5) * 6, y: ((e.clientX - rect.left) / rect.width - 0.5) * -6 })
  }

  return (
    <motion.div
      ref={cardRef}
      onMouseMove={handleMouseMove}
      onMouseLeave={() => setTilt({ x: 0, y: 0 })}
      animate={{ rotateX: tilt.x, rotateY: tilt.y }}
      transition={{ type: 'spring', stiffness: 200, damping: 20 }}
      initial={{ opacity: 0, y: 30 }}
      animate={{ opacity: 1, y: 0 }}
      style={{ transition: 'opacity 0.6s ease, transform 0.6s ease', transitionDelay: `${0.2 + index * 0.1}s` }}
      className="glass-card p-6 sm:p-8"
    >
      <div className={`h-1 w-16 rounded-full bg-gradient-to-r ${accent} mb-5`} />
      <h3 className="text-2xl font-bold mb-1 tracking-tight">{title}</h3>
      <p className="text-slate-400 text-sm mb-5 leading-relaxed">{label}</p>

      {topSignals?.length > 0 && (
        <div className="space-y-4">
          {topSignals.map((s, i) => (
            <motion.div
              key={i}
              initial={{ opacity: 0, x: -10 }}
              animate={{ opacity: 1, x: 0 }}
              style={{ transitionDelay: `${0.3 + i * 0.06}s` }}
              className="border border-white/[0.04] rounded-xl p-4 hover:bg-white/[0.02] transition-colors"
            >
              {isForensic ? (
                <>
                  <div className="flex items-center gap-3 mb-3">
                    <span className={`inline-block px-2.5 py-0.5 rounded-md text-[11px] font-bold ${sevBadge[s.severity] || 'badge-green'}`}>
                      {s.final_score || s.severity}
                    </span>
                    <span className="text-slate-400 text-xs uppercase tracking-wider font-medium">
                      {s.discrepancy_type || s.description?.split('] ')[0]?.replace('[', '')}
                    </span>
                    {s.pair && (
                      <span className="text-slate-600 text-[10px] ml-auto">{s.pair.replace(/_/g, ' ')}</span>
                    )}
                  </div>
                  <p className="text-slate-500 text-xs mb-3 leading-relaxed">{s.description}</p>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <QuoteExcerpt text={s.transcript_citation} url={s.transcript_url} docType={s.source_a_type || 'Source A'} />
                    <QuoteExcerpt text={s.filing_citation} url={s.htm_link} docType={s.source_b_type || 'Source B'} />
                  </div>
                </>
              ) : isPromise ? (
                <>
                  <div className="flex items-center gap-3 mb-3">
                    <span
                      className="inline-flex items-center justify-center w-7 h-7 rounded-lg text-sm font-bold"
                      style={{ backgroundColor: (statusConfig[s.status] || statusConfig.UNRESOLVED).bg, color: (statusConfig[s.status] || statusConfig.UNRESOLVED).color }}
                    >
                      {(statusConfig[s.status] || statusConfig.UNRESOLVED).icon}
                    </span>
                    <span
                      className="text-xs uppercase tracking-wider font-semibold px-2 py-0.5 rounded-md"
                      style={{ backgroundColor: (statusConfig[s.status] || statusConfig.UNRESOLVED).bg, color: (statusConfig[s.status] || statusConfig.UNRESOLVED).color }}
                    >
                      {s.status}
                    </span>
                    <span className="text-slate-500 text-[10px] ml-auto">
                      {s.category} · severity {s.severity}/10
                    </span>
                  </div>

                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-3">
                    <QuoteExcerpt text={s.text} url={s.promise_url} docType={s.promise_source || 'Promise'} />
                    <QuoteExcerpt text={s.evidence_text} url={s.evidence_url} docType={s.evidence_source || 'Evidence'} />
                  </div>

                  <p className="text-slate-500 text-xs leading-relaxed">{s.reasoning}</p>
                </>
              ) : isLegal ? (
                <>
                  <div className="flex items-center gap-3 mb-3">
                    <span
                      className="inline-flex items-center justify-center w-7 h-7 rounded-lg text-sm font-bold"
                      style={{ backgroundColor: (legalStatusConfig[s.status] || legalStatusConfig.UNCHANGED).bg, color: (legalStatusConfig[s.status] || legalStatusConfig.UNCHANGED).color }}
                    >
                      {(legalStatusConfig[s.status] || legalStatusConfig.UNCHANGED).icon}
                    </span>
                    <span
                      className="text-xs uppercase tracking-wider font-semibold px-2 py-0.5 rounded-md"
                      style={{ backgroundColor: (legalStatusConfig[s.status] || legalStatusConfig.UNCHANGED).bg, color: (legalStatusConfig[s.status] || legalStatusConfig.UNCHANGED).color }}
                    >
                      {s.status}
                    </span>
                    <span className="text-slate-500 text-[10px] ml-auto">{s.doc_type} · {s.case_name}</span>
                    {s.priority && (
                      <span className={`text-[10px] px-1.5 py-0.5 rounded ${s.priority === 'HIGH' ? 'bg-red-500/10 text-red-400' : 'bg-slate-500/10 text-slate-400'}`}>{s.priority}</span>
                    )}
                  </div>
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-3">
                    <QuoteExcerpt text={s.prior_quote} docType="Prior" />
                    <QuoteExcerpt text={s.current_quote} docType="Current" />
                  </div>
                  {s.liability_change && (
                    <p className="text-slate-500 text-xs mb-2 leading-relaxed">Liability: {s.liability_change}</p>
                  )}
                  {s.summary && (
                    <p className="text-slate-500 text-xs leading-relaxed">{s.summary}</p>
                  )}
                  {s.obfuscation_detected && (
                    <span className="inline-block mt-2 px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 text-amber-400 border border-amber-500/20">OBFUSCATION DETECTED</span>
                  )}
                </>
              ) : (
                <div className="text-slate-300 text-sm">
                  {s.url ? (
                    <a href={s.url} target="_blank" rel="noopener noreferrer" className="hover:text-white transition-colors">
                      {s.headline?.length > 100 ? s.headline.slice(0, 100) + '...' : s.headline}
                    </a>
                  ) : s.headline}
                  {s.source && <span className="block text-slate-600 text-[10px] mt-0.5">{s.source}</span>}
                </div>
              )}
            </motion.div>
          ))}
        </div>
      )}

      <p className="text-slate-600 text-sm mt-5 leading-relaxed">{conclusion}</p>
    </motion.div>
  )
}
