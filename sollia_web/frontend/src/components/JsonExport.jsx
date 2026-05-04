import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'

export default function JsonExport({ data, ticker }) {
  const [viewing, setViewing] = useState(false)
  const [copied, setCopied] = useState(false)

  const download = () => {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = `sollia_${ticker.toLowerCase()}.json`; a.click()
    URL.revokeObjectURL(url)
  }

  const copy = () => {
    navigator.clipboard.writeText(JSON.stringify(data, null, 2))
    setCopied(true); setTimeout(() => setCopied(false), 2000)
  }

  return (
    <div>
      <div className="flex gap-3 mb-4">
        <button onClick={() => setViewing(!viewing)} className="px-5 py-2.5 glass-card text-sm text-slate-400 hover:text-white transition-all cursor-pointer font-medium">{viewing ? 'Hide' : 'View JSON'}</button>
        <button onClick={copy} className="px-5 py-2.5 glass-card text-sm text-slate-400 hover:text-white transition-all cursor-pointer font-medium">{copied ? 'Copied!' : 'Copy'}</button>
        <button onClick={download} className="px-5 py-2.5 glass-card text-sm text-slate-400 hover:text-white transition-all cursor-pointer font-medium">Download</button>
      </div>
      <AnimatePresence>
        {viewing && (
          <motion.pre initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }}
            className="bg-[#04060C] rounded-2xl p-5 max-h-96 overflow-auto text-sm text-emerald-400/70 whitespace-pre-wrap border border-white/5 font-mono">
            {JSON.stringify(data, null, 2)}
          </motion.pre>
        )}
      </AnimatePresence>
    </div>
  )
}
