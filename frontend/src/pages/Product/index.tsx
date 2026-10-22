import { useState, useEffect, useRef, useCallback } from 'react'
import { useTapInfo } from '../../hooks/useTapInfo'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { ShoppingCart, ShoppingBag, ChevronRight, ChevronLeft, ChevronDown, Heart, Bell } from 'lucide-react'
import QtyInput from '../../components/QtyInput'
import { getProduct, getCategoriesFlat, getBundles, getProducts, getBoughtTogether, getProductReviews, postProductReview } from '../../api/catalog'
import { getProductCourses } from '../../api/courses'
import ReviewsAndQuestions from '../../components/ReviewsAndQuestions'
import type { ProductDetail, ProductVariant, Category, BundleListItem, ProductListItem, CourseListItem } from '../../types'
import { GraduationCap, PlayCircle, Clock } from 'lucide-react'
import { BundleCard, ProductCard, attrChipStatus, attrStatusLabel, fadeColor, computeDiscount } from '../Catalog'
import { getRecentlyViewedFresh, trackProductView } from '../../utils/recentlyViewed'
import { trackViewItem } from '../../lib/analytics'
import { variantMaxQty, variantInStock } from '../../utils/stock'
import { sanitizeHtml } from '../../utils/sanitize'
import { noWidow } from '../../utils/text'
import { useCart } from '../../context/CartContext'
import { useUI } from '../../context/UIContext'
import { useLiked } from '../../context/LikedContext'
import ProductLabel, { SALE_RED } from '../../components/ProductLabel'
import ProductInfoCards from '../../components/ProductInfoCards'
import { roundPrice, formatPrice } from '../../utils/price'
import HScroller from '../../components/HScroller'
import GalleryArrow from '../../components/ui/GalleryArrow'
import ImageLightbox from '../../components/ui/ImageLightbox'
import Button from '../../components/ui/Button'
import Seo from '../../components/Seo'

// Collapse limit for the info tabs on mobile (on desktop it is computed from the gallery height).
const MOBILE_TAB_LIMIT = 420

