import { useState, useEffect, useRef, useLayoutEffect, useMemo, type ReactNode, type MouseEvent } from 'react'

/** Hook: whether the name is truncated (does not fit in 2 lines). If so — align to the top,
 *  so that on expand the name grows DOWN rather than jumping. Otherwise — align to the bottom of
 *  the card.
 */
function useClampedTitle(text: string) {
  const ref = useRef<HTMLParagraphElement>(null)
  const [clamped, setClamped] = useState(false)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    // sticky: once the name has been truncated (at the card's real width) — stay true,
    // so that the flag is not reset during hover (when the clamp is removed).
    const measure = () => { if (el.scrollHeight > el.clientHeight + 1) setClamped(true) }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [text])
  return { ref, clamped }
}

import { Link, useSearchParams } from 'react-router-dom'
import { ShoppingCart, ShoppingBag, ChevronLeft, ChevronRight, ChevronDown, Heart, LayoutGrid, Package } from 'lucide-react'
import { useTapInfo } from '../../hooks/useTapInfo'
import { noWidow } from '../../utils/text'
import QtyInput from '../../components/QtyInput'
import { variantMaxQty, variantInStock } from '../../utils/stock'
import { getProducts, getCategoriesFlat, getBundles, getBrands } from '../../api/catalog'
import type { Brand } from '../../api/catalog'
import Seo from '../../components/Seo'
import type { ProductListItem, ProductListVariant, BundleListItem, Category, AttributeValueRef, VariantOrderRef, ShippingOrigin } from '../../types'
import { useCart } from '../../context/CartContext'
import { useLiked } from '../../context/LikedContext'
import { useUI } from '../../context/UIContext'
import ProductLabel, { SALE_RED } from '../../components/ProductLabel'
import StockNotifyButton from '../../components/StockNotifyButton'
import { roundPrice } from '../../utils/price'

export default function CatalogPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [products, setProducts] = useState<ProductListItem[]>([])
  const [bundles, setBundles] = useState<BundleListItem[]>([])
  const [categories, setCategories] = useState<Category[]>([])
  const [brands, setBrands] = useState<Brand[]>([])
  const [loading, setLoading] = useState(true)
  const [bundlesLoading, setBundlesLoading] = useState(false)
  const [, setCatsLoading] = useState(false)
  const [total, setTotal] = useState(0)
  const [variantsTotal, setVariantsTotal] = useState(0)
  // Card order in the "by pack size" mode comes from the backend (?as=variant),
  // globally sorted: in-stock first, out-of-stock at the very end.
  const [variantOrder, setVariantOrder] = useState<VariantOrderRef[]>([])
  const [expandedCat, setExpandedCat] = useState<number | null>(null)
  const [viewMode, setViewMode] = useState<'product' | 'variant'>('variant')
  // Classic pagination: page is the current page (1-based). The list is
  // FULLY replaced on navigation (no accumulation / infinite scroll).
  const [page, setPage] = useState(1)
  // Previous filter key — when it changes, reset to page 1.
  const lastFilterRef = useRef('')

  const categorySlug = searchParams.get('category') || ''
  const search = searchParams.get('search') || ''
  const labelParam = searchParams.get('label') || ''
  const activeLabels = labelParam ? labelParam.split(',') : []
  const brandParam = searchParams.get('brand') || ''
  // Brand name for the heading comes from the brand list (works even with 0 products),
  // falls back to the brand from the products, then to the slug itself.
  const brandName = brandParam
    ? (brands.find(b => b.slug === brandParam)?.name
       || products.find(p => p.brand_slug === brandParam)?.brand_name
       || brandParam)
    : ''
  const PAGE_SIZE = 20

  const activeCategory = categories.find(c => c.slug === categorySlug)
  const rootCategories = categories.filter(c => !c.parent_id)
  // Show bundles in ANY category they belong to (the backend takes the whole
  // subtree) and in search results. Exception — the brand filter:
  // a bundle has no brand, so it doesn't belong in brand results.
  const showBundles = !brandParam && (!!categorySlug || !!search)

  useEffect(() => {
    setCatsLoading(true)
    getCategoriesFlat()
      .then(setCategories)
      .catch(() => {})
      .finally(() => setCatsLoading(false))
  }, [])

  useEffect(() => { getBrands().then(setBrands).catch(() => {}) }, [])

  // When the active category changes, expand its root branch in the sidebar.
  useEffect(() => {
    if (!categorySlug || categories.length === 0) return
    const cat = categories.find(c => c.slug === categorySlug)
    if (!cat) return
    let root: Category | undefined = cat
    while (root && root.parent_id) {
      root = categories.find(c => c.id === root!.parent_id)
    }
    if (root) setExpandedCat(root.id)
  }, [categorySlug, categories])

  // Product request params for a given page (filters + view mode).
  const productQuery = (pageNum: number) => ({
    category: categorySlug || undefined,
    search: search || undefined,
    label: labelParam || undefined,
    brand: brandParam || undefined,
    ...(viewMode === 'variant' ? { as: 'variant' as const } : {}),
    page: pageNum,
    page_size: PAGE_SIZE,
  })

  // The backend may return one product several times because of the join over categories/labels —
  // dedupe by id, otherwise duplicate cards share the same hover-id and expand together.
  const dedupe = (list: ProductListItem[]) => [...new Map(list.map(p => [p.id, p])).values()]

  const filterKey = `${categorySlug}|${search}|${labelParam}|${brandParam}|${viewMode}`

  // Load the current page (the list is replaced). When filters change,
  // first reset to page 1, then the effect re-runs and fetches the data.
  useEffect(() => {
    if (lastFilterRef.current !== filterKey) {
      lastFilterRef.current = filterKey
      if (page !== 1) { setPage(1); return }
    }
    let cancelled = false
    setLoading(true)
    getProducts(productQuery(page))
      .then(data => {
        if (cancelled) return
        setProducts(dedupe(data.results))
        setVariantOrder(data.variant_order ?? [])
        setTotal(data.count)
        setVariantsTotal(data.variants_count ?? data.count)
      })
      .catch(() => { if (!cancelled) { setProducts([]); setVariantOrder([]); setTotal(0); setVariantsTotal(0) } })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterKey, page])

  // Bundles with no pack in stock go to the end (the last pages).
  // Stable sort preserves the original order within each group.
  // The backend doesn't filter bundles by labels — we do it here, otherwise all bundles
  // would slip into the New/Hits results.
  const sortedBundles = useMemo(() => {
    const inStock = (b: BundleListItem) => (b.stock ?? 0) > 0
    const wanted = labelParam ? labelParam.split(',') : []
    const hasLabel = (b: BundleListItem) =>
      !wanted.length || b.active_labels.some(l => wanted.includes(l.slug))
    return bundles.filter(hasLabel).sort((a, b) => Number(inStock(b)) - Number(inStock(a)))
  }, [bundles, labelParam])

  // Bundles also count toward the total — otherwise the bundles page
  // (0 products) dumped all bundles onto one page with no pagination.
  const bundlesCount = showBundles ? sortedBundles.length : 0
  const productTotal = viewMode === 'variant' ? variantsTotal : total
  const totalCount = productTotal + bundlesCount
  // In a mixed category bundles come FIRST on the first page — otherwise
  // they end up after all the products, on the last page. Pagination is then driven
  // by the products alone (bundles don't add an extra page). In a pure bundle category
  // (0 products) we slice them into pages, as before.
  const bundlesFirst = bundlesCount > 0 && productTotal > 0
  const totalPages = Math.max(1, Math.ceil((bundlesFirst ? productTotal : totalCount) / PAGE_SIZE))

  const goToPage = (p: number) => {
    const next = Math.min(Math.max(1, p), totalPages)
    if (next === page) return
    setPage(next)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  useEffect(() => {
    if (!showBundles) { setBundles([]); return }
    setBundlesLoading(true)
    // Category as is — the backend takes its whole subtree.
    // When searching without a category — search across all bundles.
    getBundles({ category: categorySlug || undefined, search: search || undefined, page_size: 100 })
      .then(data => setBundles(data.results))
      .catch(() => setBundles([]))
      .finally(() => setBundlesLoading(false))
  }, [showBundles, categorySlug, search])

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(searchParams)
    if (value) next.set(key, value)
    else next.delete(key)
    setSearchParams(next)
  }

  // Label toggle (new/hits) in the ?label=slug,slug param — the backend filters with OR.
  const toggleLabel = (slug: string) => {
    const next = activeLabels.includes(slug)
      ? activeLabels.filter(s => s !== slug)
      : [...activeLabels, slug]
    setParam('label', next.length ? next.join(',') : null)
  }

  const selectCategory = (slug: string | null) => {
    const next = new URLSearchParams()
    if (slug) next.set('category', slug)
    // Keep the current search query — search within the selected category.
    const search = searchParams.get('search')
    if (search) next.set('search', search)
    setSearchParams(next)
  }

  // SEO: don't index filtered/search result sets (duplicates), do index a clean category/brand.
  const seoNoindex = !!(
    search || labelParam ||
    searchParams.get('attrs') || searchParams.get('price_min') || searchParams.get('price_max') ||
    searchParams.get('in_stock') ||
    (categorySlug && brandParam) ||
    Number(searchParams.get('page') || '1') > 1
  )
  const seoTitle = activeCategory?.name || (brandParam ? 'Brand catalog' : 'Product catalog')
  const seoCanonical = (categorySlug && !brandParam)
    ? `/catalog?category=${categorySlug}`
    : (brandParam && !categorySlug ? `/catalog?brand=${brandParam}` : '/catalog')

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
      <Seo
        title={seoTitle}
        description={`${seoTitle}: wide selection, prices and availability. Magiya Roslyn.`}
        canonicalPath={seoCanonical}
        noindex={seoNoindex}
      />
      {/* Breadcrumb */}
      <nav className="flex items-center gap-2 text-sm text-gray-400 mb-6 flex-wrap">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        {activeCategory ? (() => {
          const chain: Category[] = []
          let cur: Category | undefined = activeCategory
          while (cur) { chain.unshift(cur); cur = categories.find(c => c.id === cur!.parent_id) }
          return chain.map((cat, i) => (
            <span key={cat.slug} className="flex items-center gap-2">
              <ChevronRight size={14} />
              {i < chain.length - 1
                ? <Link to={`/catalog?category=${cat.slug}`} className="hover:text-brand transition-colors">{cat.name}</Link>
                : <span className="text-gray-700 font-medium">{cat.name}</span>
              }
            </span>
          ))
        })() : (
          <>
            <ChevronRight size={14} />
            <span className="text-gray-700 font-medium">Catalog</span>
          </>
        )}
        {search && (
          <>
            <ChevronRight size={14} />
            <span className="text-gray-700 font-medium">Search: “{search}”</span>
          </>
        )}
      </nav>

      <div className="flex gap-5 lg:gap-7">
        {/* Sidebar */}
        <aside className="hidden lg:block shrink-0 lg:w-64 lg:sticky lg:top-[140px] lg:self-start lg:max-h-[calc(100vh-160px)] lg:overflow-y-auto lg:overscroll-contain lg:pr-1">
          <div>
            <h3 className="font-bold text-text-primary text-base px-2 py-3">Categories</h3>
            <div className="space-y-0.5">
              {rootCategories.map(cat => (
                <CategoryItem
                  key={cat.id}
                  cat={cat}
                  allCats={categories}
                  activeSlug={categorySlug}
                  expandedId={expandedCat}
                  onExpand={id => setExpandedCat(id)}
                  onSelect={slug => selectCategory(slug)}
                />
              ))}
            </div>
          </div>

        </aside>

        {/* Content */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 sm:gap-3 mb-4 sm:mb-6 flex-wrap">
            {search ? (
              <h1 className="font-bold text-lg sm:text-xl text-gray-800">
                Search: “{search}”
              </h1>
            ) : brandParam ? (
              <h1 className="font-bold text-lg sm:text-xl text-gray-800">
                Brand: {brandName}
              </h1>
            ) : (
              <h1 className="font-bold text-lg sm:text-xl text-gray-800">
                {activeCategory?.name || 'Products'}
              </h1>
            )}
            <span className="text-gray-400 text-sm sm:text-base shrink-0">({totalCount})</span>
            <div className="flex-1 min-w-[14rem] lg:flex-none lg:min-w-0 lg:ml-auto flex items-center gap-1.5 sm:gap-2 overflow-x-auto -mx-1 px-1 scrollbar-hide">
              <button
                onClick={() => toggleLabel('novynka')}
                className={`h-9 px-4 flex items-center justify-center rounded-xl font-bold transition-colors text-sm whitespace-nowrap shrink-0 ${activeLabels.includes('novynka') ? 'bg-brand text-white shadow-sm' : 'bg-gray-100 text-gray-500 hover:bg-brand-bg hover:text-brand'}`}
              >
                New arrivals
              </button>
              <button
                onClick={() => toggleLabel('khit')}
                className={`h-9 px-4 flex items-center justify-center rounded-xl font-bold transition-colors text-sm whitespace-nowrap shrink-0 ${activeLabels.includes('khit') ? 'bg-brand text-white shadow-sm' : 'bg-gray-100 text-gray-500 hover:bg-brand-bg hover:text-brand'}`}
              >
                Bestsellers
              </button>
              <div className="ml-auto lg:ml-0 flex items-center rounded-xl overflow-hidden shrink-0">
                <button
                  onClick={() => setViewMode('variant')}
                  title="By pack size"
                  className={`w-9 h-9 flex items-center justify-center transition-colors ${viewMode === 'variant' ? 'bg-brand text-white' : 'bg-gray-100 text-gray-500 hover:bg-brand-bg hover:text-brand'}`}
                >
                  <LayoutGrid size={16} />
                </button>
                <button
                  onClick={() => setViewMode('product')}
                  title="By product"
                  className={`w-9 h-9 flex items-center justify-center transition-colors ${viewMode === 'product' ? 'bg-brand text-white' : 'bg-gray-100 text-gray-500 hover:bg-brand-bg hover:text-brand'}`}
                >
                  <Package size={16} />
                </button>
              </div>
            </div>
          </div>

          {loading || bundlesLoading ? (
            <div className="flex justify-center items-center py-24">
              <div className="w-10 h-10 border-4 border-gray-200 border-t-brand rounded-full animate-spin" />
            </div>
          ) :products.length === 0 && bundlesCount === 0 && !bundlesLoading ? (
            <div className="text-center py-20">
<p className="text-xl font-bold text-gray-700 mb-2">No products found</p>
              <p className="text-gray-400">Try changing the filters</p>
            </div>
          ) : (
            <>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3 sm:gap-4 items-start">
                {showBundles && bundlesFirst && page === 1 && sortedBundles.map(b => (
                  <BundleCard key={`bundle-${b.id}`} bundle={b} expanded={viewMode === 'variant'} />
                ))}
                {viewMode === 'product'
                  ? products.map(p => <ProductCard key={p.id} product={p} />)
                  : (() => {
                      // "By pack size": EACH variant is a separate card. The order is
                      // set by the
                      // backend (variant_order) — globally sorted so that
                      // out-of-stock variants go to the end of the WHOLE list, not of the page.
                      const byId = new Map(products.map(p => [p.id, p]))
                      return variantOrder.map(({ product_id, variant_id }) => {
                        const p = byId.get(product_id)
                        if (!p) return null
                        const v = p.variants.find(x => x.id === variant_id) ?? null
                        return <VariantCard key={`${product_id}-${variant_id}`} product={p} variant={v} />
                      })
                    })()
                }
                {/* A category made up of bundles only: we cut them into pages (PAGE_SIZE
                   per page) instead of dumping them all on the first one. start/end — an offset
                   that accounts for products (there are 0 here, so these are plain bundle slices). */}
                {showBundles && !bundlesFirst && (() => {
                  const start = Math.max(0, (page - 1) * PAGE_SIZE - productTotal)
                  const end = Math.max(0, page * PAGE_SIZE - productTotal)
                  return sortedBundles.slice(start, end).map(b => (
                    <BundleCard key={`bundle-${b.id}`} bundle={b} expanded={viewMode === 'variant'} />
                  ))
                })()}
              </div>
              <Pagination page={page} totalPages={totalPages} onChange={goToPage} />
            </>
          )}
        </div>
      </div>
    </div>
  )
}


