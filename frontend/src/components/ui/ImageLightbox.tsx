import { useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { X, ChevronLeft, ChevronRight } from 'lucide-react'
import { lockScroll } from './scrollLock'

interface Props {
  images: string[]
  index: number
  onClose: () => void
  onIndex: (i: number) => void
}

/** Fullscreen photo view (click a photo on the page → open/zoom).
 *  Close: click on the backdrop, ×, Esc. Navigation: arrows / keys / swipe.
 */
export default function ImageLightbox({ images, index, onClose, onIndex }: Props) {
  const touchStart = useRef<{ x: number; y: number } | null>(null)
  const last = images.length - 1
  const go = (i: number) => onIndex(Math.max(0, Math.min(last, i)))

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      else if (e.key === 'ArrowRight') go(index + 1)
      else if (e.key === 'ArrowLeft') go(index - 1)
    }
    document.addEventListener('keydown', onKey)
    const unlock = lockScroll()
    return () => {
      document.removeEventListener('keydown', onKey)
      unlock()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [index, last])

  const url = images[index]
  if (!url) return null

  // A PORTAL is mandatory. The lightbox is called from inside the gallery column, and that column
  // is
  // `position: sticky` — i.e. its own stacking context. Inside it z-[80] counts
  // only against siblings within the column, while the header (z-50) and positioned blocks
  // lower in the DOM (description, product cards) get painted ON TOP of the photo. We move the
  // overlay
  // into body, where z-[80] really is the highest.
  return createPortal(
    <div
      className="fixed inset-0 z-[80] bg-black/90 flex items-center justify-center cursor-zoom-out select-none"
      onClick={onClose}
      onTouchStart={e => { touchStart.current = { x: e.touches[0].clientX, y: e.touches[0].clientY } }}
      onTouchEnd={e => {
        const s = touchStart.current; touchStart.current = null
        if (!s) return
        const dx = e.changedTouches[0].clientX - s.x
        const dy = e.changedTouches[0].clientY - s.y
        if (Math.abs(dx) < 40 || Math.abs(dx) < Math.abs(dy)) return
        go(dx < 0 ? index + 1 : index - 1)
      }}
    >
      <button
        type="button"
        aria-label="Close"
        onClick={onClose}
        className="absolute top-3 right-3 sm:top-5 sm:right-5 w-10 h-10 rounded-full bg-white/10 hover:bg-white/20 text-white flex items-center justify-center transition-colors"
      >
        <X size={22} />
      </button>

      {index > 0 && (
        <button
          type="button"
          aria-label="Previous photo"
          onClick={e => { e.stopPropagation(); go(index - 1) }}
          className="absolute left-2 sm:left-5 top-1/2 -translate-y-1/2 w-11 h-11 rounded-full bg-white/10 hover:bg-white/20 text-white flex items-center justify-center transition-colors"
        >
          <ChevronLeft size={26} />
        </button>
      )}

      <img
        src={url}
        alt=""
        onClick={e => e.stopPropagation()}
        className="max-w-[94vw] max-h-[92vh] object-contain cursor-default"
      />

      {index < last && (
        <button
          type="button"
          aria-label="Next photo"
          onClick={e => { e.stopPropagation(); go(index + 1) }}
          className="absolute right-2 sm:right-5 top-1/2 -translate-y-1/2 w-11 h-11 rounded-full bg-white/10 hover:bg-white/20 text-white flex items-center justify-center transition-colors"
        >
          <ChevronRight size={26} />
        </button>
      )}

      {images.length > 1 && (
        <div className="absolute bottom-4 left-1/2 -translate-x-1/2 text-white/80 text-sm font-medium">
          {index + 1} / {images.length}
        </div>
      )}
    </div>,
    document.body,
  )
}
