import { motion } from 'framer-motion'

const stagger = {
  hidden: {},
  show: { transition: { staggerChildren: 0.05 } }
}

const chip = {
  hidden: { opacity: 0, scale: 0.9 },
  show: { opacity: 1, scale: 1 }
}

// `disabled` (Part 4a): while an analysis runs, selection is locked — clicks
// are ignored and the chips render at half opacity with a not-allowed cursor.
export default function AgentSelector({ agents, selected, onToggle, onSelectAll, onDeselectAll, disabled = false }) {
  const guard = (fn) => () => { if (!disabled && fn) fn() }
  return (
    <div className="flex-1 w-full">
      <div className="flex items-center justify-between mb-2">
        <label className="text-xs text-slate-500 uppercase tracking-widest font-medium">
          Agents {disabled && <span className="text-slate-600 normal-case tracking-normal">· locked during run</span>}
        </label>
        <div className="flex gap-3">
          <button onClick={guard(onSelectAll)} disabled={disabled} className={`text-xs font-medium tracking-wide transition-colors ${disabled ? 'text-slate-700 cursor-not-allowed' : 'text-slate-600 hover:text-slate-400 cursor-pointer'}`}>Select All</button>
          <button onClick={guard(onDeselectAll)} disabled={disabled} className={`text-xs font-medium tracking-wide transition-colors ${disabled ? 'text-slate-700 cursor-not-allowed' : 'text-slate-600 hover:text-slate-400 cursor-pointer'}`}>Clear</button>
        </div>
      </div>
      <motion.div variants={stagger} initial="hidden" animate="show" className="flex flex-wrap gap-2.5">
        {agents.map(agent => (
          <motion.button
            key={agent.id}
            variants={chip}
            onClick={() => { if (!disabled) onToggle(agent.id) }}
            disabled={disabled}
            aria-disabled={disabled}
            whileHover={disabled ? undefined : { scale: 1.04 }}
            whileTap={disabled ? undefined : { scale: 0.96 }}
            className={`relative px-4 py-2.5 rounded-xl text-sm font-medium transition-all border ${
              disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'
            } ${
              selected.includes(agent.id)
                ? 'bg-blue-500/10 border-blue-400/30 text-blue-300 shadow-lg shadow-blue-500/5'
                : `bg-white/[0.02] border-white/5 text-slate-600 ${disabled ? '' : 'hover:border-white/10 hover:text-slate-400'}`
            }`}
          >
            {agent.name}
            {selected.includes(agent.id) && (
              <motion.span
                layoutId="agentCheck"
                className="absolute -top-1.5 -right-1.5 w-4 h-4 bg-blue-400 rounded-full flex items-center justify-center"
                transition={{ type: 'spring', stiffness: 500, damping: 30 }}
              >
                <svg className="w-2.5 h-2.5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </motion.span>
            )}
          </motion.button>
        ))}
      </motion.div>
    </div>
  )
}