// Page numbers with ellipses: always show the first/last and the neighbors of the current one.
function pageNumbers(current: number, total: number): (number | '…')[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1)
  const out: (number | '…')[] = [1]
  const left = Math.max(2, current - 1)
  const right = Math.min(total - 1, current + 1)
  if (left > 2) out.push('…')
  for (let i = left; i <= right; i++) out.push(i)
  if (right < total - 1) out.push('…')
  out.push(total)
  return out
}

function Pagination({ page, totalPages, onChange }: { page: number; totalPages: number; onChange: (p: number) => void }) {
  if (totalPages <= 1) return null
  const base = 'min-w-9 h-9 px-2 flex items-center justify-center rounded-xl text-sm font-bold transition-colors'
  const inactive = 'border border-gray-200 text-gray-600 hover:border-brand hover:text-brand'
  return (
    <nav className="flex items-center justify-center gap-1.5 mt-8 flex-wrap" aria-label="Pages">
      <button
        onClick={() => onChange(page - 1)}
        disabled={page <= 1}
        aria-label="Previous page"
        className={`${base} ${inactive} disabled:opacity-40 disabled:pointer-events-none`}
      >
        <ChevronLeft size={16} />
      </button>
      {pageNumbers(page, totalPages).map((n, i) =>
        n === '…' ? (
          <span key={`gap-${i}`} className="min-w-9 h-9 flex items-center justify-center text-gray-400">…</span>
        ) : (
          <button
            key={n}
            onClick={() => onChange(n)}
            aria-current={n === page ? 'page' : undefined}
            className={`${base} ${n === page ? 'bg-brand text-white shadow-sm' : inactive}`}
          >
            {n}
          </button>
        ),
      )}
      <button
        onClick={() => onChange(page + 1)}
        disabled={page >= totalPages}
        aria-label="Next page"
        className={`${base} ${inactive} disabled:opacity-40 disabled:pointer-events-none`}
      >
        <ChevronRight size={16} />
      </button>
    </nav>
  )
}

