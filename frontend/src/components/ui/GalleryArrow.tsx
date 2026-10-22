import { ChevronLeft, ChevronRight } from 'lucide-react'
import { cn } from '../../utils/cn'

interface Props {
  dir: 'left' | 'right'
  onClick: () => void
  disabled?: boolean
  /** Positioning classes (absolute left-0 / shrink-0 etc.). */
  className?: string
  label?: string
}

/** Round arrow switch — the single style for photo galleries and carousels
 *  (consistent with HScroller).
 */
export default function GalleryArrow({ dir, onClick, disabled, className, label }: Props) {
  const Icon = dir === 'left' ? ChevronLeft : ChevronRight
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label || (dir === 'left' ? 'Back' : 'Next')}
      className={cn(
        'w-10 h-10 bg-white rounded-full shadow-md border border-gray-200 flex items-center justify-center text-gray-700 hover:text-gray-900 hover:bg-gray-200 disabled:opacity-30 disabled:pointer-events-none transition-all',
        className,
      )}
    >
      <Icon size={20} />
    </button>
  )
}
