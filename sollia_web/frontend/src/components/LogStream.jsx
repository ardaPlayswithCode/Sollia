import { useRef, useEffect } from 'react'

export default function LogStream({ logs }) {
  const bottomRef = useRef(null)
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [logs])

  return (
    <div className="max-h-96 overflow-y-auto space-y-0.5 font-mono text-sm">
      {logs.map((log, i) => (
        <p key={i} className="text-slate-500 leading-relaxed py-0.5 hover:text-slate-400 transition-colors">
          <span className="text-slate-700 font-semibold">[{log.speaker}]</span>{' '}
          <span className="text-slate-400">{log.message}</span>
        </p>
      ))}
      <div ref={bottomRef} />
    </div>
  )
}
