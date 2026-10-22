import { createContext, useContext, useState, useEffect, useRef, type ReactNode } from 'react'
import type { ShippingOrigin, Supplier } from '../types'
import { useSiteSettings } from './SiteSettingsContext'
import { variantMaxQty } from '../utils/stock'
import { trackAddToCart } from '../lib/analytics'

export type CartItemKind = 'product' | 'bundle' | 'course'

export interface CartItem {
  key: string // `${productSlug}_${variantId}` for products/bundles, `course_${id}` for courses
  productSlug: string
  productName: string
  variantId: number
  variantName: string
  sku: string | null
  price: number
  image: string | null
  qty: number
  /** Max quantity (= stock level). undefined → no limit (made to order/dropshipping). */
  maxQty?: number
  /** The variant's own stock level (for a hybrid: whether the item goes via dropshipping when
   * qty>stock).
   * undefined until reconcile has checked it.
   */
  stock?: number | null
  isBundle?: boolean
  kind?: CartItemKind   // default 'product'; for a course — 'course' (no shipping, qty=1)
  shippingOrigin?: ShippingOrigin
  /** The item's dropshipping/hybrid supplier (Bradas, ARS…). null → own product. */
  supplier?: Supplier | null
  labels?: { name: string; color: string; slug: string }[]
}

export interface PendingShipmentConfirm {
  item: Omit<CartItem, 'key' | 'qty'>
  qty: number
  newOrigin: ShippingOrigin
  totalShipments: number
}

interface CartContextType {
  items: CartItem[]
  addItem: (item: Omit<CartItem, 'key' | 'qty'>, qty?: number) => void
  removeItem: (key: string) => void
  updateQty: (key: string, qty: number) => void
  clearCart: () => void
  total: number
  count: number
  /** Minimum order amount (UAH) — from the site settings. */
  minOrderAmount: number
  /** The order contains at least one physical product (not a course) → the minimum amount applies. */
  hasPhysical: boolean
  /** Total is below the minimum (only when there are physical products). Blocks checkout. */
  belowMinOrder: boolean
  pendingConfirm: PendingShipmentConfirm | null
  confirmPending: () => void
  cancelPending: () => void
}

const CartContext = createContext<CartContextType | null>(null)

/** Clamps the quantity to [1, maxQty]. maxQty == null → lower bound only. */
function clampQty(qty: number, maxQty?: number): number {
  const q = Math.max(1, qty)
  return maxQty != null ? Math.min(q, maxQty) : q
}

function originsOf(items: CartItem[]): Set<ShippingOrigin> {
  // Courses have no physical delivery — do not count them when counting sources.
  return new Set(
    items
      .filter(i => i.kind !== 'course')
      .map(i => i.shippingOrigin || 'own')
  )
}

