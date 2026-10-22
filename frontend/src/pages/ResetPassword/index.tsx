import { useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { Eye, EyeOff } from 'lucide-react'
import { apiPasswordResetConfirm } from '../../api/auth'
import Button from '../../components/ui/Button'

export default function ResetPasswordPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const uid = params.get('uid') || ''
  const token = params.get('token') || ''

  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [showPass, setShowPass] = useState(false)
  const [showConfirm, setShowConfirm] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    if (password.length < 6) { setError('The password must be at least 6 characters long'); return }
    if (password !== confirm) { setError('Passwords do not match'); return }
    setLoading(true)
    try {
      await apiPasswordResetConfirm(uid, token, password)
      setDone(true)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  if (!uid || !token) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 py-20 text-center">
        <p className="text-gray-500">Invalid password reset link.</p>
      </div>
    )
  }

  return (
    <div className="min-h-[60vh] flex items-center justify-center px-4 bg-surface-muted">
      <div className="w-full max-w-md bg-surface border border-border rounded-2xl p-5 sm:p-6 shadow-card-mid">
        <p className="text-center font-bold text-brand text-2xl mb-7">Magiya Roslyn</p>

        {done ? (
          <div className="text-center">
            <div className="w-16 h-16 rounded-full bg-brand-bg flex items-center justify-center mx-auto mb-4">
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" className="text-brand" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M20 6L9 17l-5-5"/>
              </svg>
            </div>
            <p className="font-bold text-gray-700 mb-2">Password changed!</p>
            <p className="text-sm text-gray-400 mb-6">You can now sign in with your new password.</p>
            <Button
              onClick={() => navigate('/')}
              size="lg"
              fullWidth
            >
              Back to home
            </Button>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <div>
              <label className="block text-sm font-bold text-gray-600 mb-1.5">New password</label>
              <div className="relative">
                <input
                  type={showPass ? 'text' : 'password'}
                  value={password}
                  onChange={e => { setPassword(e.target.value); setError('') }}
                  placeholder="••••••••"
                  autoComplete="new-password"
                  className="w-full border border-gray-200 rounded-xl px-4 py-3 pr-12 text-base outline-none focus:border-brand transition-colors"
                />
                <button type="button" onClick={() => setShowPass(v => !v)} aria-label={showPass ? 'Hide password' : 'Show password'} className="absolute right-2.5 top-1/2 -translate-y-1/2 p-1.5 text-gray-400 hover:text-gray-600">
                  {showPass ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </div>
            </div>

            <div>
              <label className="block text-sm font-bold text-gray-600 mb-1.5">Confirm password</label>
              <div className="relative">
                <input
                  type={showConfirm ? 'text' : 'password'}
                  value={confirm}
                  onChange={e => { setConfirm(e.target.value); setError('') }}
                  placeholder="••••••••"
                  autoComplete="new-password"
                  className={`w-full border rounded-xl px-4 py-3 pr-12 text-base outline-none focus:border-brand transition-colors ${confirm && confirm !== password ? 'border-red-300' : 'border-gray-200'}`}
                />
                <button type="button" onClick={() => setShowConfirm(v => !v)} aria-label={showConfirm ? 'Hide password' : 'Show password'} className="absolute right-2.5 top-1/2 -translate-y-1/2 p-1.5 text-gray-400 hover:text-gray-600">
                  {showConfirm ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </div>
              {confirm && confirm !== password && (
                <p className="text-xs text-red-400 mt-1">Passwords do not match</p>
              )}
            </div>

            {error && <p className="text-sm text-red-500 text-center">{error}</p>}

            <Button
              type="submit"
              disabled={loading}
              loading={loading}
              size="lg"
              fullWidth
              className="mt-1"
            >
              Save password
            </Button>
          </form>
        )}
      </div>
    </div>
  )
}
