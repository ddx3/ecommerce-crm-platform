import { forwardRef } from 'react'
import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { cn } from '../../utils/cn'

type Size = 'sm' | 'md' | 'lg'

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  size?: Size
  variant?: 'ghost' | 'outline'
  /** Required for accessibility — an icon-only button. */
  'aria-label': string
  children: ReactNode
}

const SIZES: Record<Size, string> = {
  sm: 'w-7 h-7',
  md: 'w-9 h-9',
  lg: 'w-11 h-11',
}

const VARIANTS = {
  ghost: 'text-text-muted hover:text-text-primary',
  outline: 'border border-border text-text-secondary hover:border-border-strong',
}

const IconButton = forwardRef<HTMLButtonElement, Props>(function IconButton(
  { size = 'md', variant = 'ghost', className, children, type = 'button', ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cn(
        'inline-flex items-center justify-center rounded-xl transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50',
        SIZES[size],
        VARIANTS[variant],
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  )
})

export default IconButton