// Tooltip caption for the attribute status.
export function attrStatusLabel(s: 'in_stock' | 'out_of_stock' | 'no_combo'): string {
  if (s === 'out_of_stock') return 'Out of stock'
  if (s === 'no_combo') return 'No product with this combination'
  return ''
}

// Attribute button status taking the current selection of other attributes into account:
// - 'in_stock' — the combination exists and is in stock
// - 'out_of_stock' — the combination exists, but all such variants have stock=0
// - 'no_combo' — no such combination exists (choosing it would break another attribute)
export type AttrChipStatus = 'in_stock' | 'out_of_stock' | 'no_combo'
export function attrChipStatus(
  variants: { stock: number; shipping_origin?: ShippingOrigin; attribute_values: AttributeValueRef[] }[],
  selected: Record<number, number>,
  attrId: number,
  valueId: number,
): AttrChipStatus {
  // Two attributes are "linked" only if a variant exists that carries both at once.
  // If "volume" and "weight" live on DIFFERENT variants (one attribute each), they
  // are
  // independent — choosing a weight must not cancel the volume and vice versa (not 'no_combo').
  const coOccurs = (a: number, b: number) =>
    a === b || variants.some(v =>
      v.attribute_values.some(av => av.attribute_id === a) &&
      v.attribute_values.some(av => av.attribute_id === b)
    )
  // In target keep only those selected attributes that combine with the target one.
  const target: Record<number, number> = { [attrId]: valueId }
  for (const [aidStr, vid] of Object.entries(selected)) {
    const aid = Number(aidStr)
    if (aid !== attrId && coOccurs(aid, attrId)) target[aid] = vid
  }
  const matches = variants.filter(v =>
    Object.entries(target).every(([aid, vid]) =>
      v.attribute_values.some(av => av.attribute_id === Number(aid) && av.id === vid)
    )
  )
  if (matches.length === 0) return 'no_combo'
  if (matches.some(v => variantInStock(v))) return 'in_stock'
  return 'out_of_stock'
}

// Global (module-level) hover state of catalog cards: we hold the id of the active card.
// Thanks to this, when moving quickly from one card to another, the previous one closes
// instantly without waiting for onMouseLeave.
let _hoveredCardId: string | null = null
const _hoverListeners = new Set<() => void>()
function _setHovered(id: string | null) {
  if (_hoveredCardId === id) return
  _hoveredCardId = id
  _hoverListeners.forEach(l => l())
}

// Subscription for external components (HScroller): on touch, reserve space
// for an expanded card the same way desktop reserves it on hover.
export function subscribeCardHover(cb: () => void) {
  _hoverListeners.add(cb)
  return () => { _hoverListeners.delete(cb) }
}
export function isCardHovered() {
  return _hoveredCardId !== null
}
// Calculates the discount: variant.price is the BASE price; if there is a discount %,
// final = price * (1 - % / 100), and price itself is shown as the struck-through old one.
// If variant.old_price > price is set explicitly, we use it as the old price.
export function computeDiscount(
  price: number | null | undefined,
  variant?: { old_price?: number | null; effective_discount?: number | null; discount_percent?: number | null; discount_amount?: number | null } | null
): { finalPrice: number | null; oldPrice: number | null; discountPct: number } {
  if (price == null) return { finalPrice: null, oldPrice: null, discountPct: 0 }
  const pct = Number(variant?.effective_discount ?? variant?.discount_percent ?? 0) || 0
  // An explicitly set old price takes priority.
  const explicitOld = variant?.old_price != null ? Number(variant.old_price) : null
  const amount = variant?.discount_amount != null ? Number(variant.discount_amount) : 0
  // Storefront prices are always whole numbers — round the final and the old price (the percent is
  // computed before rounding).
  let finalPrice = price
  let oldPrice: number | null = null
  let discountPct = 0
  if (explicitOld != null && explicitOld > price) {
    finalPrice = price
    oldPrice = explicitOld
    discountPct = pct > 0 ? pct : Math.round(((explicitOld - price) / explicitOld) * 100)
  } else if (pct > 0 && pct < 100) {
    finalPrice = price * (1 - pct / 100)
    oldPrice = price
    discountPct = pct
  }
  // Fixed UAH discount — on top of the % (consistent with the backend: effective_price /
  // create_order).
  if (amount > 0) {
    const reduced = finalPrice - amount
    if (reduced > 0) {
      if (oldPrice == null) oldPrice = price
      finalPrice = reduced
      if (oldPrice > 0) discountPct = Math.round(((oldPrice - reduced) / oldPrice) * 100)
    }
  }
  return {
    finalPrice: roundPrice(finalPrice),
    oldPrice: oldPrice != null ? roundPrice(oldPrice) : null,
    discountPct,
  }
}

// Device without hover (touch): expand the card on tap, not on hover.
const _isTouch = typeof window !== 'undefined'
  && typeof window.matchMedia === 'function'
  && window.matchMedia('(hover: none)').matches

// A tap outside any card closes the expanded one. The listener is attached to the document once.
let _dismissBound = false
function _bindTouchDismiss() {
  if (_dismissBound || !_isTouch) return
  _dismissBound = true
  document.addEventListener('pointerdown', e => {
    if (!_hoveredCardId) return
    const t = e.target as HTMLElement | null
    if (!t || !t.closest('[data-card]')) _setHovered(null)
  })
}

// Desktop: if the cursor leaves the window WITHOUT moving across the page (Alt+Tab, a click into
// another
// window, tab switch, the cart overlapping the card) — the browser doesn't send mouseleave,
// so the global _hoveredCardId gets «stuck». The expanded card keeps z-index:40,
// its panel sticks out downward and overlaps the neighboring cards → their hover «stops
// working» until reload. We reset hover manually on these events.
let _desktopDismissBound = false
function _bindDesktopDismiss() {
  if (_desktopDismissBound || _isTouch) return
  _desktopDismissBound = true
  const reset = () => _setHovered(null)
  window.addEventListener('blur', reset)
  document.addEventListener('mouseleave', reset)
  document.addEventListener('visibilitychange', () => { if (document.hidden) reset() })
}

export function useCardHover(id: string) {
  const [, force] = useState(0)
  useEffect(() => {
    const l = () => force(x => x + 1)
    _hoverListeners.add(l)
    _bindTouchDismiss()
    _bindDesktopDismiss()
    return () => {
      _hoverListeners.delete(l)
      // When a card unmounts (including on navigation/back) reset hover —
      // otherwise the global _hoveredCardId gets «stuck» and the card stays expanded.
      if (_hoveredCardId === id) _setHovered(null)
    }
  }, [id])
  return {
    hovered: _hoveredCardId === id,
    isTouch: _isTouch,
    // On touch we ignore mouse events — the browser emulates them after a tap.
    onEnter: () => { if (!_isTouch) _setHovered(id) },
    onLeave: () => { if (!_isTouch && _hoveredCardId === id) _setHovered(null) },
    // The first tap on a card on touch expands it instead of navigating to the page.
    // A repeat tap (card already expanded) is let through → the <Link> navigation works.
    onCardClick: (e: MouseEvent) => {
      if (!_isTouch || _hoveredCardId === id) return
      e.preventDefault()
      _setHovered(id)
    },
  }
}

// Attributes that have only one value among the passed variants
// are shown as a plain line, since there is nothing to choose from.
type WithAttrs = { attribute_values: AttributeValueRef[] }
export function collectSingleValueAttrs(variants: WithAttrs[]) {
  const valueIds = new Map<number, Set<number>>()
  const first = new Map<number, { attribute_id: number; name: string; value: string }>()
  for (const v of variants) {
    for (const av of v.attribute_values) {
      if (!valueIds.has(av.attribute_id)) valueIds.set(av.attribute_id, new Set())
      valueIds.get(av.attribute_id)!.add(av.id)
      if (!first.has(av.attribute_id)) first.set(av.attribute_id, { attribute_id: av.attribute_id, name: av.attribute_name, value: av.value })
    }
  }
  return [...valueIds.entries()]
    .filter(([, ids]) => ids.size === 1)
    .map(([attrId]) => first.get(attrId)!)
    .filter(Boolean)
}

export function isLightColor(hex: string): boolean {
  const m = /^#?([\da-f]{6})$/i.exec(hex || '')
  if (!m) return false
  const v = parseInt(m[1], 16)
  const r = (v >> 16) & 0xff, g = (v >> 8) & 0xff, b = v & 0xff
  // Relative luminance approximation
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
  return lum > 0.6
}

// Lightened color (mixed with white) for the «faded» state of a color chip.
// We do it with color, NOT opacity on the button — otherwise the child status tooltip
// inherits the transparency and becomes barely visible.
export function fadeColor(hex: string): string {
  const m = /^#?([\da-f]{6})$/i.exec(hex || '')
  if (!m) return hex
  const v = parseInt(m[1], 16)
  const mix = (c: number) => Math.round(c + (255 - c) * 0.55)
  return `rgb(${mix((v >> 16) & 0xff)}, ${mix((v >> 8) & 0xff)}, ${mix(v & 0xff)})`
}

