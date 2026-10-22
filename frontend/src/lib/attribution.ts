/**
 * Where the buyer came from: ad utm tags (Instagram stories / posts) and the external
 * referrer. We remember them on arrival and send them along with the order - the backend stores
 * them in analyst.OrderAttribution, so the Analyst can see which campaign brought the order.
 *
 * Rules:
 *  - new utm tags always overwrite old ones (the last ad interaction wins);
 *  - a bare external referrer is saved only if nothing is stored yet, so that
 *    a random visit from search does not erase an ad tag;
 *  - a tag lives for 30 days (customers often come back to buy not right away).
 */
const KEY = 'shop_attribution'
const TTL_MS = 30 * 24 * 60 * 60 * 1000
const UTM_KEYS = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content'] as const

export interface Attribution {
  utm_source?: string
  utm_medium?: string
  utm_campaign?: string
  utm_content?: string
  referrer?: string
  landing_path?: string
  ts: number
}

function read(): Attribution | null {
  try {
    const raw = localStorage.getItem(KEY)
    if (!raw) return null
    const a = JSON.parse(raw) as Attribution
    if (!a || typeof a.ts !== 'number' || Date.now() - a.ts > TTL_MS) return null
    return a
  } catch {
    return null
  }
}

function externalReferrer(): string {
  try {
    if (!document.referrer) return ''
    const u = new URL(document.referrer)
    if (u.host === window.location.host) return ''
    return u.origin + u.pathname
  } catch {
    return ''
  }
}

/** Called on every page visit (AnalyticsTracker). Errors are swallowed - this is only statistics. */
export function captureAttribution(): void {
  try {
    const params = new URLSearchParams(window.location.search)
    const utm: Partial<Attribution> = {}
    for (const k of UTM_KEYS) {
      const v = (params.get(k) || '').trim()
      if (v) utm[k] = v.slice(0, 150)
    }
    const hasUtm = Object.keys(utm).length > 0
    const referrer = externalReferrer()
    if (!hasUtm && (!referrer || read())) return
    const next: Attribution = {
      ...utm,
      referrer: referrer || undefined,
      landing_path: window.location.pathname,
      ts: Date.now(),
    }
    localStorage.setItem(KEY, JSON.stringify(next))
  } catch {
    /* private mode / blocked storage - just no attribution */
  }
}

/** Snapshot for the order body; without the timestamp (the backend doesn't need it). */
export function getAttribution(): Omit<Attribution, 'ts'> | undefined {
  const a = read()
  if (!a) return undefined
  return {
    utm_source: a.utm_source,
    utm_medium: a.utm_medium,
    utm_campaign: a.utm_campaign,
    utm_content: a.utm_content,
    referrer: a.referrer,
    landing_path: a.landing_path,
  }
}
