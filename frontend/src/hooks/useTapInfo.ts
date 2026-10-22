import { useEffect, useRef, useState } from 'react'

/**
 * "Why unavailable" hint on touch devices. There is no hover there, so on a tap
 * on a struck-through chip we show the reason and hide it ourselves after a timeout.
 * On desktop the hint stays on hover (the logic is in the chip itself).
 */
export function useTapInfo(timeout = 2200) {
  const [activeInfoId, setActiveInfoId] = useState<number | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  const showInfo = (id: number) => {
    setActiveInfoId(id)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setActiveInfoId(null), timeout)
  }

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  return { activeInfoId, showInfo }
}
