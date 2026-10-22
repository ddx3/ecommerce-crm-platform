import { useEffect, useRef } from 'react'
import { lockScroll as lockPageScroll } from './scrollLock'

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

interface Options {
  open: boolean
  onClose: () => void
  /** Trap focus and return it to the trigger on close. */
  trapFocus?: boolean
  /** Lock background scroll. For the cart/favorites — false, so the site can still be
   *  used while the panel is open on the right.
   */
  lockScroll?: boolean
}

/**
 * Dialog behavior: Esc to close, body scroll lock, focus trap, focus return.
 * Used in Modal and Drawer.
 */
export function useDialogBehavior({ open, onClose, trapFocus = true, lockScroll = true }: Options) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const triggerEl = document.activeElement as HTMLElement | null
    const unlock = lockScroll ? lockPageScroll() : null

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { onClose(); return }
      if (e.key === 'Tab' && trapFocus && ref.current) {
        const nodes = ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)
        if (nodes.length === 0) return
        const first = nodes[0]
        const last = nodes[nodes.length - 1]
        const active = document.activeElement as HTMLElement | null
        if (e.shiftKey && (active === first || !ref.current.contains(active))) {
          e.preventDefault(); last.focus()
        } else if (!e.shiftKey && active === last) {
          e.preventDefault(); first.focus()
        }
      }
    }
    document.addEventListener('keydown', onKey)

    // Auto-focus into the panel only for modal dialogs. For non-modal ones
    // (cart/favorites) we do not steal focus, so the user stays on the page.
    const t = trapFocus ? window.setTimeout(() => {
      const first = ref.current?.querySelector<HTMLElement>(FOCUSABLE)
      first?.focus()
    }, 0) : 0

    return () => {
      document.removeEventListener('keydown', onKey)
      unlock?.()
      window.clearTimeout(t)
      if (trapFocus) triggerEl?.focus?.()
    }
  }, [open, onClose, trapFocus, lockScroll])

  return ref
}
