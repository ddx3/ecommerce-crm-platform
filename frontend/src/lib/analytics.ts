/**
 * Storefront web analytics: GA4 + Google Ads + Meta (Facebook) Pixel.
 *
 * Keys come via .env (VITE_*). No key → the corresponding pixel is not loaded,
 * and all track helpers become no-ops (on dev the console is clean, nothing breaks).
 *
 * Consent Mode v2 — granted by default (the customer's decision: UA audience,
 * no cookie banner; mentioned in the Privacy Policy). If we later go to the EU —
 * change the default to denied and add a banner with gtag('consent','update', ...).
 *
 * Single entry point: init is called in main.tsx, events go through trackX functions.
 */

const env = import.meta.env as unknown as Record<string, string | undefined>
const GA4_ID = (env.VITE_GA4_ID || '').trim()
const ADS_ID = (env.VITE_GOOGLE_ADS_ID || '').trim()
const ADS_PURCHASE_LABEL = (env.VITE_GOOGLE_ADS_PURCHASE_LABEL || '').trim()
const META_PIXEL_ID = (env.VITE_META_PIXEL_ID || '').trim()

const CURRENCY = 'UAH'

declare global {
  interface Window {
    dataLayer?: unknown[]
    gtag?: (...args: unknown[]) => void
    fbq?: (...args: unknown[]) => void
    _fbq?: unknown
  }
}

/** Product snapshot for e-commerce events (shared by GA4 and Meta). */
export interface AnalyticsItem {
  id: string | number
  name: string
  price: number
  quantity?: number
  category?: string
}

let initialized = false

function loadGoogle() {
  if (!GA4_ID && !ADS_ID) return
  const tagId = GA4_ID || ADS_ID
  const s = document.createElement('script')
  s.async = true
  s.src = `https://www.googletagmanager.com/gtag/js?id=${tagId}`
  document.head.appendChild(s)

  window.dataLayer = window.dataLayer || []
  function gtag(...args: unknown[]) {
    // GA reads the pushed object by indices ([0],[1]...) — the args array is equivalent to the
    // original arguments and is type-safe at the same time.
    window.dataLayer!.push(args)
  }
  window.gtag = gtag
  gtag('js', new Date())
  // Consent Mode v2 — full access by default (see the file header).
  gtag('consent', 'default', {
    ad_storage: 'granted',
    ad_user_data: 'granted',
    ad_personalization: 'granted',
    analytics_storage: 'granted',
  })
  // We send page-view manually on route change (SPA) — disable auto-send
  // so as not to duplicate it on the first render.
  if (GA4_ID) gtag('config', GA4_ID, { send_page_view: false })
  if (ADS_ID) gtag('config', ADS_ID)
}

function loadMeta() {
  if (!META_PIXEL_ID || window.fbq) return
  /* eslint-disable @typescript-eslint/no-explicit-any */
  const n: any = function (...args: unknown[]) {
    // keep this=n, as in the original Meta Pixel snippet
    // eslint-disable-next-line prefer-spread
    if (n.callMethod) n.callMethod.apply(n, args)
    else n.queue.push(args)
  }
  n.queue = []
  n.loaded = true
  n.version = '2.0'
  n.push = n
  /* eslint-enable @typescript-eslint/no-explicit-any */
  window.fbq = n
  window._fbq = window._fbq || n
  const t = document.createElement('script')
  t.async = true
  t.src = 'https://connect.facebook.net/en_US/fbevents.js'
  document.head.appendChild(t)
  n('init', META_PIXEL_ID)
  // We do NOT send PageView here: the only source is AnalyticsTracker (on mount
  // and on route change), otherwise the first load would have a duplicate PageView.
}

/** Initialization of all pixels. Call once before rendering the app. */
export function initAnalytics() {
  if (initialized) return
  initialized = true
  try {
    loadGoogle()
    loadMeta()
  } catch {
    /* analytics must never break the storefront */
  }
}

function ga(...args: unknown[]) {
  try { window.gtag?.(...args) } catch { /* no-op */ }
}

function fb(event: string, params?: Record<string, unknown>) {
  try { window.fbq?.('track', event, params) } catch { /* no-op */ }
}

