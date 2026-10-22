import { createContext, useContext, useState, useEffect, type ReactNode } from 'react'

export interface LikedItem {
  key: string // `${productSlug}_${variantId}`
  productSlug: string
  productName: string
  variantId: number
  variantName: string
  sku: string | null
  price: number | null
  image: string | null
  isBundle?: boolean
  isCourse?: boolean
}

interface LikedContextType {
  items: LikedItem[]
  toggle: (item: LikedItem) => void
  isLiked: (key: string) => boolean
  clear: () => void
}

const LikedContext = createContext<LikedContextType | null>(null)

// Stable browser identifier for anonymous likes (to see demand server-side).
function getDeviceId(): string {
  let id = localStorage.getItem('liked_device')
  if (!id) {
    id = (typeof crypto !== 'undefined' && crypto.randomUUID)
      ? crypto.randomUUID()
      : `d_${Date.now()}_${Math.random().toString(36).slice(2)}`
    localStorage.setItem('liked_device', id)
  }
  return id
}

const itemKind = (i: LikedItem): 'variant' | 'bundle' | 'course' =>
  i.isCourse ? 'course' : i.isBundle ? 'bundle' : 'variant'

export function LikedProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<LikedItem[]>(() => {
    try { return JSON.parse(localStorage.getItem('liked') || '[]') } catch { return [] }
  })

  useEffect(() => { localStorage.setItem('liked', JSON.stringify(items)) }, [items])

  // One-time upload of existing localStorage likes to the backend (so we don't lose what
  // people liked before server-side storage). Idempotent - runs once.
  // Before the upload (and always once per browser) we normalize bundle ids: after the merge of
  // bundle 'variants' into the bundle, variantId in old rows is the variant pk, not the bundle's.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const { resolveBundleIds, remapBundleLines } = await import('../utils/bundleIds')
      const makeKey = (slug: string, id: number) => `bundle_${slug}_${id}`
      const ids = await resolveBundleIds(items, 'bundle_ids_v2_liked')
      let current = items
      if (ids) {
        current = remapBundleLines(items, ids, makeKey, a => a)
        if (!cancelled) setItems(prev => remapBundleLines(prev, ids, makeKey, a => a))
      }
      if (localStorage.getItem('liked_synced')) return
      if (current.length === 0) { localStorage.setItem('liked_synced', '1'); return }
      const { favoriteSync } = await import('../api/catalog')
      try {
        await favoriteSync(getDeviceId(), current.map(i => ({ kind: itemKind(i), id: i.variantId })))
        localStorage.setItem('liked_synced', '1')
      } catch { /* we will retry next time */ }
    })()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Auto-migration: fill in sku for bundle-items that were added before
  // we started storing sku in localStorage.
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

  // fire-and-forget: server-side demand tracking does not block the UI and works for anonymous
  // users too.
  // Explicit liked (not toggle) — so the backend state exactly matches the frontend.
  const pushSet = (item: LikedItem, liked: boolean) => {
    import('../api/catalog').then(({ favoriteSet }) => {
      favoriteSet(getDeviceId(), itemKind(item), item.variantId, liked).catch(() => {})
    }).catch(() => {})
  }

  const toggle = (item: LikedItem) => {
    const willLike = !items.some(i => i.key === item.key)
    setItems(prev =>
      willLike ? [...prev, item] : prev.filter(i => i.key !== item.key)
    )
    pushSet(item, willLike)
  }

  const isLiked = (key: string) => items.some(i => i.key === key)
  const clear = () => {
    // also remove the likes on the backend — otherwise what was cleared locally stays in the demand
    // data
    items.forEach(i => pushSet(i, false))
    setItems([])
  }

  return <LikedContext.Provider value={{ items, toggle, isLiked, clear }}>{children}</LikedContext.Provider>
}

export const useLiked = () => {
  const ctx = useContext(LikedContext)
  if (!ctx) throw new Error('useLiked must be used within LikedProvider')
  return ctx
}
