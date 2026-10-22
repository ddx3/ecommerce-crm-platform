import type { ReactNode } from 'react'
import { X } from 'lucide-react'
import { cn } from '../../utils/cn'
import { useDialogBehavior } from './useDialogBehavior'
import IconButton from './IconButton'

type Size = 'sm' | 'md' | 'lg'

interface Props {
  open: boolean
  onClose: () => void
  title?: ReactNode
  children: ReactNode
  size?: Size
  /** Close on overlay click. Defaults to true. */
  closeOnOverlay?: boolean
  /** Hide the X button (e.g. if there is a custom header). */
  hideClose?: boolean
  className?: string
  ariaLabel?: string
}

const SIZES: Record<Size, string> = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-2xl',
}

export default function Modal({ open, onClose, title, children, size = 'md', closeOnOverlay = true, hideClose, className, ariaLabel }: Props) {
  const ref = useDialogBehavior({ open, onClose })
  if (!open) return null

  return (
    <div
      className="fixed inset-0 z-[200] flex items-center justify-center"
      role="dialog"
      aria-modal="true"
      aria-label={ariaLabel ?? (typeof title === 'string' ? title : 'Dialog')}
    >
      <div
        className="absolute inset-0 bg-black/40 backdrop-blur-sm"
        onClick={closeOnOverlay ? onClose : undefined}
        aria-hidden="true"
      />
      <div
        ref={ref}
        className={cn(
          'relative w-full mx-4 bg-white rounded-3xl p-6 sm:p-8 shadow-2xl max-h-[90svh] overflow-y-auto',
          SIZES[size],
          className,
        )}
      >
        {!hideClose && (
          <IconButton
            onClick={onClose}
            aria-label="Close"
            variant="ghost"
            className="absolute top-4 right-4"
          >
            <X size={20} />
          </IconButton>
        )}
        {title && <h2 className="font-bold text-lg text-text-primary mb-5">{title}</h2>}
        {children}
      </div>
    </div>
  )
}
