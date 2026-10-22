import { Link } from 'react-router-dom'
import { ChevronRight, RotateCcw, Package, CreditCard, AlertCircle } from 'lucide-react'
import { Card } from '../../components/ui'

export default function ReturnsPage() {
  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">

      <nav className="flex items-center gap-1 text-xs sm:text-sm text-text-muted mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        <ChevronRight size={14} />
        <span className="text-text-secondary">Returns and exchanges</span>
      </nav>

      <div className="space-y-5 sm:space-y-6">
        <h1 className="text-2xl sm:text-3xl font-bold text-text-primary">Returns and exchanges</h1>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 sm:gap-6">

          <Card variant="section" className="p-5 sm:p-6">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-brand-bg flex items-center justify-center shrink-0">
                <RotateCcw size={20} className="text-brand" />
              </div>
              <div>
                <p className="font-bold text-text-primary">14 days to return</p>
                <p className="text-xs text-text-muted">Goods of proper quality</p>
              </div>
            </div>
            <p className="text-sm text-text-secondary leading-relaxed">
              You can return goods of proper quality within <span className="font-semibold">14 calendar days</span> of receiving them.
              The item must be unchanged: unused, with the original packaging and its presentable condition preserved.
            </p>
          </Card>

          <Card variant="section" className="p-5 sm:p-6">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-brand-bg flex items-center justify-center shrink-0">
                <Package size={20} className="text-brand" />
              </div>
              <div>
                <p className="font-bold text-text-primary">Goods of improper quality</p>
                <p className="text-xs text-text-muted">Defects, damage</p>
              </div>
            </div>
            <p className="text-sm text-text-secondary leading-relaxed">
              If the item arrived damaged or defective, we will replace it or refund your money within <span className="font-semibold">7 business days</span> of receiving the return.
              In this case we cover the shipping costs.
            </p>
          </Card>

          <Card variant="section" className="p-5 sm:p-6">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-brand-bg flex items-center justify-center shrink-0">
                <CreditCard size={20} className="text-brand" />
              </div>
              <div>
                <p className="font-bold text-text-primary">Refunds</p>
                <p className="text-xs text-text-muted">To your card within 7 days</p>
              </div>
            </div>
            <p className="text-sm text-text-secondary leading-relaxed">
              We refund the money to the card the payment was made with, within 7 business days of receiving and inspecting the item.
              The cost of return shipping for goods of proper quality is paid by the buyer.
            </p>
          </Card>

          <Card variant="section" className="p-5 sm:p-6">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 rounded-xl bg-brand-bg flex items-center justify-center shrink-0">
                <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" className="text-brand" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.36 12a19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 3.11 1h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.09 8.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 21 16z"/>
                </svg>
              </div>
              <div>
                <p className="font-bold text-text-primary">How to request a return</p>
                <p className="text-xs text-text-muted">Contact us</p>
              </div>
            </div>
            <p className="text-sm text-text-secondary leading-relaxed">
              Write to us at{' '}
              <a href="mailto:shop@example.com" className="text-brand hover:underline font-medium">shop@example.com</a>
              {' '}or in a messenger — include your order number and the reason for the return. We will send you instructions for the next steps.
            </p>
          </Card>

        </div>

        <div className="bg-amber-50 border border-amber-200 rounded-2xl p-5 sm:p-6">
          <div className="flex items-start gap-3">
            <AlertCircle size={18} className="text-amber-500 shrink-0 mt-0.5" />
            <p className="text-sm text-text-secondary leading-relaxed">
              In accordance with the Law of Ukraine “On Consumer Rights Protection”, goods that have been used,
              have lost their presentable condition, or have damaged packaging through the buyer's fault, cannot be returned.
            </p>
          </div>
        </div>
      </div>

    </div>
  )
}
