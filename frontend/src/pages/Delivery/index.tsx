import { Link } from 'react-router-dom'
import { ChevronRight, Truck, CreditCard, Clock, Package, AlertCircle } from 'lucide-react'
import { Card } from '../../components/ui'

export default function DeliveryPage() {
  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">

      <nav className="flex items-center gap-1 text-xs sm:text-sm text-text-muted mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        <ChevronRight size={14} />
        <span className="text-text-secondary">Payment and delivery</span>
      </nav>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 sm:gap-6 mb-5 sm:mb-6">

        {/* Payment */}
        <div className="space-y-4">
          <h2 className="text-2xl sm:text-3xl font-bold text-text-primary">Payment</h2>

          <Card variant="section" className="p-5 sm:p-6">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-brand-bg flex items-center justify-center shrink-0">
                <CreditCard size={20} className="text-brand" />
              </div>
              <div>
                <p className="font-bold text-text-primary">Online by card</p>
                <p className="text-xs text-text-muted">Visa / Mastercard via Monobank</p>
              </div>
            </div>
            <p className="text-sm text-text-secondary leading-relaxed">
              Pay by bank card through the secure Monobank payment service. All Visa and Mastercard cards are supported.
            </p>
          </Card>

          <Card variant="section" className="p-5 sm:p-6">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-brand-bg flex items-center justify-center shrink-0">
                <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" className="text-brand" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <rect width="20" height="14" x="2" y="5" rx="2"/>
                  <line x1="2" x2="22" y1="10" y2="10"/>
                </svg>
              </div>
              <div>
                <p className="font-bold text-text-primary">Card transfer</p>
                <p className="text-xs text-text-muted">PrivatBank / Monobank</p>
              </div>
            </div>
            <p className="text-sm text-text-secondary leading-relaxed">
              Transfer to a PrivatBank or Monobank card. We provide the payment details after you place the order.
            </p>
          </Card>

          <Card variant="section" className="p-5 sm:p-6">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-brand-bg flex items-center justify-center shrink-0">
                <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" className="text-brand" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><line x1="3" x2="21" y1="6" y2="6"/><path d="M16 10a4 4 0 0 1-8 0"/>
                </svg>
              </div>
              <div>
                <p className="font-bold text-text-primary">Minimum order</p>
                <p className="text-xs text-text-muted">From 250 UAH</p>
              </div>
            </div>
            <p className="text-sm text-text-secondary leading-relaxed">
              The minimum order amount is 250 UAH. Orders for a smaller amount cannot be placed.
            </p>
          </Card>

        </div>

        {/* Delivery */}
        <div className="space-y-4">
          <h2 className="text-2xl sm:text-3xl font-bold text-text-primary">Delivery</h2>

          <Card variant="section" className="p-5 sm:p-6">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-brand-bg flex items-center justify-center shrink-0">
                <Truck size={20} className="text-brand" />
              </div>
              <div>
                <p className="font-bold text-text-primary">Nova Poshta</p>
                <p className="text-xs text-text-muted">To a branch, a parcel locker or by courier</p>
              </div>
            </div>
            <p className="text-sm text-text-secondary leading-relaxed">
              Delivery requires 100% prepayment and is charged at the carrier's rates.
            </p>
          </Card>

          <Card variant="section" className="p-5 sm:p-6">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-brand-bg flex items-center justify-center shrink-0">
                <Clock size={20} className="text-brand" />
              </div>
              <div>
                <p className="font-bold text-text-primary">Delivery times</p>
                <p className="text-xs text-text-muted">Usually 1–3 days</p>
              </div>
            </div>
            <p className="text-sm text-text-secondary leading-relaxed">
              We usually ship the day after payment. Delivery takes 1–3 days depending on the region.
            </p>
          </Card>

          <Card variant="section" className="p-5 sm:p-6">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-brand-bg flex items-center justify-center shrink-0">
                <Package size={20} className="text-brand" />
              </div>
              <div>
                <p className="font-bold text-text-primary">Made-to-order items</p>
                <p className="text-xs text-text-muted">Branch pickup only</p>
              </div>
            </div>
            <p className="text-sm text-text-secondary leading-relaxed">
              Made-to-order items are delivered to a branch only — they are not delivered to an address by courier. If your order contains both regular and made-to-order items, they are shipped as separate parcels.
            </p>
          </Card>

          <div className="bg-amber-50 border border-amber-200 rounded-2xl p-5 sm:p-6">
            <div className="flex items-start gap-3">
              <AlertCircle size={18} className="text-amber-500 shrink-0 mt-0.5" />
              <div>
                <p className="font-bold text-text-primary text-sm mb-1">Bulky items</p>
                <p className="text-sm text-text-secondary leading-relaxed">
                  Agrotextile, shade netting and similar items are shipped only to a <span className="font-bold">Nova Poshta cargo branch</span> — a branch with no weight limit or a limit of 200–1000 kg.
                </p>
              </div>
            </div>
          </div>

        </div>

      </div>

    </div>
  )
}
