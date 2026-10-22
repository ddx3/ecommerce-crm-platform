import { cn } from '../../utils/cn'

type Size = 'sm' | 'md' | 'lg'
type Tone = 'brand' | 'white' | 'muted'

interface Props {
  size?: Size
  tone?: Tone
  className?: string
  label?: string
}

const SIZES: Record<Size, string> = {
  sm: 'w-4 h-4 border-2',
  md: 'w-6 h-6 border-2',
  lg: 'w-10 h-10 border-4',
}

const TONES: Record<Tone, string> = {
  brand: 'border-gray-200 border-t-brand',
  white: 'border-white/40 border-t-white',
  muted: 'border-gray-200 border-t-gray-400',
}

export default function Spinner({ size = 'md', tone = 'brand', className, label = 'Loading' }: Props) {
  return (
    <span
      role="status"
      aria-label={label}
      className={cn('inline-block rounded-full animate-spin', SIZES[size], TONES[tone], className)}
    />
  )
}