export function groupVariantsByAttributes<T extends { attribute_values: AttributeValueRef[] }>(variants: T[]) {
  const groups = new Map<number, { name: string, values: Map<number, AttributeValueRef> }>()
  for (const v of variants) {
    for (const av of (v.attribute_values || [])) {
      if (!groups.has(av.attribute_id)) {
        groups.set(av.attribute_id, { name: av.attribute_name, values: new Map() })
      }
      groups.get(av.attribute_id)!.values.set(av.id, av)
    }
  }
  return Array.from(groups.entries()).map(([attribute_id, g]) => ({
    attribute_id,
    name: g.name,
    values: Array.from(g.values.values()),
  }))
}

// Card face wrapper: clickable (Link to the product) or static (div) for hidden
// products inside a bundle. A module-level component — so the photo isn't remounted on hover.
function CardShell({ locked, href, onClick, children }: { locked: boolean; href: string; onClick: (e: MouseEvent) => void; children: ReactNode }) {
  if (locked) return <div className="group flex flex-col cursor-default">{children}</div>
  return <Link to={href} onClick={onClick} className="group flex flex-col">{children}</Link>
}

function hasActiveDescendant(cat: Category, allCats: Category[], activeSlug: string): boolean {
  const children = allCats.filter(c => c.parent_id === cat.id)
  return children.some(c => c.slug === activeSlug || hasActiveDescendant(c, allCats, activeSlug))
}

function isDescendantOfActive(cat: Category, allCats: Category[], activeSlug: string): boolean {
  let cur: Category | undefined = allCats.find(c => c.id === cat.parent_id)
  while (cur) {
    if (cur.slug === activeSlug) return true
    cur = allCats.find(c => c.id === cur!.parent_id)
  }
  return false
}