export function CartProvider({ children }: { children: ReactNode }) {
  const { minOrderAmount } = useSiteSettings()
  const [items, setItems] = useState<CartItem[]>(() => {
    try { return JSON.parse(localStorage.getItem('cart') || '[]') } catch { return [] }
  })
  const [pendingConfirm, setPendingConfirm] = useState<PendingShipmentConfirm | null>(null)

  useEffect(() => { localStorage.setItem('cart', JSON.stringify(items)) }, [items])

  // Auto-migration: fill in sku for bundle-items where it is empty
  // (old localStorage entries added before we started storing sku).
  useEffect(() => {
    const missing = items.filter(i => i.isBundle && !i.sku)
    if (missing.length === 0) return
    let cancelled = false
    ;(async () => {
      const { getBundle } = await import('../api/catalog')
      for (const it of missing) {
        try {
          const data = await getBundle(it.productSlug)
          const sku = data.sku ?? null
          if (!sku || cancelled) continue
          setItems(prev => prev.map(p => p.key === it.key ? { ...p, sku } : p))
        } catch { /* ignore */ }
      }
    })()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // One-time normalization of bundle ids: after the bundle 'variants' were merged into the bundle,
  // old localStorage rows have variantId = the variant pk, not the bundle id.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const { resolveBundleIds, remapBundleLines } = await import('../utils/bundleIds')
      const ids = await resolveBundleIds(items, 'bundle_ids_v2_cart')
      if (!ids || cancelled) return
      setItems(prev => remapBundleLines(prev, ids, (slug, id) => `${slug}_${id}`,
        (a, b) => ({ ...a, qty: clampQty(a.qty + b.qty, a.maxQty ?? b.maxQty) })))
    })()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Reconcile each item with LIVE stock (once per session per key). Covers:
  // 1) old carts from localStorage without maxQty; 2) products added from Favorites
  //    (LikedItem does not carry stock) — maxQty=undefined there; 3) stale maxQty
  //    when the stock level dropped after adding. We fetch the fresh product/bundle,
  //    backfill maxQty and clamp qty. If the request fails, the backend still
  //    blocks overselling at checkout.
  const reconciledRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    const pending = items.filter(i => i.kind !== 'course' && !reconciledRef.current.has(i.key))
    if (pending.length === 0) return
    let cancelled = false
    ;(async () => {
      const { getProduct, getBundle } = await import('../api/catalog')
      // Deduplicate requests by source (one product/bundle can have several rows).
      const bySource = new Map<string, CartItem[]>()
      for (const it of pending) {
        reconciledRef.current.add(it.key)  // mark immediately so that the effect does not loop
        const k = `${it.isBundle ? 'b' : 'p'}:${it.productSlug}`
        const arr = bySource.get(k) || []
        arr.push(it); bySource.set(k, arr)
      }
      for (const [, lines] of bySource) {
        const first = lines[0]
        try {
          if (first.isBundle) {
            const data = await getBundle(first.productSlug)
            if (cancelled) return
            const maxQty = data.stock ?? undefined
            const ownImg = data.images?.[0]?.url
            setItems(prev => prev.map(p => {
              if (!lines.some(l => l.key === p.key)) return p
              return { ...p, maxQty, stock: data.stock ?? null, qty: clampQty(p.qty, maxQty), image: ownImg ?? p.image }
            }))
          } else {
            const data = await getProduct(first.productSlug)
            if (cancelled) return
            const variants: { id: number; stock?: number | null; shipping_origin?: ShippingOrigin; supplier?: Supplier | null }[] = data.variants || []
            // Photo backfill only when the variant HAS ITS OWN photo (img.variant === variantId).
            // Old rows carried images[0] of the first variant - we overwrite it with the photo of
            // its own
            // variant. If there is no own photo, do NOT touch p.image: it already holds the
            // correct fallback (main_image, as on the card).
            const prodImages: { url: string; variant?: number | null }[] = data.images || []
            setItems(prev => prev.map(p => {
              if (!lines.some(l => l.key === p.key)) return p
              const v = variants.find(x => x.id === p.variantId)
              if (!v) return p
              const maxQty = variantMaxQty(v)
              const ownImg = prodImages.find(im => im.variant === p.variantId)?.url
              // Refresh the supplier too: in a cart from localStorage it may be
              // stale (e.g. without requires_cargo_branch, which was added later), and which
              // Nova Poshta branches we show at checkout depends on it.
              return { ...p, maxQty, stock: v.stock ?? null, qty: clampQty(p.qty, maxQty), image: ownImg ?? p.image, supplier: v.supplier ?? p.supplier }
            }))
          }
        } catch { /* ignore — the backend guarantees the check at checkout */ }
      }
    })()
    return () => { cancelled = true }
  }, [items])

  const performAdd = (item: Omit<CartItem, 'key' | 'qty'>, qty: number) => {
    const key = item.kind === 'course'
      ? `course_${item.variantId}`  // variantId for a course = course.id
      : `${item.productSlug}_${item.variantId}`
    setItems(prev => {
      const existing = prev.find(i => i.key === key)
      if (existing) {
        // Course — qty is always 1 (intangible, it is not "added" again).
        if (item.kind === 'course') return prev
        // Fresh maxQty from the card (stock may have changed); fallback — the one saved earlier.
        const maxQty = item.maxQty ?? existing.maxQty
        return prev.map(i => i.key === key
          ? { ...i, maxQty, qty: clampQty(i.qty + qty, maxQty) }
          : i)
      }
      const finalQty = item.kind === 'course' ? 1 : clampQty(qty, item.maxQty)
      return [...prev, { ...item, key, qty: finalQty }]
    })
    // add_to_cart analytics — a single hub for product/bundle/course.
    trackAddToCart({
      id: item.kind === 'course' ? `course_${item.variantId}` : item.productSlug,
      name: item.productName,
      price: item.price,
      category: item.kind,
    }, item.kind === 'course' ? 1 : qty)
  }

  const addItem = (item: Omit<CartItem, 'key' | 'qty'>, qty = 1) => {
    // Course — no shipping, add without source confirmations.
    if (item.kind === 'course') {
      performAdd(item, 1)
      return
    }
    const newOrigin: ShippingOrigin = item.shippingOrigin || 'own'
    const before = originsOf(items)
    // If such an origin is already in the cart OR there are no physical products — add without
    // asking.
    if (before.size === 0 || before.has(newOrigin)) {
      performAdd(item, qty)
      return
    }
    // New shipping source → confirmation.
    setPendingConfirm({
      item,
      qty,
      newOrigin,
      totalShipments: before.size + 1,
    })
  }

  const confirmPending = () => {
    if (!pendingConfirm) return
    performAdd(pendingConfirm.item, pendingConfirm.qty)
    setPendingConfirm(null)
  }

  const cancelPending = () => setPendingConfirm(null)

  const removeItem = (key: string) => setItems(prev => prev.filter(i => i.key !== key))

  const updateQty = (key: string, qty: number) => {
    if (qty < 1) return removeItem(key)
    setItems(prev => prev.map(i => {
      if (i.key !== key) return i
      // Course — qty is always 1, ignore changes.
      if (i.kind === 'course') return i
      return { ...i, qty: clampQty(qty, i.maxQty) }
    }))
  }

  const clearCart = () => setItems([])

  const total = items.reduce((s, i) => s + i.price * i.qty, 0)
  const count = items.reduce((s, i) => s + i.qty, 0)

  const hasPhysical = items.some(i => i.kind !== 'course')
  const belowMinOrder = hasPhysical && total < minOrderAmount

  return (
    <CartContext.Provider value={{
      items, addItem, removeItem, updateQty, clearCart, total, count,
      minOrderAmount, hasPhysical, belowMinOrder,
      pendingConfirm, confirmPending, cancelPending,
    }}>
      {children}
    </CartContext.Provider>
  )
}

export const useCart = () => {
  const ctx = useContext(CartContext)
  if (!ctx) throw new Error('useCart must be used within CartProvider')
  return ctx
}
