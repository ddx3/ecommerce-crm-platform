import { forwardRef, useId } from 'react'
import type { ReactNode } from 'react'
import { cn } from '../../utils/cn'

const TEMPLATE = '000 000 00 00'

/** Formats up to 10 digits of the local part (with the leading zero) as "0XX XXX XX XX". */
function formatLocal(digits: string): string {
  let f = ''
  if (digits.length > 0) f += digits.slice(0, 3)
  if (digits.length > 3) f += ' ' + digits.slice(3, 6)
  if (digits.length > 6) f += ' ' + digits.slice(6, 8)
  if (digits.length > 8) f += ' ' + digits.slice(8, 10)
  return f
}

/** Extracts the local 10 digits (with the leading zero) from any format (with/without +38/+380). */
export function toLocalDigits(raw: string): string {
  let d = raw.replace(/\D/g, '')
  if (d.startsWith('380')) d = d.slice(2) // keep the zero of the local part: 380XXXXXXXXX → 0XXXXXXXXX
  return d.slice(0, 10)
}

interface Props {
  label?: ReactNode
  error?: string
  helper?: string
  /** Any format — the component extracts the digits itself. */
  value: string
  /** Returns the formatted local part "XX XXX XX XX". */
  onChange: (formatted: string) => void
  id?: string
  name?: string
  autoComplete?: string
}

/**
 * The single phone input field: +38 prefix, ghost mask "000 000 00 00" (with the leading zero),
 * tabular-nums. Used in registration, login and checkout.
 */
const PhoneInput = forwardRef<HTMLInputElement, Props>(function PhoneInput(
  { label, error, helper, value, onChange, id, name = 'tel', autoComplete = 'tel-national' },
  ref,
) {
  const generatedId = useId()
  const inputId = id ?? generatedId
  const helperId = `${inputId}-helper`
  const errorId = `${inputId}-error`

  const localPart = formatLocal(toLocalDigits(value))
  const remainder = TEMPLATE.slice(localPart.length)

  return (
    <div className="flex flex-col gap-1.5">
      {label && (
        <label htmlFor={inputId} className="block text-sm font-bold text-text-secondary">
          {label}
        </label>
      )}
      <div
        className={cn(
          // Height is set on the content (h-11 below), not on the wrapper: the border is added
          // outside,
          // as in Input, otherwise the phone field would be 2px shorter than the neighboring
          // fields.
          'flex items-stretch w-full bg-white border rounded-xl overflow-hidden transition-colors',
          error ? 'border-danger/60' : 'border-border focus-within:border-brand',
        )}
      >
        {/* All three elements are centered the same way — flex items-center — so that the real
            input, the prefix and the ghost mask sit at the same height (the browser's centering
            of text inside an input computes the Nunito metrics differently and caused a mismatch).
            translate-y is an optical compensation for Nunito's bottom line reserve. */}
        <span className="pl-4 pr-2 text-base text-text-muted shrink-0 flex items-center translate-y-[2px]">+38</span>
        <div className="relative flex-1 min-w-0 flex items-center h-11">
          <input
            ref={ref}
            id={inputId}
            name={name}
            type="tel"
            inputMode="numeric"
            autoComplete={autoComplete}
            maxLength={TEMPLATE.length}
            value={localPart}
            onChange={e => onChange(formatLocal(toLocalDigits(e.target.value)))}
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? errorId : helper ? helperId : undefined}
            className="w-full pr-4 outline-none bg-transparent text-base relative z-10 translate-y-[2px]"
            style={{ fontVariantNumeric: 'tabular-nums' }}
          />
          {remainder && (
            <div
              aria-hidden
              className="absolute inset-0 flex items-center pr-4 text-base pointer-events-none select-none translate-y-[2px]"
              style={{ fontVariantNumeric: 'tabular-nums' }}
            >
              <span className="invisible">{localPart}</span>
              <span className="text-gray-300 whitespace-pre">{remainder}</span>
            </div>
          )}
        </div>
      </div>
      {error
        ? <p id={errorId} className="text-xs text-danger">{error}</p>
        : helper
          ? <p id={helperId} className="text-xs text-text-muted">{helper}</p>
          : null}
    </div>
  )
})

export default PhoneInput
