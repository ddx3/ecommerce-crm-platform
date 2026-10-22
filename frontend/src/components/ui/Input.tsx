import { forwardRef, useId } from 'react'
import type { InputHTMLAttributes, ReactNode } from 'react'
import { cn } from '../../utils/cn'

type Size = 'sm' | 'md'

interface Props extends Omit<InputHTMLAttributes<HTMLInputElement>, 'size'> {
  label?: ReactNode
  error?: string
  helper?: string
  size?: Size
  /** Right-hand suffix (an icon button such as show/hide password). */
  rightAddon?: ReactNode
}

const SIZES: Record<Size, string> = {
  sm: 'h-9 text-sm',
  md: 'h-11 text-base',
}

const Input = forwardRef<HTMLInputElement, Props>(function Input(
  { label, error, helper, size = 'md', rightAddon, className, id, ...rest },
  ref,
) {
  const generatedId = useId()
  const inputId = id ?? generatedId
  const helperId = `${inputId}-helper`
  const errorId = `${inputId}-error`

  const wrapperBase = cn(
    'flex items-center w-full bg-white border rounded-xl overflow-hidden transition-colors',
    'focus-within:border-brand',
    error ? 'border-danger/60' : 'border-border',
  )

  return (
    <div className="flex flex-col gap-1.5">
      {label && (
        <label htmlFor={inputId} className="block text-sm font-bold text-text-secondary">
          {label}
        </label>
      )}
      <div className={wrapperBase}>
        <input
          ref={ref}
          id={inputId}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errorId : helper ? helperId : undefined}
          className={cn(
            'flex-1 px-4 outline-none bg-transparent placeholder:text-gray-400',
            SIZES[size],
            className,
          )}
          {...rest}
        />
        {rightAddon && <div className="pr-3 flex items-center shrink-0">{rightAddon}</div>}
      </div>
      {error
        ? <p id={errorId} className="text-xs text-danger">{error}</p>
        : helper
          ? <p id={helperId} className="text-xs text-text-muted">{helper}</p>
          : null}
    </div>
  )
})

export default Input
