// Invisible anti-bot protection: the frontend fetches a server-signed token once
// and attaches it (together with an empty honeypot field) to every POST. The user
// notices nothing; a bot hitting the API directly has no token.
import axios from 'axios'

const HONEYPOT_FIELD = 'hp_field'
const TOKEN_FIELD = 'hp_ts'

let tokenPromise: Promise<string | null> | null = null
let fetchedAt = 0
const MAX_TOKEN_AGE_MS = 2 * 60 * 60 * 1000 // 2 h — refresh before the server-side limit of 3 h

function fetchToken(): Promise<string | null> {
  fetchedAt = Date.now()
  // Separate axios instance (without our interceptor) — to avoid looping.
  return axios
    .get('/api/antibot-token/')
    .then(r => (r.data?.token as string) || null)
    .catch(() => null)
}

export function getAntibotToken(force = false): Promise<string | null> {
  const stale = Date.now() - fetchedAt > MAX_TOKEN_AGE_MS
  if (force || !tokenPromise || stale) tokenPromise = fetchToken()
  return tokenPromise
}

// Reset the cache so the next request reissues the token (e.g. after a rejection).
export function resetAntibotToken() {
  tokenPromise = null
}

export function antibotField() {
  return HONEYPOT_FIELD
}

export { TOKEN_FIELD, HONEYPOT_FIELD }

// Prefetch right at app load — by the time any form is submitted
// the token is ready, and its "age" is guaranteed to exceed the minimum threshold.
getAntibotToken()
