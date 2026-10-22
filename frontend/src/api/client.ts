import axios from 'axios'
import { getAntibotToken, resetAntibotToken, TOKEN_FIELD, HONEYPOT_FIELD } from './antibot'

const client = axios.create({
  baseURL: '/api',
  headers: { 'Content-Type': 'application/json' },
})

const ACCESS_KEY = 'token'
const REFRESH_KEY = 'refresh'
const WRITE_METHODS = ['post', 'put', 'patch']

// One in-flight refresh per tab: with ROTATE_REFRESH_TOKENS=True
// two parallel refreshes would blacklist each other. The remaining 401 requests
// wait on the same promise.
let refreshPromise: Promise<string | null> | null = null

async function refreshAccess(): Promise<string | null> {
  const refresh = localStorage.getItem(REFRESH_KEY)
  if (!refresh) return null
  try {
    // Bare fetch (not through client), so a 401 on the refresh itself does not recurse.
    const res = await fetch('/api/auth/refresh/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh }),
    })
    if (!res.ok) return null
    const data = await res.json()
    if (data.access) localStorage.setItem(ACCESS_KEY, data.access)
    if (data.refresh) localStorage.setItem(REFRESH_KEY, data.refresh)  // rotation
    return data.access ?? null
  } catch {
    return null
  }
}

function ensureRefresh(): Promise<string | null> {
  if (!refreshPromise) {
    refreshPromise = refreshAccess().finally(() => { refreshPromise = null })
  }
  return refreshPromise
}

function clearAuth() {
  if (localStorage.getItem(ACCESS_KEY) || localStorage.getItem(REFRESH_KEY)) {
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
    window.dispatchEvent(new CustomEvent('auth:logout'))
  }
}

// Add the anti-bot fields to every write. Works with both JSON and FormData.
async function injectAntibot(config: any) {
  if (!WRITE_METHODS.includes((config.method || '').toLowerCase())) return config
  const token = await getAntibotToken()
  if (config.data instanceof FormData) {
    config.data.set(HONEYPOT_FIELD, '')
    if (token) config.data.set(TOKEN_FIELD, token)
  } else {
    // A retry after a 401 refresh / anti-bot rejection gets a config where axios has already
    // serialized data into a JSON string; spreading a string would scatter the body into
    // characters.
    let data = config.data || {}
    if (typeof data === 'string') {
      try { data = JSON.parse(data) } catch { return config }
    }
    config.data = { ...data, [HONEYPOT_FIELD]: '' }
    if (token) config.data[TOKEN_FIELD] = token
  }
  return config
}

client.interceptors.request.use(async config => {
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return injectAntibot(config)
})

client.interceptors.response.use(
  response => response,
  async error => {
    const status = error?.response?.status
    // Anti-bot rejection (e.g. expired token) — reissue the token and retry once.
    const cfg = error?.config
    if (status === 400 && error?.response?.data?.code === 'antibot' && cfg && !cfg._abRetried) {
      cfg._abRetried = true
      resetAntibotToken()
      await getAntibotToken(true)
      return client(cfg)
    }
    if (status === 401 && cfg && !cfg._authRetried) {
      // Expired access token — try to renew it using the refresh token and retry the request once.
      cfg._authRetried = true
      const newAccess = await ensureRefresh()
      if (newAccess) {
        cfg.headers = cfg.headers || {}
        cfg.headers.Authorization = `Bearer ${newAccess}`
        return client(cfg)
      }
      // Refresh failed (missing/expired/blacklisted) — full logout.
      clearAuth()
    }
    // Maintenance mode: the backend responded with 503 and {maintenance: true, message}.
    if (status === 503 && error?.response?.data?.maintenance) {
      try {
        const msg = error.response.data.message
        if (typeof msg === 'string' && msg) {
          sessionStorage.setItem('maintenance:message', msg)
        }
      } catch {}
      if (window.location.pathname !== '/maintenance') {
        window.location.href = '/maintenance'
      }
    }
    return Promise.reject(error)
  }
)

export default client
