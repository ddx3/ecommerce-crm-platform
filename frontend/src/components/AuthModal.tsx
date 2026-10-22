import { useState, useEffect } from 'react'
import type { FormEvent } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import { useUI } from '../context/UIContext'
import { useAuth } from '../context/AuthContext'
import { apiLogin, apiRegister, apiPasswordResetRequest } from '../api/auth'
import { Modal, Button, Input, PhoneInput, IconButton } from './ui'

export default function AuthModal() {
  const { panel, authTab, authEmail, close } = useUI()
  const { setAuth } = useAuth()
  const [mode, setMode] = useState<'login' | 'register' | 'forgot'>(authTab)
  const [showPass, setShowPass] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  const [form, setForm] = useState({ email: '', phone: '', name: '', password: '', confirm: '' })
  const [consent, setConsent] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [forgotSent, setForgotSent] = useState(false)

  const open = panel === 'auth'

  useEffect(() => {
    if (!open) return
    setMode(authTab)
    setError('')
    setForgotSent(false)
    // authEmail — prefill (a guest saves their order); otherwise empty.
    setForm({ email: authEmail, phone: '', name: '', password: '', confirm: '' })
    setConsent(false)
    setShowPass(false)
    setShowConfirm(false)
  }, [open, authTab, authEmail])

  const set = (k: string, v: string) => { setForm(f => ({ ...f, [k]: v })); setError('') }

  const handleSubmit = async (e?: FormEvent) => {
    e?.preventDefault()
    if (loading) return
    setError('')
    if (mode === 'forgot') {
      if (!form.email.includes('@') || !form.email.includes('.')) {
        setError('Enter a valid email'); return
      }
      setLoading(true)
      try {
        await apiPasswordResetRequest(form.email)
        setForgotSent(true)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to send')
      } finally {
        setLoading(false)
      }
      return
    }
    if (mode === 'register' && form.password !== form.confirm) {
      setError('Passwords do not match'); return
    }
    if (mode === 'register' && !consent) {
      setError('You must accept the privacy policy'); return
    }
    setLoading(true)
    try {
      const res = mode === 'login'
        ? await apiLogin({ identifier: form.email, password: form.password })
        : await apiRegister({
            email: form.email,
            password: form.password,
            name: form.name,
            phone: form.phone ? `+38${form.phone.replace(/\s/g, '')}` : '',
          })
      setAuth(res.user, res.access, res.refresh)
      if (mode === 'register') {
        // The "gardener profile" questionnaire is temporarily hidden — we just
        // close the modal.
        close()
      } else {
        close()
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Authorization error')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={close}
      ariaLabel={mode === 'forgot' ? 'Password reset' : mode === 'login' ? 'Sign in' : 'Sign up'}
    >
      <p className="text-center font-bold text-brand text-xl sm:text-2xl mb-5 sm:mb-7">Magiya Roslyn</p>

      {mode !== 'forgot' && (
        <div className="flex bg-surface-muted rounded-2xl p-1 mb-7" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'login'}
            onClick={() => { setMode('login'); setError('') }}
            className={`flex-1 py-2.5 rounded-xl text-sm font-bold transition-colors ${mode === 'login' ? 'bg-white text-brand shadow-sm' : 'text-text-muted hover:text-text-primary'}`}
          >
            Sign in
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === 'register'}
            onClick={() => { setMode('register'); setError('') }}
            className={`flex-1 py-2.5 rounded-xl text-sm font-bold transition-colors ${mode === 'register' ? 'bg-white text-brand shadow-sm' : 'text-text-muted hover:text-text-primary'}`}
          >
            Sign up
          </button>
        </div>
      )}

      {mode === 'forgot' && (
        <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
          {forgotSent ? (
            <div className="text-center py-4">
              <div className="w-16 h-16 rounded-full bg-brand-bg flex items-center justify-center mx-auto mb-4">
                <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#1a5c2a" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="2" y="4" width="20" height="16" rx="2"/>
                  <path d="M2 7l10 7 10-7"/>
                </svg>
              </div>
              <p className="font-bold text-text-primary mb-2">Email sent!</p>
              <p className="text-sm text-text-muted">Check your inbox (including spam) — the email usually arrives within 1-5 minutes.</p>
            </div>
          ) : (
            <>
              <p className="text-sm text-text-secondary -mt-2">Enter your email — we will send you a password reset link.</p>
              <Input
                label="Email"
                name="email"
                type="email"
                autoComplete="email"
                value={form.email}
                onChange={e => set('email', e.target.value)}
                placeholder="email@example.com"
              />
              {error && <p className="text-sm text-danger text-center">{error}</p>}
              <Button type="submit" loading={loading} fullWidth size="lg">
                {loading ? 'Sending...' : 'Send link'}
              </Button>
            </>
          )}
          {!forgotSent && (
            <button type="button" onClick={() => { setMode('login'); setError(''); setForgotSent(false) }} className="text-brand hover:underline text-sm flex items-center justify-center gap-1">
              <svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m15 18-6-6 6-6"/></svg>
              Back to sign in
            </button>
          )}
        </form>
      )}

      {mode !== 'forgot' && (
        <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
          {mode === 'register' && (
            <Input
              label="Full name"
              name="name"
              type="text"
              autoComplete="name"
              value={form.name}
              onChange={e => set('name', e.target.value)}
              placeholder="Last name, first name, patronymic"
            />
          )}

          <Input
            label={mode === 'login' ? 'Email or phone number' : 'Email'}
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

          {mode === 'register' && (
            <Input
              label="Confirm password"
              name="password-confirm"
              type={showConfirm ? 'text' : 'password'}
              autoComplete="new-password"
              value={form.confirm}
              onChange={e => set('confirm', e.target.value)}
              placeholder="••••••••"
              error={form.confirm && form.confirm !== form.password ? 'Passwords do not match' : undefined}
              rightAddon={
                <IconButton
                  size="sm"
                  onClick={() => setShowConfirm(v => !v)}
                  aria-label={showConfirm ? 'Hide password' : 'Show password'}
                >
                  {showConfirm ? <EyeOff size={18} /> : <Eye size={18} />}
                </IconButton>
              }
            />
          )}

          {mode === 'register' && (
            <label className="flex items-start gap-2.5 cursor-pointer">
              <input
                type="checkbox"
                checked={consent}
                onChange={e => { setConsent(e.target.checked); setError('') }}
                className="mt-0.5 shrink-0 accent-brand"
              />
              <span className="text-xs text-text-secondary leading-relaxed">
                I agree to the{' '}
                <a href="/privacy" target="_blank" rel="noopener noreferrer" className="text-brand hover:underline">Privacy Policy</a>
                {' '}and{' '}
                <a href="/offer" target="_blank" rel="noopener noreferrer" className="text-brand hover:underline">Public Offer</a>
                , and I consent to the processing of my personal data
              </span>
            </label>
          )}

          {error && <p className="text-sm text-danger text-center">{error}</p>}

          <Button type="submit" loading={loading} fullWidth size="lg">
            {loading ? 'Please wait...' : mode === 'login' ? 'Sign in' : 'Sign up'}
          </Button>
        </form>
      )}

      {mode === 'login' && (
        <p className="text-center text-sm text-text-muted mt-4">
          <button type="button" onClick={() => { setMode('forgot'); setError('') }} className="text-brand hover:underline">Forgot your password?</button>
        </p>
      )}
    </Modal>
  )
}
