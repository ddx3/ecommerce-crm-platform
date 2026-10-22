import { useRef, useState, useEffect, useCallback, useLayoutEffect, Children, cloneElement, isValidElement, type ReactNode, type CSSProperties } from 'react'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { subscribeCardHover, isCardHovered } from '../pages/Catalog'

interface ColsConfig {
  base: number
  sm?: number
  md?: number
  lg?: number
  xl?: number
}

interface Props {
  children: ReactNode
  /** How many cards are in the visible area at each breakpoint. */
  cols: ColsConfig
  /** Extra padding-bottom (for the cards' hover panels). Compensated by a negative margin. */
  extraBottom?: number
  className?: string
  /** Gap between cards in px. base — for mobile, sm — from ≥640px. */
  gap?: { base: number; sm: number }
}

/** Reserve for card shadows. The scroller necessarily clips overflow-y (because of overflow-x:auto),
 *  so the shadow must fit entirely in this margin, otherwise there is a sharp cut.
 *  Hover-shadow `0 8px 18px`: a gaussian blur of 18px visually spreads ~27px,
 *  so with the downward offset the shadow reaches ~35px down / ~19px up / ~27px sideways. We take
 *  it with a buffer.
 */
const SHADOW_PAD = 32
const SHADOW_TOP = 28
const SHADOW_BOTTOM = 48

// Device without hover (touch): the reserve for an expanded card is enabled when a card
// is actually expanded (tap), not on mouseenter — there is none on touch.
const IS_TOUCH = typeof window !== 'undefined'
  && typeof window.matchMedia === 'function'
  && window.matchMedia('(hover: none)').matches

function pickCols(cols: ColsConfig, ww: number): number {
  if (ww >= 1280 && cols.xl) return cols.xl
  if (ww >= 1024 && cols.lg) return cols.lg
  if (ww >= 768 && cols.md) return cols.md
  if (ww >= 640 && cols.sm) return cols.sm
  return cols.base
}

