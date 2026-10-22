import { Truck, ChevronDown } from 'lucide-react'

interface Props {
  index: number
  total: number
  open: boolean
  onToggle: () => void
}

export default function ShipmentGroupHeader({ index, total, open, onToggle }: Props) {
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={open}
      className="w-full flex items-center gap-2 px-4 py-2.5 bg-surface-muted border-b border-border hover:bg-gray-200 active:bg-gray-300 transition-colors"
    >
      <Truck size={14} className="text-amber-600 shrink-0" />
      <span className="flex-1 text-left text-xs font-bold text-text-primary leading-none">
        Parcel {index}/{total}
      </span>
      <ChevronDown
        size={18}
        className={`text-text-muted shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
      />
    </button>
  )
}
