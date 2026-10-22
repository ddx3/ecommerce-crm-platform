import { UserPlus } from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import { useUI } from '../context/UIContext'
import Button from './ui/Button'

/**
 * A call for a guest to save their order by creating an account. Shown only to
 * unauthenticated users and when an email is present (from the checkout URL). On registration the
 * backend
 * itself links all guest orders with this email to the new account
 * (signal link_guest_orders_on_user_create) — here it is only a UI prompt.
 */
export default function GuestSavePrompt({ email }: { email: string }) {
  const { user } = useAuth()
  const { openAuth } = useUI()
  if (user || !email) return null

  return (
    <div className="mt-2 mb-4 max-w-md w-full mx-auto bg-brand-bg/60 border border-brand/15 rounded-2xl px-5 py-4 flex flex-col items-center text-center gap-3">
      <div className="w-11 h-11 rounded-full bg-brand-bg text-brand flex items-center justify-center">
        <UserPlus size={22} />
      </div>
      <div>
        <p className="font-bold text-text-primary text-sm mb-1">Keep your orders with you</p>
        <p className="text-sm text-text-muted leading-relaxed">
          Create an account to see the status of all your orders in one place.
        </p>
      </div>
      <Button size="sm" onClick={() => openAuth('register', email)}>
        Create account
      </Button>
    </div>
  )
}
