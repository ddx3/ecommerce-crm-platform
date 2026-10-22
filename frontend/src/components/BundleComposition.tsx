import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { ChevronDown, Package } from 'lucide-react'
import { getBundle } from '../api/catalog'
import { Spinner } from './ui'
import { useUI } from '../context/UIContext'

interface BundleItem {
  product_name: string
  product_slug: string
  variant_name: string
  quantity: number
  image: string | null
}

interface Props {
  slug: string
  onClose?: () => void
}

export default function BundleComposition({ slug, onClose }: Props) {
  const [items, setItems] = useState<BundleItem[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const { panel } = useUI()
  // When the drawer closes (panel=null) — collapse the bundle composition.
  useEffect(() => {
    if (!panel) setOpen(false)
  }, [panel])

  const toggle = () => {
    if (!open && items === null) {
      setLoading(true)
      getBundle(slug)
        .then(data => setItems(data.items ?? []))
        .catch(() => setItems([]))
        .finally(() => setLoading(false))
    }
    setOpen(v => !v)
  }

  return (
    <div>
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="flex items-center gap-1 text-xs text-gray-400 hover:text-brand transition-colors"
      >
        <ChevronDown size={13} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
        Bundle contents
      </button>
      {open && (
        <div className="mt-2 flex flex-col gap-1.5">
          {loading && <p className="text-xs text-gray-400 flex items-center gap-2"><Spinner size="sm" tone="muted" /> Loading...</p>}
          {items?.map((item, i) => (
            <Link key={`${item.product_slug}_${i}`} to={`/product/${item.product_slug}`} onClick={onClose} className="flex items-center gap-2 bg-surface-muted border border-border rounded-xl px-2.5 py-1.5 hover:bg-gray-200 active:bg-gray-300 transition-colors">
              <div className="w-7 h-7 shrink-0 rounded-lg overflow-hidden bg-white ring-1 ring-border">
                {item.image
                  ? <img src={item.image} alt={item.variant_name} className="w-full h-full object-contain p-0.5" />
                  : <Package size={12} className="text-gray-300 m-auto mt-1.5" />
                }
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-xs text-gray-700 line-clamp-1">{item.variant_name || item.product_name}</p>
              </div>
              <span className="text-xs font-bold text-gray-500 shrink-0">× {item.quantity}</span>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}
