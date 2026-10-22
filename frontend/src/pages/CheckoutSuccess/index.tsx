import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { CheckCircle } from 'lucide-react'
import client from '../../api/client'
import Button from '../../components/ui/Button'
import GuestSavePrompt from '../../components/GuestSavePrompt'
import { flushPurchase } from '../../lib/analytics'

/**
 * Success page after payment: order confirmation, monobank payment status
 * and buttons to go to orders / catalog.
 */
export default function CheckoutSuccessPage() {
  const [params] = useSearchParams()
  const orderNumber = params.get('order_id') || params.get('order') || ''
  const contact = params.get('contact') === '1'
  const token = (params.get('t') || '').trim()
  const email = (params.get('email') || '').trim()

  // Payment status (for the monobank redirect): don't show "paid" until the webhook
  // has confirmed it. If payment is not completed — prompt to retry.
  const [payInfo, setPayInfo] = useState<{ method: string; status: string; hasCourses: boolean } | null>(null)

  useEffect(() => {
    if (!orderNumber || contact) return
    let alive = true
    client.get(`/orders/${orderNumber}/status/`, { params: token ? { t: token } : {} })
      .then(r => { if (alive) setPayInfo({
        method: r.data?.payment_method || '',
        status: r.data?.payment_status || '',
        hasCourses: !!r.data?.has_courses,
      }) })
      .catch(() => {})
    return () => { alive = false }
  }, [orderNumber, token, contact])

  // Purchase: send from the order snapshot. Idempotent.
  // Monobank — ONLY after confirmed payment (otherwise abandoned/unpaid
  // online orders inflate conversion → overpaying for ads). Other methods
  // (transfer to phone etc.) — order placed = conversion.
  useEffect(() => {
    if (!orderNumber) return
    if (contact) { flushPurchase(orderNumber); return }
    if (payInfo?.method === 'monobank') {
      if (payInfo.status === 'paid') flushPurchase(orderNumber)
    } else if (payInfo) {
      flushPurchase(orderNumber)
    }
  }, [orderNumber, contact, payInfo])

  const monoPending = payInfo?.method === 'monobank' && payInfo.status !== 'paid'
  const monoPaid = payInfo?.method === 'monobank' && payInfo.status === 'paid'

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-12 sm:py-20 flex flex-col items-center">
      <div className="w-20 h-20 rounded-full bg-brand-bg flex items-center justify-center mb-6">
        <CheckCircle size={42} className="text-brand" />
      </div>
      <h1 className="text-xl sm:text-2xl font-bold text-gray-900 mb-2 text-center">Order placed!</h1>
      {orderNumber && (
        <p className="text-gray-500 mb-1 text-center">
          Order number: <span className="font-bold text-gray-700">№{orderNumber}</span>
        </p>
      )}
      {contact && (
        <p className="text-gray-500 text-sm mt-2 mb-2 text-center">
          We will send you the payment details to your phone number
        </p>
      )}

      {monoPaid && (
        <p className="text-brand text-sm font-bold mt-1 mb-1 text-center">✓ Payment received</p>
      )}
      {monoPending && (
        <div className="mt-3 mb-2 max-w-md w-full bg-amber-50 border border-amber-100 rounded-2xl px-5 py-4 text-sm text-amber-800 text-center">
          <p className="font-bold mb-1">Payment not confirmed yet</p>
          <p>
            If you have completed the payment, the funds will arrive within a few minutes
            and the status will update automatically. If the payment was not completed, you can retry
            in the “My orders” section.
          </p>
        </div>
      )}

      {/* A course is a digital item: without this hint the buyer looks for it in “My
          orders” and cannot find where to look. */}
      {payInfo?.hasCourses && (
        <div className="mt-3 mb-2 max-w-md w-full bg-brand-bg border border-brand/20 rounded-2xl px-5 py-4 text-sm text-gray-700 text-center">
          <p className="font-bold text-brand mb-1">Your order includes a course</p>
          <p>
            {payInfo.status === 'paid'
              ? 'It is already open in the “My courses” section.'
              : 'As soon as the payment is credited, it will appear in the “My courses” section.'}
          </p>
        </div>
      )}

      <GuestSavePrompt email={email} />

      <div className="mb-6" />

      {/* Navigation buttons */}
      <div className="flex gap-3 flex-col sm:flex-row w-full sm:w-auto">
        {payInfo?.hasCourses && (
          <Button as={Link} to="/my-courses">
            To my courses
          </Button>
        )}
        <Button as={Link} to="/orders" variant={payInfo?.hasCourses ? 'secondary' : 'primary'}>
          My orders
        </Button>
        <Link
          to="/catalog"
          className="border border-gray-200 hover:border-gray-400 hover:bg-gray-50 text-gray-700 font-bold px-7 py-3 rounded-2xl transition-colors text-center"
        >
          To the catalog
        </Link>
      </div>
    </div>
  )
}
