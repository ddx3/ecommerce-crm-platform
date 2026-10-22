import type { ProductListItem, ProductDetail } from '../types'
import { getProducts } from '../api/catalog'

const STORAGE_KEY = 'recently_viewed_products'
const MAX_ITEMS = 20

function snapshotFromDetail(p: ProductDetail): ProductListItem {
  // Turn a ProductDetail into a lightweight ProductListItem snapshot for the card.
  const variants = (p.variants || []).filter(v => v.is_active).map(v => ({
    id: v.id,
    name: v.name,
    price: v.price,
    stock: v.stock,
    sku: v.sku,
    attribute_values: v.attribute_values || [],
    image: v.images && v.images.length > 0 ? v.images[0].url : null,
    shipping_origin: v.shipping_origin,
    old_price: v.old_price ?? null,
    discount_percent: v.discount_percent ?? null,
    discount_amount: v.discount_amount ?? null,
    effective_discount: v.effective_discount ?? null,
    active_labels: v.active_labels || [],
  }))
  const min_price = variants.length > 0
    ? variants.reduce((m, v) => (m === null || v.price < m ? v.price : m), null as number | null)
    : null
  const main_image = (p.images && p.images.length > 0 ? p.images[0].url : null) || (variants[0]?.image ?? null)
  // Collect labels from all active variants (unique by slug).
  const labelsMap = new Map<string, { name: string; color: string; slug: string }>()
  for (const v of (p.variants || [])) {
    for (const l of (v.active_labels || [])) {
      labelsMap.set(l.slug, l)
    }
  }
  return {
    id: p.id,
    name: p.name,
    slug: p.slug,
    brand_name: p.brand_name,
    brand_slug: p.brand_slug,
    main_image,
    min_price,
    old_price: null,
    has_discount: false,
    labels: Array.from(labelsMap.values()),
    variants,
    packaging_kind: p.packaging_kind,
    // On the product page we don't have a full snapshot of the paired product — in the
    // "Recently viewed" block the switcher will not be shown.
    paired: null,
  }
}

export function getRecentlyViewed(): ProductListItem[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const data = JSON.parse(raw)
    return Array.isArray(data) ? data : []
  } catch {
    return []
  }
}

/**
 * Validate recentlyViewed via the API: localStorage may contain «phantoms» —
 * products the admin has already deleted. Return only products that actually exist
 * (with current data — photo, price, variants) in the same order.
 */
export async function getRecentlyViewedFresh(limit = 10): Promise<ProductListItem[]> {
  const list = getRecentlyViewed().slice(0, limit)
  if (list.length === 0) return []
  try {
    const slugs = list.map(p => p.slug).join(',')
    const data = await getProducts({ slugs, page_size: limit })
    const bySlug = new Map(data.results.map(p => [p.slug, p]))
    // Keep the order from localStorage; filter out those that no longer exist.
    return list.map(p => bySlug.get(p.slug)).filter((p): p is ProductListItem => !!p)
  } catch {
    // If the API is down, return the snapshot from localStorage so we don't show an empty list.
    return list
  }
}

export function trackProductView(product: ProductListItem | ProductDetail) {
  try {
    const isDetail = 'description' in product && 'images' in product
    const item: ProductListItem = isDetail ? snapshotFromDetail(product as ProductDetail) : (product as ProductListItem)
    const list = getRecentlyViewed().filter(p => p.slug !== item.slug)
    list.unshift(item)
    localStorage.setItem(STORAGE_KEY, JSON.stringify(list.slice(0, MAX_ITEMS)))
  } catch {
    /* localStorage may be unavailable — silent fail */
  }
}
