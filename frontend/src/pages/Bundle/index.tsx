import { useState, useEffect, useRef } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ChevronRight, ChevronLeft, ChevronDown, ShoppingCart, Package, Heart, Bell } from 'lucide-react'
import QtyInput from '../../components/QtyInput'
import ProductInfoCards from '../../components/ProductInfoCards'
import ProductLabel, { SALE_RED } from '../../components/ProductLabel'
import { roundPrice, formatPrice } from '../../utils/price'
import { getBundle, getBundleReviews, postBundleReview, getBundles } from '../../api/catalog'
import ReviewsAndQuestions from '../../components/ReviewsAndQuestions'
import type { BundleDetail, BundleListItem, ProductListItem } from '../../types'
import { useCart } from '../../context/CartContext'
import { useUI } from '../../context/UIContext'
import { useLiked } from '../../context/LikedContext'
import { sanitizeHtml } from '../../utils/sanitize'
import { BundleCard, ProductCard } from '../Catalog'
import Seo from '../../components/Seo'
import { getRecentlyViewedFresh } from '../../utils/recentlyViewed'
import HScroller from '../../components/HScroller'
import GalleryArrow from '../../components/ui/GalleryArrow'
import ImageLightbox from '../../components/ui/ImageLightbox'
import Button from '../../components/ui/Button'

