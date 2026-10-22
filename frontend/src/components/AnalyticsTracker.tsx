import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'
import { trackPageView } from '../lib/analytics'
import { captureAttribution } from '../lib/attribution'

/**
 * SPA page-view tracker: the single source of page_view (GA4 + Meta PageView).
 * Sends on mount (first load) and on every route change. That is why in
 * analytics.ts GA4 runs with send_page_view:false, and Meta init has no
 * auto-PageView (otherwise the first load would produce a duplicate).
 */
export default function AnalyticsTracker() {
  const { pathname, search } = useLocation()
  const lastPath = useRef<string>('')

  useEffect(() => {
    const path = pathname + search
    if (lastPath.current === path) return
    lastPath.current = path
    captureAttribution()
    trackPageView(path)
  }, [pathname, search])

  return null
}
