import { useRef, useState, useEffect } from 'react'
import { motion, useSpring, useTransform } from 'framer-motion'

const glowColors = {
  '#22C55E': { ring: 'rgba(34,197,94,0.25)', text: 'from-emerald-300 via-green-400 to-emerald-300', badge: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20' },
  '#F59E0B': { ring: 'rgba(245,158,11,0.25)', text: 'from-amber-300 via-yellow-400 to-amber-300', badge: 'bg-amber-500/10 text-amber-400 border-amber-500/20' },
  '#EF4444': { ring: 'rgba(239,68,68,0.25)', text: 'from-red-300 via-rose-400 to-red-300', badge: 'bg-red-500/10 text-red-400 border-red-500/20' },
}

function AnimatedNumber({ value, duration = 2 }) {
  const spring = useSpring(0, { stiffness: 80, damping: 20, duration: duration * 1000 })
  const [display, setDisplay] = useState(0)
  
  useEffect(() => {
    spring.set(value)
    const unsub = spring.on('change', v => setDisplay(Math.round(v)))
    return unsub
  }, [value, spring])

  return <span>{display}</span>
}

export default function ScoreCard({ result, score, label, signal, signal_color }) {
  const cardRef = useRef(null)
  const [tilt, setTilt] = useState({ x: 0, y: 0 })
  const colors = glowColors[signal_color || "#F59E0B"] || glowColors['#F59E0B']

  const handleMouseMove = (e) => {
    if (!cardRef.current) return
    const rect = cardRef.current.getBoundingClientRect()
    const x = (e.clientX - rect.left) / rect.width - 0.5
    const y = (e.clientY - rect.top) / rect.height - 0.5
    setTilt({ x: y * 8, y: x * -8 })
  }

  const handleMouseLeave = () => setTilt({ x: 0, y: 0 })

  return (
    <motion.div
      ref={cardRef}
      onMouseMove={handleMouseMove}
      onMouseLeave={handleMouseLeave}
      animate={{ rotateX: tilt.x, rotateY: tilt.y }}
      transition={{ type: 'spring', stiffness: 200, damping: 20 }}
      style={{ transformStyle: 'preserve-3d' }}
      className="glass-card p-10 sm:p-16 text-center relative overflow-hidden cursor-default"
    >
      {/* Glow ring */}
      <div
        className="absolute inset-0 rounded-3xl opacity-30"
        style={{
          background: `radial-gradient(ellipse at center, ${colors.ring} 0%, transparent 70%)`,
          transform: 'translateZ(-20px)',
        }}
      />

      {/* Pulsating outer ring */}
      <motion.div
        animate={{ opacity: [0.15, 0.3, 0.15], scale: [1, 1.02, 1] }}
        transition={{ repeat: Infinity, duration: 3, ease: 'easeInOut' }}
        className="absolute inset-0 rounded-3xl"
        style={{
          boxShadow: `0 0 120px ${colors.ring}, 0 0 240px ${colors.ring}`,
          transform: 'translateZ(-10px)',
        }}
      />

      {/* Score */}
      <motion.div
        initial={{ scale: 0.5, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ type: 'spring', stiffness: 120, damping: 14, delay: 0.1 }}
        style={{ transform: 'translateZ(30px)' }}
      >
        <span className={`text-8xl sm:text-9xl lg:text-[10rem] font-black tracking-tighter bg-gradient-to-r ${colors.text} bg-clip-text text-transparent`}>
          <AnimatedNumber value={Math.round(score || 0)} />
          <span className="text-5xl sm:text-6xl">%</span>
        </span>
      </motion.div>

      {/* Signal badge */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.4 }}
        style={{ transform: 'translateZ(20px)' }}
        className="mt-4"
      >
        <span className={`inline-block px-6 py-2 rounded-full text-xl font-bold tracking-wide border ${colors.badge}`}>
          {result.signal}
        </span>
      </motion.div>

      {/* Company */}
      <motion.p
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 0.6 }}
        style={{ transform: 'translateZ(10px)' }}
        className="text-slate-400 mt-4 text-xl font-medium"
      >
        {result.company}
      </motion.p>

      {/* Summary */}
      <motion.p
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 0.8 }}
        style={{ transform: 'translateZ(10px)' }}
        className="text-slate-500 mt-6 max-w-2xl mx-auto text-base leading-relaxed"
      >
        {result.summary}
      </motion.p>

      {result.agents_run?.length === 1 && (
        <motion.p initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="text-slate-700 text-sm mt-3 tracking-wide">
          Single Agent Mode
        </motion.p>
      )}
    </motion.div>
  )
}