export default function BundlePage() {
  const { slug } = useParams<{ slug: string }>()
  const [bundle, setBundle] = useState<BundleDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [notFound, setNotFound] = useState(false)
  const [activeImg, setActiveImg] = useState(0)
  const [lightbox, setLightbox] = useState(false)
  // Mobile swipe of the main photo: remember the touch start.
  const touchStart = useRef<{ x: number; y: number } | null>(null)
  const [qty, setQty] = useState(1)
  const [activeTab, setActiveTab] = useState<'desc' | 'norms' | 'composition' | 'reviews'>('desc')
  const [tabMenuOpen, setTabMenuOpen] = useState(false)
  const tabMenuRef = useRef<HTMLDivElement>(null)
  const [reviewCount, setReviewCount] = useState<number | null>(null)
  const leftColRef = useRef<HTMLDivElement>(null)
  const tabBodyRef = useRef<HTMLDivElement>(null)
  const [tabExpanded, setTabExpanded] = useState(false)
  const [tabOverflow, setTabOverflow] = useState(false)
  const [tabMaxH, setTabMaxH] = useState<number | undefined>(undefined)
  const [isDesktop, setIsDesktop] = useState(false)
  const [otherBundles, setOtherBundles] = useState<BundleListItem[]>([])
  const [recentlyViewed, setRecentlyViewed] = useState<ProductListItem[]>([])
  const { addItem } = useCart()
  const { openCart, openStockNotify } = useUI()
  const liked = useLiked()

  // New bundle - reset the image/qty.
  useEffect(() => { setActiveImg(0) }, [bundle?.id])
  useEffect(() => {
    const max = bundle?.stock
    if (max != null) setQty(q => Math.min(q, Math.max(1, max)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bundle?.id])

  // "Read in full" — desktop only (lg).
  useEffect(() => {
    const mq = window.matchMedia('(min-width: 1024px)')
    const u = () => setIsDesktop(mq.matches)
    u(); mq.addEventListener('change', u)
    return () => mq.removeEventListener('change', u)
  }, [])

  // The tabs' height limit = the distance from the top of the tab content to the bottom of the left
  // column
  // (gallery + info blocks), so the description cuts off exactly at its level.
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
  }, [bundle, activeTab])

  // Reset the expansion when the tab changes; check for overflow.
  useEffect(() => { setTabExpanded(false) }, [bundle?.id, activeTab])
  useEffect(() => {
    const el = tabBodyRef.current
    if (!el || !tabMaxH) { setTabOverflow(false); return }
    setTabOverflow(el.scrollHeight > tabMaxH + 8)
  }, [activeTab, bundle, tabMaxH])

  // Close the section-picker dropdown on outside click
  useEffect(() => {
    if (!tabMenuOpen) return
    const handler = (e: MouseEvent) => {
      if (tabMenuRef.current && !tabMenuRef.current.contains(e.target as Node)) setTabMenuOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [tabMenuOpen])

  useEffect(() => {
    if (!slug) return
    setLoading(true)
    setReviewCount(null)
    setOtherBundles([])
    // Recently viewed products — validate via the API so that phantoms of deleted ones do not
    // linger.
    getRecentlyViewedFresh(10).then(setRecentlyViewed).catch(() => setRecentlyViewed([]))
    getBundle(slug)
      .then(data => {
        setBundle(data)
        getBundleReviews(slug).then(r => setReviewCount(r.length)).catch(() => {})
        // Other bundles — excluding the current one.
        getBundles({ page_size: 20 })
          .then(r => setOtherBundles(r.results.filter((b: BundleListItem) => b.slug !== slug).slice(0, 15)))
          .catch(() => {})
      })
      .catch(() => setNotFound(true))
      .finally(() => setLoading(false))
  }, [slug])

  if (loading) return <PageSkeleton />
  if (notFound || !bundle) return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-12 sm:py-20 text-center">
      <div className="w-20 h-20 rounded-full bg-brand-bg text-brand flex items-center justify-center mx-auto mb-4"><Package size={36} /></div>
      <h1 className="text-xl sm:text-2xl font-bold text-gray-700 mb-2">Bundle not found</h1>
      <Link to="/catalog?category=komplekti" className="text-brand hover:underline font-bold inline-flex items-center gap-1"><ChevronLeft size={18} /> Back to bundles</Link>
    </div>
  )

  const inStock = (bundle.stock ?? 0) > 0
  const likeKey = `bundle_${bundle.slug}_${bundle.id}`
  const isLiked = liked.isLiked(likeKey)
  const galleryImages = bundle.images.map(img => ({ id: `b-${img.id}`, url: img.url }))

  // The photo for the cart/favorites is the bundle photo. Aligned with the catalog card
  // (main_image) and with the reconcile in the cart, otherwise the cart photo wouldn't match the
  // card.
  const cartImage = bundle.images?.[0]?.url ?? null

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 pt-6 sm:pt-8 pb-6 sm:pb-8">
      <Seo
        title={bundle.meta_title || bundle.name}
        description={bundle.meta_description || bundle.description?.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 160)}
        canonicalPath={`/bundle/${bundle.slug}`}
        image={cartImage}
        ogType="product"
      />

      {/* Breadcrumb */}
      <nav className="flex items-center gap-2 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6 flex-wrap">
        <Link to="/" className="hover:text-brand">Home</Link>
        <ChevronRight size={14} />
        <Link to="/catalog?category=komplekti" className="hover:text-brand">Bundles</Link>
        <ChevronRight size={14} />
        <span className="text-gray-600 font-medium line-clamp-1">{bundle.name}</span>
      </nav>

      <div className="flex gap-6 sm:gap-8 lg:gap-10 flex-col lg:flex-row">

        {/* Gallery */}
        <div ref={leftColRef} className="lg:w-[600px] lg:shrink-0 lg:sticky lg:top-[calc(var(--header-h)+0.75rem)] lg:self-start">
          <div
            className="aspect-square mb-3 relative touch-pan-y"
            onTouchStart={e => {
              touchStart.current = { x: e.touches[0].clientX, y: e.touches[0].clientY }
            }}
            onTouchEnd={e => {
              const start = touchStart.current
              touchStart.current = null
              if (!start || galleryImages.length < 2) return
              const dx = e.changedTouches[0].clientX - start.x
              const dy = e.changedTouches[0].clientY - start.y
              if (Math.abs(dx) < 40 || Math.abs(dx) < Math.abs(dy)) return
              if (dx < 0) setActiveImg(i => Math.min(galleryImages.length - 1, i + 1))
              else setActiveImg(i => Math.max(0, i - 1))
            }}
          >
            {galleryImages.length > 0 ? (
              <img src={galleryImages[activeImg]?.url} alt={bundle.name} onClick={() => setLightbox(true)} className="w-full h-full object-contain p-2 rounded-2xl lg:rounded-3xl cursor-zoom-in" />
            ) : (
              <div className="w-full h-full flex items-center justify-center bg-white"><div className="w-20 h-20 rounded-full bg-brand-bg text-brand flex items-center justify-center"><Package size={36} /></div></div>
            )}
            <div className="absolute -top-1 -left-1 pointer-events-none flex flex-col items-start gap-1.5">
              <ProductLabel name="Bundle" color="#0ea5e9" size="lg" />
              {(bundle.active_labels ?? []).map(l => (
                <ProductLabel key={l.slug} name={l.name} color={l.color} size="lg" />
              ))}
            </div>
            {galleryImages.length > 1 && (
              <>
                <GalleryArrow
                  dir="left"
                  onClick={() => setActiveImg(i => Math.max(0, i - 1))}
                  disabled={activeImg === 0}
                  className="absolute left-2 top-1/2 -translate-y-1/2 z-10"
                />
                <GalleryArrow
                  dir="right"
                  onClick={() => setActiveImg(i => Math.min(galleryImages.length - 1, i + 1))}
                  disabled={activeImg === galleryImages.length - 1}
                  className="absolute right-2 top-1/2 -translate-y-1/2 z-10"
                />
              </>
            )}
          </div>

          {lightbox && (
            <ImageLightbox
              images={galleryImages.map(g => g.url)}
              index={activeImg}
              onClose={() => setLightbox(false)}
              onIndex={setActiveImg}
            />
          )}

          {galleryImages.length > 1 && (
            <div className="flex gap-2 flex-wrap mb-6">
              {galleryImages.map((img, i) => (
                <button
                  key={img.id}
                  type="button"
                  onClick={() => setActiveImg(i)}
                  aria-label={`${bundle.name} — photo ${i + 1}`}
                  aria-pressed={activeImg === i}
                  className={`w-16 h-16 rounded-xl overflow-hidden border-2 transition-all ${activeImg === i ? 'border-brand' : 'border-gray-100 hover:border-gray-300'}`}
                >
                  <img src={img.url} alt="" className="w-full h-full object-contain p-1 rounded-xl" />
                </button>
              ))}
            </div>
          )}

          {/* Info blocks — on desktop under the gallery */}
          <div className="hidden lg:block">
            <ProductInfoCards />
          </div>

        </div>

        {/* Information */}
        <div className="flex-1">
          <h1 className="text-lg sm:text-xl lg:text-2xl font-bold text-gray-900 leading-tight mb-1">{bundle.name}</h1>

          {!inStock && <p className="text-sm font-bold text-red-500 mb-1">Out of stock</p>}

          {bundle.sku && (
            <p className="text-sm text-gray-400 mb-1">SKU: <span className="font-bold text-gray-600">{bundle.sku}</span></p>
          )}

          {/* Price */}
          {(() => {
            const fmt = formatPrice
            const finalPrice = bundle.price != null ? Number(bundle.price) : null
            const oldPrice = bundle.old_price != null ? Number(bundle.old_price) : null
            const hasDiscount = oldPrice != null && finalPrice != null && oldPrice > finalPrice
            const pctShown = hasDiscount ? Math.round(((oldPrice - finalPrice) / oldPrice) * 100) : 0
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
                    {finalPrice != null ? `${fmt(finalPrice)} UAH` : '—'}
                  </span>
                </div>
              </div>
            )
          })()}

          {/* Quantity + buttons */}
          <div className="flex items-center gap-3 mb-6">
            {inStock ? (
              <>
                <QtyInput value={qty} onChange={setQty} max={bundle.stock ?? undefined} />
                <Button
                  size="sm"
                  onClick={() => { addItem({ productSlug: bundle.slug, productName: bundle.name, variantId: bundle.id, variantName: bundle.name, sku: bundle.sku ?? null, price: roundPrice(bundle.price ?? 0), image: cartImage, isBundle: true, maxQty: bundle.stock ?? undefined }, qty); openCart() }}
                  className="flex-1 min-w-0"
                  iconLeft={<ShoppingCart size={16} />}
                >
                  <span className="hidden sm:inline">Add to cart</span>
                </Button>
              </>
            ) : (
              <button
                type="button"
                onClick={() => openStockNotify(bundle.id, 'bundle')}
                className="flex-1 flex items-center justify-center gap-2 bg-gray-100 text-gray-600 hover:bg-gray-200 hover:text-text-primary disabled:opacity-50 font-bold py-2.5 rounded-xl transition-colors text-sm"
              >
                <Bell size={16} />
                Notify me when available
              </button>
            )}
            <button
              onClick={() => liked.toggle({ key: likeKey, productSlug: bundle.slug, productName: bundle.name, variantId: bundle.id, variantName: bundle.name, sku: bundle.sku ?? null, price: bundle.price != null ? roundPrice(bundle.price) : null, image: cartImage, isBundle: true })}
              className={`w-9 h-9 shrink-0 flex items-center justify-center rounded-xl border-2 transition-colors ${isLiked ? 'border-red-300 bg-red-50 text-red-500' : 'border-gray-200 text-gray-400 hover:border-red-300 hover:text-red-400'}`}
            >
              <Heart size={16} fill={isLiked ? 'currentColor' : 'none'} />
            </button>
          </div>

          {/* Tabs */}
          <div className="mt-8">
            {(() => {
              const tabs = ([
                { key: 'desc', label: 'Description' },
                { key: 'norms', label: 'Usage' },
                { key: 'composition', label: bundle.items.length ? `Bundle contents (${bundle.items.length})` : 'Bundle contents' },
                { key: 'reviews', label: reviewCount ? `Comments (${reviewCount})` : 'Comments' },
              ] as const).filter(t => t.key !== 'norms' || !!bundle.application_norms)
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

            {(activeTab === 'desc' || activeTab === 'norms') && (() => {
              const limited = isDesktop && !tabExpanded && !!tabMaxH
              return (
                <div className="relative">
                  <div
                    ref={tabBodyRef}
                    className={limited ? 'overflow-hidden' : ''}
                    style={limited ? { maxHeight: tabMaxH } : undefined}
                  >
                    {activeTab === 'desc' && (
                      <div className="prose max-w-none text-gray-700 text-sm leading-relaxed"
                        dangerouslySetInnerHTML={{ __html: sanitizeHtml(bundle.description) || '<p class="text-gray-400">No description</p>' }} />
                    )}
                    {activeTab === 'norms' && (
                      <div className="prose max-w-none text-gray-700 text-sm leading-relaxed"
                        dangerouslySetInnerHTML={{ __html: sanitizeHtml(bundle.application_norms) || '<p class="text-gray-400">No information available</p>' }} />
                    )}
                  </div>
                  {isDesktop && tabOverflow && !tabExpanded && (
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
                  {isDesktop && tabOverflow && tabExpanded && (
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

            {activeTab === 'reviews' && slug && (
              <ReviewsAndQuestions
                slug={slug}
                getReviews={getBundleReviews}
                postReview={postBundleReview}
              />
            )}

            {activeTab === 'composition' && (
              <div className="grid grid-cols-2 lg:grid-cols-3 gap-3 sm:gap-4">
                {bundle.items.map((item, i) => (
                  <ProductCard
                    key={i}
                    product={item.product}
                    scope="bundle-composition"
                    expectedVariantId={item.variant_id}
                    badge={
                      <span
                        className="absolute top-2 right-2 z-10 inline-flex items-center justify-center leading-none bg-brand text-white text-[11px] font-bold px-2 py-1.5 rounded-lg pointer-events-none"
                        title="Quantity in the bundle"
                      >
                        {item.quantity} pcs
                      </span>
                    }
                  />
                ))}
              </div>
            )}
          </div>

        </div>
      </div>

      {/* Info blocks on mobile — after the content, before recommendations */}
      <div className="lg:hidden mt-6">
        <ProductInfoCards />
      </div>

      {/* Other bundles */}
      {otherBundles.length > 0 && (
        <div className="mt-12">
          <div className="flex items-baseline justify-between gap-3 mb-5">
            <h2 className="text-xl font-bold text-gray-800">Other bundles</h2>
            <Link to="/catalog?category=komplekti" className="text-sm sm:text-base text-brand font-bold hover:underline flex items-center gap-1 whitespace-nowrap shrink-0">
              All bundles <ChevronRight size={18} />
            </Link>
          </div>
          <HScroller extraBottom={340} cols={{ base: 2, sm: 3, md: 4, lg: 5 }}>
            {otherBundles.map(b => <div key={b.id}><BundleCard bundle={b} /></div>)}
          </HScroller>
        </div>
      )}

      {/* You recently viewed */}
      {recentlyViewed.length > 0 && (
        <div className="mt-12">
          <h2 className="text-xl font-bold text-gray-800 mb-5">You recently viewed</h2>
          <HScroller extraBottom={340} cols={{ base: 2, sm: 3, md: 4, lg: 5 }}>
            {recentlyViewed.map(p => <div key={p.id}><ProductCard product={p} /></div>)}
          </HScroller>
        </div>
      )}
    </div>
  )
}

function PageSkeleton() {
  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8 animate-pulse">
      <div className="h-4 bg-gray-100 rounded w-64 mb-7" />
      <div className="flex gap-6 sm:gap-10 flex-col lg:flex-row">
        <div className="lg:w-[600px] lg:shrink-0 aspect-square bg-gray-100 rounded-3xl" />
        <div className="flex-1 space-y-4">
          <div className="h-5 bg-gray-100 rounded w-20" />
          <div className="h-9 bg-gray-100 rounded w-3/4" />
          <div className="h-12 bg-gray-100 rounded w-1/3" />
          <div className="h-12 bg-gray-100 rounded" />
        </div>
      </div>
    </div>
  )
}
