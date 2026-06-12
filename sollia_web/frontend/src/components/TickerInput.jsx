import { useRef, useEffect } from 'react'
import { motion } from 'framer-motion'

const COMMON_TICKERS = ['NVDA', 'AAPL', 'MSFT', 'GOOGL', 'META', 'TSLA', 'AMD', 'F']

export default function TickerInput({ ticker, setTicker, year, setYear, showYear, onSubmit, disabled }) {
  const inputRef = useRef(null)

  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === 'Enter' && ticker.trim() && !disabled) {
        onSubmit()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [ticker, disabled, onSubmit])

  return (
    <div className="flex flex-col sm:flex-row gap-3 items-start sm:items-end w-full lg:w-auto">
      <div className="w-full sm:w-auto">
        <label className="block text-xs text-slate-500 uppercase tracking-widest mb-2 font-medium">Ticker{showYear ? ' · Year' : ''}</label>
        <div className="flex gap-3">
          <div className="relative">
            <input
              ref={inputRef}
              type="text"
              value={ticker}
              onChange={e => setTicker(e.target.value.toUpperCase().slice(0, 5))}
              placeholder="NVDA"
              maxLength={5}
              className="bg-[#0A0B14] border border-white/10 rounded-xl px-5 py-3.5 text-white text-2xl font-black tracking-[0.2em] placeholder-slate-700 focus:outline-none focus:border-blue-400/50 focus:ring-1 focus:ring-blue-400/20 transition-all w-full sm:w-36 text-center"
            />
            {ticker && COMMON_TICKERS.includes(ticker) && (
              <motion.span
                initial={{ scale: 0 }}
                animate={{ scale: 1 }}
                className="absolute -top-1 -right-1 w-2.5 h-2.5 bg-emerald-400 rounded-full"
              />
            )}
          </div>
          {showYear && (
            <motion.div
              initial={{ opacity: 0, width: 0 }}
              animate={{ opacity: 1, width: 'auto' }}
              className="relative"
            >
              <input
                type="number"
                value={year || ''}
                onChange={e => setYear(parseInt(e.target.value) || 2023)}
                placeholder="2023"
                min={2016}
                max={2026}
                className="bg-[#0A0B14] border border-white/10 rounded-xl px-4 py-3.5 text-white text-xl font-bold placeholder-slate-700 focus:outline-none focus:border-purple-400/50 focus:ring-1 focus:ring-purple-400/20 transition-all w-24 text-center"
              />
            </motion.div>
          )}
        </div>
      </div>
      <motion.button
        onClick={onSubmit}
        disabled={disabled || !ticker.trim()}
        whileHover={disabled ? {} : { scale: 1.03, boxShadow: '0 0 30px rgba(59, 130, 246, 0.3)' }}
        whileTap={disabled ? {} : { scale: 0.97 }}
        className={`px-8 py-3.5 rounded-xl font-bold text-sm tracking-wide transition-all cursor-pointer ${
          disabled || !ticker.trim()
            ? 'bg-white/5 text-slate-700 cursor-not-allowed border border-white/5'
            : 'bg-gradient-to-r from-blue-500 to-cyan-500 text-white hover:from-blue-400 hover:to-cyan-400 shadow-lg shadow-blue-500/20 border border-blue-400/20'
        }`}
      >
        Analyze
      </motion.button>
    </div>
  )
}
