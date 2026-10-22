import { MessageCircleQuestion, Truck } from 'lucide-react'

// Info blocks under the price/cart on product, bundle and dropshipping pages.
// Extracted into a shared component so the cards look the same on all page types.
const ITEMS = [
  { Icon: MessageCircleQuestion, title: 'Not sure what to choose?', desc: 'Contact us — we will help' },
  { Icon: Truck, title: 'Delivery with 100% prepayment', desc: 'We ship only after full payment' },
]

export default function ProductInfoCards() {
  return (
    <div className="flex flex-col gap-3">
      {ITEMS.map(({ Icon, title, desc }) => (
        <div
          key={title}
          className="flex items-center gap-4 p-4 sm:p-5 rounded-2xl bg-surface border border-border shadow-card-low"
        >
          <div className="w-10 h-10 shrink-0 flex items-center justify-center rounded-xl bg-brand-bg">
            <Icon size={20} className="text-brand" />
          </div>
          <div>
            <p className="text-sm font-bold text-text-primary">{title}</p>
            <p className="text-xs text-text-muted mt-0.5">{desc}</p>
          </div>
        </div>
      ))}
    </div>
  )
}
