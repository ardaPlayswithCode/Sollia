// Sollia UI effects (production polish Part 7) — Aceternity/Magic-UI style
// components implemented locally (those libraries are copy-paste, not npm).
// Professional constraints honored throughout: max 400ms animations, CSS
// transform/opacity only (60fps), prefers-reduced-motion disables everything,
// dark slate palette, no particles/sound/video.
import { useEffect, useRef, useState } from 'react'
import { motion, useReducedMotion, animate } from 'framer-motion'

// ── severity → glow tone (GlowingEffect equivalent) ─────────────────────────
const GLOW = {
  bearish: 'rgba(239,68,68,0.45)',   // red — adverse findings
  bullish: 'rgba(34,197,94,0.40)',   // green — positive
  neutral: 'rgba(59,130,246,0.40)',  // blue — neutral
  audit: 'rgba(168,85,247,0.40)',    // purple — audit agents
}

/** Card wrapper with a subtle severity-colored glow border that breathes when
 * findings are present and intensifies on hover. Pure box-shadow/opacity. */
export function GlowCard({ tone = 'neutral', active = false, className = '', children }) {
  const reduced = useReducedMotion()
  const color = GLOW[tone] || GLOW.neutral
  return (
    <div className={`relative group h-full ${className}`}>
      <div
        aria-hidden
        className={`pointer-events-none absolute -inset-px rounded-2xl transition-opacity duration-200
          ${active && !reduced ? 'sollia-glow-pulse' : ''}`}
        style={{
          boxShadow: `0 0 18px 1px ${color}, inset 0 0 12px -6px ${color}`,
          opacity: active ? 0.5 : 0.18,
        }}
      />
      <div
        aria-hidden
        className="pointer-events-none absolute -inset-px rounded-2xl opacity-0 group-hover:opacity-70 transition-opacity duration-200"
        style={{ boxShadow: `0 0 24px 2px ${color}` }}
      />
      {children}
    </div>
  )
}

/** Animated dot-grid background (GridBackground equivalent): ~2px dots on a
 * 30px lattice, drifting very slowly. Opacity 0.04 — depth, not distraction. */
export function GridBackground() {
  const reduced = useReducedMotion()
  return (
    <div
      aria-hidden
      className={`pointer-events-none fixed inset-0 z-0 ${reduced ? '' : 'sollia-grid-drift'}`}
      style={{
        backgroundImage: 'radial-gradient(circle, rgba(148,163,184,0.9) 1px, transparent 1.2px)',
        backgroundSize: '30px 30px',
        opacity: 0.045,
      }}
    />
  )
}

/** NumberTicker: animates 0 → value in 400ms. Static under reduced motion. */
export function NumberTicker({ value, duration = 0.4, className = '', style }) {
  const reduced = useReducedMotion()
  const ref = useRef(null)
  const target = Math.round(Number(value) || 0)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (reduced) { el.textContent = String(target); return }
    const controls = animate(0, target, {
      duration,
      ease: 'easeOut',
      onUpdate: v => { el.textContent = String(Math.round(v)) },
    })
    return () => controls.stop()
  }, [target, duration, reduced])
  return <span ref={ref} className={className} style={style}>{reduced ? target : 0}</span>
}

/** TextReveal: blur-to-clear title reveal, 300ms. */
export function TextReveal({ children, className = '' }) {
  const reduced = useReducedMotion()
  if (reduced) return <span className={className}>{children}</span>
  return (
    <motion.span
      className={`inline-block ${className}`}
      initial={{ opacity: 0, filter: 'blur(6px)' }}
      animate={{ opacity: 1, filter: 'blur(0px)' }}
      transition={{ duration: 0.3, ease: 'easeOut' }}
    >
      {children}
    </motion.span>
  )
}

/** AnimatedList: children slide in from the left with a 50ms stagger. */
export function AnimatedList({ children, className = '' }) {
  const reduced = useReducedMotion()
  if (reduced) return <div className={className}>{children}</div>
  return (
    <motion.div
      className={className}
      initial="hidden"
      animate="show"
      variants={{ hidden: {}, show: { transition: { staggerChildren: 0.05 } } }}
    >
      {children}
    </motion.div>
  )
}

export function AnimatedItem({ children, className = '' }) {
  const reduced = useReducedMotion()
  if (reduced) return <div className={className}>{children}</div>
  return (
    <motion.div
      className={className}
      variants={{
        hidden: { opacity: 0, x: -16 },
        show: { opacity: 1, x: 0, transition: { duration: 0.2, ease: 'easeOut' } },
      }}
    >
      {children}
    </motion.div>
  )
}

/** Presence entrance for an agent card arriving mid-run: fade + 20px rise. */
export function CardEntrance({ children, delay = 0, className = '' }) {
  const reduced = useReducedMotion()
  if (reduced) return <div className={className}>{children}</div>
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 8 }}
      transition={{ duration: 0.3, ease: 'easeOut', delay }}
    >
      {children}
    </motion.div>
  )
}

/** Hover scale for interactive elements: 1.02, ~150ms spring. */
export function HoverScale({ children, className = '' }) {
  const reduced = useReducedMotion()
  if (reduced) return <div className={className}>{children}</div>
  return (
    <motion.div
      className={className}
      whileHover={{ scale: 1.02 }}
      transition={{ type: 'spring', stiffness: 400, damping: 25 }}
    >
      {children}
    </motion.div>
  )
}

/** AnimatedTooltip: hover tooltip that fades/rises in 150ms. CSS-only reveal. */
export function Tip({ label, children, className = '' }) {
  const [show, setShow] = useState(false)
  if (!label) return children
  return (
    <span
      className={`relative inline-flex ${className}`}
      onMouseEnter={() => setShow(true)}
      onMouseLeave={() => setShow(false)}
      onFocus={() => setShow(true)}
      onBlur={() => setShow(false)}
    >
      {children}
      <span
        role="tooltip"
        className={`pointer-events-none absolute bottom-full left-1/2 z-50 mb-1.5 w-max max-w-[260px]
          -translate-x-1/2 rounded-lg border border-white/10 bg-[#101322] px-2.5 py-1.5 text-[11px]
          font-medium leading-snug text-slate-200 shadow-xl transition-all duration-150
          ${show ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-1'}`}
      >
        {label}
      </span>
    </span>
  )
}