function CategoryItem({ cat, allCats, activeSlug, onSelect, onExpand, expandedId, depth = 0 }: {
  cat: Category; allCats: Category[]; activeSlug: string; onSelect: (s: string) => void
  onExpand?: (id: number | null) => void; expandedId?: number | null; depth?: number
}) {
  const children = allCats.filter(c => c.parent_id === cat.id)
  const isActive = activeSlug === cat.slug
  const isRootLevel = depth === 0
  // Auto-expand the branch that contains the active category (and its subtree is visible too).
  const autoOpen = isRootLevel
    ? expandedId === cat.id
    : isActive
      || hasActiveDescendant(cat, allCats, activeSlug)
      || isDescendantOfActive(cat, allCats, activeSlug)

  // Local state for non-root levels — so the user can expand/collapse
  // subcategories by clicking the arrow regardless of the active category.
  const [localOpen, setLocalOpen] = useState<boolean | null>(null)
  const open = isRootLevel
    ? autoOpen
    : (localOpen ?? autoOpen)

  const toggle = () => {
    if (isRootLevel) {
      if (onExpand) onExpand(expandedId === cat.id ? null : cat.id)
    } else {
      setLocalOpen(!open)
    }
  }

  return (
    <div>
      <div className={`flex items-center gap-1 rounded-xl transition-colors ${isActive ? 'bg-brand-bg' : 'hover:bg-gray-100'}`}>
        <button
          onClick={() => { onSelect(cat.slug); if (isRootLevel && onExpand) onExpand(cat.id) }}
          className={`flex-1 text-left py-2 px-3 text-base transition-colors ${isActive ? 'text-brand font-bold' : 'text-gray-600'}`}
        >
          {cat.name}
        </button>
        {children.length > 0 && (
          <button onClick={toggle} className={`p-2 transition-colors ${isActive ? 'text-brand' : 'text-gray-400 hover:text-gray-600'}`}>
            <ChevronDown size={22} className={`transition-transform ${open ? 'rotate-180' : ''}`} />
          </button>
        )}
      </div>
      {open && children.length > 0 && (
        <div className="relative ml-[1.02em] pl-1.5 flex flex-col gap-0.5 pt-0.5 before:absolute before:left-0 before:top-1.5 before:bottom-1.5 before:w-[2px] before:rounded before:bg-gray-200">
          {children.map(child => (
            <CategoryItem key={child.id} cat={child} allCats={allCats} activeSlug={activeSlug} onSelect={onSelect} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  )
}


export function ProductCard({ product, scope = '', badge, expectedVariantId }: { product: ProductListItem; scope?: string; badge?: ReactNode; expectedVariantId?: number }) {
  const [qty, setQty] = useState(1)
  const { hovered, onEnter, onLeave, onCardClick } = useCardHover(`p:${scope}:${product.id}`)
  const { activeInfoId } = useTapInfo()
  const elevated = hovered
  // Default variant: the first in stock → otherwise the first in the list.
  // (The backend listing returns only active variants, so no is_active filter is needed.)
  const pickDefaultVariant = (p: ProductListItem): ProductListVariant | null => {
    return p.variants.find(v => v.stock > 0) || p.variants[0] || null
  }
  const hasInStock = (p: ProductListItem) => p.variants.some(v => variantInStock(v))
  // If expectedVariantId is passed (bundle composition) — it belongs to this very `product`,
  // so we don't switch the pack to the twin, to avoid losing the needed variant.
  const expectedInProduct = expectedVariantId != null && product.variants.some(v => v.id === expectedVariantId)
  // Factory↔own switcher. If the product has nothing in stock by default but its twin does — start
  // from the twin.
  const initialKind: 'factory' | 'own' = !expectedInProduct && product.paired && !hasInStock(product) && hasInStock(product.paired)
    ? product.paired.packaging_kind
    : product.packaging_kind
  const [activeKind, setActiveKind] = useState<'factory' | 'own'>(initialKind)
  const current: ProductListItem = activeKind === product.packaging_kind || !product.paired ? product : product.paired
  // Keep only the id of the selected variant — derive the object itself from current.variants.
  // When switching the pack the old selectedVariantId won't be found → the fallback gives us the
  // first «in stock» one.
  // For bundle composition start exactly from the variant that is part of the bundle.
  const [selectedVariantId, setSelectedVariantId] = useState<number | null>(
    (expectedVariantId != null && current.variants.some(v => v.id === expectedVariantId))
      ? expectedVariantId
      : pickDefaultVariant(current)?.id ?? null
  )
  const selectedVariant: ProductListVariant | null = current.variants.find(v => v.id === selectedVariantId) ?? pickDefaultVariant(current)
  const cart = useCart()
  const liked = useLiked()
  const { openCart } = useUI()

  // Pack change — clamp qty to the new variant's limit.
  useEffect(() => {
    const max = selectedVariant ? variantMaxQty(selectedVariant) : undefined
    if (max != null) setQty(q => Math.min(q, Math.max(1, max)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedVariantId])

  const displayPrice = selectedVariant?.price ?? current.min_price
  const sku = selectedVariant?.sku
  const inStock = selectedVariant
    ? variantInStock(selectedVariant)
    : (current.variants.length > 0 ? current.variants.some(v => variantInStock(v)) : current.min_price !== null)

  // Group variants by attributes for the pack selection UI.
  const attrGroups = groupVariantsByAttributes(current.variants)
  const hasAttrGroups = attrGroups.length > 0
  // selectedValues: attribute_id → value_id, initialized from the selected variant.
  const selectedValues: Record<number, number> = {}
  if (selectedVariant) {
    for (const av of selectedVariant.attribute_values || []) {
      selectedValues[av.attribute_id] = av.id
    }
  }
  const pickVariantByAttr = (attrId: number, valueId: number) => {
    const next = { ...selectedValues, [attrId]: valueId }
    // Find the variant that has ALL the selected values.
    const exact = current.variants.find(v =>
      Object.entries(next).every(([aid, vid]) =>
        v.attribute_values.some(av => av.attribute_id === Number(aid) && av.id === vid)
      )
    )
    // If there is no exact one (the combination is unavailable) — any variant
    // with the new value is taken, the other attributes are ignored.
    const fallback = current.variants.find(v =>
      v.attribute_values.some(av => av.attribute_id === attrId && av.id === valueId)
    )
    if (exact) setSelectedVariantId(exact.id)
    else if (fallback) setSelectedVariantId(fallback.id)
  }
  const likeKey = `${current.slug}_${selectedVariant?.id ?? 0}`
  const isLiked = liked.isLiked(likeKey)

  // If the product has only one variant — show the pack itself as the name,
  // otherwise the product name (and the variant choice is in the hover panel).
  // In the listing all variants are already active (backend filter).
  const onlyVariant = current.variants.length === 1 ? current.variants[0] : null
  const displayTitle = noWidow(onlyVariant?.name || current.name)
  const { ref: titleRef, clamped: titleClamped } = useClampedTitle(displayTitle)

  // Hidden product (only inside a bundle): the card is not clickable and cannot be bought
  // separately.
  const locked = product.is_active === false
  const href = `/product/${current.slug}${selectedVariant ? `?variant=${selectedVariant.id}` : ''}`

  return (
    <div
      data-card="1"
      className="relative"
      style={{ zIndex: elevated ? 40 : undefined }}
    >
      {/* Spacer — reserves space in the grid so that the overlay card can grow downward
         (as the name expands) without pushing the neighboring cards. */}
      <div className="invisible flex flex-col pointer-events-none" aria-hidden="true">
        <div className="aspect-square w-full" />
        <div className="px-4 pt-3 pb-3">
          <div className="flex items-end min-h-[2lh]"><p className="text-base font-bold leading-snug line-clamp-2">{displayTitle}</p></div>
        </div>
      </div>
      <div className="absolute inset-0">
      <div
        className={`bg-white flex flex-col relative rounded-2xl border`}
        style={{
          borderColor: 'var(--color-border)',
          minHeight: '100%',
          boxShadow: hovered ? 'var(--shadow-card-hover)' : 'var(--shadow-card-low)',
        }}
        onMouseEnter={onEnter}
        onMouseLeave={onLeave}
      >
        {badge}
        <CardShell locked={locked} href={href} onClick={onCardClick}>
          <div className="aspect-square relative">
            {/* Inner clip wrapper for the image only — so that overlays
               (especially the paired switcher) can extend beyond the bottom. */}
            <div className="absolute inset-0 overflow-hidden rounded-t-2xl">
              {(selectedVariant?.image || current.main_image)
                ? <img src={selectedVariant?.image_thumb || selectedVariant?.image || current.main_image_thumb || current.main_image || ''} alt={current.name} loading="lazy" decoding="async" className="w-full h-full object-contain p-3 rounded-2xl lg:rounded-3xl" />
                : <div className="w-full h-full flex items-center justify-center bg-white"><div className="w-14 h-14 rounded-full bg-brand-bg text-brand flex items-center justify-center"><ShoppingBag size={26} /></div></div>
              }
              {!inStock && (
                <div className="absolute inset-0 bg-white/60 flex items-center justify-center">
                  <span className="text-xs font-bold text-gray-400 bg-white px-2 py-1 rounded-lg border border-gray-200">Out of stock</span>
                </div>
              )}
            </div>
            {/* New/Hit — a PRODUCT status: always shown (an aggregate over the pack sizes),
               so that a card on the "New arrivals"/"Bestsellers" tab always has its label. Promo/
               made to order/discount stay bound to the selected pack size (otherwise
               "Promo" would show even when the displayed variant has no discount). */}
            {(() => {
              const STATUS = new Set(['novynka', 'khit'])
              const rank = (slug: string) =>
                ({ khit: 10, aktsia: 20, novynka: 30, 'pid-zamovlennya': 1000 } as Record<string, number>)[slug] ?? 100
              const variantLabels = selectedVariant?.active_labels ?? current.labels
              const bySlug = new Map<string, { name: string; color: string; slug: string }>()
              for (const l of [...current.labels.filter(l => STATUS.has(l.slug)), ...variantLabels]) {
                if (!bySlug.has(l.slug)) bySlug.set(l.slug, l)
              }
              const cardLabels = [...bySlug.values()].sort((a, b) => rank(a.slug) - rank(b.slug))
              if (cardLabels.length === 0) return null
              return (
                <div className="absolute top-2 left-2 flex flex-col items-start gap-1 z-10">
                  {cardLabels.map(l => (
                    <ProductLabel key={l.slug} name={l.name} color={l.color} />
                  ))}
                </div>
              )
            })()}
          </div>
          <div className={`px-4 pt-3 ${hovered ? 'pb-0' : 'pb-3'}`}>
            <div className={`flex ${titleClamped ? 'items-start' : 'items-end'} min-h-[2lh]`}><p ref={titleRef} className={`text-base font-bold leading-snug text-gray-800 ${hovered ? '' : 'line-clamp-2'}`}>{displayTitle}</p></div>
          </div>
        </CardShell>

        {/* Panel with SKU/price/variants/button — slides out under the card (absolute)
           so as not to shift the name/photo when it expands. */}
        <div style={{ display: hovered ? 'block' : 'none' }}>
          <div className="px-4 pt-2 pb-4 flex flex-col gap-2.5">
            {/* Warning: a variant that is not part of the bundle is selected. */}
            {expectedVariantId != null && selectedVariant && selectedVariant.id !== expectedVariantId && (
              <p className="-mt-1 -mb-1.5 text-[11px] font-bold text-amber-700 bg-amber-50 rounded-lg px-2 py-1.5 text-center leading-snug">
                This variant is not part of the bundle
              </p>
            )}
            {/* SKU + price — shown on hover. */}
            {(() => {
              const { finalPrice, oldPrice, discountPct } = computeDiscount(displayPrice, selectedVariant)
              const hasDiscount = oldPrice != null
              return (
                <div className="flex items-end justify-between gap-2">
                  <span className="text-sm text-gray-400">{sku ?? '—'}</span>
                  <div className="text-right">
                    {hasDiscount && (
                      <div className="flex items-center justify-end gap-1.5 mb-0.5">
                        <span className="text-sm text-gray-400 line-through">{Math.round(oldPrice!)} UAH</span>
                        {discountPct > 0 && (
                          <ProductLabel name={`-${Math.round(discountPct)}%`} color="#ef4444" size="md" showIcon={false} />
                        )}
                      </div>
                    )}
                    <span className={`font-bold text-xl leading-none ${hasDiscount ? '' : 'text-gray-900'}`} style={hasDiscount ? { color: SALE_RED } : undefined}>
                      {finalPrice != null ? `${Math.round(finalPrice)} UAH` : '—'}
                    </span>
                  </div>
                </div>
              )
            })()}

            {/* Factory / Own pack size switch — under the price, together with the parameters. */}
            {product.paired && (
              <div>
                <p className="text-[11px] text-gray-500 mb-1 text-center">Packaging type</p>
                <div
                  className="flex items-center justify-center gap-1.5 flex-wrap"
                  onClick={e => { e.preventDefault(); e.stopPropagation() }}
                >
                  {(['factory', 'own'] as const).map(kind => {
                    const active = activeKind === kind
                    return (
                      <button
                        key={kind}
                        type="button"
                        onClick={() => setActiveKind(kind)}
                        title={kind === 'factory' ? 'Factory' : 'Our own'}
                        className={`inline-flex items-center justify-center min-w-[40px] h-7 px-2.5 rounded-lg text-xs font-bold leading-none pt-px transition-colors ${
                          active ? 'bg-brand text-white' : 'bg-gray-100 text-gray-500 hover:bg-brand-bg hover:text-brand'
                        }`}
                      >
                        {kind === 'factory' ? 'Factory' : 'Our own'}
                      </button>
                    )
                  })}
                </div>
              </div>
            )}

            {current.variants.length > 1 && (
              hasAttrGroups ? (
                attrGroups.map(group => (
                  <div key={group.attribute_id}>
                    <p className="text-[11px] text-gray-500 mb-1 text-center">{group.name}</p>
                    <div className="flex gap-1.5 flex-wrap justify-center">
                      {group.values.map(av => {
                        const isSelected = selectedValues[group.attribute_id] === av.id
                        const status = attrChipStatus(current.variants, selectedValues, group.attribute_id, av.id)
                        if (av.is_color && av.color_hex) {
                          return (
                            <button
                              key={av.id}
                              onClick={() => pickVariantByAttr(group.attribute_id, av.id)}
                              aria-label={av.value}
                              className={`group/chip relative w-7 h-7 rounded-lg transition-all ${!isSelected && status !== 'no_combo' && status !== 'out_of_stock' ? 'hover:scale-110' : ''}`}
                              style={{ background: !isSelected && (status === 'no_combo' || status === 'out_of_stock') ? fadeColor(av.color_hex) : av.color_hex }}
                            >
                              {isSelected && (
                                <span className="absolute inset-0 flex items-center justify-center pointer-events-none">
                                  <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke={isLightColor(av.color_hex) ? '#111827' : '#fff'} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                                    <path d="M3 8.5l3.2 3.2L13 5" />
                                  </svg>
                                </span>
                              )}
                              {!isSelected && status === 'no_combo' && (
                                <span className="absolute inset-0 pointer-events-none">
                                  <svg viewBox="0 0 28 28" className="w-full h-full" preserveAspectRatio="none">
                                    <line x1="2" y1="26" x2="26" y2="2" stroke="rgba(0,0,0,0.5)" strokeWidth="2" />
                                  </svg>
                                </span>
                              )}
                              {!isSelected && (status === 'out_of_stock' || status === 'no_combo') && (
                                <span className={`pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 px-2 py-1 bg-gray-900 text-white text-[10px] font-medium rounded whitespace-nowrap transition-opacity z-50 ${activeInfoId === av.id ? 'opacity-100' : 'opacity-0 group-hover/chip:opacity-100'}`}>
                                  {attrStatusLabel(status)}
                                </span>
                              )}
                            </button>
                          )
                        }
                        return (
                          <button
                            key={av.id}
                            onClick={() => pickVariantByAttr(group.attribute_id, av.id)}
                            className={`group/chip relative inline-flex items-center justify-center min-w-[40px] h-7 px-2.5 rounded-lg text-xs font-bold leading-none pt-px transition-colors ${
                              isSelected
                                ? 'bg-brand text-white'
                                : status === 'no_combo'
                                  ? 'bg-gray-50 text-gray-300 line-through hover:bg-gray-100 hover:text-gray-400'
                                  : status === 'out_of_stock'
                                    ? 'bg-gray-50 text-gray-300 hover:bg-gray-100 hover:text-gray-400'
                                    : 'bg-gray-100 text-gray-500 hover:bg-brand-bg hover:text-brand'
                            }`}
                          >
                            {av.value}
                            {!isSelected && (status === 'out_of_stock' || status === 'no_combo') && (
                              <span className={`pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 px-2 py-1 bg-gray-900 text-white text-[10px] font-medium rounded whitespace-nowrap transition-opacity z-50 ${activeInfoId === av.id ? 'opacity-100' : 'opacity-0 group-hover/chip:opacity-100'}`}>
                                {attrStatusLabel(status)}
                              </span>
                            )}
                          </button>
                        )
                      })}
                    </div>
                  </div>
                ))
              ) : (
                <div className="flex gap-1.5 flex-wrap">
                  {current.variants.map(v => {
                    const label = v.name.includes(',') ? v.name.split(',').pop()!.trim() : v.name
                    return (
                      <button
                        key={v.id}
                        onClick={() => setSelectedVariantId(v.id)}
                        title={v.name}
                        className={`inline-flex items-center justify-center min-w-[40px] h-7 px-2.5 rounded-lg text-xs font-bold leading-none pt-px transition-colors ${
                          selectedVariant?.id === v.id
                            ? 'bg-brand text-white'
                            : 'bg-gray-100 text-gray-500 hover:bg-brand-bg hover:text-brand'
                        }`}
                      >
                        {label}
                      </button>
                    )
                  })}
                </div>
              )
            )}
            {locked ? (
              <span className="flex-1 h-8 flex items-center justify-center text-xs font-bold text-gray-500 bg-gray-100 rounded-xl">Part of the bundle</span>
            ) : inStock ? (
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                <div className="sm:hidden">
                  <QtyInput value={qty} onChange={setQty} fullWidth max={selectedVariant ? variantMaxQty(selectedVariant) : undefined} />
                </div>
                <div className="hidden sm:block">
                  <QtyInput value={qty} onChange={setQty} size="sm" max={selectedVariant ? variantMaxQty(selectedVariant) : undefined} />
                </div>
                <div className="flex items-stretch gap-2 sm:contents">
                <button
                  onClick={() => {
                    if (selectedVariant && displayPrice) {
                      const { finalPrice } = computeDiscount(displayPrice, selectedVariant)
                      cart.addItem({ productSlug: current.slug, productName: current.name, variantId: selectedVariant.id, variantName: selectedVariant.name, sku: selectedVariant.sku ?? null, price: finalPrice ?? displayPrice, image: selectedVariant.image ?? current.main_image ?? null, shippingOrigin: selectedVariant.shipping_origin, supplier: selectedVariant.supplier ?? null, labels: current.labels ?? [], maxQty: variantMaxQty(selectedVariant) }, qty)
                      openCart()
                    }
                  }}
                  className="flex-1 h-9 sm:h-8 flex items-center justify-center bg-brand hover:bg-brand-hover text-white rounded-xl transition-colors"
                >
                  <ShoppingCart size={16} />
                </button>
                <button
                  onClick={() => liked.toggle({
                    key: likeKey,
                    productSlug: current.slug,
                    productName: current.name,
                    variantId: selectedVariant?.id ?? 0,
                    variantName: selectedVariant?.name ?? '',
                    sku: selectedVariant?.sku ?? null,
                    price: displayPrice ?? null,
                    image: selectedVariant?.image ?? current.main_image ?? null,
                  })}
                  className={`w-9 h-9 sm:w-8 sm:h-8 shrink-0 flex items-center justify-center rounded-xl border-2 transition-colors ${
                    isLiked ? 'border-red-300 text-red-500 bg-red-50' : 'border-gray-200 text-gray-400 hover:border-red-300 hover:text-red-400'
                  }`}
                >
                  <Heart size={14} fill={isLiked ? 'currentColor' : 'none'} />
                </button>
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                {selectedVariant?.id ? <StockNotifyButton variantId={selectedVariant.id} /> : <span className="flex-1 h-9 sm:h-8 flex items-center justify-center text-xs font-bold text-gray-600 bg-gray-100 rounded-xl">Out of stock</span>}
                <button
                  onClick={() => liked.toggle({
                    key: likeKey,
                    productSlug: current.slug,
                    productName: current.name,
                    variantId: selectedVariant?.id ?? 0,
                    variantName: selectedVariant?.name ?? '',
                    sku: selectedVariant?.sku ?? null,
                    price: displayPrice ?? null,
                    image: selectedVariant?.image ?? current.main_image ?? null,
                  })}
                  className={`w-9 h-9 sm:w-8 sm:h-8 shrink-0 flex items-center justify-center rounded-xl border-2 transition-colors ${
                    isLiked ? 'border-red-300 text-red-500 bg-red-50' : 'border-gray-200 text-gray-400 hover:border-red-300 hover:text-red-400'
                  }`}
                >
                  <Heart size={14} fill={isLiked ? 'currentColor' : 'none'} />
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
      </div>

    </div>
  )
}

export function BundleCard({ bundle, expanded = false }: { bundle: BundleListItem; expanded?: boolean }) {
  const [qty, setQty] = useState(1)
  const { hovered, onEnter, onLeave, onCardClick } = useCardHover(`b:${bundle.id}`)
  const elevated = hovered
  const displayTitle = bundle.name
  const { ref: titleRef, clamped: titleClamped } = useClampedTitle(displayTitle)
  const cart = useCart()
  const liked = useLiked()
  const { openCart } = useUI()

  // Clamp qty to the stock level.
  useEffect(() => {
    const max = bundle.stock
    if (max != null) setQty(q => Math.min(q, Math.max(1, max)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bundle.id])

  const inStock = (bundle.stock ?? 0) > 0
  const displayPrice = bundle.price != null ? roundPrice(bundle.price) : null
  const sku = bundle.sku
  const likeKey = `bundle_${bundle.slug}_${bundle.id}`
  const isLiked = liked.isLiked(likeKey)

  return (
    <div
      data-card="1"
      className="relative"
      style={{ zIndex: elevated ? 40 : undefined }}
    >
      {/* Spacer — reserves space in the grid. */}
      <div className="invisible flex flex-col pointer-events-none" aria-hidden="true">
        <div className="aspect-square w-full" />
        <div className="px-4 pt-3 pb-3 flex flex-col">
          <div className="flex items-end min-h-[2lh]"><p className="text-base font-bold leading-snug line-clamp-2">{noWidow(displayTitle)}</p></div>
          {expanded && (
            <div className="flex items-end justify-between pt-2">
              <span className="text-sm">{sku ?? '—'}</span>
              <div className="text-right">
                <span className="font-bold text-xl leading-none">{displayPrice ? `${displayPrice} UAH` : '—'}</span>
              </div>
            </div>
          )}
        </div>
      </div>
      <div className="absolute inset-0">
      <div
        className={`bg-white flex flex-col relative rounded-2xl border`}
        style={{
          borderColor: 'var(--color-border)',
          minHeight: '100%',
          boxShadow: hovered ? 'var(--shadow-card-hover)' : 'var(--shadow-card-low)',
        }}
        onMouseEnter={onEnter}
        onMouseLeave={onLeave}
      >
        <Link to={`/bundle/${bundle.slug}`} onClick={onCardClick} className="group flex flex-col">
          <div className="aspect-square overflow-hidden relative rounded-t-2xl">
            {bundle.main_image
              ? <img src={bundle.main_image_thumb || bundle.main_image} alt={bundle.name} loading="lazy" decoding="async" className="w-full h-full object-contain p-3 rounded-2xl lg:rounded-3xl" />
              : <div className="w-full h-full flex items-center justify-center bg-white"><div className="w-14 h-14 rounded-full bg-brand-bg text-brand flex items-center justify-center"><Package size={26} /></div></div>
            }
            {!inStock && (
              <div className="absolute inset-0 bg-white/60 flex items-center justify-center">
                <span className="text-xs font-bold text-gray-400 bg-white px-2 py-1 rounded-lg border border-gray-200">Out of stock</span>
              </div>
            )}
            <div className="absolute top-2 left-2 flex flex-col items-start gap-1">
              <ProductLabel name="Bundle" color="#0ea5e9" />
              {bundle.active_labels.map(l => (
                <ProductLabel key={l.slug} name={l.name} color={l.color} />
              ))}
            </div>
          </div>
          <div className={`px-4 pt-3 ${hovered ? 'pb-0' : 'pb-3'} flex flex-col`}>
            <div className={`flex ${titleClamped ? 'items-start' : 'items-end'} min-h-[2lh]`}><p ref={titleRef} className={`text-base font-bold leading-snug text-gray-800 ${hovered ? '' : 'line-clamp-2'}`}>{noWidow(displayTitle)}</p></div>
            {expanded && (() => {
              const { finalPrice, oldPrice, discountPct } = computeDiscount(displayPrice, bundle)
              const hasDiscount = oldPrice != null
              return (
                <div className="flex items-end justify-between pt-2 gap-2">
                  <span className="text-sm text-gray-400">{sku ?? '—'}</span>
                  <div className="text-right">
                    {hasDiscount && hovered && (
                      <div className="flex items-center justify-end gap-1.5 mb-0.5">
                        <span className="text-sm text-gray-400 line-through">{Math.round(oldPrice!)} UAH</span>
                        {discountPct > 0 && (
                          <ProductLabel name={`-${Math.round(discountPct)}%`} color="#ef4444" size="md" showIcon={false} />
                        )}
                      </div>
                    )}
                    <span className={`font-bold text-xl leading-none ${hasDiscount ? '' : 'text-gray-900'}`} style={hasDiscount ? { color: SALE_RED } : undefined}>
                      {finalPrice != null ? `${Math.round(finalPrice)} UAH` : '—'}
                    </span>
                  </div>
                </div>
              )
            })()}
          </div>
        </Link>

        <div style={{ display: hovered ? 'block' : 'none' }}>
          <div className="px-4 pt-2 pb-4 flex flex-col gap-2.5">
            {/* In the "by pack size" mode (expanded) the SKU+price are already shown in the card body. */}
            {!expanded && (() => {
              const { finalPrice, oldPrice, discountPct } = computeDiscount(displayPrice, bundle)
              const hasDiscount = oldPrice != null
              return (
                <div className="flex items-end justify-between gap-2">
                  <span className="text-sm text-gray-400">{sku ?? '—'}</span>
                  <div className="text-right">
                    {hasDiscount && (
                      <div className="flex items-center justify-end gap-1.5 mb-0.5">
                        <span className="text-sm text-gray-400 line-through">{Math.round(oldPrice!)} UAH</span>
                        {discountPct > 0 && (
                          <ProductLabel name={`-${Math.round(discountPct)}%`} color="#ef4444" size="md" showIcon={false} />
                        )}
                      </div>
                    )}
                    <span className={`font-bold text-xl leading-none ${hasDiscount ? '' : 'text-gray-900'}`} style={hasDiscount ? { color: SALE_RED } : undefined}>
                      {finalPrice != null ? `${Math.round(finalPrice)} UAH` : '—'}
                    </span>
                  </div>
                </div>
              )
            })()}
            {inStock ? (
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                <div className="sm:hidden">
                  <QtyInput value={qty} onChange={setQty} fullWidth max={bundle.stock ?? undefined} />
                </div>
                <div className="hidden sm:block">
                  <QtyInput value={qty} onChange={setQty} size="sm" max={bundle.stock ?? undefined} />
                </div>
                <div className="flex items-stretch gap-2 sm:contents">
                <button
                  onClick={() => { if (displayPrice) { cart.addItem({ productSlug: bundle.slug, productName: bundle.name, variantId: bundle.id, variantName: bundle.name, sku: bundle.sku ?? null, price: displayPrice, image: bundle.main_image ?? null, isBundle: true, maxQty: bundle.stock ?? undefined }, qty); openCart() } }}
                  className="flex-1 h-9 sm:h-8 flex items-center justify-center bg-brand hover:bg-brand-hover text-white rounded-xl transition-colors"
                >
                  <ShoppingCart size={16} />
                </button>
                <button
                  onClick={() => liked.toggle({
                    key: likeKey,
                    productSlug: bundle.slug,
                    productName: bundle.name,
                    variantId: bundle.id,
                    variantName: bundle.name,
                    sku: bundle.sku ?? null,
                    price: displayPrice ?? null,
                    image: bundle.main_image ?? null,
                    isBundle: true,
                  })}
                  className={`w-9 h-9 sm:w-8 sm:h-8 shrink-0 flex items-center justify-center rounded-xl border-2 transition-colors ${
                    isLiked ? 'border-red-300 text-red-500 bg-red-50' : 'border-gray-200 text-gray-400 hover:border-red-300 hover:text-red-400'
                  }`}
                >
                  <Heart size={14} fill={isLiked ? 'currentColor' : 'none'} />
                </button>
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <StockNotifyButton variantId={bundle.id} kind="bundle" />
                <button
                  onClick={() => liked.toggle({
                    key: likeKey,
                    productSlug: bundle.slug,
                    productName: bundle.name,
                    variantId: bundle.id,
                    variantName: bundle.name,
                    sku: bundle.sku ?? null,
                    price: displayPrice ?? null,
                    image: bundle.main_image ?? null,
                    isBundle: true,
                  })}
                  className={`w-9 h-9 sm:w-8 sm:h-8 shrink-0 flex items-center justify-center rounded-xl border-2 transition-colors ${
                    isLiked ? 'border-red-300 text-red-500 bg-red-50' : 'border-gray-200 text-gray-400 hover:border-red-300 hover:text-red-400'
                  }`}
                >
                  <Heart size={14} fill={isLiked ? 'currentColor' : 'none'} />
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
      </div>
    </div>
  )
}

function VariantCard({ product, variant }: { product: ProductListItem; variant: ProductListVariant | null }) {
  const [qty, setQty] = useState(1)
  // On hover we allow "trying on" other variants right in this card;
  // on mouse-leave we return to the original variant.
  const [previewId, setPreviewId] = useState<number | null>(variant?.id ?? null)
  const previewVariant: ProductListVariant | null =
    product.variants.find(v => v.id === previewId) ?? variant
  const { hovered, onEnter, onLeave, onCardClick } = useCardHover(`v:${variant?.id ?? product.id}`)
  const { activeInfoId } = useTapInfo()
  const elevated = hovered
  const { ref: titleRef, clamped: titleClamped } = useClampedTitle(previewVariant?.name || product.name)
  const cart = useCart()
  const liked = useLiked()
  const { openCart } = useUI()

  // Preview variant switch — clamp qty to the new variant's limit.
  useEffect(() => {
    const max = previewVariant ? variantMaxQty(previewVariant) : undefined
    if (max != null) setQty(q => Math.min(q, Math.max(1, max)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewId])

  const price = previewVariant?.price ?? product.min_price
  const sku = previewVariant?.sku
  const inStock = previewVariant ? variantInStock(previewVariant) : false
  const likeKey = `${product.slug}_${previewVariant?.id ?? 0}`
  const isLiked = liked.isLiked(likeKey)

  // Attributes of all the product's variants — on hover show them with the active one highlighted;
  // a click switches the preview within the same card (no jumps in the grid).
  const attrGroups = groupVariantsByAttributes(product.variants)
  const selectedValues: Record<number, number> = {}
  if (previewVariant) {
    for (const av of previewVariant.attribute_values || []) selectedValues[av.attribute_id] = av.id
  }
  const pickPreviewByAttr = (attrId: number, valueId: number) => {
    const next = { ...selectedValues, [attrId]: valueId }
    const target =
      product.variants.find(v =>
        Object.entries(next).every(([aid, vid]) =>
          v.attribute_values.some(av => av.attribute_id === Number(aid) && av.id === vid)
        )
      ) ||
      product.variants.find(v =>
        v.attribute_values.some(av => av.attribute_id === attrId && av.id === valueId)
      )
    if (target) setPreviewId(target.id)
  }
  const handleLeave = () => {
    onLeave()
    // Return to the card's «own» variant.
    setPreviewId(variant?.id ?? null)
  }

  return (
    <div
      id={variant ? `variant-card-${variant.id}` : undefined}
      data-card="1"
      className="relative"
      style={{ zIndex: elevated ? 40 : undefined }}
    >
      {/* Spacer — reserves space in the grid. */}
      <div className="invisible flex flex-col pointer-events-none" aria-hidden="true">
        <div className="aspect-square w-full" />
        <div className="px-4 pt-3 pb-3 flex flex-col">
          <div className="flex items-end min-h-[2lh]"><p className="text-base font-bold leading-snug line-clamp-2">{noWidow(previewVariant?.name || product.name)}</p></div>
          <div className="flex items-end justify-between pt-2">
            <span className="text-sm">{sku ?? '—'}</span>
            <div className="text-right">
              <span className="font-bold text-xl leading-none">{price ? `${price} UAH` : '—'}</span>
            </div>
          </div>
        </div>
      </div>
      <div className="absolute inset-0">
      <div
        className={`bg-white flex flex-col relative rounded-2xl border`}
        style={{
          borderColor: 'var(--color-border)',
          minHeight: '100%',
          boxShadow: hovered ? 'var(--shadow-card-hover)' : 'var(--shadow-card-low)',
        }}
        onMouseEnter={onEnter}
        onMouseLeave={handleLeave}
      >
        <Link to={`/product/${product.slug}${previewVariant ? `?variant=${previewVariant.id}` : ''}`} onClick={onCardClick} className="group flex flex-col">
          <div className="aspect-square overflow-hidden relative rounded-t-2xl">
            {(previewVariant?.image || product.main_image)
              ? <img src={previewVariant?.image_thumb || previewVariant?.image || product.main_image_thumb || product.main_image || ''} alt={previewVariant?.name || product.name} loading="lazy" decoding="async" className="w-full h-full object-contain p-3 rounded-2xl lg:rounded-3xl" />
              : <div className="w-full h-full flex items-center justify-center bg-white"><div className="w-14 h-14 rounded-full bg-brand-bg text-brand flex items-center justify-center"><ShoppingBag size={26} /></div></div>
            }
            {!inStock && (
              <div className="absolute inset-0 bg-white/60 flex items-center justify-center">
                <span className="text-xs font-bold text-gray-400 bg-white px-2 py-1 rounded-lg border border-gray-200">Out of stock</span>
              </div>
            )}
            {/* Labels of the preview variant, not a union over the product (see the comment above). */}
            {(() => {
              const cardLabels = previewVariant?.active_labels ?? product.labels
              if (cardLabels.length === 0) return null
              return (
                <div className="absolute top-2 left-2 flex flex-col items-start gap-1">
                  {cardLabels.map(l => (
                    <ProductLabel key={l.slug} name={l.name} color={l.color} />
                  ))}
                </div>
              )
            })()}
          </div>
          <div className="px-4 pt-3 pb-3 flex flex-col">
            <div className={`flex ${titleClamped ? 'items-start' : 'items-end'} min-h-[2lh]`}><p ref={titleRef} className={`text-base font-bold leading-snug text-gray-800 ${hovered ? '' : 'line-clamp-2'}`}>
              {noWidow(previewVariant?.name || product.name)}
            </p></div>
            {(() => {
              const { finalPrice, oldPrice, discountPct } = computeDiscount(price, previewVariant)
              const hasDiscount = oldPrice != null
              return (
                <div className="flex items-end justify-between pt-2 gap-2">
                  <span className="text-sm text-gray-400">{sku ?? '—'}</span>
                  <div className="text-right">
                    {hasDiscount && hovered && (
                      <div className="flex items-center justify-end gap-1.5 mb-0.5">
                        <span className="text-sm text-gray-400 line-through">{Math.round(oldPrice!)} UAH</span>
                        {discountPct > 0 && (
                          <ProductLabel name={`-${Math.round(discountPct)}%`} color="#ef4444" size="md" showIcon={false} />
                        )}
                      </div>
                    )}
                    <span className={`font-bold text-xl leading-none ${hasDiscount ? '' : 'text-gray-900'}`} style={hasDiscount ? { color: SALE_RED } : undefined}>
                      {finalPrice != null ? `${Math.round(finalPrice)} UAH` : '—'}
                    </span>
                  </div>
                </div>
              )
            })()}
          </div>
        </Link>

        <div style={{ display: hovered ? 'block' : 'none' }}>
          <div className="px-4 pt-2 pb-4 flex flex-col gap-2.5">
            {/* Attributes of the product's other variants. Statuses: in_stock — regular; out_of_stock —
               muted; no_combo — struck through. A click always switches the preview: if there is no
               exact combination, the linking attributes are pulled in. */}
            {product.variants.length > 1 && attrGroups.map(group => (
              <div key={group.attribute_id}>
                <p className="text-[11px] text-gray-500 mb-1 text-center">{group.name}</p>
                <div className="flex gap-1.5 flex-wrap justify-center">
                  {group.values.map(av => {
                    const isSelected = selectedValues[group.attribute_id] === av.id
                    const status = attrChipStatus(product.variants, selectedValues, group.attribute_id, av.id)
                    if (av.is_color && av.color_hex) {
                      return (
                        <button
                          key={av.id}
                          onClick={() => pickPreviewByAttr(group.attribute_id, av.id)}
                          aria-label={av.value}
                          className={`group/chip relative w-7 h-7 rounded-lg transition-all ${!isSelected && status !== 'no_combo' && status !== 'out_of_stock' ? 'hover:scale-110' : ''}`}
                          style={{ background: !isSelected && (status === 'no_combo' || status === 'out_of_stock') ? fadeColor(av.color_hex) : av.color_hex }}
                        >
                          {isSelected && (
                            <span className="absolute inset-0 flex items-center justify-center pointer-events-none">
                              <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke={isLightColor(av.color_hex) ? '#111827' : '#fff'} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M3 8.5l3.2 3.2L13 5" />
                              </svg>
                            </span>
                          )}
                          {!isSelected && status === 'no_combo' && (
                            <span className="absolute inset-0 pointer-events-none">
                              <svg viewBox="0 0 28 28" className="w-full h-full" preserveAspectRatio="none">
                                <line x1="2" y1="26" x2="26" y2="2" stroke="rgba(0,0,0,0.5)" strokeWidth="2" />
                              </svg>
                            </span>
                          )}
                          {!isSelected && (status === 'out_of_stock' || status === 'no_combo') && (
                            <span className={`pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 px-2 py-1 bg-gray-900 text-white text-[10px] font-medium rounded whitespace-nowrap transition-opacity z-50 ${activeInfoId === av.id ? 'opacity-100' : 'opacity-0 group-hover/chip:opacity-100'}`}>
                              {attrStatusLabel(status)}
                            </span>
                          )}
                        </button>
                      )
                    }
                    return (
                      <button
                        key={av.id}
                        onClick={() => pickPreviewByAttr(group.attribute_id, av.id)}
                        className={`group/chip relative inline-flex items-center justify-center min-w-[40px] h-7 px-2.5 rounded-lg text-xs font-bold leading-none pt-px transition-colors ${
                          isSelected
                            ? 'bg-brand text-white'
                            : status === 'no_combo'
                              ? 'bg-gray-50 text-gray-300 line-through hover:bg-gray-100 hover:text-gray-400'
                              : status === 'out_of_stock'
                                ? 'bg-gray-50 text-gray-300 hover:bg-gray-100 hover:text-gray-400'
                                : 'bg-gray-100 text-gray-500 hover:bg-brand-bg hover:text-brand'
                        }`}
                      >
                        {av.value}
                        {!isSelected && (status === 'out_of_stock' || status === 'no_combo') && (
                          <span className={`pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 px-2 py-1 bg-gray-900 text-white text-[10px] font-medium rounded whitespace-nowrap transition-opacity z-50 ${activeInfoId === av.id ? 'opacity-100' : 'opacity-0 group-hover/chip:opacity-100'}`}>
                            {attrStatusLabel(status)}
                          </span>
                        )}
                      </button>
                    )
                  })}
                </div>
              </div>
            ))}
            {inStock ? (
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                <div className="sm:hidden">
                  <QtyInput value={qty} onChange={setQty} fullWidth max={previewVariant ? variantMaxQty(previewVariant) : undefined} />
                </div>
                <div className="hidden sm:block">
                  <QtyInput value={qty} onChange={setQty} size="sm" max={previewVariant ? variantMaxQty(previewVariant) : undefined} />
                </div>
                <div className="flex items-stretch gap-2 sm:contents">
                <button
                  onClick={() => {
                    if (previewVariant && price) {
                      const { finalPrice } = computeDiscount(price, previewVariant)
                      cart.addItem({ productSlug: product.slug, productName: product.name, variantId: previewVariant.id, variantName: previewVariant.name, sku: previewVariant.sku ?? null, price: finalPrice ?? price, image: previewVariant.image ?? product.main_image ?? null, shippingOrigin: previewVariant.shipping_origin, supplier: previewVariant.supplier ?? null, labels: product.labels ?? [], maxQty: variantMaxQty(previewVariant) }, qty)
                      openCart()
                    }
                  }}
                  className="flex-1 h-9 sm:h-8 flex items-center justify-center bg-brand hover:bg-brand-hover text-white rounded-xl transition-colors"
                >
                  <ShoppingCart size={16} />
                </button>
                <button
                  onClick={() => liked.toggle({
                    key: likeKey,
                    productSlug: product.slug,
                    productName: product.name,
                    variantId: previewVariant?.id ?? 0,
                    variantName: previewVariant?.name ?? '',
                    sku: previewVariant?.sku ?? null,
                    price: price ?? null,
                    image: previewVariant?.image ?? product.main_image ?? null,
                  })}
                  className={`w-9 h-9 sm:w-8 sm:h-8 shrink-0 flex items-center justify-center rounded-xl border-2 transition-colors ${
                    isLiked ? 'border-red-300 text-red-500 bg-red-50' : 'border-gray-200 text-gray-400 hover:border-red-300 hover:text-red-400'
                  }`}
                >
                  <Heart size={14} fill={isLiked ? 'currentColor' : 'none'} />
                </button>
                </div>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                {previewVariant?.id ? <StockNotifyButton variantId={previewVariant.id} /> : <span className="flex-1 h-9 sm:h-8 flex items-center justify-center text-xs font-bold text-gray-600 bg-gray-100 rounded-xl">Out of stock</span>}
                <button
                  onClick={() => liked.toggle({
                    key: likeKey,
                    productSlug: product.slug,
                    productName: product.name,
                    variantId: previewVariant?.id ?? 0,
                    variantName: previewVariant?.name ?? '',
                    sku: previewVariant?.sku ?? null,
                    price: price ?? null,
                    image: previewVariant?.image ?? product.main_image ?? null,
                  })}
                  className={`w-9 h-9 sm:w-8 sm:h-8 shrink-0 flex items-center justify-center rounded-xl border-2 transition-colors ${
                    isLiked ? 'border-red-300 text-red-500 bg-red-50' : 'border-gray-200 text-gray-400 hover:border-red-300 hover:text-red-400'
                  }`}
                >
                  <Heart size={14} fill={isLiked ? 'currentColor' : 'none'} />
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
      </div>
    </div>
  )
}

