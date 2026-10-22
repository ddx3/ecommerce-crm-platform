import { useEffect } from 'react'
import { useLocation, useNavigationType } from 'react-router-dom'

/**
 * Scroll to top on PUSH navigation (a new transition).
 * On POP (back/forward) we leave the scroll in place; the browser restores the position itself.
 *
 * Important: the full ScrollRestoration of React Router 6 requires createBrowserRouter (Data
 * Router).
 * This is a simple analogue for a regular BrowserRouter.
 */
export default function ScrollToTop() {
  const { pathname } = useLocation()
  const navType = useNavigationType()

  useEffect(() => {
    if (navType === 'POP') return
    window.scrollTo({ top: 0, left: 0, behavior: 'instant' })
  }, [pathname, navType])

  return null
}
