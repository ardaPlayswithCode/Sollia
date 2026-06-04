import { useRef, useState } from 'react'
import { motion } from 'framer-motion'

const sevBadge = {
  'RED': 'badge-red',
  'YELLOW': 'badge-amber',
  'GREEN': 'badge-green',
}

function QuoteExcerpt({ text, url, docType }) {
  if (!text) return <span className="text-slate-700 italic">—</span>
  const words = text.split(' ').slice(0, 8).join(' ')
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
  const accent = isForensic ? 'from-blue-400 to-cyan-400' : 'from-emerald-400 to-teal-400'

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

              {isForensic ? (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <QuoteExcerpt text={s.transcript_citation} url={s.transcript_url} docType={s.source_a_type || 'Source A'} />
                  <QuoteExcerpt text={s.filing_citation} url={s.htm_link} docType={s.source_b_type || 'Source B'} />
                </div>
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
