import { useRef, useState } from 'react'
import { motion } from 'framer-motion'

const sevBadge = {
  'RED': 'badge-red',
  'YELLOW': 'badge-amber',
  'GREEN': 'badge-green',
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
      transition={{ delay: 0.2 + index * 0.1, duration: 0.6 }}
      className="glass-card p-6 sm:p-8"
    >
      {/* Accent line */}
      <div className={`h-1 w-16 rounded-full bg-gradient-to-r ${accent} mb-5`} />

      <h3 className="text-2xl font-bold mb-3 tracking-tight">{title}</h3>
      <p className="text-slate-400 text-base mb-6 leading-relaxed">{label}</p>

      {topSignals?.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="text-xs text-slate-600 uppercase tracking-wider border-b border-white/5">
                <th className="text-left py-3 px-2 font-semibold">Score</th>
                <th className="text-left py-3 px-2 font-semibold">Finding</th>
                {isForensic ? (
                  <>
                    <th className="text-left py-3 px-2 font-semibold">Transcript</th>
                    <th className="text-left py-3 px-2 font-semibold">10-K Filing</th>
                  </>
                ) : (
                  <th className="text-left py-3 px-2 font-semibold">Headline</th>
                )}
              </tr>
            </thead>
            <tbody>
              {topSignals.map((s, i) => (
                <motion.tr
                  key={i}
                  initial={{ opacity: 0, x: -10 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: 0.3 + i * 0.06 }}
                  className="border-b border-white/[0.02] hover:bg-white/[0.02] transition-colors"
                >
                  <td className="py-3 px-2">
                    <span className={`inline-block px-2 py-0.5 rounded-md text-[11px] font-bold ${sevBadge[s.severity] || 'badge-green'}`}>
                      {s.final_score || s.severity}
                    </span>
                  </td>
                  <td className="py-3 px-2 text-slate-300 text-sm">{s.description}</td>
                  {isForensic ? (
                    <>
                      <td className="py-3 px-2 text-slate-500 text-xs max-w-[160px]">
                        <span className="block truncate">{s.transcript_citation}</span>
                        {s.transcript_url && <a href={s.transcript_url} target="_blank" className="text-blue-400 text-[11px] hover:underline mt-0.5 inline-block">View /</a>}
                      </td>
                      <td className="py-3 px-2 text-slate-500 text-xs max-w-[160px]">
                        <span className="block truncate">{s.filing_citation}</span>
                        {s.htm_link && <a href={s.htm_link} target="_blank" className="text-blue-400 text-[11px] hover:underline mt-0.5 inline-block">View /</a>}
                      </td>
                    </>
                  ) : (
                    <td className="py-3 px-2 text-slate-300 text-sm max-w-[300px]">
                      {s.url ? (
                        <a href={s.url} target="_blank" className="hover:text-white transition-colors">{s.headline?.length > 100 ? s.headline.slice(0, 100) + '...' : s.headline}</a>
                      ) : s.headline}
                      {s.source && <span className="block text-slate-600 text-[10px] mt-0.5">{s.source}</span>}
                    </td>
                  )}
                </motion.tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="text-slate-600 text-sm mt-5 leading-relaxed">{conclusion}</p>
    </motion.div>
  )
}
