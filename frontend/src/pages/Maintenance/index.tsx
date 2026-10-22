import { useEffect, useState } from 'react'
import { Wrench } from 'lucide-react'

const DEFAULT_MESSAGE = 'The site is temporarily down for maintenance. Please try again later.'

export default function MaintenancePage() {
  // If the middleware responded with 503 and a message, the axios interceptor saved it in
  // sessionStorage.
  const [message] = useState<string>(() => {
    try {
      return sessionStorage.getItem('maintenance:message') || DEFAULT_MESSAGE
    } catch {
      return DEFAULT_MESSAGE
    }
  })

  // Poll the backend — as soon as maintenance is turned off, return the user to the home page.
  useEffect(() => {
    let stopped = false
    const tick = async () => {
      try {
        const res = await fetch('/api/maintenance/', { cache: 'no-store' })
        if (!stopped && res.status !== 503) {
          try { sessionStorage.removeItem('maintenance:message') } catch {}
          window.location.href = '/'
        }
      } catch {
        // network error — keep waiting
      }
    }
    tick() // immediately on open, do not wait 30s
    const id = window.setInterval(tick, 30_000)
    // background tabs throttle/freeze setInterval — check when returning to the tab
    const onVisible = () => { if (document.visibilityState === 'visible') tick() }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      stopped = true
      window.clearInterval(id)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])

  return (
    <div className="min-h-screen flex items-center justify-center px-4 py-12 bg-[#f7f7f5]">
      <div className="max-w-[480px] text-center">
        <div className="mx-auto mb-6 w-16 h-16 rounded-full bg-[#1A5C2A]/10 flex items-center justify-center">
          <Wrench className="w-8 h-8 text-[#1A5C2A]" />
        </div>
        <h1 className="text-3xl sm:text-4xl font-bold text-[#1A5C2A] mb-4">
          Maintenance
        </h1>
        <p className="text-base sm:text-lg text-gray-600 whitespace-pre-line">
          {message}
        </p>
      </div>
    </div>
  )
}