function gaItems(items: AnalyticsItem[]) {
  return items.map(i => ({
    item_id: String(i.id),
    item_name: i.name,
    price: i.price,
    quantity: i.quantity ?? 1,
    item_category: i.category,
  }))
}

const sumValue = (items: AnalyticsItem[]) =>
  items.reduce((s, i) => s + i.price * (i.quantity ?? 1), 0)

/** Page view (SPA) — call on route change. */
export function trackPageView(path: string) {
  if (GA4_ID) ga('event', 'page_view', { page_path: path, page_location: window.location.href, page_title: document.title })
  fb('PageView')
}

/** Product card view. */
export function trackViewItem(item: AnalyticsItem) {
  ga('event', 'view_item', {
    currency: CURRENCY,
    value: item.price,
    items: gaItems([item]),
  })
  fb('ViewContent', {
    content_ids: [String(item.id)],
    content_name: item.name,
    content_type: 'product',
    value: item.price,
    currency: CURRENCY,
  })
}

/** Add to cart. */
export function trackAddToCart(item: AnalyticsItem, qty = 1) {
  const it = { ...item, quantity: qty }
  ga('event', 'add_to_cart', {
    currency: CURRENCY,
    value: item.price * qty,
    items: gaItems([it]),
  })
  fb('AddToCart', {
    content_ids: [String(item.id)],
    content_name: item.name,
    content_type: 'product',
    value: item.price * qty,
    currency: CURRENCY,
  })
}

/** Checkout start. */
export function trackBeginCheckout(items: AnalyticsItem[]) {
  const value = sumValue(items)
  ga('event', 'begin_checkout', {
    currency: CURRENCY,
    value,
    items: gaItems(items),
  })
  fb('InitiateCheckout', {
    content_ids: items.map(i => String(i.id)),
    content_type: 'product',
    num_items: items.reduce((s, i) => s + (i.quantity ?? 1), 0),
    value,
    currency: CURRENCY,
  })
}

// ─── Purchase via snapshot ────────────────────────────────────────────────
// The order reaches the success page as a separate load (especially after a
// redirect to Monobank and back), where the cart contents are already gone. So at
// checkout we put an order snapshot into sessionStorage, and on success/payment we
// read it and send purchase exactly once (the snapshot is deleted on send).

const PURCHASE_KEY = 'analytics_purchase'

/** Save the order snapshot before a redirect/navigation (call before clearCart). */
export function saveCheckoutSnapshot(orderId: string, items: AnalyticsItem[], value: number) {
  try {
    sessionStorage.setItem(PURCHASE_KEY, JSON.stringify({ orderId, items, value }))
  } catch { /* sessionStorage is unavailable — skip */ }
}

/** Send purchase from the saved snapshot if it belongs to this order. Idempotent. */
export function flushPurchase(orderId: string) {
  if (!orderId) return
  try {
    const raw = sessionStorage.getItem(PURCHASE_KEY)
    if (!raw) return
    const snap = JSON.parse(raw) as { orderId: string; items: AnalyticsItem[]; value: number }
    if (String(snap.orderId) !== String(orderId)) return
    sessionStorage.removeItem(PURCHASE_KEY) // guard against re-sending (page refresh)
    trackPurchase(String(orderId), snap.items, snap.value)
  } catch { /* corrupted snapshot — ignore */ }
}

/** Successful purchase (success page). transactionId — the order number. */
export function trackPurchase(transactionId: string, items: AnalyticsItem[], value?: number) {
  const total = value ?? sumValue(items)
  ga('event', 'purchase', {
    transaction_id: transactionId,
    currency: CURRENCY,
    value: total,
    items: gaItems(items),
  })
  // Google Ads conversion (if a label is set) — as a separate event.
  if (ADS_PURCHASE_LABEL) {
    ga('event', 'conversion', {
      send_to: ADS_PURCHASE_LABEL,
      transaction_id: transactionId,
      value: total,
      currency: CURRENCY,
    })
  }
  fb('Purchase', {
    content_ids: items.map(i => String(i.id)),
    content_type: 'product',
    value: total,
    currency: CURRENCY,
  })
}
