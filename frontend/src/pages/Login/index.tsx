import { useState, useEffect } from 'react'
import type { FormEvent } from 'react'
import { Link, useNavigate, useLocation } from 'react-router-dom'
import { Eye, EyeOff } from 'lucide-react'
import { apiLogin, apiRegister } from '../../api/auth'
import { useAuth } from '../../context/AuthContext'
import { Button, Input, PhoneInput, IconButton } from '../../components/ui'

export default function LoginPage() {
  // ?register=1 — so that a "Register" link (e.g. from the course checkout) opens
  // the right tab right away rather than the login form.
  const [mode, setMode] = useState<'login' | 'register'>(
    () => new URLSearchParams(window.location.search).get('register') === '1' ? 'register' : 'login',
  )
  const [showPass, setShowPass] = useState(false)
  const [form, setForm] = useState({ email: '', phone: '', name: '', password: '' })
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const { setAuth, token } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()

  // A logged-in user must not see the login form — redirect away.
  useEffect(() => {
    if (token) {
      const params = new URLSearchParams(location.search)
      navigate(params.get('next') || '/', { replace: true })
    }
  }, [token, navigate, location.search])

  const set = (k: string, v: string) => setForm(f => ({ ...f, [k]: v }))

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (loading) return
    setError(null)
    setLoading(true)
    try {
      if (mode === 'login') {
        const identifier = form.email.trim()
        if (!identifier || !form.password) {
          setError('Enter your email/phone and password')
          return
        }
        const res = await apiLogin({ identifier, password: form.password })
        setAuth(res.user, res.access, res.refresh)
      } else {
        if (!form.email.trim() || !form.password) {
          setError("Email and password are required")
          return
        }
        const res = await apiRegister({
          email: form.email.trim(),
          password: form.password,
          name: form.name.trim(),
          phone: form.phone ? `+38${form.phone.replace(/\s/g, '')}` : '',
        })
        setAuth(res.user, res.access, res.refresh)
      }
      const params = new URLSearchParams(location.search)
      const next = params.get('next') || '/'
      navigate(next, { replace: true })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Error')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-[60vh] flex items-center justify-center px-4 py-12">
      <div className="w-full max-w-md">
        <div className="bg-surface border border-border rounded-2xl p-5 sm:p-6 shadow-card-mid">
          <Link to="/" className="block text-center font-bold text-brand text-2xl mb-8">Magiya Roslyn</Link>

          <div className="flex bg-surface-muted rounded-2xl p-1 mb-8" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'login'}
              onClick={() => { setMode('login'); setError(null) }}
              className={`flex-1 py-2.5 rounded-xl text-sm font-bold transition-colors ${mode === 'login' ? 'bg-white text-brand shadow-sm' : 'text-text-muted hover:text-text-primary'}`}
            >
              Sign in
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'register'}
              onClick={() => { setMode('register'); setError(null) }}
              className={`flex-1 py-2.5 rounded-xl text-sm font-bold transition-colors ${mode === 'register' ? 'bg-white text-brand shadow-sm' : 'text-text-muted hover:text-text-primary'}`}
            >
              Sign up
            </button>
          </div>

          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            {mode === 'register' && (
              <Input
                label="Name"
                name="name"
                autoComplete="name"
                value={form.name}
                onChange={e => set('name', e.target.value)}
                placeholder="Your name"
              />
            )}

            <Input
              label={mode === 'login' ? 'Email or phone' : 'Email'}
              name={mode === 'login' ? 'username' : 'email'}
              type={mode === 'login' ? 'text' : 'email'}
              autoComplete={mode === 'login' ? 'username' : 'email'}
              inputMode={mode === 'login' ? 'text' : 'email'}
              value={form.email}
              onChange={e => set('email', e.target.value)}
              placeholder={mode === 'login' ? 'email@example.com or +380...' : 'email@example.com'}
            />

            {mode === 'register' && (
              <PhoneInput
                label="Phone number"
                value={form.phone}
                onChange={v => set('phone', v)}
              />
            )}

            <Input
              label="Password"
              name="password"
              type={showPass ? 'text' : 'password'}
              autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
              value={form.password}
              onChange={e => set('password', e.target.value)}
              placeholder="••••••••"
              rightAddon={
                <IconButton
                  size="sm"
                  onClick={() => setShowPass(v => !v)}
                  aria-label={showPass ? 'Hide password' : 'Show password'}
                >
                  {showPass ? <EyeOff size={18} /> : <Eye size={18} />}
                </IconButton>
              }
            />

            {error && <div className="text-sm text-danger -mt-1">{error}</div>}

            <Button type="submit" variant="primary" size="lg" fullWidth loading={loading} className="mt-2">
              {loading ? 'Please wait...' : mode === 'login' ? 'Sign in' : 'Sign up'}
            </Button>
          </form>

          {mode === 'login' && (
            <p className="text-center text-sm text-text-muted mt-4">
              Don't have an account?{' '}
              <button type="button" onClick={() => setMode('register')} className="text-brand font-bold hover:underline">
                Sign up
              </button>
            </p>
          )}
        </div>
      </div>
    </div>
  )
}
