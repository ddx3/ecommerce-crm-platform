import { Bell } from 'lucide-react'
import { useUI } from '../context/UIContext'
import type { StockNotifyKind } from '../api/catalog'

interface Props {
  variantId: number
  kind?: StockNotifyKind
  className?: string
}

/**
 * "Notify me" button (notify when back in stock) instead of the static "Out of stock"
 * label.
 * On click it opens the global StockNotifyModal (rendered in Layout).
 */
export default function StockNotifyButton({ variantId, kind = 'variant', className = '' }: Props) {
  const { openStockNotify } = useUI()

  const handleClick = (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    openStockNotify(variantId, kind)
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      className={`flex-1 h-9 sm:h-8 flex items-center justify-center gap-2 text-xs font-bold text-gray-600 bg-gray-100 hover:bg-gray-200 hover:text-text-primary rounded-xl transition-colors px-2 ${className}`}
    >
      <Bell size={16} className="hidden lg:block" />
      <span>Notify me</span>
    </button>
  )
}
