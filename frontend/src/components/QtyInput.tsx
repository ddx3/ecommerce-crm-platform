import { Minus, Plus } from 'lucide-react'
import { useState, useEffect } from 'react'

interface Props {
  value: number
  onChange: (v: number) => void
  min?: number
  max?: number
  size?: 'sm' | 'md'
  /** Stretch to the full available width (the input takes the free space). */
  fullWidth?: boolean
  className?: string
}

export default function QtyInput({ value, onChange, min = 1, max = 999, size = 'md', fullWidth = false, className = '' }: Props) {
  const [inputVal, setInputVal] = useState(String(value))

  useEffect(() => { setInputVal(String(value)) }, [value])

  const commit = (raw: string) => {
    const v = parseInt(raw, 10)
    if (!isNaN(v) && v >= min) onChange(Math.min(v, max))
    else setInputVal(String(value))
  }

  const inputEl = (
    <input
      type="text"
      inputMode="numeric"
      aria-label="Quantity"
      value={inputVal}
      onChange={e => { const v = e.target.value.replace(/\D/g, ''); const n = parseInt(v, 10); setInputVal(n > max ? String(max) : v) }}
      onBlur={e => commit(e.target.value)}
      onKeyDown={e => { if (e.key === 'Enter') commit(inputVal) }}
      className={`${fullWidth ? 'w-10 shrink-0' : (size === 'sm' ? 'w-7' : 'w-10')} text-center text-sm font-bold text-text-primary bg-transparent outline-none`}
    />
  )

  // Mobile/stretched variant: a solid h-8 block, −/+ buttons at full height.
  if (fullWidth) {
    const fbtn = 'flex-1 h-full text-text-muted hover:bg-gray-200 active:bg-gray-300 hover:text-text-primary transition-colors flex items-center justify-center disabled:opacity-40 disabled:cursor-not-allowed'
    return (
      <div className={`flex items-stretch h-9 bg-surface-muted border border-border rounded-xl overflow-hidden ${className}`}>
        <button type="button" onClick={() => onChange(Math.max(min, value - 1))} disabled={value <= min} aria-label="Decrease quantity" className={fbtn}>
          <Minus size={16} />
        </button>
        {inputEl}
        <button type="button" onClick={() => onChange(Math.min(value + 1, max))} disabled={value >= max} aria-label="Increase quantity" className={fbtn}>
          <Plus size={16} />
        </button>
      </div>
    )
  }

  const btn = size === 'sm'
    ? 'w-6 h-6 text-text-muted hover:bg-gray-200 hover:text-text-primary rounded-lg transition-colors flex items-center justify-center disabled:opacity-40 disabled:cursor-not-allowed'
    : 'w-9 h-8 sm:w-8 sm:h-8 text-text-muted hover:bg-gray-200 hover:text-text-primary rounded-xl transition-colors flex items-center justify-center disabled:opacity-40 disabled:cursor-not-allowed'
  const iconSize = size === 'sm' ? 11 : 13

  return (
    <div className={`inline-flex items-center gap-1 bg-surface-muted border border-border rounded-xl px-1 ${size === 'sm' ? 'h-8' : 'h-9'} ${className}`}>
      <button
        type="button"
        onClick={() => onChange(Math.max(min, value - 1))}
        disabled={value <= min}
        aria-label="Decrease quantity"
        className={btn}
      >
        <Minus size={iconSize} />
      </button>
      {inputEl}
      <button
        type="button"
        onClick={() => onChange(Math.min(value + 1, max))}
        disabled={value >= max}
        aria-label="Increase quantity"
        className={btn}
      >
        <Plus size={iconSize} />
      </button>
    </div>
  )
}
