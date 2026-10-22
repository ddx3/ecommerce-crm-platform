import { createContext, useContext, useState, useEffect, type ReactNode } from 'react'
import type { AuthUser } from '../api/auth'
import { apiMe } from '../api/auth'

interface AuthContextType {
  user: AuthUser | null
  token: string | null
  // access/refresh are optional: a profile update changes only user, leaving the tokens alone;
  // login/registration/password change pass a fresh pair.
  setAuth: (user: AuthUser, access?: string, refresh?: string) => void
  logout: () => void
}

const AuthContext = createContext<AuthContextType | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [token, setToken] = useState<string | null>(() => localStorage.getItem('token'))

  useEffect(() => {
    if (!token) {
      setUser(null)
      return
    }
    let cancelled = false
    // apiMe takes the token from localStorage via the axios client; on 401 — auto-refresh.
    apiMe()
      .then(u => { if (!cancelled) setUser(u) })
      .catch(() => {
        if (cancelled) return
        localStorage.removeItem('token')
        localStorage.removeItem('refresh')
        setToken(null)
        setUser(null)
      })
    return () => { cancelled = true }
  }, [token])

  // Signal from the axios interceptor about a failed refresh (401) → full logout
  useEffect(() => {
    const onLogout = () => { setToken(null); setUser(null) }
    window.addEventListener('auth:logout', onLogout)
    return () => window.removeEventListener('auth:logout', onLogout)
  }, [])

  // Cross-tab sync: another tab logged in/logged out/refreshed the token.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== 'token') return
      if (e.newValue === null) { setToken(null); setUser(null) }
      else if (e.newValue !== token) setToken(e.newValue)
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [token])

  const setAuth = (user: AuthUser, access?: string, refresh?: string) => {
    if (access !== undefined) {
      localStorage.setItem('token', access)
      setToken(access)
    }
    if (refresh !== undefined) localStorage.setItem('refresh', refresh)
    setUser(user)
  }

  const logout = () => {
    // Best-effort blacklist of the refresh token on the backend. We read the tokens BEFORE deletion
    // and send them explicitly (not via client), so as not to depend on the interceptor's timing.
    const access = localStorage.getItem('token')
    const refresh = localStorage.getItem('refresh')
    if (access && refresh) {
      fetch('/api/auth/logout/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${access}` },
        body: JSON.stringify({ refresh }),
      }).catch(() => {})
    }
    localStorage.removeItem('token')
    localStorage.removeItem('refresh')
    setToken(null)
    setUser(null)
  }

  return (
    <AuthContext.Provider value={{ user, token, setAuth, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
