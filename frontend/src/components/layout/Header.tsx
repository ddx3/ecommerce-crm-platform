import { useState, useRef, useEffect, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { Link, NavLink, useNavigate, useLocation } from 'react-router-dom'
import { ShoppingCart, User, Search, LayoutGrid, ChevronDown, Heart, Package, Menu, X, GraduationCap } from 'lucide-react'
import { getCategories, searchItems } from '../../api/catalog'
import type { SearchResult } from '../../api/catalog'
import type { Category } from '../../types'
import { useCart } from '../../context/CartContext'
import { useLiked } from '../../context/LikedContext'
import { useUI } from '../../context/UIContext'
import { useAuth } from '../../context/AuthContext'
import { formatPrice } from '../../utils/price'
import { lockScroll } from '../ui/scrollLock'

const TOP_NAV = [
  { label: 'About us', to: '/about' },
  { label: 'Blog', to: '/blog' },
  { label: 'Payment and delivery', to: '/delivery' },
  { label: 'Reviews', to: '/reviews' },
  { label: 'Contacts', to: '/contacts' },
]

export default function Header() {
  const { count } = useCart()
  const { items: likedItems } = useLiked()
  const likedCount = likedItems.length
  const location = useLocation()
  const isCatalog = location.pathname.startsWith('/catalog')
  const { panel, openAuth, openCart, openLiked, close } = useUI()
  const { user, logout } = useAuth()
  const [query, setQuery] = useState('')
  const [scopeCleared, setScopeCleared] = useState(false)  // user clicked ✕ — search the whole site
  const scopeRef = useRef('')
  const [open, setOpen] = useState(false)
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const [categories, setCategories] = useState<Category[]>([])
  const [hoveredCat, setHoveredCat] = useState<Category | null>(null)
  const [hoveredSub, setHoveredSub] = useState<Category | null>(null)
  const [searchResults, setSearchResults] = useState<SearchResult | null>(null)
  const [searchOpen, setSearchOpen] = useState(false)
  const [searchLoading, setSearchLoading] = useState(false)
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false)
  const [mobileCatalogOpen, setMobileCatalogOpen] = useState(false)
  const [mobileAccountOpen, setMobileAccountOpen] = useState(false)
  const [openCats, setOpenCats] = useState<Set<number>>(new Set())
  const toggleCat = (id: number) => setOpenCats(prev => {
    const next = new Set(prev)
    next.has(id) ? next.delete(id) : next.add(id)
    return next
  })
  const ref = useRef<HTMLDivElement>(null)
  const headerRef = useRef<HTMLElement>(null)
  const searchRef = useRef<HTMLDivElement>(null)
  const searchRefMobile = useRef<HTMLDivElement>(null)
  const resultsRef = useRef<HTMLDivElement>(null)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const searchSeqRef = useRef(0)  // search request id: a response with a stale id is ignored (race with Enter/navigation)
  const navigate = useNavigate()

  // Close the mobile menu on route change
  useEffect(() => { setMobileMenuOpen(false) }, [location.pathname, location.search])

  // Close hover dropdowns (catalog, user menu, search) on route change.
  // Otherwise, when a menu item is clicked, navigation happens but mouseleave
  // does not fire (the mouse has not physically moved relative to the element) — and the dropdown
  // stays open on the new page.
  useEffect(() => {
    setOpen(false)
    setHoveredCat(null)
    setHoveredSub(null)
    setUserMenuOpen(false)
    setSearchOpen(false)
    setSearchResults(null)
    // The search field shows the active query: on the catalog page with ?search=…
    // we show that text, on any other page we clear it.
    const sp = new URLSearchParams(location.search).get('search') || ''
    setQuery(location.pathname === '/catalog' ? sp : '')
  }, [location.pathname, location.search])

  // Lock background scroll while the mobile menu is open.
  useEffect(() => {
    if (!mobileMenuOpen) {
      setMobileCatalogOpen(false); setOpenCats(new Set()); setMobileAccountOpen(false)
      return
    }
    return lockScroll()
  }, [mobileMenuOpen])

  useEffect(() => {
    getCategories().then(setCategories).catch(() => {})
  }, [])

  // Write the real header height into --header-h so that sticky columns
  // (product, checkout) stick right below it. It changes at breakpoints.
  useEffect(() => {
    const el = headerRef.current
    if (!el) return
    const apply = () => document.documentElement.style.setProperty('--header-h', `${el.offsetHeight}px`)
    apply()
    const ro = new ResizeObserver(apply)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const t = e.target as Node
      const inside = searchRef.current?.contains(t) || searchRefMobile.current?.contains(t)
      if (!inside) setSearchOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // Search scope: if we are in the catalog inside a category and have not clicked ✕, search within
  // it; otherwise the whole site.
  const currentCategory = isCatalog ? (new URLSearchParams(location.search).get('category') || '') : ''
  const searchScope = scopeCleared ? '' : currentCategory
  // Any navigation (including a repeat click on the same category) narrows the search
  // to the current category again — ✕ only works until the next navigation.
  useEffect(() => { setScopeCleared(false) }, [location.key])
  useEffect(() => { scopeRef.current = searchScope }, [searchScope])

  // Category name by slug (for the scope chip) — recursive over the tree.
  const findCatName = (slug: string): string => {
    const walk = (nodes: Category[]): string | null => {
      for (const n of nodes) {
        if (n.slug === slug) return n.name
        const c = walk(n.children || [])
        if (c) return c
      }
      return null
    }
    return walk(categories) || ''
  }

  const handleQueryChange = useCallback((val: string) => {
    setQuery(val)
    if (debounceRef.current) clearTimeout(debounceRef.current)
    if (val.trim().length < 2) { searchSeqRef.current++; setSearchResults(null); setSearchOpen(false); return }
    debounceRef.current = setTimeout(() => {
      const seq = ++searchSeqRef.current
      setSearchLoading(true)
      searchItems(val.trim(), scopeRef.current || undefined)
        .then(data => { if (seq === searchSeqRef.current) { setSearchResults(data); setSearchOpen(true) } })
        .catch(() => {})
        .finally(() => { if (seq === searchSeqRef.current) setSearchLoading(false) })
    }, 300)
  }, [])

  const handleSearch = (e: React.SyntheticEvent) => {
    e.preventDefault()
    if (query.trim()) {
      if (debounceRef.current) clearTimeout(debounceRef.current)
      searchSeqRef.current++  // invalidate the in-flight request — so the suggestions popup does not appear after navigation
      const qp = `search=${encodeURIComponent(query.trim())}`
      navigate(searchScope ? `/catalog?category=${searchScope}&${qp}` : `/catalog?${qp}`)
      setSearchOpen(false)
      setSearchResults(null)
      setSearchLoading(false)
    }
  }

  const handleResultClick = () => {
    setQuery('')
    setSearchOpen(false)
    setSearchResults(null)
  }

  // ✕ in the input — clear the query and suggestions (focus stays thanks to
  // preventDefault on the button's mousedown).
  const clearQuery = () => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    searchSeqRef.current++
    setQuery('')
    setSearchOpen(false)
    setSearchResults(null)
    // If we are on the search results page, reset the active ?search= in the URL,
    // otherwise the catalog keeps searching by the old query. Keep the category.
    const sp = new URLSearchParams(location.search)
    if (location.pathname === '/catalog' && sp.get('search')) {
      const cat = sp.get('category')
      navigate(cat ? `/catalog?category=${cat}` : '/catalog')
    }
  }

  // ✕ next to the category chip — remove the narrowing, search the whole site; update suggestions
  // right away.
  const clearScope = () => {
    setScopeCleared(true); scopeRef.current = ''
    if (query.trim().length >= 2) {
      const seq = ++searchSeqRef.current
      setSearchLoading(true)
      searchItems(query.trim(), undefined)
        .then(data => { if (seq === searchSeqRef.current) { setSearchResults(data); setSearchOpen(true) } })
        .catch(() => {})
        .finally(() => { if (seq === searchSeqRef.current) setSearchLoading(false) })
    }
  }

  const hasResults = searchResults && (searchResults.variants.length > 0 || searchResults.bundles.length > 0)

  // Recursive category tree node in the mobile menu (any depth).
  const renderCatNode = (node: Category): React.ReactNode => {
    const isOpen = openCats.has(node.id)
    const hasChildren = node.children.length > 0
    const isActive = node.slug === currentCategory
    return (
      <div key={node.slug}>
        <div className={`group flex items-center rounded-lg transition-colors ${isActive ? 'bg-[#eef7ec]' : 'hover:bg-[#eef7ec]'}`}>
          <button
            onClick={() => { navigate(`/catalog?category=${node.slug}`); setMobileMenuOpen(false) }}
            className={`flex-1 text-left px-3 py-2.5 text-base font-medium transition-colors ${isActive ? 'text-[#1a5c2a]' : 'text-gray-700 group-hover:text-[#1a5c2a]'}`}
          >
            {node.name}
          </button>
          {hasChildren && (
            <button
              onClick={() => toggleCat(node.id)}
              className={`w-9 h-9 shrink-0 flex items-center justify-center transition-colors ${isActive ? 'text-[#1a5c2a]' : 'text-gray-400'}`}
              aria-label="Expand"
            >
              <ChevronDown size={18} className={`transition-transform ${isOpen ? 'rotate-180' : ''}`} />
            </button>
          )}
        </div>
        {isOpen && hasChildren && (
          <div className="mt-1.5 mb-1 ml-[1.02em] pl-4 border-l-2 border-gray-200 flex flex-col gap-1.5">
            {node.children.map(renderCatNode)}
          </div>
        )}
      </div>
    )
  }

  return (
    <header ref={headerRef} className="sticky top-0 z-50 bg-white shadow-sm">
      {/* ── Mobile header ── */}
      <div className="lg:hidden border-b border-gray-200">
        <div className="flex items-center gap-2 h-14 px-3">
          <button
            onClick={() => setMobileMenuOpen(true)}
            className="w-10 h-10 flex items-center justify-center text-gray-700"
            aria-label="Menu"
          >
            <Menu size={24} />
          </button>
          <Link to="/" className="flex-1 flex items-center justify-center text-xl font-black text-[#1a5c2a] whitespace-nowrap leading-none">
            Magiya Roslyn
          </Link>
          <button onClick={() => panel === 'liked' ? close() : openLiked()} className="relative w-10 h-10 flex items-center justify-center text-gray-600" aria-label="Favorites">
            <Heart size={22} />
            {likedCount > 0 && (
              <span className="absolute top-0 right-0 min-w-[18px] h-[18px] px-1 text-[11px] font-bold leading-none tabular-nums rounded-full bg-red-100 text-red-500 ring-2 ring-white grid place-items-center">{likedCount}</span>
            )}
          </button>
          <button onClick={() => panel === 'cart' ? close() : openCart()} className="relative w-10 h-10 flex items-center justify-center text-gray-600" aria-label="Cart">
            <ShoppingCart size={22} />
            {count > 0 && (
              <span className="absolute top-0 right-0 min-w-[18px] h-[18px] px-1 text-[11px] font-bold leading-none tabular-nums rounded-full bg-[#eef7ec] text-[#1a5c2a] ring-2 ring-white grid place-items-center">{count}</span>
            )}
          </button>
          <button
            onClick={() => user ? navigate('/profile') : openAuth('login')}
            className="w-10 h-10 flex items-center justify-center text-gray-600"
            aria-label={user ? 'Profile' : 'Sign in'}
          >
            <User size={22} />
          </button>
        </div>
        {/* Search on mobile */}
        <div ref={searchRefMobile} className="px-3 pb-3">
          <div className="relative">
          <form onSubmit={handleSearch}>
            <div className="flex items-center bg-gray-50 border border-gray-200 rounded-xl h-10 focus-within:border-[#1a5c2a] focus-within:bg-white transition-all">
              {searchScope && (
                <button type="button" onClick={clearScope} aria-label="Search the whole site"
                  className="group inline-flex items-center gap-1 shrink-0 ml-2 my-1 pl-2.5 pr-1.5 py-1.5 bg-[#eef7ec] text-[#1a5c2a] rounded-lg text-xs font-bold leading-none max-w-[130px]">
                  <span className="truncate min-w-0">{findCatName(searchScope) || 'Category'}</span>
                  <X size={14} className="shrink-0 text-[#1a5c2a]/60 group-hover:text-[#1a5c2a]" />
                </button>
              )}
              <input
                type="text"
                value={query}
                onChange={e => handleQueryChange(e.target.value)}
                onFocus={() => hasResults && setSearchOpen(true)}
                placeholder={searchScope ? 'Search in category...' : 'Search products...'}
                className={`flex-1 ${searchScope ? 'pl-2' : 'pl-4'} text-sm bg-transparent outline-none text-gray-700 placeholder-gray-400 min-w-0`}
              />
              {query && (
                <button type="button" onMouseDown={e => e.preventDefault()} onClick={clearQuery}
                  aria-label="Clear" className="shrink-0 px-3 h-full flex items-center text-gray-400 hover:text-gray-600 transition-colors">
                  <X size={16} />
                </button>
              )}
              <span className="shrink-0 w-px self-stretch bg-gray-200" />
              <button type="submit" className="px-3 h-full text-gray-400" aria-label="Search">
                {searchLoading
                  ? <div className="w-4 h-4 border-2 border-gray-300 border-t-[#1a5c2a] rounded-full animate-spin" />
                  : <Search size={18} />
                }
              </button>
            </div>
          </form>
          {searchOpen && hasResults && (
            <div className="absolute top-full left-0 right-0 mt-1 bg-white rounded-2xl z-50 overflow-hidden" style={{ boxShadow: '0 4px 24px rgba(0,0,0,0.13)' }}>
            <div className="scroll-thin overflow-y-auto overscroll-contain max-h-[60vh]">
              {searchResults!.variants.length > 0 && (
                <div>
                  <p className="px-4 py-2 text-xs font-bold text-gray-400 uppercase tracking-wider"><span style={{ position: 'relative', top: '1px' }}>Products</span></p>
                  {searchResults!.variants.map((v, i) => (
                    <Link key={i} to={`/product/${v.product_slug}`} onClick={handleResultClick}
                      className="flex items-center gap-3 px-4 py-2.5 hover:bg-[#eef7ec] transition-colors">
                      <div className="w-10 h-10 rounded-lg bg-white ring-1 ring-border shrink-0 overflow-hidden">
                        {v.image ? <img src={v.image} alt={v.product_name} loading="lazy" className="w-full h-full object-contain p-0.5" /> : <Package size={20} className="text-gray-200 m-auto mt-2.5" />}
                      </div>
                      <div className="flex-1 min-w-0 h-10 flex flex-col justify-between">
                        <p className="text-sm text-gray-700 line-clamp-1">{v.variant_name || v.product_name}</p>
                        <p className="text-xs text-gray-400 whitespace-nowrap">{v.sku}</p>
                      </div>
                      <div className="text-right shrink-0 h-10 flex flex-col justify-between items-end">
                        <p className="text-sm font-bold text-gray-900">{v.price ? `${formatPrice(v.price)} UAH` : '—'}</p>
                        <p className={`text-xs ${v.in_stock ? 'text-[#1a5c2a]' : 'text-red-400'}`}>{v.in_stock ? 'In stock' : 'Out of stock'}</p>
                      </div>
                    </Link>
                  ))}
                </div>
              )}
              {searchResults!.bundles.length > 0 && (
                <div>
                  <p className="px-4 py-2 text-xs font-bold text-gray-400 uppercase tracking-wider"><span style={{ position: 'relative', top: '1px' }}>Bundles</span></p>
                  {searchResults!.bundles.map(b => (
                    <Link key={b.slug} to={`/bundle/${b.slug}`} onClick={handleResultClick}
                      className="flex items-center gap-3 px-4 py-2.5 hover:bg-[#eef7ec] transition-colors">
                      <div className="w-10 h-10 rounded-lg bg-white ring-1 ring-border shrink-0 overflow-hidden">
                        {b.image ? <img src={b.image} alt={b.name} loading="lazy" className="w-full h-full object-contain p-0.5" /> : <Package size={20} className="text-gray-200 m-auto mt-2.5" />}
                      </div>
                      <div className="flex-1 min-w-0 h-10 flex flex-col justify-between">
                        <p className="text-sm text-gray-700 line-clamp-1">{b.name}</p>
                        <p className="text-xs text-gray-400 whitespace-nowrap">{b.sku}</p>
                      </div>
                      <div className="text-right shrink-0 h-10 flex flex-col justify-between items-end">
                        <p className="text-sm font-bold text-gray-900">{b.price ? `${formatPrice(b.price)} UAH` : '—'}</p>
                        <p className={`text-xs ${b.in_stock ? 'text-[#1a5c2a]' : 'text-red-400'}`}>{b.in_stock ? 'In stock' : 'Out of stock'}</p>
                      </div>
                    </Link>
                  ))}
                </div>
              )}
              <div className="border-t border-gray-100">
                <button onClick={handleSearch as any}
                  className="w-full text-left px-4 py-3 text-sm text-[#1a5c2a] font-bold hover:bg-[#eef7ec] transition-colors">
                  Show all results for “{query}”
                </button>
              </div>
              </div>
            </div>
          )}
          {searchOpen && !hasResults && !searchLoading && searchResults && (
            <div className="absolute top-full left-0 right-0 mt-1 bg-white rounded-2xl z-50 px-4 py-4 text-center text-gray-400 text-sm" style={{ boxShadow: '0 4px 24px rgba(0,0,0,0.13)' }}>
              Nothing found
            </div>
          )}
          </div>
        </div>
      </div>

      {/* ── Mobile menu (drawer) ── */}
      {/* A portal into body is required: the menu sits inside <header sticky z-50>,
          and sticky creates its own stacking context, so z-[110] only counted within the header.
          The chat button and panel (fixed z-50, later in the DOM) were drawn OVER the menu
          and intercepted taps on the catalog. */}
      {mobileMenuOpen && createPortal(
          <div className="lg:hidden fixed inset-0 bg-white z-[110] flex flex-col">
            <div className="flex items-center justify-between px-4 h-14 border-b border-gray-200 shrink-0">
              <Link to="/" onClick={() => setMobileMenuOpen(false)} className="text-xl font-black text-[#1a5c2a] leading-none">Magiya Roslyn</Link>
              <button onClick={() => setMobileMenuOpen(false)} className="w-9 h-9 flex items-center justify-center text-gray-400 hover:text-gray-700 transition-colors" aria-label="Close">
                <X size={22} />
              </button>
            </div>
            <div className="flex-1 overflow-y-auto">
              {/* Account */}
              <div className="px-4 py-3 border-b border-gray-100">
                {user ? (
                  <>
                    <button
                      onClick={() => setMobileAccountOpen(v => !v)}
                      className="w-full flex items-center gap-2 border border-gray-200 text-gray-700 px-4 h-11 rounded-xl text-base font-bold transition-colors hover:border-[#1a5c2a] hover:text-[#1a5c2a]"
                    >
                      <User size={18} /> {user.first_name || user.email}
                      <ChevronDown size={18} className={`ml-auto transition-transform ${mobileAccountOpen ? 'rotate-180' : ''}`} />
                    </button>
                    {mobileAccountOpen && (
                      <div className="mt-2 flex flex-col gap-0.5">
                        <Link to="/orders" onClick={() => setMobileMenuOpen(false)} className="px-3 py-2.5 text-base font-medium text-gray-600 rounded-lg hover:bg-[#eef7ec] hover:text-[#1a5c2a] transition-colors">My orders</Link>
                        <Link to="/my-courses" onClick={() => setMobileMenuOpen(false)} className="px-3 py-2.5 text-base font-medium text-gray-600 rounded-lg hover:bg-[#eef7ec] hover:text-[#1a5c2a] transition-colors">My courses</Link>
                        <Link to="/profile" onClick={() => setMobileMenuOpen(false)} className="px-3 py-2.5 text-base font-medium text-gray-600 rounded-lg hover:bg-[#eef7ec] hover:text-[#1a5c2a] transition-colors">Personal data</Link>
                        <button onClick={() => { logout(); setMobileMenuOpen(false) }} className="px-3 py-2.5 text-base font-medium text-red-500 text-left rounded-lg hover:bg-red-50 transition-colors">Log out</button>
                      </div>
                    )}
                  </>
                ) : (
                  <button
                    onClick={() => { setMobileMenuOpen(false); openAuth('login') }}
                    className="w-full flex items-center justify-center gap-2 border border-gray-200 hover:border-[#1a5c2a] hover:text-[#1a5c2a] text-gray-600 px-4 h-11 rounded-xl text-base font-medium transition-colors"
                  >
                    <User size={18} /> Sign in / Sign up
                  </button>
                )}
              </div>

              {/* Quick actions — Favorites / Cart (as on desktop) */}
              <div className="px-4 py-3 border-b border-gray-100 flex gap-2">
                <button
                  onClick={() => { setMobileMenuOpen(false); openLiked() }}
                  className="flex-1 flex items-center justify-center gap-2 border border-gray-200 hover:border-red-400 hover:text-red-500 text-gray-600 h-11 rounded-xl text-base font-medium transition-colors"
                >
                  <Heart size={18} /> Favorites
                  <span className={`text-sm font-bold rounded-full w-6 h-6 flex items-center justify-center shrink-0 ${likedCount > 0 ? 'bg-red-100 text-red-500' : 'bg-gray-100 text-gray-500'}`}>{likedCount}</span>
                </button>
                <button
                  onClick={() => { setMobileMenuOpen(false); openCart() }}
                  className="flex-1 flex items-center justify-center gap-2 border border-gray-200 hover:border-[#1a5c2a] hover:text-[#1a5c2a] text-gray-600 h-11 rounded-xl text-base font-medium transition-colors"
                >
                  <ShoppingCart size={18} /> Cart
                  <span className={`text-sm font-bold rounded-full w-6 h-6 flex items-center justify-center shrink-0 ${count > 0 ? 'bg-[#eef7ec] text-[#1a5c2a]' : 'bg-gray-100 text-gray-500'}`}>{count}</span>
                </button>
              </div>

              {/* Categories */}
              <div className="px-4 py-3 border-b border-gray-100">
                <button
                  onClick={() => setMobileCatalogOpen(v => !v)}
                  className="w-full flex items-center gap-2.5 px-5 h-11 rounded-xl bg-[#1a5c2a] hover:bg-[#2d8a45] text-white font-bold text-base transition-colors"
                >
                  <LayoutGrid size={18} /> Catalog
                  <ChevronDown size={18} className={`ml-auto transition-transform ${mobileCatalogOpen ? 'rotate-180' : ''}`} />
                </button>
                {mobileCatalogOpen && (
                <div className="mt-2 flex flex-col gap-0.5">
                <Link to="/catalog" onClick={() => setMobileMenuOpen(false)} className="px-3 py-2.5 text-base font-bold text-[#1a5c2a] rounded-lg hover:bg-[#eef7ec] transition-colors">
                  All products
                </Link>
                {categories.map(renderCatNode)}
                </div>
                )}
              </div>

              {/* Courses */}
              <div className="px-4 py-3 border-b border-gray-100">
                <Link
                  to="/courses"
                  onClick={() => setMobileMenuOpen(false)}
                  className="flex items-center gap-2 px-4 h-11 bg-white border-2 border-[#1a5c2a] text-[#1a5c2a] font-bold text-base rounded-xl hover:bg-[#1a5c2a] hover:text-white transition-colors"
                >
                  <GraduationCap size={18} /> Courses
                </Link>
              </div>

              {/* Information */}
              <div className="px-4 py-3">
                <div className="flex flex-col gap-0.5">
                  {TOP_NAV.map(link => (
                    <NavLink
                      key={link.to}
                      to={link.to}
                      onClick={() => setMobileMenuOpen(false)}
                      className={({ isActive }) =>
                        `block px-3 py-2.5 text-base font-medium rounded-lg transition-colors ${isActive ? 'text-[#1a5c2a] font-bold bg-[#eef7ec]' : 'text-gray-600 hover:text-[#1a5c2a] hover:bg-[#eef7ec]'}`
                      }
                    >
                      {link.label}
                    </NavLink>
                  ))}
                </div>
              </div>

            </div>
          </div>,
          document.body,
      )}

      {/* Row 1 */}
      <div className="relative z-10 hidden lg:block">
        <div className="max-w-[1440px] mx-auto px-8">
        <div className="flex items-center h-14 gap-2 border-b border-gray-200">
          <Link to="/" className="text-2xl font-extrabold text-[#1a5c2a] whitespace-nowrap shrink-0 mr-3 hover:opacity-80 transition-opacity">Magiya Roslyn</Link>
          <nav className="flex items-center gap-1 flex-1">
            {TOP_NAV.map(link => (
              <NavLink
                key={link.to}
                to={link.to}
                className={({ isActive }) =>
                  `px-4 py-2 text-base font-medium transition-colors whitespace-nowrap ${isActive ? 'text-[#1a5c2a]' : 'text-gray-500 hover:text-[#1a5c2a]'}`
                }
              >
                {link.label}
              </NavLink>
            ))}
          </nav>

          {user ? (
            <div className="relative shrink-0" onMouseEnter={() => setUserMenuOpen(true)} onMouseLeave={() => setUserMenuOpen(false)}>
              <button type="button" onClick={() => setUserMenuOpen(v => !v)} aria-expanded={userMenuOpen} className={`flex items-center gap-2 border px-4 py-2 rounded-xl transition-colors text-base ${userMenuOpen ? 'border-[#1a5c2a] text-[#1a5c2a]' : 'border-gray-200 text-gray-600 hover:border-[#1a5c2a] hover:text-[#1a5c2a]'}`}>
                <User size={18} />
                {user.first_name || user.email}
              </button>
              {userMenuOpen && (
                <>
                <div className="absolute left-0 right-0 top-full h-1 z-50" />
                <div className="absolute left-1/2 -translate-x-1/2 top-full mt-1 bg-white rounded-2xl border border-gray-100 min-w-[200px] z-50 overflow-hidden" style={{ boxShadow: '0 4px 16px rgba(0,0,0,0.10)' }}>
                  <Link to="/orders" onClick={() => setUserMenuOpen(false)} className="flex items-center px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-100 transition-colors">My orders</Link>
                  <Link to="/my-courses" onClick={() => setUserMenuOpen(false)} className="flex items-center px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-100 transition-colors">My courses</Link>
                  <Link to="/profile" onClick={() => setUserMenuOpen(false)} className="flex items-center px-4 py-2.5 text-sm text-gray-700 hover:bg-gray-100 transition-colors">Personal data</Link>
                  <div className="border-t border-gray-100">
                    <button onClick={() => { logout(); setUserMenuOpen(false) }} className="w-full text-left px-4 py-2.5 text-sm text-red-500 hover:bg-red-100 transition-colors">Log out</button>
                  </div>
                </div>
                </>
              )}
            </div>
          ) : (
            <button onClick={() => openAuth('login')} className="flex items-center gap-2 border border-gray-200 hover:border-[#1a5c2a] hover:text-[#1a5c2a] text-gray-600 px-4 py-2 rounded-xl transition-colors text-base shrink-0">
              <User size={18} />
              Sign in
            </button>
          )}

        </div>
        </div>
      </div>

      {/* Row 2 */}
      <div className="bg-white hidden lg:block">
        <div className="max-w-[1440px] mx-auto px-8 flex items-center gap-4 h-16">

          {!isCatalog && <div
            ref={ref}
            className="relative shrink-0"
            onMouseEnter={() => setOpen(true)}
            onMouseLeave={() => { setOpen(false); setHoveredCat(null); setHoveredSub(null) }}
          >
            <Link
              to="/catalog"
              className={`group flex items-center gap-2.5 font-bold px-5 h-11 rounded-xl transition-colors text-base ${
                open || isCatalog ? 'bg-[#2d8a45] text-white' : 'bg-[#1a5c2a] hover:bg-[#2d8a45] text-white'
              }`}
            >
              <LayoutGrid size={18} />
              Catalog
              <ChevronDown size={18} className={`transition-transform duration-300 ${open ? 'rotate-180' : ''}`} />
            </Link>

            {open && (
              <div
                className="absolute top-full left-0 pt-2 z-50"
              >
                <div
                  className="flex rounded-2xl overflow-hidden bg-white"
                  style={{ boxShadow: '0 0 18px rgba(0,0,0,0.16)' }}
                >
                  <div className="w-64">
                    {categories.map(cat => (
                      <button
                        key={cat.slug}
                        onMouseEnter={() => { setHoveredCat(cat); setHoveredSub(null) }}
                        onClick={() => { navigate(`/catalog?category=${cat.slug}`); setOpen(false) }}
                        className={`w-full flex items-center gap-3 px-5 h-12 text-base text-left transition-colors ${
                          hoveredCat?.slug === cat.slug
                            ? 'bg-[#eef7ec] text-[#1a5c2a]'
                            : 'text-gray-700 hover:bg-gray-50'
                        }`}
                      >
                        <span className="flex-1 font-medium">{cat.name}</span>
                        {cat.children.length > 0 && <ChevronDown size={22} className={`shrink-0 transition-transform text-gray-400 ${hoveredCat?.slug === cat.slug ? 'rotate-180 text-[#1a5c2a]' : ''}`} />}
                      </button>
                    ))}
                  </div>

                  {hoveredCat && hoveredCat.children.length > 0 && (
                    <div className="w-64 bg-gray-50 overflow-hidden relative">
                      <div className="absolute left-0 top-0 bottom-0 w-1 z-10 pointer-events-none" style={{ background: 'linear-gradient(to right, rgba(0,0,0,0.06) 0%, rgba(0,0,0,0.02) 60%, transparent 100%)' }} />
                      {hoveredCat.children.map((sub: Category) => (
                        <button
                          key={sub.slug}
                          onMouseEnter={() => setHoveredSub(sub.children.length > 0 ? sub : null)}
                          onClick={() => { navigate(`/catalog?category=${sub.slug}`); setOpen(false) }}
                          className={`w-full flex items-center gap-2 px-5 py-2.5 text-base text-left transition-colors ${
                            hoveredSub?.slug === sub.slug ? 'bg-[#eef7ec] text-[#1a5c2a]' : 'text-gray-600 hover:text-[#1a5c2a] hover:bg-[#eef7ec]'
                          }`}
                        >
                          <span className="flex-1">{sub.name}</span>
                          {sub.children.length > 0 && <ChevronDown size={18} className={`shrink-0 transition-transform ${hoveredSub?.slug === sub.slug ? 'rotate-180 text-[#1a5c2a]' : 'text-gray-400'}`} />}
                        </button>
                      ))}
                    </div>
                  )}

                  {hoveredSub && hoveredSub.children.length > 0 && (
                    <div className="w-64 bg-white overflow-hidden relative">
                      <div className="absolute left-0 top-0 bottom-0 w-1 z-10 pointer-events-none" style={{ background: 'linear-gradient(to right, rgba(0,0,0,0.06) 0%, rgba(0,0,0,0.02) 60%, transparent 100%)' }} />
                      {hoveredSub.children.map((gc: Category) => (
                        <button
                          key={gc.slug}
                          onClick={() => { navigate(`/catalog?category=${gc.slug}`); setOpen(false) }}
                          className="w-full text-left px-5 py-2.5 text-base text-gray-600 hover:text-[#1a5c2a] hover:bg-[#eef7ec] transition-colors"
                        >
                          {gc.name}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>}

          {/* Courses — hidden on the courses page itself */}
          {!location.pathname.startsWith('/course') && (
            <Link
              to="/courses"
              className="group flex items-center gap-2 font-bold px-4 h-11 rounded-xl border-2 border-brand text-brand bg-white transition-colors text-base shrink-0 hover:bg-brand hover:text-white"
              title="Online plant courses"
            >
              <GraduationCap size={18} />
              Courses
            </Link>
          )}

          {/* Search with dropdown */}
          <div ref={searchRef} className="flex-1 relative">
            <form onSubmit={handleSearch}>
              <div className="flex items-center bg-gray-50 border border-gray-200 rounded-xl overflow-hidden focus-within:border-[#1a5c2a] focus-within:bg-white focus-within:shadow-sm transition-all h-11">
                {searchScope && (
                  <button type="button" onClick={clearScope} aria-label="Search the whole site"
                    className="group inline-flex items-center gap-1 shrink-0 ml-2 my-1 pl-2.5 pr-1.5 py-1.5 bg-[#eef7ec] text-[#1a5c2a] rounded-lg text-xs font-bold leading-none max-w-[180px]">
                    <span className="truncate min-w-0">{findCatName(searchScope) || 'Category'}</span>
                    <X size={14} className="shrink-0 text-[#1a5c2a]/60 group-hover:text-[#1a5c2a]" />
                  </button>
                )}
                <input
                  type="text"
                  value={query}
                  onChange={e => handleQueryChange(e.target.value)}
                  onFocus={() => hasResults && setSearchOpen(true)}
                  placeholder={searchScope ? 'Search in category...' : 'Search products...'}
                  className="flex-1 pl-3 text-base bg-transparent outline-none text-gray-700 placeholder-gray-400 min-w-0"
                />
                {query && (
                  <button type="button" onMouseDown={e => e.preventDefault()} onClick={clearQuery}
                    aria-label="Clear" className="shrink-0 px-4 h-full flex items-center text-gray-400 hover:text-gray-600 transition-colors">
                    <X size={18} />
                  </button>
                )}
                <span className="shrink-0 w-px self-stretch bg-gray-200" />
                <button type="submit" className="px-4 h-full text-gray-400 hover:text-[#1a5c2a] transition-colors">
                  {searchLoading
                    ? <div className="w-5 h-5 border-2 border-gray-300 border-t-[#1a5c2a] rounded-full animate-spin" />
                    : <Search size={20} />
                  }
                </button>
              </div>
            </form>

            {searchOpen && hasResults && (
              <div className="absolute top-full left-0 right-0 mt-2 bg-white rounded-2xl z-50 overflow-hidden" style={{ boxShadow: '0 4px 24px rgba(0,0,0,0.13)' }}>
              <div ref={resultsRef} className="scroll-thin overflow-y-auto overscroll-contain max-h-[calc(100vh-var(--header-h,80px)-1.5rem)]">
                {searchResults!.variants.length > 0 && (
                  <div>
                    <p className="px-4 py-2 text-xs font-bold text-gray-400 uppercase tracking-wider"><span style={{ position: 'relative', top: '1px' }}>Products</span></p>
                    {searchResults!.variants.map((v, i) => (
                      <Link key={i} to={`/product/${v.product_slug}`} onClick={handleResultClick}
                        className="flex items-center gap-3 px-4 py-2.5 hover:bg-[#eef7ec] transition-colors">
                        <div className="w-10 h-10 rounded-lg bg-white ring-1 ring-border shrink-0 overflow-hidden">
                          {v.image ? <img src={v.image} alt={v.product_name} loading="lazy" className="w-full h-full object-contain p-0.5" /> : <Package size={20} className="text-gray-200 m-auto mt-2.5" />}
                        </div>
                        <div className="flex-1 min-w-0">
                          <p className="text-sm text-gray-700 line-clamp-1">{v.variant_name || v.product_name}</p>
                          <p className="text-xs text-gray-400 whitespace-nowrap">{v.sku}</p>
                        </div>
                        <div className="text-right shrink-0">
                          <p className="text-sm font-bold text-gray-900">{v.price ? `${formatPrice(v.price)} UAH` : '—'}</p>
                          <p className={`text-xs ${v.in_stock ? 'text-[#1a5c2a]' : 'text-red-400'}`}>{v.in_stock ? 'In stock' : 'Out of stock'}</p>
                        </div>
                      </Link>
                    ))}
                  </div>
                )}
                {searchResults!.bundles.length > 0 && (
                  <div>
                    <p className="px-4 py-2 text-xs font-bold text-gray-400 uppercase tracking-wider"><span style={{ position: 'relative', top: '1px' }}>Bundles</span></p>
                    {searchResults!.bundles.map(b => (
                      <Link key={b.slug} to={`/bundle/${b.slug}`} onClick={handleResultClick}
                        className="flex items-center gap-3 px-4 py-2.5 hover:bg-[#eef7ec] transition-colors">
                        <div className="w-10 h-10 rounded-lg bg-white ring-1 ring-border shrink-0 overflow-hidden">
                          {b.image ? <img src={b.image} alt={b.name} loading="lazy" className="w-full h-full object-contain p-0.5" /> : <Package size={20} className="text-gray-200 m-auto mt-2.5" />}
                        </div>
                        <div className="flex-1 min-w-0">
                          <p className="text-sm text-gray-700 line-clamp-1">{b.name}</p>
                          <p className="text-xs text-gray-400 whitespace-nowrap">{b.sku}</p>
                        </div>
                        <div className="text-right shrink-0">
                          <p className="text-sm font-bold text-gray-900">{b.price ? `${formatPrice(b.price)} UAH` : '—'}</p>
                          <p className={`text-xs ${b.in_stock ? 'text-[#1a5c2a]' : 'text-red-400'}`}>{b.in_stock ? 'In stock' : 'Out of stock'}</p>
                        </div>
                      </Link>
                    ))}
                  </div>
                )}
                <div className="border-t border-gray-100">
                  <button onClick={handleSearch as any}
                    className="w-full text-left px-4 py-3 text-sm text-[#1a5c2a] font-bold hover:bg-[#eef7ec] transition-colors">
                    Show all results for “{query}”
                  </button>
                </div>
              </div>
              </div>
            )}

            {searchOpen && !hasResults && !searchLoading && searchResults && (
              <div className="absolute top-full left-0 right-0 mt-2 bg-white rounded-2xl z-50 px-4 py-5 text-center text-gray-400 text-sm" style={{ boxShadow: '0 4px 24px rgba(0,0,0,0.13)' }}>
                Nothing found
              </div>
            )}

          </div>

          <button onClick={() => panel === 'liked' ? close() : openLiked()} className="group ml-auto flex items-center gap-2 border border-gray-200 hover:border-red-400 hover:text-red-500 text-gray-600 px-4 py-2 rounded-xl transition-colors text-base shrink-0">
            <Heart size={18} />
            Favorites
            <span className={`text-sm font-bold rounded-full w-6 h-6 flex items-center justify-center shrink-0 transition-colors ${likedCount > 0 ? 'bg-red-100 text-red-500' : 'bg-gray-100 text-gray-500 group-hover:bg-red-100 group-hover:text-red-500'}`}>{likedCount}</span>
          </button>
          <button onClick={() => panel === 'cart' ? close() : openCart()} className="group flex items-center gap-2 border border-gray-200 hover:border-[#1a5c2a] hover:text-[#1a5c2a] text-gray-600 px-4 py-2 rounded-xl transition-colors text-base shrink-0">
            <ShoppingCart size={18} />
            Cart
            <span className={`text-sm font-bold rounded-full w-6 h-6 flex items-center justify-center shrink-0 transition-colors ${count > 0 ? 'bg-[#eef7ec] text-[#1a5c2a]' : 'bg-gray-100 text-gray-500 group-hover:bg-[#eef7ec] group-hover:text-[#1a5c2a]'}`}>{count}</span>
          </button>

        </div>
      </div>
    </header>
  )
}
