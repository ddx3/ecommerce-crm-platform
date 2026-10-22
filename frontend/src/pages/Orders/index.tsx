import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Package, ChevronDown, ChevronRight, GraduationCap, FileText } from 'lucide-react'

import { useAuth } from '../../context/AuthContext'
import { useUI } from '../../context/UIContext'
import { apiMyOrders, apiOpenOrderInstruction, apiChangePaymentMethod } from '../../api/auth'
import client from '../../api/client'
import type { UserOrder } from '../../api/auth'
import Button from '../../components/ui/Button'
import { formatPrice } from '../../utils/price'

const STATUS_COLOR: Record<string, string> = {
  new: 'text-blue-500',
  processing: 'text-amber-500',
  shipped: 'text-purple-500',
  delivered: 'text-brand',
  cancelled: 'text-red-400',
}

const STATUS_LABEL: Record<string, string> = {
  new: 'Accepted',
  processing: 'Processing',
  shipped: 'Shipped',
  delivered: 'Delivered',
  cancelled: 'Cancelled',
}

const PAYMENT_LABEL: Record<string, string> = {
  monobank: 'Monobank',
  cod: 'Cash on delivery',
  bank_transfer: 'Card transfer',
  cash: 'Cash',
  own_expense: 'At our expense',
}

// Only what checkout offers (cod remains only as a label for old orders — we don't offer it for new
// orders in self-service, same as at checkout).
const PAYMENT_METHODS: { value: string; label: string; hint: string }[] = [
  { value: 'monobank', label: 'Monobank', hint: 'Online card payment or Apple/Google Pay' },
  { value: 'bank_transfer', label: 'Card transfer', hint: 'PrivatBank or Monobank' },
]

