import { forwardRef } from 'react'
import type { ButtonHTMLAttributes, AnchorHTMLAttributes, ReactNode, ElementType } from 'react'
import { cn } from '../../utils/cn'

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger'
type Size = 'sm' | 'md' | 'lg'

type OwnProps = {
  variant?: Variant
  size?: Size
  loading?: boolean
  fullWidth?: boolean
  iconLeft?: ReactNode
  iconRight?: ReactNode
  /** Render a different element (e.g. react-router <Link>). Defaults to <button>. */
  as?: ElementType
  type?: ButtonHTMLAttributes<HTMLButtonElement>['type']
  /** react-router <Link> accepts to. Passed through only when as=Link. */
  to?: string
}

// Own props plus <button> and <a>/<Link> props. No index signature,
// so as not to "poison" the types of own props (variant/size etc.) as unknown.
type Props = OwnProps &
  Omit<ButtonHTMLAttributes<HTMLButtonElement>, keyof OwnProps> &
  Omit<AnchorHTMLAttributes<HTMLAnchorElement>, keyof OwnProps | keyof ButtonHTMLAttributes<HTMLButtonElement>>

const VARIANTS: Record<Variant, string> = {
  primary: 'bg-brand text-white hover:bg-brand-hover active:bg-brand-active disabled:bg-gray-300 disabled:cursor-not-allowed',
  secondary: 'bg-white text-text-primary border border-border hover:border-border-strong hover:bg-surface-muted disabled:opacity-60 disabled:cursor-not-allowed',
  ghost: 'bg-transparent text-text-secondary hover:text-text-primary hover:bg-surface-muted disabled:opacity-50 disabled:cursor-not-allowed',
  danger: 'bg-danger text-white hover:bg-red-600 active:bg-red-700 disabled:bg-gray-300 disabled:cursor-not-allowed',
}

const SIZES: Record<Size, string> = {
  sm: 'h-9 px-3 text-sm rounded-xl gap-1.5',
  md: 'h-11 px-5 text-base rounded-xl gap-2',
  lg: 'h-13 px-6 text-base rounded-2xl gap-2',
}

const Button = forwardRef<HTMLElement, Props>(function Button(
  { variant = 'primary', size = 'md', loading, fullWidth, iconLeft, iconRight, className, children, disabled, type, as, ...rest },
  ref,
) {
  const Comp: ElementType = as ?? 'button'
  const isNative = Comp === 'button'
  // Props valid only for a native <button>. We do not pass them through for <Link>/<a>.
  const nativeProps = isNative
    ? { type: type ?? 'button', disabled: disabled || loading, 'aria-busy': loading || undefined }
    : {}

  return (
    <Comp
      ref={ref}
      {...nativeProps}
      className={cn(
        'inline-flex items-center justify-center font-bold transition-colors select-none whitespace-nowrap',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50 focus-visible:ring-offset-2',
        VARIANTS[variant],
        SIZES[size],
        fullWidth && 'w-full',
        className,
      )}
      {...rest}
    >
      {loading
        ? <span className="w-4 h-4 border-2 border-white/40 border-t-white rounded-full animate-spin" aria-hidden="true" />
        : iconLeft}
      {children}
      {iconRight}
    </Comp>
  )
})

export default Button
