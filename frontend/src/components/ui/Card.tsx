import { forwardRef } from 'react'
import type { HTMLAttributes, ElementType, ReactNode } from 'react'
import { cn } from '../../utils/cn'

type Variant = 'listing' | 'section' | 'action'

interface BaseProps {
  /** Visual preset. listing = clickable card with hover-lift, section = static info card, action = accent. */
  variant?: Variant
  /** Whether to add hover elevation (listing only). Default true for listing. */
  interactive?: boolean
  /** HTML tag (`div`, `article`, `a`, `Link` etc.). Defaults to `div`. */
  as?: ElementType
  className?: string
  children?: ReactNode
}

// ── Base classes for each variant ──────────────────────────────────────────
// Patterns are fixed in the design system (see CLAUDE-instructions):
// listing  — idle: shadow-low + border-border → hover: shadow-hover + border-brand/25
// section  — static info card, shadow-mid, no hover
// action   — accent / sticky CTA, shadow-mid, no hover

const VARIANT_BASE: Record<Variant, string> = {
  listing: 'bg-surface border border-border rounded-2xl shadow-card-low',
  section: 'bg-surface border border-border rounded-2xl shadow-card-mid',
  action: 'bg-surface border border-border rounded-2xl shadow-card-mid',
}

const VARIANT_INTERACTIVE: Record<Variant, string> = {
  listing: 'transition-[box-shadow,border-color,transform] duration-200 hover:shadow-card-hover',
  section: '',
  action: '',
}

type Props = BaseProps & Omit<HTMLAttributes<HTMLElement>, 'className' | 'children'>

const Card = forwardRef<HTMLElement, Props>(function Card(
  { variant = 'section', interactive, as: Component = 'div', className, children, ...rest },
  ref,
) {
  const isInteractive = interactive ?? variant === 'listing'
  return (
    <Component
      ref={ref}
      className={cn(
        VARIANT_BASE[variant],
        isInteractive && VARIANT_INTERACTIVE[variant],
        className,
      )}
      {...rest}
    >
      {children}
    </Component>
  )
})

export default Card
