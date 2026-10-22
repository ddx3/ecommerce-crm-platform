import { useState, useEffect } from 'react'
import type { FormEvent } from 'react'
import { useUI } from '../context/UIContext'
import { useAuth } from '../context/AuthContext'
import { notifyStock } from '../api/catalog'
import { isAxiosError } from 'axios'
import { Modal, Button, Input } from './ui'

/**
 * Global "Notify me when the product is back in stock" modal.
 * Styled 1-to-1 with AuthModal: branded header, form with Input + Button fullWidth,
 * success state with a green circle and a message.
 * Opened via useUI().openStockNotify(id, kind) — for a product's or a bundle's packaging variant.
 * Rendered in Layout.
 */
export default function StockNotifyModal() {
  const { panel, stockNotifyTarget, close } = useUI()
  const { user } = useAuth()
  const open = panel === 'stock-notify' && !!stockNotifyTarget
  const what = stockNotifyTarget?.kind === 'bundle' ? 'bundle' : 'product'

  const [email, setEmail] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [done, setDone] = useState(false)

  useEffect(() => {
    if (!open) return
    setEmail(user?.email || '')
    setError('')
    setDone(false)
    setLoading(false)
  }, [open, user])

  const handleSubmit = async (e?: FormEvent) => {
    e?.preventDefault()
    if (loading || !stockNotifyTarget) return
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      setError('Enter a valid email')
      return
    }
    setLoading(true)
    setError('')
    try {
      await notifyStock(stockNotifyTarget.id, email, stockNotifyTarget.kind)
      setDone(true)
    } catch (err) {
      const detail = isAxiosError(err) ? err.response?.data?.detail : undefined
      setError(typeof detail === 'string' ? detail : 'Could not subscribe. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Modal open={open} onClose={close} ariaLabel="Back-in-stock notification">
      <p className="text-center font-bold text-brand text-xl sm:text-2xl mb-5 sm:mb-7">Magiya Roslyn</p>

      {done ? (
        <div className="text-center py-4">
          <div className="w-16 h-16 rounded-full bg-brand-bg flex items-center justify-center mx-auto mb-4">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="#1a5c2a" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7A2 2 0 0 1 22 16.92z"/>
            </svg>
          </div>
          <p className="font-bold text-text-primary mb-2">Thank you!</p>
          <p className="text-sm text-text-muted">We will email <strong className="text-text-primary">{email}</strong> as soon as the {what} is back in stock.</p>
        </div>
      ) : (
        <form className="flex flex-col gap-4" onSubmit={handleSubmit}>
          <p className="text-sm text-text-secondary -mt-2 text-center">
            Leave your email and we will write to you as soon as the {what} is back in stock.
          </p>
          <Input
            label="Email"
            name="email"
            type="email"
            autoComplete="email"
            value={email}
            onChange={e => { setEmail(e.target.value); if (error) setError('') }}
            placeholder="email@example.com"
          />
          {error && <p className="text-sm text-danger text-center">{error}</p>}
          <Button type="submit" loading={loading} fullWidth size="lg" disabled={!email.trim()}>
            {loading ? 'Sending...' : 'Subscribe'}
          </Button>
        </form>
      )}
    </Modal>
  )
}