// Collect the attributes that have only one value across the passed variants.
// Used to show "Color: White · Volume: 5L" where there is no choice.
type WithAttrs = { attribute_values: { id: number; attribute_id: number; attribute_name: string; value: string }[] }
function collectSingleValueAttrs(variants: WithAttrs[]) {
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

export default function ProductPage() {
  const { slug } = useParams<{ slug: string }>()
  const [searchParams] = useSearchParams()
  const variantParam = searchParams.get('variant')
  const [product, setProduct] = useState<ProductDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [notFound, setNotFound] = useState(false)
  const [activeVariant, setActiveVariant] = useState<ProductVariant | null>(null)
  const [activeImg, setActiveImg] = useState(0)
  const [lightbox, setLightbox] = useState(false)
  // Mobile swipe of the main photo: remember the touch start.
  const touchStart = useRef<{ x: number; y: number } | null>(null)
  const { activeInfoId } = useTapInfo()
  const thumbsRef = useRef<HTMLDivElement>(null)
  const [thumbsAtStart, setThumbsAtStart] = useState(true)
  const [thumbsAtEnd, setThumbsAtEnd] = useState(false)
  // The strip stretches to the full block width; thumbnails adjust their size
  // so that exactly cols of them split the width with no remainder — the edge is always crisp.
  const [thumbsCols, setThumbsCols] = useState(1)
  const [thumbW, setThumbW] = useState(72)
  // step of a single thumbnail (width + gap) — for arrow scrolling.
  const thumbsStepRef = useRef(80)
  const TARGET_THUMB = 72 // desired thumbnail size, cols is computed from it
  const updateThumbsEdges = useCallback(() => {
    const el = thumbsRef.current
    if (!el) return
    setThumbsAtStart(el.scrollLeft <= 1)
    setThumbsAtEnd(el.scrollLeft + el.clientWidth >= el.scrollWidth - 1)
  }, [])
  const recalcThumbsLayout = useCallback(() => {
    const el = thumbsRef.current
    if (!el || el.children.length === 0) return
    const count = el.children.length
    const gap = parseFloat(getComputedStyle(el).gap) || 8
    const sw = el.clientWidth // the strip is flex-1 → this is the whole available width
    if (sw === 0) return
    // how many thumbnails close to the desired size fit across the full width
    let cols = Math.max(1, Math.round((sw + gap) / (TARGET_THUMB + gap)))
    cols = Math.min(cols, count)
    // thumbnail width such that exactly cols of them fill the width with no remainder
    const w = Math.min((sw - (cols - 1) * gap) / cols, 96)
    setThumbsCols(cols)
    setThumbW(w)
    thumbsStepRef.current = w + gap
  }, [])
  const scrollThumbs = (dir: -1 | 1) => {
    const el = thumbsRef.current
    if (!el) return
    // Scroll by exactly a whole number of thumbnails that the visible area holds.
    el.scrollBy({ left: dir * thumbsStepRef.current * thumbsCols, behavior: 'smooth' })
  }
  const [qty, setQty] = useState(1)
  const [relatedBundles, setRelatedBundles] = useState<BundleListItem[]>([])
  const [boughtTogether, setBoughtTogether] = useState<ProductListItem[]>([])
  const [productCourses, setProductCourses] = useState<CourseListItem[]>([])
  const [brandProducts, setBrandProducts] = useState<ProductListItem[]>([])
  const [categoryProducts, setCategoryProducts] = useState<ProductListItem[]>([])
  const [recentlyViewed, setRecentlyViewed] = useState<ProductListItem[]>([])
  const [allCategories, setAllCategories] = useState<Category[]>([])
  const [activeTab, setActiveTab] = useState<'desc' | 'chars' | 'composition' | 'norms' | 'reviews' | 'bundles'>('desc')
  const [tabMenuOpen, setTabMenuOpen] = useState(false)
  const tabMenuRef = useRef<HTMLDivElement>(null)
  const [reviewCount, setReviewCount] = useState<number | null>(null)
  // Collapse the tab content (description/characteristics…) to the height of the left column on
  // desktop.
  const leftColRef = useRef<HTMLDivElement>(null)
  const tabBodyRef = useRef<HTMLDivElement>(null)
  const [tabExpanded, setTabExpanded] = useState(false)
  const [tabMaxH, setTabMaxH] = useState<number | undefined>(undefined)
  const [tabOverflow, setTabOverflow] = useState(false)
  const [isDesktop, setIsDesktop] = useState(false)
  const { addItem } = useCart()
  const { openCart, openStockNotify } = useUI()
  const liked = useLiked()

  useEffect(() => { getCategoriesFlat().then(setAllCategories).catch(() => {}) }, [])

  // Whether this is desktop (lg) — tab collapsing works only there.
  useEffect(() => {
    const mq = window.matchMedia('(min-width: 1024px)')
    const u = () => setIsDesktop(mq.matches)
    u(); mq.addEventListener('change', u)
    return () => mq.removeEventListener('change', u)
  }, [])

  // Tab content limit = distance from the top of the tab content to the bottom of the left column.
  useEffect(() => {
    const left = leftColRef.current
    if (!left) return
    const apply = () => {
      const body = tabBodyRef.current
      if (!body) return
      const h = Math.round(left.getBoundingClientRect().bottom - body.getBoundingClientRect().top)
      setTabMaxH(h > 160 ? h : undefined)
    }
    apply()
    const ro = new ResizeObserver(apply)
    ro.observe(left)
    window.addEventListener('resize', apply)
    return () => { ro.disconnect(); window.removeEventListener('resize', apply) }
  }, [product, activeTab])

  // "Expanded" is preserved between tabs; reset only when the product/variant changes.
  useEffect(() => { setTabExpanded(false) }, [product, activeVariant?.id])

  // Whether the tab content is taller than the limit (show the "Read in full"
  // button).
  // Desktop: limit = gallery height (tabMaxH); mobile: fixed MOBILE_TAB_LIMIT.
  useEffect(() => {
    const el = tabBodyRef.current
    const limit = isDesktop ? tabMaxH : MOBILE_TAB_LIMIT
    if (!el || !limit) { setTabOverflow(false); return }
    setTabOverflow(el.scrollHeight > limit + 8)
  }, [activeTab, product, activeVariant?.id, tabMaxH, isDesktop, reviewCount])

  // Close the section-picker dropdown on outside click
  useEffect(() => {
    if (!tabMenuOpen) return
    const handler = (e: MouseEvent) => {
      if (tabMenuRef.current && !tabMenuRef.current.contains(e.target as Node)) setTabMenuOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [tabMenuOpen])

  // Recompute thumbnail size + cols when the strip width changes
  // (window, arrows appearing/disappearing, etc.).
  useEffect(() => {
    const el = thumbsRef.current
    if (!el) return
    recalcThumbsLayout()
    const ro = new ResizeObserver(() => recalcThumbsLayout())
    ro.observe(el)
    return () => ro.disconnect()
  }, [recalcThumbsLayout, product])

  // After the new thumbnail size has been applied, recompute the edges.
  useEffect(() => { updateThumbsEdges() }, [updateThumbsEdges, thumbW, product])

  // Scroll the thumbnail strip so the active preview is always visible.
  useEffect(() => {
    const el = thumbsRef.current
    if (!el) return
    const thumb = el.children[activeImg] as HTMLElement | undefined
    if (!thumb) return
    const left = thumb.offsetLeft
    const right = left + thumb.offsetWidth
    if (left < el.scrollLeft) el.scrollTo({ left, behavior: 'smooth' })
    else if (right > el.scrollLeft + el.clientWidth) el.scrollTo({ left: right - el.clientWidth, behavior: 'smooth' })
  }, [activeImg, product])

  // On variant change: jump to the first packaging photo of this variant in the gallery.
  // If the variant has no photos of its own, keep the first position (the shared main image).
  useEffect(() => {
    if (!product) return
    const visible = (product.images || []).filter(img =>
      img.variant === null || (activeVariant && img.variant === activeVariant.id)
    )
    const idx = activeVariant
      ? visible.findIndex(img => img.variant === activeVariant.id)
      : 0
    setActiveImg(idx >= 0 ? idx : 0)
  }, [activeVariant?.id, product?.id])

  // On variant change, clamp the selected quantity to the new variant's limit,
  // so the field doesn't hold a number greater than the available stock.
  useEffect(() => {
    const max = activeVariant ? variantMaxQty(activeVariant) : undefined
    if (max != null) setQty(q => Math.min(q, Math.max(1, max)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeVariant?.id])

  useEffect(() => {
    if (!slug) return
    // Switching between paired packagings (factory pack ↔ own packaging) is a different product
    // (different slug),
    // but we avoid flashing a skeleton: keep the current content while the new one loads.
    const isPairedSwitch = product?.paired_product?.slug === slug
    if (!isPairedSwitch) setLoading(true)
    setActiveTab('desc')
    setActiveImg(0)
    setQty(1)
    setReviewCount(null)
    // "Recently viewed" — validate via the API so as not to show phantoms of
    // deleted products.
    getRecentlyViewedFresh(10).then(list => setRecentlyViewed(list.filter(p => p.slug !== slug))).catch(() => setRecentlyViewed([]))
    setBoughtTogether([])
    setProductCourses([])
    setBrandProducts([])
    setCategoryProducts([])
    // Requests that depend only on the slug run in parallel with getProduct (no waterfall).
    getBundles({ product_slug: slug, page_size: 10 }).then(r => setRelatedBundles(r.results)).catch(() => {})
    getBoughtTogether(slug, 10).then(setBoughtTogether).catch(() => {})
    getProductCourses(slug).then(setProductCourses).catch(() => {})
    getProduct(slug)
      .then(data => {
        setProduct(data)
        // If the query has ?variant=ID, open exactly that variant (the user picked it in the
        // catalog card).
        const fromUrl = variantParam ? data.variants.find(v => String(v.id) === variantParam) : null
        const firstActive = fromUrl || data.variants.find(v => v.is_active) || data.variants[0] || null
        setActiveVariant(firstActive)
        // Comment count for the tab comes from the serializer, without a separate heavy list
        // request.
        setReviewCount(data.reviews_count)
        // "Other products of the brand"
        if (data.brand_slug) {
          getProducts({ brand: data.brand_slug, exclude: slug, page_size: 10, ordering: 'new' })
            .then(r => setBrandProducts(r.results)).catch(() => {})
        }
        // "Other products in the category" — take the first category
        const cat = data.categories[0]
        if (cat) {
          getProducts({ category: cat.slug, exclude: slug, page_size: 10, ordering: 'new' })
            .then(r => setCategoryProducts(r.results)).catch(() => {})
        }
        // Track the current product in localStorage AFTER reading the list.
        trackProductView(data)
        trackViewItem({ id: data.slug, name: data.name, price: firstActive?.price ?? data.variants?.[0]?.price ?? 0, category: cat?.slug })
      })
      .catch(() => setNotFound(true))
      .finally(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug])

  // If ?variant= changed without reloading the product, switch the active one without a re-fetch.
  useEffect(() => {
    if (!product || !variantParam) return
    const v = product.variants.find(x => String(x.id) === variantParam)
    if (v && v.id !== activeVariant?.id) {
      setActiveVariant(v)
      setActiveImg(0)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [variantParam, product?.id])

  if (loading) return <PageSkeleton />
  if (notFound || !product) return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-12 sm:py-20 text-center">
      <h1 className="text-xl sm:text-2xl font-bold text-gray-700 mb-2">Product not found</h1>
      <Link to="/catalog" className="text-brand hover:underline font-bold inline-flex items-center gap-1"><ChevronLeft size={18} /> Back to catalog</Link>
    </div>
  )

  // Gallery: shared product photos + photos of the active variant.
  // Photos of other variants are not shown here — the admin shows them only when that variant is
  // selected.
  const allImages = (product.images || []).filter(img =>
    img.variant === null || (activeVariant && img.variant === activeVariant.id)
  )

  // Photo for the cart/favorites — a representative photo of the active variant:
  // the variant's own photo → shared → the first of any. Otherwise the cart would show
  // images[0] (the photo of the FIRST variant) regardless of the selection.
  const cartImage =
    allImages.find(img => img.variant === activeVariant?.id)?.url
    ?? allImages.find(img => img.variant === null)?.url
    ?? product.images?.[0]?.url
    ?? null

  const inStock = activeVariant ? variantInStock(activeVariant) : false
  const activeVariants = product.variants.filter(v => v.is_active)

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 pt-6 sm:pt-8 pb-6 sm:pb-8">
      <Seo
        title={product.meta_title || product.name}
        description={product.meta_description || product.description?.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 160)}
        canonicalPath={`/product/${product.slug}`}
        image={cartImage}
        ogType="product"
      />

      {/* Breadcrumb */}
      <nav className="flex items-center gap-2 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6 flex-wrap">
        <Link to="/" className="hover:text-brand">Home</Link>
        {product.categories[0] && (() => {
          const chain: Category[] = []
          let cur: Category | undefined = allCategories.find(c => c.slug === product.categories[0].slug) || product.categories[0]
          while (cur) { chain.unshift(cur); cur = allCategories.find(c => c.id === cur!.parent_id) }
          return chain.map(cat => (
            <span key={cat.slug} className="flex items-center gap-2">
              <ChevronRight size={14} />
              <Link to={`/catalog?category=${cat.slug}`} className="hover:text-brand">{cat.name}</Link>
            </span>
          ))
        })()}
        <ChevronRight size={14} />
        <span className="text-gray-600 font-medium line-clamp-1">{activeVariant?.name || product.name}</span>
      </nav>

      {/* Main block */}
      <div className="flex gap-6 sm:gap-8 lg:gap-10 flex-col lg:flex-row">

        {/* ── Gallery ── */}
        <div ref={leftColRef} className="lg:w-[600px] lg:shrink-0 lg:sticky lg:top-[calc(var(--header-h)+0.75rem)] lg:self-start">
          {/* Main image */}
          <div
            className="aspect-square mb-3 relative touch-pan-y"
            onTouchStart={e => {
              touchStart.current = { x: e.touches[0].clientX, y: e.touches[0].clientY }
            }}
            onTouchEnd={e => {
              const start = touchStart.current
              touchStart.current = null
              if (!start || allImages.length < 2) return
              const dx = e.changedTouches[0].clientX - start.x
              const dy = e.changedTouches[0].clientY - start.y
              if (Math.abs(dx) < 40 || Math.abs(dx) < Math.abs(dy)) return
              if (dx < 0) setActiveImg(i => Math.min(allImages.length - 1, i + 1))
              else setActiveImg(i => Math.max(0, i - 1))
            }}
          >
            {allImages.length > 0
              ? <img
                  src={allImages[activeImg]?.display_url || allImages[activeImg]?.url}
                  alt={activeVariant?.name || product.name}
                  decoding="async"
                  fetchPriority="high"
                  onClick={() => setLightbox(true)}
                  className="w-full h-full object-contain p-2 rounded-2xl lg:rounded-3xl cursor-zoom-in"
                />
              : <div className="w-full h-full flex items-center justify-center bg-white"><div className="w-20 h-20 rounded-full bg-brand-bg text-brand flex items-center justify-center"><ShoppingBag size={36} /></div></div>
            }
            {/* Labels overlaid on the image — labels of the SELECTED variant, not the union of
               all of them. Otherwise a "Promo" from another color hangs on a variant without a discount,
               although the price has no discount (out of sync with the -% badge). */}
            {(() => {
              const labels = (activeVariant || activeVariants[0])?.active_labels || []
              if (labels.length === 0) return null
              return (
                <div className="absolute -top-1 -left-1 flex flex-col items-start gap-1.5 pointer-events-none">
                  {labels.map(l => (
                    <ProductLabel key={l.slug} name={l.name} color={l.color} size="lg" />
                  ))}
                </div>
              )
            })()}
            {allImages.length > 1 && (
              <>
                <GalleryArrow
                  dir="left"
                  onClick={() => setActiveImg(i => Math.max(0, i - 1))}
                  disabled={activeImg === 0}
                  className="absolute left-0 top-1/2 -translate-y-1/2"
                />
                <GalleryArrow
                  dir="right"
                  onClick={() => setActiveImg(i => Math.min(allImages.length - 1, i + 1))}
                  disabled={activeImg === allImages.length - 1}
                  className="absolute right-0 top-1/2 -translate-y-1/2"
                />
              </>
            )}
          </div>
          {lightbox && (
            <ImageLightbox
              images={allImages.map(im => im.url)}
              index={activeImg}
              onClose={() => setLightbox(false)}
              onIndex={setActiveImg}
            />
          )}
          {/* Thumbnails — a strip spanning the full block width; arrows are neighbors in the flex row
             (they physically do not overlap the photo). Thumbnails share the width equally. */}
          {allImages.length > 1 && (
            <div className="flex items-center gap-1 mb-4">
              {allImages.length > thumbsCols && (
                <GalleryArrow
                  dir="left"
                  onClick={() => scrollThumbs(-1)}
                  disabled={thumbsAtStart}
                  className="shrink-0"
                  label="Previous photos"
                />
              )}
              <div
                ref={thumbsRef}
                className="flex-1 min-w-0 flex gap-2 py-1 overflow-x-auto overflow-y-hidden scrollbar-hide snap-x snap-mandatory"
                onScroll={updateThumbsEdges}
              >
                {allImages.map((img, i) => (
                  <button
                    key={img.id}
                    type="button"
                    onClick={() => setActiveImg(i)}
                    aria-label={`${product.name} — photo ${i + 1}`}
                    aria-pressed={activeImg === i}
                    style={{ width: thumbW, height: thumbW }}
                    className={`group relative shrink-0 rounded-xl overflow-hidden border transition-all snap-start ${
                      activeImg === i ? 'border-brand' : 'border-gray-200'
                    }`}
                  >
                    <img src={img.display_url || img.url} alt="" loading="lazy" decoding="async" className="w-full h-full object-contain p-1 rounded-xl" />
                    {activeImg !== i && (
                      <span className="absolute inset-0 bg-gray-900/5 opacity-0 group-hover:opacity-100 transition-opacity" />
                    )}
                  </button>
                ))}
              </div>
              {allImages.length > thumbsCols && (
                <GalleryArrow
                  dir="right"
                  onClick={() => scrollThumbs(1)}
                  disabled={thumbsAtEnd}
                  className="shrink-0"
                  label="Next photos"
                />
              )}
            </div>
          )}

          {/* Info blocks — on desktop under the gallery */}
          <div className="hidden lg:block">
            <ProductInfoCards />
          </div>

        </div>

        {/* ── Information ── */}
        <div className="flex-1">

          <h1 className="text-lg sm:text-xl lg:text-2xl font-bold text-gray-900 leading-tight mb-1">{noWidow(activeVariant?.name || product.name)}</h1>
          {!inStock && (
            <p className="text-sm font-bold text-red-500 mb-1">Out of stock</p>
          )}
          {activeVariant?.sku && (
            <p className="text-sm text-gray-400 mb-1">SKU: <span className="font-bold text-gray-600">{activeVariant.sku}</span></p>
          )}
          {product.brand_name && (
            <p className="text-sm text-gray-400 mb-1">
              Brand: <Link to={`/catalog?brand=${product.brand_slug}`} className="font-bold text-brand hover:underline">{product.brand_name}</Link>
            </p>
          )}

          {/* Attributes with a single value among the active variants — shown as a line
             (nothing to choose from, but the user should see "Color: White · Volume: 5L"). */}
          {(() => {
            const lines = collectSingleValueAttrs(activeVariants)
            if (!lines.length) return null
            return (
              <p className="text-sm text-gray-500 mb-1">
                {lines.map((l, i) => (
                  <span key={l.attribute_id}>
                    {i > 0 && <span className="text-gray-300"> · </span>}
                    <span className="text-gray-400">{l.name}: </span>
                    <span className="font-bold text-gray-600">{l.value}</span>
                  </span>
                ))}
              </p>
            )
          })()}

          {/* Packaging type (factory ↔ own) — a parameter in the same style as the variants.
             The active side = the current product, the inactive one = a link to the paired one. */}
          {product.paired_product && (
            <div className="mb-4">
              <p className="text-sm font-bold text-gray-600 mb-2">Packaging type:</p>
              <div className="flex gap-2 flex-wrap">
                {(['factory', 'own'] as const).map(kind => {
                  const isActive = product.packaging_kind === kind
                  const label = kind === 'factory' ? 'Factory' : 'Our own'
                  const cls = `inline-flex items-center justify-center min-w-[44px] h-9 px-3 rounded-lg text-sm font-bold leading-none pt-px transition-colors ${
                    isActive ? 'bg-brand text-white' : 'bg-gray-100 text-gray-500 hover:bg-brand-bg hover:text-brand'
                  }`
                  return isActive ? (
                    <span key={kind} className={cls}>{label}</span>
                  ) : (
                    <Link key={kind} to={`/product/${product.paired_product!.slug}`} className={cls}>{label}</Link>
                  )
                })}
              </div>
            </div>
          )}

          {/* Variants (pack size) — shown ONLY if the variants have no attributes.
             If there are attributes (Weight / Volume / Color etc.) — we show them below under the real attribute name.
             If only one variant is active — there is nothing to choose from, the block is not shown. */}
          {activeVariants.length > 1 &&
            !activeVariants.some(v => v.attribute_values.length > 0) && (
            <div className="mb-4">
              <p className="text-sm font-bold text-gray-600 mb-2">Pack size:</p>
              <div className="flex gap-2 flex-wrap">
                {activeVariants.map(v => {
                  const label = v.name.includes(',') ? v.name.split(',').pop()!.trim() : v.name
                  return (
                    <button
                      key={v.id}
                      onClick={() => { setActiveVariant(v); setActiveImg(0) }}
                      className={`inline-flex items-center justify-center min-w-[44px] h-9 px-3 rounded-lg text-sm font-bold leading-none pt-px transition-colors ${
                        activeVariant?.id === v.id
                          ? 'bg-brand text-white'
                          : 'bg-gray-100 text-gray-500 hover:bg-brand-bg hover:text-brand'
                      }`}
                    >
                      {label}
                    </button>
                  )
                })}
              </div>
            </div>
          )}

          {/* Attributes (color, size, etc.) — hidden if only one variant is active (nothing to choose from). */}
          {activeVariants.length > 1 && (() => {
            const groups = new Map<number, { name: string; isColor: boolean; values: { id: number; value: string; hex: string }[] }>()
            activeVariants.forEach(v => {
              v.attribute_values.forEach(av => {
                let g = groups.get(av.attribute_id)
                if (!g) { g = { name: av.attribute_name, isColor: av.is_color, values: [] }; groups.set(av.attribute_id, g) }
                if (!g.values.find(x => x.id === av.id)) g.values.push({ id: av.id, value: av.value, hex: av.color_hex })
              })
            })
            if (!groups.size) return null
            const selectedValues: Record<number, number> = {}
            for (const av of activeVariant?.attribute_values || []) selectedValues[av.attribute_id] = av.id
            return [...groups.entries()].map(([attrId, g]) => {
              const activeId = activeVariant?.attribute_values.find(av => av.attribute_id === attrId)?.id
              const pickValue = (vid: number) => {
                // Exact combination while preserving the current selection — otherwise pull in
                // any variant with the selected value (the remaining attributes update to
                // compatible ones), preferring those in stock.
                const next = { ...selectedValues, [attrId]: vid }
                const exact = activeVariants.find(v =>
                  Object.entries(next).every(([aid, vv]) =>
                    v.attribute_values.some(av => av.attribute_id === Number(aid) && av.id === vv)
                  )
                )
                const candidates = activeVariants.filter(v => v.attribute_values.some(av => av.id === vid))
                const target = exact || candidates.find(v => (v.stock ?? 0) > 0) || candidates[0]
                if (target) { setActiveVariant(target); setActiveImg(0) }
              }
              return (
                <div key={attrId} className="mb-4">
                  <p className="text-sm font-bold text-gray-600 mb-2">{g.name}:</p>
                  <div className="flex gap-2 flex-wrap">
                    {g.values.map(val => {
                      const isOn = val.id === activeId
                      const status = attrChipStatus(activeVariants as any, selectedValues, attrId, val.id)
                      if (g.isColor && val.hex) {
                        const isLight = (() => {
                          const m = /^#?([\da-f]{6})$/i.exec(val.hex || '')
                          if (!m) return false
                          const v = parseInt(m[1], 16)
                          const r = (v >> 16) & 0xff, gg = (v >> 8) & 0xff, b = v & 0xff
                          return (0.299 * r + 0.587 * gg + 0.114 * b) / 255 > 0.6
                        })()
                        return (
                          <button
                            key={val.id}
                            type="button"
                            onClick={() => pickValue(val.id)}
                            aria-label={val.value}
                            className={`group/chip relative w-9 h-9 rounded-lg transition-all ${!isOn && status !== 'no_combo' && status !== 'out_of_stock' ? 'hover:scale-110' : ''}`}
                            style={{ background: !isOn && (status === 'no_combo' || status === 'out_of_stock') ? fadeColor(val.hex) : val.hex }}
                          >
                            {isOn && (
                              <span className="absolute inset-0 flex items-center justify-center pointer-events-none">
                                <svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke={isLight ? '#111827' : '#fff'} strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                                  <path d="M3 8.5l3.2 3.2L13 5" />
                                </svg>
                              </span>
                            )}
                            {!isOn && status === 'no_combo' && (
                              <span className="absolute inset-0 pointer-events-none">
                                <svg viewBox="0 0 36 36" className="w-full h-full" preserveAspectRatio="none">
                                  <line x1="3" y1="33" x2="33" y2="3" stroke="rgba(0,0,0,0.5)" strokeWidth="2.5" />
                                </svg>
                              </span>
                            )}
                            {!isOn && (status === 'out_of_stock' || status === 'no_combo') && (
                              <span className={`pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-2 py-1 bg-gray-900 text-white text-xs font-medium rounded whitespace-nowrap transition-opacity z-20 ${activeInfoId === val.id ? 'opacity-100' : 'opacity-0 group-hover/chip:opacity-100'}`}>
                                {attrStatusLabel(status)}
                              </span>
                            )}
                          </button>
                        )
                      }
                      return (
                        <button
                          key={val.id}
                          type="button"
                          onClick={() => pickValue(val.id)}
                          className={`group/chip relative inline-flex items-center justify-center min-w-[44px] h-9 px-3 rounded-lg text-sm font-bold leading-none pt-px transition-colors ${
                            isOn
                              ? 'bg-brand text-white'
                              : status === 'no_combo'
                                ? 'bg-gray-50 text-gray-300 line-through hover:bg-gray-100 hover:text-gray-400'
                                : status === 'out_of_stock'
                                  ? 'bg-gray-50 text-gray-300 hover:bg-gray-100 hover:text-gray-400'
                                  : 'bg-gray-100 text-gray-500 hover:bg-brand-bg hover:text-brand'
                          }`}
                        >
                          {val.value}
                          {!isOn && (status === 'out_of_stock' || status === 'no_combo') && (
                            <span className={`pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-2 px-2 py-1 bg-gray-900 text-white text-xs font-medium rounded whitespace-nowrap transition-opacity z-20 ${activeInfoId === val.id ? 'opacity-100' : 'opacity-0 group-hover/chip:opacity-100'}`}>
                              {attrStatusLabel(status)}
                            </span>
                          )}
                        </button>
                      )
                    })}
                  </div>
                </div>
              )
            })
          })()}

          {/* Price */}
          {(() => {
            const fmt = formatPrice
            const basePrice = activeVariant ? parseFloat(String(activeVariant.price)) : null
            const discountPct = activeVariant?.effective_discount
              ? Number(activeVariant.effective_discount)
              : (activeVariant?.discount_percent ? Number(activeVariant.discount_percent) : 0)
            const explicitOld = activeVariant?.old_price ? parseFloat(String(activeVariant.old_price)) : null
            let finalPrice = basePrice
            let oldPrice: number | null = null
            let pctShown = 0
            if (basePrice != null && explicitOld != null && explicitOld > basePrice) {
              oldPrice = explicitOld
              finalPrice = basePrice
              pctShown = discountPct > 0 ? discountPct : Math.round(((explicitOld - basePrice) / explicitOld) * 100)
            } else if (basePrice != null && discountPct > 0 && discountPct < 100) {
              oldPrice = basePrice
              finalPrice = basePrice * (1 - discountPct / 100)
              pctShown = discountPct
            }
            // Fixed discount in UAH — on top of the % (agreed with the backend).
            const discAmount = activeVariant?.discount_amount != null ? Number(activeVariant.discount_amount) : 0
            if (finalPrice != null && discAmount > 0) {
              const reduced = finalPrice - discAmount
              if (reduced > 0) {
                if (oldPrice == null) oldPrice = basePrice
                finalPrice = reduced
                pctShown = oldPrice != null && oldPrice > 0
                  ? Math.round(((oldPrice - reduced) / oldPrice) * 100)
                  : pctShown
              }
            }
            const hasDiscount = oldPrice != null && finalPrice != null
            return (
              <div className="mt-6 mb-4">
                {hasDiscount && (
                  <div className="flex items-center gap-2 mb-0.5">
                    <span className="text-base text-gray-400 line-through">
                      {fmt(oldPrice!)} UAH
                    </span>
                    {pctShown > 0 && (
                      <ProductLabel name={`-${Math.round(pctShown)}%`} color="#ef4444" size="md" showIcon={false} />
                    )}
                  </div>
                )}
                <div className="flex items-center gap-3 flex-wrap">
                  <span
                    className={`text-3xl sm:text-4xl font-bold ${hasDiscount ? '' : 'text-gray-900'}`}
                    style={hasDiscount ? { color: SALE_RED } : undefined}
                  >
                    {finalPrice != null ? `${fmt(finalPrice)} UAH` : 'Price on request'}
                  </span>
                </div>
              </div>
            )
          })()}

          {/* Quantity + button */}
          <div className="flex items-center gap-3 mb-6">
            {inStock ? (
              <>
                <QtyInput value={qty} onChange={setQty} max={activeVariant ? variantMaxQty(activeVariant) : undefined} />
                <Button
                  size="sm"
                  className="flex-1 min-w-0"
                  iconLeft={<ShoppingCart size={16} />}
                  onClick={() => {
                    if (activeVariant) {
                      // Apply the discount (if any): variant.price is the base price,
                      // the sale price goes to the cart.
                      const basePrice = parseFloat(String(activeVariant.price))
                      const pct = activeVariant.effective_discount != null
                        ? Number(activeVariant.effective_discount)
                        : (activeVariant.discount_percent != null ? Number(activeVariant.discount_percent) : 0)
                      const explicitOld = activeVariant.old_price ? parseFloat(String(activeVariant.old_price)) : null
                      let finalPrice = basePrice
                      if (explicitOld != null && explicitOld > basePrice) {
                        finalPrice = basePrice
                      } else if (pct > 0 && pct < 100) {
                        finalPrice = basePrice * (1 - pct / 100)
                      }
                      const discAmount = activeVariant.discount_amount != null ? Number(activeVariant.discount_amount) : 0
                      if (discAmount > 0 && finalPrice - discAmount > 0) {
                        finalPrice = finalPrice - discAmount
                      }
                      // Union of all active variant labels — consistent with what the card shows.
                      const seenSlug = new Set<string>()
                      const cartLabels: { name: string; color: string; slug: string }[] = []
                      for (const v of (product.variants || [])) {
                        for (const l of (v.active_labels || [])) {
                          if (seenSlug.has(l.slug)) continue
                          seenSlug.add(l.slug)
                          cartLabels.push(l)
                        }
                      }
                      addItem({ productSlug: product.slug, productName: product.name, variantId: activeVariant.id, variantName: activeVariant.name, sku: activeVariant.sku ?? null, price: roundPrice(finalPrice), image: cartImage, shippingOrigin: activeVariant.shipping_origin, supplier: activeVariant.supplier ?? null, labels: cartLabels, maxQty: variantMaxQty(activeVariant) }, qty)
                      openCart()
                    }
                  }}
                >
                  <span className="hidden sm:inline">Add to cart</span>
                </Button>
              </>
            ) : (
              // Shared subscription modal — the same one as in the bundle and the catalog.
              <button
                type="button"
                disabled={!activeVariant}
                onClick={() => { if (activeVariant) openStockNotify(activeVariant.id, 'variant') }}
                className="flex-1 flex items-center justify-center gap-2 bg-gray-100 text-gray-600 hover:bg-gray-200 hover:text-text-primary disabled:opacity-50 font-bold py-2.5 rounded-xl transition-colors text-sm"
              >
                <Bell size={15} /> Notify me when available
              </button>
            )}
            {activeVariant && (() => {
              const likeKey = `${product.slug}_${activeVariant.id}`
              const isLiked = liked.isLiked(likeKey)
              return (
                <button
                  onClick={() => liked.toggle({ key: likeKey, productSlug: product.slug, productName: product.name, variantId: activeVariant.id, variantName: activeVariant.name, sku: activeVariant.sku ?? null, price: computeDiscount(activeVariant.price, activeVariant).finalPrice ?? roundPrice(activeVariant.price), image: cartImage })}
                  className={`w-9 h-9 shrink-0 flex items-center justify-center rounded-xl border-2 transition-colors ${isLiked ? 'border-red-300 bg-red-50 text-red-500' : 'border-gray-200 text-gray-400 hover:border-red-300 hover:text-red-400'}`}
                >
                  <Heart size={16} fill={isLiked ? 'currentColor' : 'none'} />
                </button>
              )
            })()}
          </div>

          {/* Tabs — in the right column under the button */}
          <div className="mt-8">
            {(() => {
              // Count the unique rows of the characteristics table (with the same deduplication as
              // in CharacteristicsTable).
              const seenChars = new Set<string>()
              const addChar = (n?: string, v?: string) => {
                if (!n || !v) return
                const k = n.trim().toLowerCase()
                if (seenChars.has(k)) return
                seenChars.add(k)
              }
              ;(activeVariant?.attribute_values || []).forEach(a => addChar(a.attribute_name, a.value))
              ;(activeVariant?.characteristics || []).forEach(c => addChar(c.name, c.value))
              ;(product.characteristics || []).forEach(c => addChar(c.name, c.value))
              const charsCount = seenChars.size
              const tabs = ([
                { key: 'desc', label: 'Description', show: true },
                { key: 'chars', label: 'Specifications', show: charsCount > 1 },
                { key: 'composition', label: 'Composition', show: (product.compositions?.length || 0) > 0 },
                { key: 'norms', label: 'Application rates', show: !!product.application_norms },
                { key: 'bundles', label: relatedBundles.length ? `Bundles (${relatedBundles.length})` : 'Bundles', show: relatedBundles.length > 0 },
                { key: 'reviews', label: reviewCount ? `Comments (${reviewCount})` : 'Comments', show: true },
              ] as const).filter(t => t.show)
              const current = tabs.find(t => t.key === activeTab) ?? tabs[0]
              return (
                <>
                {/* Desktop — underline tabs */}
                <div className="hidden lg:flex gap-0.5 border-b border-gray-100 mb-5">
                  {tabs.map(({ key, label }) => (
                    <button
                      key={key}
                      onClick={() => setActiveTab(key)}
                      className={`px-3 py-2.5 font-bold text-sm whitespace-nowrap border-b-2 -mb-px transition-colors ${
                        activeTab === key ? 'border-brand text-brand' : 'border-transparent text-gray-400 hover:text-gray-700'
                      }`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
                {/* Mobile — dropdown */}
                <div ref={tabMenuRef} className="relative mb-5 lg:hidden">
                  <button
                    type="button"
                    onClick={() => setTabMenuOpen(v => !v)}
                    className="w-full flex items-center justify-between gap-2 bg-gray-50 border border-gray-200 text-gray-800 text-sm font-bold rounded-xl px-4 py-2.5 transition-colors hover:border-brand"
                  >
                    <span>{current.label}</span>
                    <ChevronDown size={18} className={`text-gray-400 transition-transform ${tabMenuOpen ? 'rotate-180' : ''}`} />
                  </button>
                  {tabMenuOpen && (
                    <div className="absolute top-full left-0 right-0 mt-1 bg-white rounded-xl border border-gray-100 shadow-card-high z-20 overflow-hidden">
                      {tabs.map(({ key, label }) => (
                        <button
                          key={key}
                          type="button"
                          onClick={() => { setActiveTab(key); setTabMenuOpen(false) }}
                          className={`w-full text-left px-4 py-2.5 text-sm font-bold transition-colors ${
                            activeTab === key ? 'bg-brand-bg text-brand' : 'text-gray-600 hover:bg-gray-50'
                          }`}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                </>
              )
            })()}
            {(() => {
              // Collapse only the informational tabs; reviews/bundles are always shown in full.
              const collapsible = ['desc', 'chars', 'composition', 'norms'].includes(activeTab)
              const limit = isDesktop ? tabMaxH : MOBILE_TAB_LIMIT
              // Truncate only when there is something to expand (a button will exist). Otherwise
              // content
              // that overflows by a couple of lines (<= the tabOverflow threshold) is shown in
              // full.
              const limited = collapsible && !tabExpanded && !!limit && tabOverflow
              return (
                <div className="relative">
                  <div
                    ref={tabBodyRef}
                    className={limited ? 'overflow-hidden' : ''}
                    style={limited ? { maxHeight: limit } : undefined}
                  >
                    {activeTab === 'desc' && (
                      <div className="prose max-w-none text-gray-700 text-sm leading-relaxed"
                        dangerouslySetInnerHTML={{ __html: sanitizeHtml(activeVariant?.description || product.description) || '<p class="text-gray-400">No description</p>' }} />
                    )}
                    {activeTab === 'chars' && (
                      <CharacteristicsTable
                        productChars={product.characteristics}
                        variantChars={activeVariant?.characteristics || []}
                        variantAttrs={(activeVariant?.attribute_values as any) || []}
                      />
                    )}
                    {activeTab === 'composition' && (
                      <CompositionTable rows={product.compositions || []} />
                    )}
                    {activeTab === 'norms' && (
                      <div className="prose max-w-none text-gray-700 text-sm leading-relaxed"
                        dangerouslySetInnerHTML={{ __html: sanitizeHtml(product.application_norms) || '<p class="text-gray-400">No information available</p>' }} />
                    )}
                    {activeTab === 'reviews' && slug && (
                      <ReviewsAndQuestions
                        slug={slug}
                        getReviews={getProductReviews}
                        postReview={postProductReview}
                        onCountLoad={setReviewCount}
                        variantId={activeVariant?.id}
                        variantName={activeVariant?.name}
                      />
                    )}
                    {activeTab === 'bundles' && (
                      <div className="grid grid-cols-2 lg:grid-cols-3 gap-3 sm:gap-4">
                        {relatedBundles.map(b => <BundleCard key={b.id} bundle={b} />)}
                      </div>
                    )}
                  </div>
                  {limited && (
                    <>
                      <div className="absolute inset-x-0 bottom-0 h-24 bg-gradient-to-t from-white to-transparent pointer-events-none" />
                      <div className="absolute inset-x-0 bottom-0 flex justify-center pb-1">
                        <button
                          type="button"
                          onClick={() => setTabExpanded(true)}
                          className="flex items-center gap-1.5 px-4 h-9 rounded-xl bg-white border border-gray-200 text-gray-700 text-sm font-bold hover:border-brand hover:text-brand transition-colors shadow-card-low"
                        >
                          Read in full <ChevronDown size={16} />
                        </button>
                      </div>
                    </>
                  )}
                  {collapsible && tabExpanded && tabOverflow && (
                    <div className="flex justify-center mt-4">
                      <button
                        type="button"
                        onClick={() => setTabExpanded(false)}
                        className="flex items-center gap-1.5 px-4 h-9 rounded-xl border border-gray-200 text-gray-600 text-sm font-bold hover:border-brand hover:text-brand transition-colors"
                      >
                        Collapse <ChevronDown size={16} className="rotate-180" />
                      </button>
                    </div>
                  )}
                </div>
              )
            })()}
          </div>

        </div>
      </div>

      {/* Info blocks on mobile — after the product content, before the recommendations */}
      <div className="lg:hidden mt-6">
        <ProductInfoCards />
      </div>

      {/* We show the recommendation blocks only when there are enough products for a full
         row (desktop = 5), otherwise the row is half-empty and looks stubby.
         Exception — "You recently viewed", it is always shown. */}

      {/* Bought together with this product */}
      {boughtTogether.length >= 5 && (
        <div className="mt-12">
          <h2 className="text-xl font-bold text-gray-800 mb-5">Frequently bought together</h2>
          <HScroller extraBottom={340} cols={{ base: 2, sm: 3, md: 4, lg: 5 }}>
            {boughtTogether.map(p => <div key={p.id}><ProductCard product={p} scope="bought" /></div>)}
          </HScroller>
        </div>
      )}

      {/* Courses on this product's topic — a lg:3 grid, shown from a full row */}
      {productCourses.length >= 3 && (
        <div className="mt-12">
          <div className="flex items-baseline justify-between gap-3 mb-5">
            <h2 className="text-xl font-bold text-gray-800 flex items-center gap-2">
              <GraduationCap size={22} className="text-brand" /> Courses on this topic
            </h2>
            <Link to="/courses" className="text-sm sm:text-base text-brand font-bold hover:underline flex items-center gap-1 whitespace-nowrap shrink-0">
              All courses <ChevronRight size={18} />
            </Link>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 sm:gap-5">
            {productCourses.map(c => (
              <Link
                key={c.id}
                to={`/course/${c.slug}`}
                className="group flex bg-surface border border-border rounded-2xl shadow-card-low overflow-hidden hover:shadow-card-hover"
              >
                <div className="w-32 h-24 sm:w-36 sm:h-28 shrink-0 bg-gradient-to-br from-brand-bg to-[#d7ebd0] overflow-hidden">
                  {c.cover_url ? (
                    <img src={c.cover_url} alt={c.name} loading="lazy" className="w-full h-full object-cover" />
                  ) : (
                    <div className="w-full h-full flex items-center justify-center">
                      <GraduationCap size={32} className="text-brand/30" />
                    </div>
                  )}
                </div>
                <div className="flex-1 min-w-0 p-3 flex flex-col justify-between">
                  <h3 className="text-sm font-bold text-text-primary line-clamp-2">{c.name}</h3>
                  <div className="flex items-center justify-between gap-2 mt-1">
                    <div className="flex items-center gap-2 text-xs text-text-muted">
                      <span className="flex items-center gap-1"><PlayCircle size={11} /> {c.total_lessons}</span>
                      {c.total_duration_minutes > 0 && (
                        <span className="flex items-center gap-1"><Clock size={11} /> {c.total_duration_minutes} min</span>
                      )}
                    </div>
                    <span className="text-sm font-bold text-text-primary">{formatPrice(c.price)} UAH</span>
                  </div>
                </div>
              </Link>
            ))}
          </div>
        </div>
      )}

      {/* Other products of the brand */}
      {brandProducts.length >= 5 && product?.brand_slug && (
        <div className="mt-12">
          <div className="flex items-baseline justify-between gap-3 mb-5">
            <h2 className="text-xl font-bold text-gray-800">Other products of the brand “{product.brand_name}”</h2>
            <Link to={`/catalog?brand=${product.brand_slug}`} className="text-sm sm:text-base text-brand font-bold hover:underline flex items-center gap-1 whitespace-nowrap shrink-0">
              All products <ChevronRight size={18} />
            </Link>
          </div>
          <HScroller extraBottom={340} cols={{ base: 2, sm: 3, md: 4, lg: 5 }}>
            {brandProducts.map(p => <div key={p.id}><ProductCard product={p} scope="brand" /></div>)}
          </HScroller>
        </div>
      )}

      {/* Other products in the category */}
      {categoryProducts.length >= 5 && product?.categories[0] && (
        <div className="mt-12">
          <div className="flex items-baseline justify-between gap-3 mb-5">
            <h2 className="text-xl font-bold text-gray-800">Other products in the category “{product.categories[0].name}”</h2>
            <Link to={`/catalog?category=${product.categories[0].slug}`} className="text-sm sm:text-base text-brand font-bold hover:underline flex items-center gap-1 whitespace-nowrap shrink-0">
              All products <ChevronRight size={18} />
            </Link>
          </div>
          <HScroller extraBottom={340} cols={{ base: 2, sm: 3, md: 4, lg: 5 }}>
            {categoryProducts.map(p => <div key={p.id}><ProductCard product={p} scope="category" /></div>)}
          </HScroller>
        </div>
      )}

      {/* You recently viewed */}
      {recentlyViewed.length > 0 && (
        <div className="mt-12">
          <h2 className="text-xl font-bold text-gray-800 mb-5">You recently viewed</h2>
          <HScroller extraBottom={340} cols={{ base: 2, sm: 3, md: 4, lg: 5 }}>
            {recentlyViewed.map(p => <div key={p.id}><ProductCard product={p} scope="recent" /></div>)}
          </HScroller>
        </div>
      )}

    </div>
  )
}

type AttrValue = { attribute_name: string; value: string; color_hex?: string; is_color?: boolean }
type Char = { name: string; value: string; color_hex?: string }

function CharacteristicsTable({ productChars, variantChars, variantAttrs }: {
  productChars: Char[]
  variantChars: Char[]
  variantAttrs: AttrValue[]
}) {
  // Single source of truth: variant attribute_values come first as the spec
  // table for the chosen variant; then variant-level characteristics (anything
  // not already covered by an attribute); then product-level characteristics
  // (general info common to all variants — material, application norms etc).
  // Dedup is by name (case-insensitive, trimmed).
  type Row = { name: string; value: string; colorHex?: string }
  const seen = new Set<string>()
  const rows: Row[] = []
  const addRow = (r: Row) => {
    const key = r.name.trim().toLowerCase()
    if (seen.has(key) || !r.value) return
    seen.add(key)
    rows.push(r)
  }
  for (const a of variantAttrs) {
    addRow({ name: a.attribute_name, value: a.value, colorHex: a.is_color ? a.color_hex : undefined })
  }
  for (const c of variantChars) addRow({ name: c.name, value: c.value, colorHex: c.color_hex || undefined })
  for (const c of productChars) addRow({ name: c.name, value: c.value, colorHex: c.color_hex || undefined })

  if (!rows.length) return <p className="text-gray-400">No specifications</p>
  // If there is only one characteristic, don't show the table at all (it duplicates the attribute
  // above).
  if (rows.length === 1) return null
  return (
    <div className="rounded-2xl border border-border bg-surface shadow-card-low overflow-hidden">
      <div className="grid grid-cols-[minmax(110px,38%)_1fr] bg-gray-50 border-b border-border text-xs sm:text-sm font-bold text-text-secondary">
        <div className="px-4 sm:px-6 py-2.5 border-r border-border">Name</div>
        <div className="px-4 sm:px-6 py-2.5">Value</div>
      </div>
      <dl>
        {rows.map((r, i) => (
          <div
            key={i}
            className="grid grid-cols-[minmax(110px,38%)_1fr] border-t border-border first:border-t-0 even:bg-gray-50/60"
          >
            <dt className="px-4 sm:px-6 py-2.5 sm:py-3 text-sm text-text-muted border-r border-border flex items-start break-words min-w-0">
              {r.name}
            </dt>
            <dd className="px-4 sm:px-6 py-2.5 sm:py-3 text-sm font-bold text-text-primary flex items-start gap-2 min-w-0">
              {r.colorHex && (
                <span className="w-4 h-4 rounded-full border border-gray-200 shrink-0 mt-0.5" style={{ background: r.colorHex }} />
              )}
              <span className="min-w-0 break-words">{r.value}</span>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}

function CompositionTable({ rows }: { rows: { name: string; value: string }[] }) {
  const filtered = rows.filter(r => r.name)
  if (!filtered.length) return <p className="text-gray-400">No composition</p>
  // If a row has no value, show only the name across the full width (a component without a share).
  const hasValues = filtered.some(r => r.value)
  return (
    <div className="rounded-2xl border border-border bg-surface shadow-card-low overflow-hidden">
      {hasValues && (
        <div className="grid grid-cols-[1fr_minmax(90px,32%)] bg-gray-50 border-b border-border text-xs sm:text-sm font-bold text-text-secondary">
          <div className="px-4 sm:px-6 py-2.5 border-r border-border">Component</div>
          <div className="px-4 sm:px-6 py-2.5">Content</div>
        </div>
      )}
      <dl>
        {filtered.map((r, i) => (
          <div
            key={i}
            className={`border-t border-border first:border-t-0 even:bg-gray-50/60 ${hasValues ? 'grid grid-cols-[1fr_minmax(90px,32%)]' : ''}`}
          >
            <dt className={`px-4 sm:px-6 py-2.5 sm:py-3 text-sm text-text-primary font-medium break-words min-w-0 ${hasValues ? 'border-r border-border' : ''}`}>
              {r.name}
            </dt>
            {hasValues && (
              <dd className="px-4 sm:px-6 py-2.5 sm:py-3 text-sm font-bold text-text-primary break-words min-w-0">
                {r.value}
              </dd>
            )}
          </div>
        ))}
      </dl>
    </div>
  )
}

function PageSkeleton() {
  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8 animate-pulse">
      <div className="h-4 bg-gray-100 rounded w-64 mb-7" />
      <div className="flex gap-6 sm:gap-10 flex-col lg:flex-row">
        <div className="lg:w-[480px] lg:shrink-0 aspect-square bg-gray-100 rounded-3xl" />
        <div className="flex-1 space-y-4">
          <div className="h-5 bg-gray-100 rounded w-32" />
          <div className="h-9 bg-gray-100 rounded w-3/4" />
          <div className="h-9 bg-gray-100 rounded w-1/2" />
          <div className="h-12 bg-gray-100 rounded w-1/3" />
          <div className="h-12 bg-gray-100 rounded" />
        </div>
      </div>
    </div>
  )
}
