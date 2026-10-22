import { useState, useEffect, type ReactNode } from 'react'
import { cn } from '../../utils/cn'
import { useDialogBehavior } from './useDialogBehavior'

type Side = 'left' | 'right'

interface Props {
  open: boolean
  onClose: () => void
  side?: Side
  width?: string
  children: ReactNode
  ariaLabel: string
  className?: string
  /** Dimming overlay behind the drawer. Defaults to false — for the cart
   *  and favorites the user explicitly does not want dimming.
   */
  overlay?: boolean
}

const SIDE: Record<Side, { pos: string; from: string }> = {
  right: { pos: 'right-0', from: 'translateX(100%)' },
  left: { pos: 'left-0', from: 'translateX(-100%)' },
}

export default function Drawer({ open, onClose, side = 'right', width = 'w-full sm:w-[420px]', children, ariaLabel, className, overlay = false }: Props) {
  // On mobile the drawer takes up the whole screen — behave like a menu: lock background scroll
  // (otherwise a swipe on the panel falls through to the page behind it, and even an empty
  // cart "scrolls"). On desktop (≥sm) the cart/favorites remain a non-modal
  // side panel — the background scrolls so the user can keep browsing the site.
  const [isMobile, setIsMobile] = useState(() => window.matchMedia('(max-width: 639px)').matches)
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 639px)')
    const update = () => setIsMobile(mq.matches)
    update()
    mq.addEventListener('change', update)
    return () => mq.removeEventListener('change', update)
  }, [])

  const ref = useDialogBehavior({ open, onClose, lockScroll: overlay || isMobile, trapFocus: overlay })

  return (
    <>
      {overlay && (
        <div
          aria-hidden="true"
          onClick={open ? onClose : undefined}
          className={cn(
            'fixed inset-0 bg-black/40 z-[155] transition-opacity duration-300',
            open ? 'opacity-100' : 'opacity-0 pointer-events-none',
          )}
        />
      )}
      <div
        ref={ref}
        role="dialog"
        aria-modal={overlay}
        aria-label={ariaLabel}
        aria-hidden={!open}
        inert={!open ? true : undefined}
        className={cn(
          'fixed top-0 h-full max-w-full bg-white z-[160] flex flex-col transition-transform duration-300',
          width,
          SIDE[side].pos,
          className,
        )}
        style={{ transform: open ? 'translateX(0)' : SIDE[side].from }}
      >
        {/* Even shadow along the whole edge: the horizontal gradient is the same
            over the full height — unlike box-shadow, it does not fade out at the corners. */}
        <div
          aria-hidden
          className={cn(
            'absolute top-0 h-full w-9 pointer-events-none',
            side === 'right' ? 'right-full' : 'left-full',
          )}
          style={{
            background: `linear-gradient(to ${side === 'right' ? 'left' : 'right'}, rgba(0,0,0,0.1) 0%, rgba(0,0,0,0.05) 32%, rgba(0,0,0,0.015) 66%, rgba(0,0,0,0) 100%)`,
          }}
        />
        {children}
      </div>
    </>
  )
}
