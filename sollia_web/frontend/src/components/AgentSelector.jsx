import { motion } from 'framer-motion'

const stagger = {
  hidden: {},
  show: { transition: { staggerChildren: 0.05 } }
}

const chip = {
  hidden: { opacity: 0, scale: 0.9 },
  show: { opacity: 1, scale: 1 }
}

export default function AgentSelector({ agents, selected, onToggle, onSelectAll, onDeselectAll }) {
  return (
    <div className="flex-1 w-full">
      <div className="flex items-center justify-between mb-2">
        <label className="text-xs text-slate-500 uppercase tracking-widest font-medium">Agents</label>
        <div className="flex gap-3">
          <button onClick={onSelectAll} className="text-xs text-slate-600 hover:text-slate-400 transition-colors cursor-pointer font-medium tracking-wide">Select All</button>
          <button onClick={onDeselectAll} className="text-xs text-slate-600 hover:text-slate-400 transition-colors cursor-pointer font-medium tracking-wide">Clear</button>
        </div>
      </div>
      <motion.div variants={stagger} initial="hidden" animate="show" className="flex flex-wrap gap-2.5">
        {agents.map(agent => (
          <motion.button
            key={agent.id}
            variants={chip}
            onClick={() => onToggle(agent.id)}
            whileHover={{ scale: 1.04 }}
            whileTap={{ scale: 0.96 }}
            className={`relative px-4 py-2.5 rounded-xl text-sm font-medium transition-all cursor-pointer border ${
              selected.includes(agent.id)
                ? 'bg-blue-500/10 border-blue-400/30 text-blue-300 shadow-lg shadow-blue-500/5'
                : 'bg-white/[0.02] border-white/5 text-slate-600 hover:border-white/10 hover:text-slate-400'
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
