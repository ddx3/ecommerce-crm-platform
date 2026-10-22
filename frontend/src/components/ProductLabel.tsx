import type { CSSProperties } from 'react'
import { Tag, Flame, TrendingUp, Clock, Package, type LucideIcon } from 'lucide-react'

// We trim the empty space above capital letters and below the baseline so that the
// optical center of the text = the center of the badge = the center of the icon. Applied ONLY when
// there is an
// icon (since then the badge is held by the icon height and the text does not collapse).
// text-box-* is not yet in the CSSProperties types — cast via unknown.
export const CENTER_ON_CAPS = { textBoxTrim: 'trim-both', textBoxEdge: 'cap alphabetic' } as unknown as CSSProperties

export interface ProductLabelProps {
  name: string
  color: string  // hex like "#22c55e"
  size?: 'sm' | 'md' | 'lg'
  showIcon?: boolean
}

// Language data: the label names come from the CRM and the existing (Ukrainian) catalog labels
// are matched by their Ukrainian/Russian stems — "під замовлення" (made to order), "знижка"
// (discount), "акція" (promo), "хіт" (hit), "топ" (top), "комплект"/"набір" (bundle/set),
// "новинка" (new). The English alternatives cover English label names.
function pickIcon(name: string): LucideIcon {
  const n = name.toLowerCase()
  if (/під\s*замовл|пiд\s*замовл|on.?demand|order/.test(n)) return Clock
  if (/знижк|sale|акці|%/.test(n)) return Flame
  if (/хіт|хит|топ\b|top\b|hit\b|best/.test(n)) return TrendingUp
  if (/комплект|bundle|набір|набор/.test(n)) return Package
  if (/новинк|new\b/.test(n)) return Tag
  return Tag
}

function hexToHsl(hex: string): [number, number, number] | null {
  const m = /^#?([\da-f]{6})$/i.exec(hex)
  if (!m) return null
  const v = parseInt(m[1], 16)
  const r = ((v >> 16) & 0xff) / 255
  const g = ((v >> 8) & 0xff) / 255
  const b = (v & 0xff) / 255
  const max = Math.max(r, g, b), min = Math.min(r, g, b)
  const l = (max + min) / 2
  let h = 0, s = 0
  if (max !== min) {
    const d = max - min
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
    switch (max) {
      case r: h = (g - b) / d + (g < b ? 6 : 0); break
      case g: h = (b - r) / d + 2; break
      case b: h = (r - g) / d + 4; break
    }
    h *= 60
  }
  return [h, s * 100, l * 100]
}

function hslToHex(h: number, s: number, l: number): string {
  s /= 100; l /= 100
  const k = (n: number) => (n + h / 30) % 12
  const a = s * Math.min(l, 1 - l)
  const f = (n: number) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)))
  const toHex = (c: number) => Math.round(c * 255).toString(16).padStart(2, '0')
  return '#' + toHex(f(0)) + toHex(f(8)) + toHex(f(4))
}

/** Soft background in the brand tone: a noticeable backing with muted saturation. */
function softBg(hex: string): string {
  const hsl = hexToHsl(hex)
  if (!hsl) return hex
  const [h, s] = hsl
  return hslToHex(h, Math.min(s, 50), 92)
}

/** Text in brand style `#1A5C2A`: deep but muted (low-sat). */
function deepText(hex: string): string {
  const hsl = hexToHsl(hex)
  if (!hsl) return hex
  const [h, s] = hsl
  return hslToHex(h, Math.min(s, 38), 30)
}

/** Bright, saturated text (for the promo label) — not muted, unlike deepText. */
function vividText(hex: string): string {
  const hsl = hexToHsl(hex)
  if (!hsl) return hex
  const [h, s] = hsl
  return hslToHex(h, Math.max(s, 70), 48)
}

/** Promo label background — a light, cleaner backing (slightly different from softBg). */
function saleBg(hex: string): string {
  const hsl = hexToHsl(hex)
  if (!hsl) return hex
  const [h, s] = hsl
  return hslToHex(h, Math.min(s, 45), 95)
}

/** The single "promo" red — for the discount label, the -% badge and the discounted price. */
export const SALE_RED = vividText('#ef4444')
/** Light backing of the promo label — for compact -% badges on cards. */
export const SALE_BG = saleBg('#ef4444')

/**
 * Product label in the style of the site's icon badge: rounded-xl, a soft background
 * of the corresponding color, text in a saturated color. No uppercase, shadow or
 * border — to blend with the rest of the interface (About/Delivery info blocks).
 */
export default function ProductLabel({ name, color, size = 'md', showIcon = true }: ProductLabelProps) {
  const Icon = pickIcon(name)
  // The "New" label ("новинка" in Ukrainian catalog data) — in the chip hover colors
  // (brand-bg / brand), the other labels — in the tone of their own color.
  const isNew = /новинк|new\b/.test(name.toLowerCase())
  // The "Promo" / discount label ("акція"/"знижка" in Ukrainian catalog data) — bright red text on a
  // light backing so it stands out.
  const isSale = /знижк|sale|акці|%/.test(name.toLowerCase())
  const bgColor = isNew ? 'var(--color-brand-bg)' : isSale ? saleBg(color) : softBg(color)
  const textColor = isNew ? 'var(--color-brand)' : isSale ? vividText(color) : deepText(color)

  const sizeMap = {
    sm: { icon: 12, text: 'text-[11px]', pad: 'px-2 py-1',     gap: 'gap-1' },
    md: { icon: 13, text: 'text-xs',     pad: 'px-2.5 py-1.5', gap: 'gap-1' },
    lg: { icon: 15, text: 'text-sm',     pad: 'px-3 py-1.5',   gap: 'gap-1.5' },
  } as const
  const s = sizeMap[size]

  return (
    <span
      className={`inline-flex items-center ${s.gap} ${s.pad} rounded-lg font-bold ${s.text} leading-none select-none whitespace-nowrap max-w-full`}
      style={{ backgroundColor: bgColor, color: textColor }}
    >
      {showIcon && <Icon size={s.icon} className="shrink-0 block" />}
      {/* Trim the box to cap/baseline (text-box-trim) so that text without descenders
          sits in the center of the badge. WITHOUT overflow:hidden — otherwise trimming + clipping
          cut the text (as happened with the -30% discount). Labels are short, so there will be no overflow. */}
      <span
        className="min-w-0 whitespace-nowrap"
        style={CENTER_ON_CAPS}
      >{name}</span>
    </span>
  )
}
