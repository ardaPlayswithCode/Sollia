// Draggable / resizable agent-card grid (production polish Part 7f).
// react-grid-layout: drag via the card header handle, resize from the corner,
// layout persists to localStorage, "Reset Layout" restores the defaults.
import { useCallback, useMemo, useState } from 'react'
// v2 ships the v1-compatible API (WidthProvider/Responsive/draggableHandle)
// under the /legacy entry; the modern entry is hook-based.
import { Responsive, WidthProvider } from 'react-grid-layout/legacy'
import 'react-grid-layout/css/styles.css'

const RGL = WidthProvider(Responsive)

const STORAGE_KEY = 'sollia-layout-v1'
// 12-col grid, rowHeight 30px. Heights sized so a card shows a useful amount
// of findings before its internal scroll kicks in.
const DEFAULT_H = 14
const MIN_H = 6

function defaultLayoutFor(ids) {
  return ids.map((id, i) => ({
    i: id,
    x: (i % 2) * 6,
    y: Math.floor(i / 2) * DEFAULT_H,
    w: 6,
    h: DEFAULT_H,
    minW: 3,
    minH: MIN_H,
  }))
}

function loadSaved() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export default function AgentGrid({ ids, renderCard, resetToken = 0 }) {
  const [layouts, setLayouts] = useState(() => loadSaved() || {})

  // Reset Layout: parent bumps resetToken; wipe storage and re-derive
  // defaults. Adjust-state-during-render pattern (no effect → no cascading
  // re-render; React re-runs this render with the fresh state immediately).
  const [lastReset, setLastReset] = useState(resetToken)
  if (resetToken !== lastReset) {
    setLastReset(resetToken)
    try { localStorage.removeItem(STORAGE_KEY) } catch { /* private mode */ }
    setLayouts({})
  }

  // Merge defaults for any agent ids the saved layout doesn't know yet.
  const merged = useMemo(() => {
    const base = defaultLayoutFor(ids)
    const out = {}
    for (const bp of ['lg', 'md', 'sm']) {
      const saved = Array.isArray(layouts[bp]) ? layouts[bp] : []
      const savedIds = new Set(saved.map(l => l.i))
      out[bp] = [
        ...saved.filter(l => ids.includes(l.i)),
        ...base.filter(l => !savedIds.has(l.i)),
      ]
    }
    return out
  }, [ids, layouts])

  const onLayoutChange = useCallback((_current, all) => {
    setLayouts(all)
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(all)) } catch { /* private mode */ }
  }, [])

  return (
    <RGL
      className="layout agent-grid-board"
      layouts={merged}
      breakpoints={{ lg: 1024, md: 768, sm: 0 }}
      cols={{ lg: 12, md: 12, sm: 6 }}
      rowHeight={30}
      margin={[16, 16]}
      draggableHandle=".drag-handle"
      resizeHandles={['se', 'sw', 'e', 's']}
      onLayoutChange={onLayoutChange}
      useCSSTransforms
      compactType="vertical"
    >
      {ids.map(id => (
        <div key={id} className="h-full">
          {renderCard(id)}
        </div>
      ))}
    </RGL>
  )
}