export default function HScroller({
  children,
  cols,
  extraBottom = 0,
  className = '',
  gap = { base: 12, sm: 16 },
}: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const childRefs = useRef<(HTMLElement | null)[]>([])
  const [atStart, setAtStart] = useState(true)
  const [atEnd, setAtEnd] = useState(false)
  const [overflow, setOverflow] = useState(false)
  const [cardWidth, setCardWidth] = useState<number | null>(null)
  // Card height (stable — the spacer fixes it regardless of hover).
  // Needed so the arrows sit exactly at the center of the card and don't "teleport".
  const [cardH, setCardH] = useState(0)
  // The large paddingBottom (for the cards' hover panel) is reserved only while the cursor is
  // over the scroller — otherwise it constantly "stretches" the document and leaves blank space
  // under the footer.
  const [hoverReserve, setHoverReserve] = useState(false)
  const effectiveExtraBottom = hoverReserve ? extraBottom : 0
  const [curGap, setCurGap] = useState<number>(gap.sm)
  // Hidden card (offscreen): its filter:drop-shadow is not rendered →
  // the shadow does not "jump out" into the visible area as a ghost.
  const [hiddenIdx, setHiddenIdx] = useState<Set<number>>(new Set())

  const updateEdges = useCallback(() => {
    const el = ref.current
    if (!el) return
    const hasOverflow = el.scrollWidth > el.clientWidth + 1
    setOverflow(hasOverflow)
    setAtStart(el.scrollLeft <= 1)
    setAtEnd(el.scrollLeft + el.clientWidth >= el.scrollWidth - 1)
  }, [])

  const recompute = useCallback(() => {
    const el = ref.current
    if (!el) return
    const ww = window.innerWidth
    const n = pickCols(cols, ww)
    // Minimum gap = SHADOW_PAD: so that the (N+1)th card doesn't peek into the padding-x.
    const gUser = ww >= 640 ? gap.sm : gap.base
    const g = Math.max(gUser, SHADOW_PAD)
    // The scroll container is wider than the parent by 2*SHADOW_PAD (via negative margin);
    // padding-x = SHADOW_PAD eats this margin. The cards in inner cover the parent exactly.
    const inner = el.clientWidth - 2 * SHADOW_PAD
    // Float — no rounding, N cards cover inner exactly.
    const card = (inner - (n - 1) * g) / n
    setCardWidth(card > 0 ? card : null)
    setCurGap(g)
    const firstChild = childRefs.current.find((c): c is HTMLElement => !!c)
    if (firstChild) setCardH(firstChild.offsetHeight)
    updateEdges()
  }, [cols, gap.base, gap.sm, updateEdges])

  useLayoutEffect(() => {
    recompute()
  }, [recompute, children])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const onScroll = () => updateEdges()
    el.addEventListener('scroll', onScroll, { passive: true })
    const onResize = () => recompute()
    window.addEventListener('resize', onResize)
    const ro = new ResizeObserver(recompute)
    ro.observe(el)
    return () => {
      el.removeEventListener('scroll', onScroll)
      window.removeEventListener('resize', onResize)
      ro.disconnect()
    }
  }, [recompute, updateEdges])

  // IntersectionObserver: marks offscreen children so they can be hidden
  // (visibility:hidden), thereby keeping their drop-shadow "ghost" from creeping
  // into the visible area during mid-scroll.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const observed = childRefs.current.filter((c): c is HTMLElement => !!c)
    if (!observed.length) return
    const io = new IntersectionObserver((entries) => {
      setHiddenIdx(prev => {
        let changed = false
        const next = new Set(prev)
        for (const entry of entries) {
          const idx = childRefs.current.indexOf(entry.target as HTMLElement)
          if (idx === -1) continue
          if (entry.isIntersecting) {
            if (next.has(idx)) { next.delete(idx); changed = true }
          } else {
            if (!next.has(idx)) { next.add(idx); changed = true }
          }
        }
        return changed ? next : prev
      })
    }, { root: el, threshold: 0.01 })
    observed.forEach(c => io.observe(c))
    return () => io.disconnect()
  }, [children, cardWidth])

  // On touch there is no mouseenter — we sync the reserve with the global state of the
  // cards: a tap expanded a card → reserve the space so overflow-y doesn't clip the panel.
  useEffect(() => {
    if (!IS_TOUCH) return
    return subscribeCardHover(() => setHoverReserve(isCardHovered()))
  }, [])

  const scrollByDir = (dir: -1 | 1) => {
    const el = ref.current
    if (!el || !cardWidth) return
    const ww = window.innerWidth
    const n = pickCols(cols, ww)
    const step = n * (cardWidth + curGap)
    el.scrollBy({ left: dir * step, behavior: 'smooth' })
  }

  const btnBase = 'absolute w-10 h-10 bg-white rounded-full shadow-md flex items-center justify-center text-gray-700 hover:text-gray-900 hover:bg-gray-200 border border-gray-200 z-10 transition-opacity'
  // Arrows — at the center of the card (stable position, independent of the hover reserve).
  const btnTop = SHADOW_TOP + cardH / 2

  // Clone the children — exact width + flex:0 0 auto + snap-start + a ref for the observer.
  // Offscreen cards get visibility:hidden to hide their drop-shadow.
  const styledChildren = Children.map(children, (child, i) => {
    if (!isValidElement(child)) return child
    const existing = ((child.props as { style?: CSSProperties }).style) ?? {}
    const existingClass = (child.props as { className?: string }).className ?? ''
    const hidden = hiddenIdx.has(i)
    return cloneElement(
      child as React.ReactElement<{ style?: CSSProperties; className?: string; ref?: React.Ref<HTMLElement> }>,
      {
        ref: (el: HTMLElement | null) => { childRefs.current[i] = el },
        style: {
          ...existing,
          width: cardWidth ?? existing.width,
          flex: '0 0 auto',
          scrollSnapAlign: 'start',
          visibility: hidden ? 'hidden' : 'visible',
        },
        className: existingClass,
      },
    )
  })

  return (
    <div
      className={`relative ${className}`}
      style={{ marginLeft: -SHADOW_PAD, marginRight: -SHADOW_PAD }}
      onMouseEnter={IS_TOUCH ? undefined : () => setHoverReserve(true)}
      onMouseLeave={IS_TOUCH ? undefined : () => setHoverReserve(false)}
    >
      <div
        ref={ref}
        className="flex items-start scrollbar-hide snap-x snap-mandatory"
        style={{
          gap: curGap,
          paddingTop: SHADOW_TOP,
          paddingBottom: effectiveExtraBottom + SHADOW_BOTTOM,
          paddingLeft: SHADOW_PAD,
          paddingRight: SHADOW_PAD,
          marginTop: -SHADOW_TOP,
          marginBottom: -(effectiveExtraBottom + SHADOW_BOTTOM),
          scrollPaddingLeft: SHADOW_PAD,
          scrollPaddingRight: SHADOW_PAD,
          scrollbarWidth: 'none',
          // overflow-x: auto + overflow-y: hidden — horizontal scroll exists,
          // a vertical scrollbar never appears. The large paddingBottom
          // (for the hover panel) is reserved only while the cursor is over the scroller.
          overflowX: 'auto',
          overflowY: 'hidden',
        }}
      >
        {styledChildren}
      </div>
      {overflow && (
        <>
          <button
            type="button"
            onClick={() => scrollByDir(-1)}
            aria-label="Back"
            disabled={atStart}
            className={`${btnBase} left-4 sm:-left-9 ${atStart ? 'opacity-0 pointer-events-none' : 'opacity-100'}`}
            style={{ top: btnTop, transform: 'translateY(-50%)' }}
          >
            <ChevronLeft size={20} />
          </button>
          <button
            type="button"
            onClick={() => scrollByDir(1)}
            aria-label="Next"
            disabled={atEnd}
            className={`${btnBase} right-4 sm:-right-9 ${atEnd ? 'opacity-0 pointer-events-none' : 'opacity-100'}`}
            style={{ top: btnTop, transform: 'translateY(-50%)' }}
          >
            <ChevronRight size={20} />
          </button>
        </>
      )}
    </div>
  )
}
