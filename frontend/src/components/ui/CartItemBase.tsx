import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Trash2, GraduationCap } from 'lucide-react'
import QtyInput from '../QtyInput'
import type { CartItem } from '../../context/CartContext'
import { cn } from '../../utils/cn'
import { formatPrice } from '../../utils/price'

function itemHref(item: CartItem): string {
  if (item.kind === 'course') return `/course/${item.productSlug}`
  if (item.isBundle) return `/bundle/${item.productSlug}`
  return `/product/${item.productSlug}`
}

type Mode = 'page' | 'drawer' | 'checkout'

interface Props {
  item: CartItem
  mode?: Mode
  onQtyChange?: (qty: number) => void
  onRemove?: () => void
  onLinkClick?: () => void
  /** Extra block under the price / qty (e.g. bundle contents). */
  extra?: ReactNode
  className?: string
}

/** Universal product card for cart/checkout/drawer.
 *  - page: horizontal layout (Cart page) — image on the left, qty/price/remove on the right in a
 *    row.
 *  - drawer: compact in the side panel — image top-left, qty/price under it, remove absolutely
 *    positioned.
 *  - checkout: same as drawer, but with a smaller image (16x16).
 */
export default function CartItemBase({
  item,
  mode = 'drawer',
  onQtyChange,
  onRemove,
  onLinkClick,
  extra,
  className,
}: Props) {
  const href = itemHref(item)
  const isCourse = item.kind === 'course'

  if (mode === 'page') {
    return (
      <div
        className={cn(
          'bg-surface border border-border rounded-2xl shadow-card-low p-3 sm:p-4',
          'flex flex-col sm:flex-row sm:items-center gap-3 sm:gap-4',
          className,
        )}
      >
        <div className="flex gap-3 flex-1 min-w-0">
          <Link
            to={href}
            onClick={onLinkClick}
            className="w-16 h-16 sm:w-20 sm:h-20 shrink-0 rounded-xl bg-surface-muted overflow-hidden"
          >
            {item.image
              ? <img src={item.image} alt={item.variantName} className="w-full h-full object-contain p-1" />
              : <div className="w-full h-full bg-surface-muted" />
            }
          </Link>
          <div className="flex-1 min-w-0">
            <Link
              to={href}
              onClick={onLinkClick}
              className="text-sm sm:text-base font-bold text-text-primary hover:text-brand transition-colors line-clamp-2 block"
            >
              {item.variantName}
            </Link>
            {!isCourse && item.sku && <p className="text-xs text-text-disabled">{item.sku}</p>}
          </div>
        </div>
        <div className="flex items-center justify-between gap-3 sm:gap-4">
          {!isCourse && onQtyChange && (
            <QtyInput value={item.qty} onChange={onQtyChange} max={item.maxQty} />
          )}
          <div className="text-right shrink-0 sm:w-28">
            <p className="font-bold text-base sm:text-lg text-gray-900">{formatPrice(item.price * item.qty)} UAH</p>
            <p className="text-xs text-text-muted">{formatPrice(item.price)} UAH / pc</p>
          </div>
          {onRemove && (
            <button
              type="button"
              onClick={onRemove}
              aria-label={`Remove ${item.variantName}`}
              className="w-9 h-9 flex items-center justify-center text-text-disabled hover:text-danger transition-colors shrink-0"
            >
              <Trash2 size={16} />
            </button>
          )}
        </div>
        {extra && <div className="basis-full">{extra}</div>}
      </div>
    )
  }

  // drawer / checkout layout — identical look in both modes.
  void mode
  return (
    <div className={cn('relative px-4 py-3 border-b border-border flex flex-col gap-3', className)}>
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          aria-label={`Remove ${item.variantName}`}
          className="absolute bottom-3 right-4 text-text-disabled hover:text-danger transition-colors"
        >
          <Trash2 size={18} />
        </button>
      )}
      <div className="flex gap-3">
        <Link
          to={href}
          onClick={onLinkClick}
          className={cn(
            'group relative block shrink-0 self-start rounded-xl overflow-hidden bg-white ring-1 ring-border hover:ring-border-strong transition-colors',
            isCourse ? 'w-28' : 'w-20 h-20',
          )}
        >
          {item.image
            ? (
              <div className={cn('relative w-full', isCourse ? '' : 'h-full')}>
                <img src={item.image} alt={item.variantName} className={cn('w-full object-contain', isCourse ? 'h-auto block' : 'h-full p-1.5')} />
                <div className="absolute inset-0 bg-black/0 group-hover:bg-black/10 transition-colors duration-300" />
              </div>
            )
            : <div className="w-full h-full bg-white" />
          }
        </Link>
        {/* Name on top; SKU and price — next to each other at the bottom. */}
        <div className="flex-1 min-w-0 flex flex-col justify-between self-stretch">
          <Link
            to={href}
            onClick={onLinkClick}
            className="text-sm font-bold text-text-primary hover:text-brand transition-colors line-clamp-2 leading-snug block"
          >
            {isCourse ? (item.productName || item.variantName) : item.variantName}
          </Link>
          <div className="flex items-baseline justify-between gap-2 mt-2">
            <span className="text-xs text-text-muted truncate">{isCourse ? '' : (item.sku ?? '')}</span>
            <div className="flex items-baseline gap-2 shrink-0">
              {!isCourse && item.qty > 1 && (
                <span className="text-xs text-text-muted">{item.price} × {item.qty}</span>
              )}
              <p className="text-base font-bold text-gray-900 leading-none">{formatPrice(item.price * item.qty)} UAH</p>
            </div>
          </div>
        </div>
      </div>
      {isCourse && (
        <p className="text-base text-brand font-bold flex items-center gap-1.5">
          <GraduationCap size={18} /> Digital course
        </p>
      )}
      {extra}
      {!isCourse && onQtyChange && (
        <div className="flex items-center">
          <QtyInput value={item.qty} onChange={onQtyChange} size="sm" max={item.maxQty} />
        </div>
      )}
    </div>
  )
}
