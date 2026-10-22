/**
 * Background scroll lock under an overlay — one implementation for the whole frontend.
 *
 * Two pitfalls that used to be hit separately in each component:
 *
 * 1. The page's scroll container is <html>, not <body> (because of `overflow-x: clip`
 *    in index.css). `body { overflow: hidden }` alone does not lock the background, but it
 *    collapses the body height and the browser RESETS the scroll position: open a photo
 *    in the middle of the page, close it — and you are almost at the top. Lock html.
 *
 * 2. Along with the scroll, the scrollbar disappears and the page jumps sideways by its
 *    width. `scrollbar-gutter: stable` on html (index.css) prevents this — the space
 *    for the scrollbar is always reserved, so hiding it is safe.
 *
 * The counter exists because overlays can be nested (a photo lightbox over the chat panel,
 * a modal over a drawer): leaving the top one must not unlock the one below.
 */
let depth = 0
let restore: (() => void) | null = null

export function lockScroll(): () => void {
  if (depth === 0) {
    const root = document.documentElement
    const prevRoot = root.style.overflow
    const prevBody = document.body.style.overflow
    root.style.overflow = 'hidden'
    document.body.style.overflow = 'hidden'
    restore = () => {
      root.style.overflow = prevRoot
      document.body.style.overflow = prevBody
    }
  }
  depth += 1

  let released = false
  return () => {
    if (released) return
    released = true
    depth -= 1
    if (depth === 0 && restore) { restore(); restore = null }
  }
}
