import { useEffect, useState } from 'react'
import { useAuth } from '../context/AuthContext'

const VISIT_COUNT_KEY = 'q_visit_count'
const DISMISSED_FOREVER_KEY = 'q_dismissed_forever'
const REMIND_AFTER_KEY = 'q_remind_after_ms'
const VISITS_BEFORE_PROMPT = 3

/**
 * The counter is incremented once per mount (i.e. per full SPA load).
 * This is a rough approximation of the "Nth visit to the site" — a more accurate one would need
 * server-side tracking,
 * but it is enough for a soft pop-up.
 */
function bumpVisitCountOnce(): number {
  // Guard against a double increment in React StrictMode (mount→unmount→mount in dev).
  const w = window as any
  if (w.__q_visit_bumped__) return Number(localStorage.getItem(VISIT_COUNT_KEY) || 0)
  w.__q_visit_bumped__ = true

  const prev = Number(localStorage.getItem(VISIT_COUNT_KEY) || 0)
  const next = prev + 1
  try { localStorage.setItem(VISIT_COUNT_KEY, String(next)) } catch { /* private mode */ }
  return next
}

export function useQuestionnairePrompt() {
  const { user } = useAuth()
  const [visits] = useState<number>(() => bumpVisitCountOnce())
  const [shouldShow, setShouldShow] = useState(false)

  useEffect(() => {
    if (!user) { setShouldShow(false); return }
    if ((user.questionnaire_filled_count ?? 0) > 0) { setShouldShow(false); return }
    if (user.questionnaire_dismissed_at) { setShouldShow(false); return }
    if (localStorage.getItem(DISMISSED_FOREVER_KEY) === '1') { setShouldShow(false); return }

    const remindMs = Number(localStorage.getItem(REMIND_AFTER_KEY) || 0)
    if (Date.now() < remindMs) { setShouldShow(false); return }

    if (visits < VISITS_BEFORE_PROMPT) { setShouldShow(false); return }

    setShouldShow(true)
  }, [user, visits])

  const snooze7 = () => {
    try {
      localStorage.setItem(REMIND_AFTER_KEY, String(Date.now() + 7 * 24 * 3600 * 1000))
    } catch { /* */ }
    setShouldShow(false)
  }

  const dismissForever = () => {
    try {
      localStorage.setItem(DISMISSED_FOREVER_KEY, '1')
    } catch { /* */ }
    setShouldShow(false)
  }

  const close = () => setShouldShow(false)

  return { shouldShow, snooze7, dismissForever, close }
}
