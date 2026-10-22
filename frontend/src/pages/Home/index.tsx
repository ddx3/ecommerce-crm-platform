import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight, Star, User } from 'lucide-react'
import { getProducts, getBrands, getBundles, getSiteReviews } from '../../api/catalog'
import { getBlogPosts } from '../../api/blog'
import type { Brand } from '../../api/catalog'
import type { ProductListItem, BundleListItem, BlogPostListItem } from '../../types'
import { ProductCard, BundleCard } from '../Catalog'
import { BlogCard } from '../Blog'
import HScroller from '../../components/HScroller'
import Seo from '../../components/Seo'

// Tab priority: new arrival -> sale -> bestseller. A product is shown in only one feed (the higher
// priority takes it), so feeds don't duplicate. New arrival is limited to 60 days — it is not
// topped up, while bestsellers are the broadest and pick up everything else.
const TABS = [
  { label: 'New arrivals', params: { label: 'novynka', ordering: 'new' as const } },
  { label: 'Sales', params: { label: 'aktsia', exclude_label: 'novynka', ordering: 'new' as const } },
  { label: 'Bestsellers', params: { label: 'khit', exclude_label: 'novynka,aktsia', ordering: 'new' as const } },
]

export default function HomePage() {

  // Load all 3 sets (New/Sale/Bestsellers) in parallel on mount.
  // Empty tabs are hidden; if all are empty, the section is not shown at all.
  const [tabsData, setTabsData] = useState<ProductListItem[][]>([[], [], []])
  const [tab, setTab] = useState(0)
  const [loading, setLoading] = useState(true)
  const [brands, setBrands] = useState<Brand[]>([])
  const [bundles, setBundles] = useState<BundleListItem[]>([])
  const [reviews, setReviews] = useState<{ id: number; author_name: string; rating: number; text: string }[]>([])
  const [blogPosts, setBlogPosts] = useState<BlogPostListItem[]>([])

  useEffect(() => {
    setLoading(true)
    Promise.all(
      TABS.map(t => getProducts({ ...t.params, page_size: 10 }).then(d => d.results).catch(() => [] as ProductListItem[]))
    ).then(results => {
      setTabsData(results)
      // Make the first tab with a full row (>= 5) the active one.
      const firstFull = results.findIndex(r => r.length >= 5)
      if (firstFull !== -1) setTab(firstFull)
    }).finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    getBrands().then(setBrands).catch(() => {})
    getBundles({ page_size: 10 }).then(d => setBundles(d.results || [])).catch(() => {})
    getSiteReviews().then(rs => setReviews((rs || []).slice(0, 6))).catch(() => {})
    getBlogPosts({ limit: 10 }).then(d => setBlogPosts(d.results || [])).catch(() => {})
  }, [])

  const products = tabsData[tab] ?? []
  // Show a tab only if it has a full row (cols.lg = 5).
  // Otherwise it looks sloppy (3 cards in a wide row).
  const MIN_PER_TAB = 5
  const visibleTabs = TABS.map((t, i) => ({ t, i })).filter(({ i }) => (tabsData[i]?.length ?? 0) >= MIN_PER_TAB)
  const hasAnyProducts = visibleTabs.length > 0

  return (
    <div className="bg-white">
      <Seo
        title="Magiya Roslyn — products for the garden, vegetable plot and plants"
        description="Online store «Magiya Roslyn»: fertilizers, plant protection products, seeds, goods for the garden and vegetable plot. Delivery across Ukraine."
        canonicalPath="/"
      />

      {/* ── Hero ── */}
      <section className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 pt-4 sm:pt-6 pb-4 sm:pb-5">
        <div className="rounded-2xl sm:rounded-3xl overflow-hidden bg-gradient-to-br from-[#1a5c2a] via-[#2d7a3f] to-[#4a9e5c] relative min-h-[280px] sm:min-h-[360px] lg:min-h-[400px] flex items-center">
          <div className="relative z-10 px-6 sm:px-10 lg:px-14 py-10 sm:py-12 max-w-2xl">
            <h1 className="text-3xl sm:text-5xl lg:text-6xl font-black text-white leading-tight mb-3 sm:mb-4">
              Experience, knowledge<br />and a little magic
            </h1>
            <div className="flex flex-wrap items-center gap-3 sm:gap-4 mt-7 sm:mt-9">
              <Link
                to="/catalog"
                className="inline-flex items-center gap-2 bg-white text-[#1a5c2a] font-black px-6 sm:px-8 py-3 sm:py-4 rounded-xl border-2 border-white hover:bg-transparent hover:text-white transition-colors duration-100 shadow-lg text-base sm:text-lg"
              >
                Go to catalog
                <ChevronRight size={20} />
              </Link>
              <Link
                to="/about"
                className="inline-flex items-center gap-2 text-white font-bold px-5 sm:px-6 py-3 sm:py-4 rounded-xl border border-white/30 hover:bg-white/10 transition-colors text-base sm:text-lg"
              >
                About us
              </Link>
            </div>
          </div>

          <div className="absolute right-0 top-0 w-[460px] h-[460px] bg-white/5 rounded-full translate-x-1/3 -translate-y-1/3 pointer-events-none" />
          <div className="absolute right-20 bottom-0 w-72 h-72 bg-white/5 rounded-full translate-y-1/3 pointer-events-none" />
        </div>
      </section>

      {/* ── Products ── */}
      {(loading || hasAnyProducts) && (
        <section className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8 pb-10 sm:pb-16">
          <div className="flex items-center gap-2 sm:gap-3 mb-5 sm:mb-7 flex-wrap">
            {visibleTabs.map(({ t, i }) => (
              <button
                key={t.label}
                onClick={() => setTab(i)}
                className={`px-4 sm:px-6 py-2 sm:py-2.5 rounded-xl font-bold transition-colors text-sm sm:text-base ${
                  tab === i
                    ? 'bg-brand text-white shadow-sm'
                    : 'bg-gray-100 text-gray-500 hover:bg-brand-bg hover:text-brand'
                }`}
              >
                {t.label}
              </button>
            ))}
            <Link to={`/catalog?label=${TABS[tab].params.label}`} className="ml-auto text-sm sm:text-base text-brand font-bold hover:underline flex items-center gap-1 whitespace-nowrap shrink-0">
              All products <ChevronRight size={18} />
            </Link>
          </div>

          {loading ? (
            <HScroller extraBottom={340} cols={{ base: 2, sm: 3, md: 4, lg: 5 }}>
              {Array.from({ length: 10 }).map((_, i) => (
                <div key={i}><ProductCardSkeleton /></div>
              ))}
            </HScroller>
          ) : (
            // key={tab} — recreate the scroller when the tab changes so the horizontal position
            // does not carry over from the previous tab (scrollLeft lives in the DOM).
            <HScroller key={tab} extraBottom={340} cols={{ base: 2, sm: 3, md: 4, lg: 5 }}>
              {products.map(p => (
                <div key={p.id}><ProductCard product={p} /></div>
              ))}
            </HScroller>
          )}
        </section>
      )}

      {/* ── Bundles ── */}
      {bundles.length > 0 && (
        <section className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 pb-10 sm:pb-16">
          <div className="relative z-20 flex items-baseline justify-between gap-3 mb-4 sm:mb-6">
            <h2 className="text-xl sm:text-2xl font-bold text-gray-800">Bundles</h2>
            <Link to="/catalog?category=komplekti" className="text-sm sm:text-base text-brand font-bold hover:underline flex items-center gap-1 whitespace-nowrap shrink-0">
              All bundles <ChevronRight size={18} />
            </Link>
          </div>
          <HScroller extraBottom={340} cols={{ base: 2, sm: 3, md: 4, lg: 5 }}>
            {bundles.slice(0, 10).map(b => (
              <div key={b.id}><BundleCard bundle={b} /></div>
            ))}
          </HScroller>
        </section>
      )}

      {/* ── Reviews ── */}
      {reviews.length > 0 && (
        <section className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 pb-10 sm:pb-16">
          <div className="relative z-20 flex items-baseline justify-between gap-3 mb-4 sm:mb-6">
            <h2 className="text-xl sm:text-2xl font-bold text-gray-800">Customer reviews</h2>
            <Link to="/reviews" className="text-sm sm:text-base text-brand font-bold hover:underline flex items-center gap-1 whitespace-nowrap shrink-0">
              All reviews <ChevronRight size={18} />
            </Link>
          </div>
          <HScroller cols={{ base: 1, sm: 2, md: 3, lg: 4, xl: 5 }}>
            {reviews.slice(0, 10).map(r => (
              <div
                key={r.id}
                className="bg-surface border border-border rounded-2xl shadow-card-low p-3 sm:p-4 flex flex-col"
              >
                <div className="flex items-center gap-2 mb-2">
                  <div className="w-8 h-8 rounded-full bg-brand-bg text-brand flex items-center justify-center shrink-0">
                    <User size={16} />
                  </div>
                  <div className="min-w-0">
                    <p className="font-bold text-sm text-text-primary truncate leading-tight">{r.author_name}</p>
                    <div className="flex items-center gap-0.5 mt-0.5">
                      {Array.from({ length: 5 }).map((_, i) => (
                        <Star key={i} size={11} className={i < r.rating ? 'fill-amber-400 text-amber-400' : 'text-gray-200'} />
                      ))}
                    </div>
                  </div>
                </div>
                <p className="text-[13px] text-text-secondary leading-snug line-clamp-3 min-h-[3lh]">{r.text}</p>
              </div>
            ))}
          </HScroller>
        </section>
      )}

      {/* ── Brands ── */}
      {brands.length > 0 && (
        <section className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 pb-10 sm:pb-16">
          <div className="relative z-20 flex items-baseline justify-between gap-3 mb-4 sm:mb-6">
            <h2 className="text-xl sm:text-2xl font-bold text-gray-800">Brands</h2>
          </div>
          <HScroller cols={{ base: 3, sm: 4, md: 5, lg: 7 }} gap={{ base: 8, sm: 12 }}>
            {brands.map(brand => (
              <Link
                key={brand.id}
                to={`/catalog?brand=${brand.slug}`}
                className="group flex flex-col items-center justify-between bg-surface border border-border rounded-2xl shadow-card-low hover:shadow-card-hover py-4 px-3 h-[110px]"
              >
                <div className="flex-1 flex items-center justify-center w-full">
                  {brand.logo
                    ? <img src={brand.logo} alt={brand.name} loading="lazy" decoding="async" className="max-h-14 max-w-full object-contain" />
                    : null
                  }
                </div>
                <span className="text-xs text-gray-400 font-medium text-center leading-tight truncate w-full">
                  {brand.name}
                </span>
              </Link>
            ))}
          </HScroller>
        </section>
      )}

      {/* ── Blog ── */}
      {blogPosts.length > 0 && (
        <section className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 pb-8 sm:pb-12">
          <div className="relative z-20 flex items-baseline justify-between gap-3 mb-4 sm:mb-6">
            <h2 className="text-xl sm:text-2xl font-bold text-gray-800">Blog</h2>
            <Link to="/blog" className="text-sm sm:text-base text-brand font-bold hover:underline flex items-center gap-1 whitespace-nowrap shrink-0">
              All posts <ChevronRight size={18} />
            </Link>
          </div>
          <HScroller cols={{ base: 1, sm: 2, lg: 3, xl: 4 }}>
            {blogPosts.map(post => (
              <div key={post.id}><BlogCard post={post} /></div>
            ))}
          </HScroller>
        </section>
      )}

    </div>
  )
}

function ProductCardSkeleton() {
  return (
    <div className="bg-surface border border-border rounded-2xl shadow-card-low overflow-hidden">
      <div className="aspect-square bg-surface-muted animate-pulse" />
      <div className="px-4 pt-3 pb-4 space-y-3">
        <div className="h-3 bg-gray-100 rounded-full animate-pulse w-1/2" />
        <div className="h-4 bg-gray-100 rounded-full animate-pulse w-3/4" />
        <div className="h-4 bg-gray-100 rounded-full animate-pulse w-2/3" />
        <div className="h-6 bg-gray-100 rounded-full animate-pulse w-1/3 mt-1" />
      </div>
    </div>
  )
}