export default function OrdersPage() {
  const { token } = useAuth()
  const { openAuth } = useUI()
  const navigate = useNavigate()
  const [orders, setOrders] = useState<UserOrder[]>([])
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [openComposition, setOpenComposition] = useState<string | null>(null)
  const [paying, setPaying] = useState<string | null>(null)
  const [payError, setPayError] = useState<string | null>(null)
  const [loadingInstruction, setLoadingInstruction] = useState<number | null>(null)
  const [instructionError, setInstructionError] = useState<number | null>(null)
  const [changingPayment, setChangingPayment] = useState<string | null>(null)
  const [savingPayment, setSavingPayment] = useState<string | null>(null)
  const [paymentMethodError, setPaymentMethodError] = useState<string | null>(null)

  const handleChangePaymentMethod = async (orderNumber: string, method: string) => {
    setSavingPayment(orderNumber)
    setPaymentMethodError(null)
    try {
      await apiChangePaymentMethod(orderNumber, method)
      setOrders(prev => prev.map(o => o.order_number === orderNumber ? { ...o, payment_method: method } : o))
      setChangingPayment(null)
    } catch {
      setPaymentMethodError(orderNumber)
    } finally {
      setSavingPayment(null)
    }
  }

  const handleInstruction = async (orderNumber: string, itemId: number) => {
    setLoadingInstruction(itemId)
    setInstructionError(null)
    try {
      await apiOpenOrderInstruction(orderNumber, itemId)
    } catch {
      setInstructionError(itemId)
    } finally {
      setLoadingInstruction(null)
    }
  }

  // NOTE: the mark-paid endpoint on the backend no longer sets payment_status='paid' immediately,
  // it only signals the manager (verification needed). If needed, call here
  // /api/orders/<num>/mark-paid/ and show "Awaiting verification" instead of "Paid".

  const handlePay = async (orderNumber: string) => {
    if (!token) return
    setPaying(orderNumber)
    setPayError(null)
    try {
      // Through the axios client: token from localStorage + auto-refresh on 401.
      const { data } = await client.post(`/orders/${orderNumber}/pay/`)
      if (data?.payment_url) {
        window.location.href = data.payment_url
        return
      }
      setPayError(orderNumber)
    } catch {
      setPayError(orderNumber)
    } finally {
      setPaying(null)
    }
  }

  useEffect(() => {
    if (!token) { setLoading(false); return }
    apiMyOrders()
      .then(setOrders)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [token])

  if (loading) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-12 sm:py-20 flex justify-center">
        <div className="w-10 h-10 border-4 border-gray-200 border-t-brand rounded-full animate-spin" />
      </div>
    )
  }

  if (!token) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
        <nav className="flex items-center gap-1 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6">
          <Link to="/" className="hover:text-brand transition-colors">Home</Link>
          <ChevronRight size={14} />
          <span className="text-gray-600">My orders</span>
        </nav>
        <h1 className="text-xl sm:text-2xl font-bold text-gray-900 mb-6 sm:mb-8">My orders</h1>
        <div className="flex flex-col items-center justify-center py-16 sm:py-32 text-center">
          <div className="w-20 h-20 rounded-full bg-brand-bg flex items-center justify-center mb-5">
            <Package size={36} className="text-brand" />
          </div>
          <p className="text-xl font-bold text-gray-700 mb-6">Sign in to your account</p>
          <Button onClick={() => openAuth('login')}>
            Sign in
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
      <nav className="flex items-center gap-1 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        <ChevronRight size={14} />
        <span className="text-gray-600">My orders</span>
      </nav>
      <div className="flex items-center justify-between mb-6 sm:mb-8">
        <h1 className="text-xl sm:text-2xl font-bold text-gray-900">
          My orders{orders.length > 0 && <span className="text-gray-400 font-bold ml-2">({orders.length})</span>}
        </h1>
      </div>

      {orders.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-16 sm:py-32 text-center">
          <div className="w-20 h-20 rounded-full bg-brand-bg flex items-center justify-center mb-5">
            <Package size={36} className="text-brand" />
          </div>
          <p className="text-xl font-bold text-gray-700 mb-2">No orders yet</p>
          <Button as={Link} to="/catalog">
            Go to catalog
          </Button>
        </div>
      ) : (
        <div className="flex flex-col gap-4">
          {orders.map(order => {
            const isOpen = expanded === order.id

            return (
              <div key={order.id} className="bg-surface rounded-2xl border border-border shadow-card-mid overflow-hidden">
                <div className="flex-1 min-w-0">

                {/* Header */}
                <button
                  onClick={() => setExpanded(isOpen ? null : order.id)}
                  className="w-full flex items-center gap-3 sm:gap-6 px-4 sm:px-6 py-4 sm:py-5 hover:bg-gray-50 transition-colors text-left"
                >
                  <div className="flex-1 min-w-0">
                    <p className="font-bold text-gray-800 text-base">#{order.order_number}</p>
                    <p className="text-sm text-gray-400 mt-0.5 truncate">{order.created_at}</p>
                  </div>

                  <div className="text-right shrink-0">
                    <p className="font-bold text-gray-800 text-base">{order.total_price ? `${formatPrice(order.total_price)} UAH` : '—'}</p>
                    <p className={`text-xs font-medium mt-0.5 ${STATUS_COLOR[order.status] || 'text-gray-400'}`}>
                      {STATUS_LABEL[order.status] || order.status_display}
                    </p>
                  </div>

                  <ChevronDown size={18} className={`text-gray-400 shrink-0 transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`} />
                </button>

                {/* Details */}
                {isOpen && (
                  <div className="border-t border-gray-100">
                    <div>
                      <div className="flex flex-col text-sm">
                        {(order.delivery_address || order.delivery_type) && (
                          <>
                            <div className="px-4 sm:px-6 py-3.5">
                              <span className="font-bold text-gray-800 text-base">Delivery</span>
                              <p className="text-gray-700 font-medium mt-1">
                                {/* The label is computed by the backend: self-pickup and the old Ukrposhta ones too. */}
                                {order.delivery_type_display || order.delivery_type}
                                {order.delivery_address ? ` · ${order.delivery_address}` : ''}
                              </p>
                            </div>
                            <hr className="border-gray-100 mx-0" />
                          </>
                        )}
                        <div className="px-4 sm:px-6 py-3.5">
                          <span className="flex items-center gap-2">
                            <span className="font-bold text-gray-800 text-base">Payment method</span>
                            {order.payment_status !== 'paid' && order.status !== 'cancelled' && (
                              <button
                                type="button"
                                onClick={() => setChangingPayment(changingPayment === order.order_number ? null : order.order_number)}
                                className="text-xs font-medium text-gray-400 hover:text-brand transition-colors"
                              >
                                (Change)
                              </button>
                            )}
                          </span>
                          <p className="text-gray-700 font-medium mt-1">
                            {PAYMENT_LABEL[order.payment_method] || order.payment_method}
                          </p>
                          {changingPayment === order.order_number && (
                            <div className="flex flex-col gap-2 mt-3">
                              {PAYMENT_METHODS.map(m => (
                                <label
                                  key={m.value}
                                  className={`flex items-center gap-4 p-4 border rounded-xl transition-colors ${order.payment_method === m.value ? 'border-gray-300 bg-gray-50 cursor-default' : 'border-gray-200 hover:bg-gray-100 cursor-pointer'} ${savingPayment === order.order_number ? 'opacity-60 pointer-events-none' : ''}`}
                                >
                                  <input
                                    type="radio"
                                    name={`payment-${order.order_number}`}
                                    value={m.value}
                                    checked={order.payment_method === m.value}
                                    onChange={() => handleChangePaymentMethod(order.order_number, m.value)}
                                    className="accent-gray-600"
                                  />
                                  <div>
                                    <p className="text-sm font-bold text-gray-800">{m.label}</p>
                                    <p className="text-xs text-gray-400">{m.hint}</p>
                                  </div>
                                </label>
                              ))}
                            </div>
                          )}
                          {paymentMethodError === order.order_number && (
                            <p className="text-xs text-red-600 mt-2">Could not change the payment method. Please try again.</p>
                          )}
                        </div>
                        <hr className="border-gray-100 mx-0" />
                        <div className="px-4 sm:px-6 py-3.5">
                          <span className="flex items-center gap-2">
                            <span className="font-bold text-gray-800 text-base">Payment status</span>
                            {order.payment_method === 'monobank' && order.payment_status !== 'paid' && (
                              <button
                                type="button"
                                onClick={() => handlePay(order.order_number)}
                                disabled={paying === order.order_number}
                                className="text-xs font-medium text-gray-400 hover:text-brand transition-colors disabled:opacity-50"
                              >
                                ({paying === order.order_number ? '...' : 'Pay'})
                              </button>
                            )}
                            {order.payment_method === 'bank_transfer' && order.payment_status !== 'paid' && (
                              <button
                                type="button"
                                onClick={() => navigate(`/checkout/payment?order_id=${order.order_number}&total=${Math.round(parseFloat(order.total_price || '0'))}`)}
                                className="text-xs font-medium text-gray-400 hover:text-brand transition-colors"
                              >
                                (Payment details)
                              </button>
                            )}
                          </span>
                          <p className={`font-medium mt-1 ${order.payment_status === 'paid' ? 'text-brand' : 'text-amber-500'}`}>
                            {order.payment_status_display}
                          </p>
                        </div>
                        {payError === order.order_number && (
                          <p className="px-4 sm:px-6 pb-3 text-sm text-red-600">
                            Could not open the payment page. Please try again.
                          </p>
                        )}
                        {order.comment && (
                          <>
                            <hr className="border-gray-100 mx-0" />
                            <div className="px-4 sm:px-6 py-3.5">
                              <span className="font-bold text-gray-800 text-base">Comment</span>
                              <p className="text-gray-700 mt-1">{order.comment}</p>
                            </div>
                          </>
                        )}
                      </div>
                      <hr className="border-gray-100 mx-0" />
                      <div className="px-4 sm:px-6 py-3.5">
                        <p className="font-bold text-gray-800 text-base mb-3">Order contents</p>
                        <div className="flex flex-col gap-3">
                          {order.items.map((item, i) => {
                            // A course is a digital item: the link leads to the account area where
                            // it is already open,
                            // not to the catalog (it has no product card there).
                            const itemLink = item.is_course
                              ? '/my-courses'
                              : item.is_bundle ? `/bundle/${item.slug}` : `/product/${item.slug}`
                            return (
                            <div key={i} className="flex flex-col gap-2">
                              <div className="flex items-center gap-4">
                                <Link to={itemLink} className="group w-14 h-14 rounded-xl shrink-0 overflow-hidden flex items-center justify-center ring-1 ring-gray-200 hover:ring-gray-400 transition-all">
                                  {item.image
                                    ? <div className="relative w-full h-full">
                                        <img src={item.image} alt={item.name} loading="lazy" className="w-full h-full object-contain p-1 rounded-lg" />
                                        <div className="absolute inset-0 bg-black/0 group-hover:bg-black/8 group-active:bg-black/8 transition-colors duration-200" />
                                      </div>
                                    : item.is_course
                                      ? <GraduationCap size={20} className="text-brand" />
                                      : <Package size={20} className="text-gray-300" />
                                  }
                                </Link>
                                <div className="flex-1 min-w-0 self-stretch flex flex-col justify-between py-0.5">
                                  <Link to={itemLink} className="text-sm font-bold text-gray-800 hover:text-brand transition-colors line-clamp-1 block">{item.name}</Link>
                                  {item.is_course
                                    ? <p className="text-xs text-brand font-medium">Course — see "My courses"</p>
                                    : <p className="text-xs text-gray-400">{item.sku}</p>}
                                </div>
                                <div className="text-right shrink-0 self-start py-0.5">
                                  <p className="text-sm font-bold text-gray-800">{formatPrice(parseFloat(item.price) * item.quantity)} UAH</p>
                                  {item.quantity > 1 && <p className="text-xs text-gray-400">{formatPrice(item.price)} × {item.quantity}</p>}
                                </div>
                              </div>

                              {(item.has_instruction || (item.composition && item.composition.length > 0)) && (
                                <div className="flex flex-col gap-2">
                                  <div className="flex items-center gap-4 flex-wrap">
                                    {item.composition && item.composition.length > 0 && (
                                      <button
                                        onClick={() => { const key = `${order.id}-${i}`; setOpenComposition(openComposition === key ? null : key) }}
                                        className="flex items-center gap-1 text-xs text-gray-400 hover:text-brand transition-colors"
                                      >
                                        <ChevronDown size={13} className={`transition-transform ${openComposition === `${order.id}-${i}` ? 'rotate-180' : ''}`} />
                                        Bundle contents
                                      </button>
                                    )}
                                    {item.has_instruction && item.id != null && (
                                      <button
                                        onClick={() => handleInstruction(order.order_number, item.id!)}
                                        disabled={loadingInstruction === item.id}
                                        className="flex items-center gap-1 text-xs text-brand font-medium hover:underline transition-colors"
                                      >
                                        <FileText size={13} />
                                        {loadingInstruction === item.id ? 'Opening…' : 'Instructions (PDF)'}
                                      </button>
                                    )}
                                  </div>
                                  {instructionError === item.id && (
                                    <p className="text-xs text-red-500">Could not open the instructions. Please try again.</p>
                                  )}
                                  {openComposition === `${order.id}-${i}` && item.composition?.map((c, j) => (
                                    <Link key={j} to={`/product/${c.slug}`} className="flex items-center gap-2 bg-surface-muted border border-border rounded-xl px-2.5 py-1.5 hover:bg-gray-200 active:bg-gray-300 transition-colors">
                                      <div className="w-7 h-7 shrink-0 rounded-lg overflow-hidden bg-white ring-1 ring-border">
                                        {c.image
                                          ? <img src={c.image} alt={c.name} loading="lazy" className="w-full h-full object-contain p-0.5" />
                                          : <Package size={12} className="text-gray-300 m-auto mt-1.5" />
                                        }
                                      </div>
                                      <div className="flex-1 min-w-0">
                                        <p className="text-xs text-gray-700 line-clamp-1">{c.name}</p>
                                      </div>
                                      <span className="text-xs font-bold text-gray-500 shrink-0">× {c.quantity}</span>
                                    </Link>
                                  ))}
                                </div>
                              )}
                            </div>
                            )
                          })}
                        </div>

                      </div>

                    </div>
                  </div>
                )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
