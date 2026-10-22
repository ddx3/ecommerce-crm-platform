import { useState, useEffect, useRef } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Check, MessageSquare, X, ChevronRight } from 'lucide-react'
import { Button, Input, PhoneInput, Spinner, Card, CartItemBase } from '../../components/ui'
import { toLocalDigits } from '../../components/ui/PhoneInput'
import ShipmentGroupHeader from '../../components/ShipmentGroupHeader'
import BundleComposition from '../../components/BundleComposition'
import type { ShippingOrigin, Supplier } from '../../types'
import type { CartItem } from '../../context/CartContext'
import { useCart } from '../../context/CartContext'
import { useAuth } from '../../context/AuthContext'
import { useUI } from '../../context/UIContext'
import { useSiteSettings } from '../../context/SiteSettingsContext'
import client from '../../api/client'
import { roundPrice, formatPrice } from '../../utils/price'
import { trackBeginCheckout, saveCheckoutSnapshot, type AnalyticsItem } from '../../lib/analytics'
import { getAttribution } from '../../lib/attribution'

/** CartItem → snapshot for analytics (shared mapping for begin_checkout and purchase). */
function toAnalyticsItems(cart: CartItem[]): AnalyticsItem[] {
  return cart.map(i => ({
    id: i.kind === 'course' ? `course_${i.variantId}` : i.productSlug,
    name: i.productName,
    price: i.price,
    quantity: i.qty,
  }))
}

// ─── Nova Poshta ─────────────────────────────────────────────────────────────

interface NPCity {
  Ref: string
  Present: string
  MainDescription: string
  Area: string
}

interface NPWarehouse {
  Ref: string
  Description: string
  Number: string
  CategoryOfWarehouse?: string
  TypeOfWarehouse?: string
  PlaceMaxWeightAllowed?: string | number
  TotalMaxWeightAllowed?: string | number
}

// What can be chosen for the parcel:
//   'all'    — own parcel: any branch, parcel locker, courier to an address;
//   'branch' — dropshipping: branches only (a parcel locker doesn't accept the dimensions);
//   'cargo'  — supplier dropshipping with requires_cargo_branch: cargo branches only.
type WarehouseMode = 'all' | 'branch' | 'cargo'

// Parcel locker: Category='Postomat' + fallback by name (some records come without a category).
// LANGUAGE DATA: the regex below matches the Ukrainian word "поштомат" ("parcel locker") in the
// Description text returned by the Nova Poshta API - it must stay Ukrainian.
const isBranch = (w: NPWarehouse) =>
  w.CategoryOfWarehouse !== 'Postomat' && !/поштомат/i.test(w.Description || '')

// Cargo branch. TypeOfWarehouse is a stable nationwide Nova Poshta GUID
// (verified on Lviv/Kyiv/Odesa/Mykolaiv: exactly 3 types — parcel locker, regular
// branch up to 30 kg, cargo). It is not described in the Nova Poshta docs, so we keep a
// fallback on the weight limit: for cargo it is 200…1100 kg, for regular ones 0 or 30.
// Fulfillment is a warehouse, not a pickup point for the customer, so we keep only Branch.
const NP_CARGO_TYPE = '9a68df70-0267-42a8-bb5c-37f427e36ee4'
const isCargoBranch = (w: NPWarehouse) => {
  if (!isBranch(w) || w.CategoryOfWarehouse !== 'Branch') return false
  if (w.TypeOfWarehouse) return w.TypeOfWarehouse === NP_CARGO_TYPE
  const max = Math.max(Number(w.PlaceMaxWeightAllowed) || 0, Number(w.TotalMaxWeightAllowed) || 0)
  return max >= 200
}

const warehouseFilter = (mode: WarehouseMode) =>
  mode === 'cargo' ? isCargoBranch : mode === 'branch' ? isBranch : null

interface NPStreet {
  // Nova Poshta returns a ready-made representation with the type (e.g. "St. Khreshchatyk")
  // in Present.
  Present: string
  SettlementStreetDescription?: string
  SettlementStreetRef?: string
}

function useNPCities(query: string) {
  const [cities, setCities] = useState<NPCity[]>([])
  const [loading, setLoading] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (query.trim().length < 2) { setCities([]); return }
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => {
      setLoading(true)
      client.get('/nova-poshta/', { params: { method: 'searchSettlements', q: query } })
        .then(r => setCities(r.data?.data?.[0]?.Addresses || []))
        .catch(() => setCities([]))
        .finally(() => setLoading(false))
    }, 300)
  }, [query])

  return { cities, loading }
}

function useNPWarehouses(cityRef: string, mode: WarehouseMode = 'all') {
  const [warehouses, setWarehouses] = useState<NPWarehouse[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!cityRef) { setWarehouses([]); setError(null); return }
    setLoading(true)
    setError(null)
    client.get('/nova-poshta/', { params: { method: 'getWarehouses', city_ref: cityRef } })
      .then(r => {
        const data = r.data?.data
        if (Array.isArray(data)) {
          const list = data as NPWarehouse[]
          const filter = warehouseFilter(mode)
          const kept = filter ? list.filter(filter) : list
          setWarehouses(kept)
          if (data.length === 0) setError('There are no Nova Poshta branches in this city')
          // The city exists but there are no suitable branches — otherwise the list is silently
          // empty
          // and the customer doesn't understand why they can't choose anything.
          else if (kept.length === 0) setError(mode === 'cargo'
            ? 'There are no Nova Poshta cargo branches in this city — choose another locality'
            : 'There are no Nova Poshta branches in this city (parcel lockers only)')
        } else {
          setWarehouses([])
          setError('Nova Poshta service is temporarily unavailable')
        }
      })
      .catch(err => {
        setWarehouses([])
        const status = err?.response?.status
        if (status === 503) setError('Nova Poshta service is not configured')
        else setError('Nova Poshta service is temporarily unavailable')
      })
      .finally(() => setLoading(false))
  }, [cityRef, mode])

  return { warehouses, loading, error }
}

// Streets of a settlement for address delivery. Server-side autocomplete (debounce
// 300, ≥2 characters) — unlike branches, the street directory is large and Nova Poshta searches
// it itself. cityRef is the SettlementRef (the same Ref as from searchSettlements).
function useNPStreets(cityRef: string, query: string) {
  const [streets, setStreets] = useState<NPStreet[]>([])
  const [loading, setLoading] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (!cityRef || query.trim().length < 2) { setStreets([]); return }
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => {
      setLoading(true)
      client.get('/nova-poshta/', { params: { method: 'searchSettlementStreets', city_ref: cityRef, q: query } })
        .then(r => setStreets(r.data?.data?.[0]?.Addresses || []))
        .catch(() => setStreets([]))
        .finally(() => setLoading(false))
    }, 300)
  }, [cityRef, query])

  return { streets, loading }
}

function CitySearch({ value, onChange }: { value: string; onChange: (val: string, ref: string) => void }) {
  const [query, setQuery] = useState(value)
  const [open, setOpen] = useState(false)
  const { cities, loading } = useNPCities(query)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => { setQuery(value) }, [value])

  useEffect(() => {
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  return (
    <div ref={ref} className="relative">
      <div className="relative">
        <input
          type="text"
          value={query}
          onChange={e => { setQuery(e.target.value); setOpen(true); onChange('', '') }}
          onFocus={() => cities.length > 0 && setOpen(true)}
          placeholder="Start typing a city..."
          className="w-full border border-border rounded-xl px-4 py-3 pr-10 text-base outline-none focus:border-brand transition-colors"
        />
        {loading && <Spinner size="sm" tone="muted" className="absolute right-3 top-1/2 -translate-y-1/2" />}
      </div>
      {open && cities.length > 0 && (
        <div className="absolute top-full left-0 right-0 mt-1 bg-white border border-border rounded-xl shadow-lg z-[70] overflow-hidden">
          <div className="max-h-56 overflow-y-auto overscroll-contain">
            {cities.map(city => (
              <button
                key={city.Ref}
                type="button"
                onClick={() => { setQuery(city.Present); onChange(city.Present, city.Ref); setOpen(false) }}
                className="w-full text-left px-4 py-2.5 text-sm hover:bg-brand-bg hover:text-brand transition-colors"
              >
                {city.Present}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function WarehouseSearch({ warehouses, value, onChange, loading, error, mode = 'all' }: { warehouses: NPWarehouse[]; value: string; onChange: (val: string) => void; loading?: boolean; error?: string | null; mode?: WarehouseMode }) {
  const [query, setQuery] = useState(value)
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  const filtered = query.trim()
    ? warehouses.filter(w => w.Description.toLowerCase().includes(query.toLowerCase()))
    : warehouses

  // Large cities have thousands of points (Kyiv ~7600). Rendering all of them freezes the browser,
  // so we show the first RENDER_LIMIT, but the filter searches the FULL set — any
  // branch/locker can be found by number or address.
  const RENDER_LIMIT = 50
  const shown = filtered.slice(0, RENDER_LIMIT)
  const hiddenCount = filtered.length - shown.length

  useEffect(() => {
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  useEffect(() => { setQuery(value) }, [value])

  return (
    <div ref={ref} className="relative">
      <div className="relative">
        <input
          type="text"
          value={query}
          onChange={e => { setQuery(e.target.value); setOpen(true); onChange('') }}
          onFocus={() => setOpen(true)}
          placeholder={loading ? 'Loading…' : (mode === 'cargo' ? 'Cargo branch...' : mode === 'branch' ? 'Branch...' : 'Branch or parcel locker...')}
          disabled={loading || warehouses.length === 0}
          className="w-full border border-border rounded-xl px-4 py-3 pr-10 text-base outline-none focus:border-brand transition-colors disabled:bg-surface-muted disabled:cursor-not-allowed"
        />
        {loading && <Spinner size="sm" tone="muted" className="absolute right-3 top-1/2 -translate-y-1/2" />}
      </div>
      {error && !loading && <p className="text-xs text-danger mt-1">{error}</p>}
      {open && filtered.length > 0 && (
        <div className="absolute top-full left-0 right-0 mt-1 bg-white border border-border rounded-xl shadow-lg z-[70] overflow-hidden">
          <div className="max-h-56 overflow-y-auto overscroll-contain">
            {shown.map(w => (
              <button
                key={w.Ref}
                type="button"
                onClick={() => { setQuery(w.Description); onChange(w.Description); setOpen(false) }}
                className="w-full text-left px-4 py-2.5 text-sm hover:bg-brand-bg hover:text-brand transition-colors"
              >
                {w.Description}
              </button>
            ))}
            {hiddenCount > 0 && (
              <p className="px-4 py-2.5 text-xs text-text-secondary border-t border-border">
                {hiddenCount} more — refine your search (number or street)
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// Street search for address delivery. Also allows free input: in small villages
// the Nova Poshta street directory is empty, so any typed text goes as the street name
// (the street Ref isn't needed — we store the address as a string).
function StreetSearch({ cityRef, value, onChange }: { cityRef: string; value: string; onChange: (val: string) => void }) {
  const [query, setQuery] = useState(value)
  const [open, setOpen] = useState(false)
  const { streets, loading } = useNPStreets(cityRef, query)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => { setQuery(value) }, [value])

  useEffect(() => {
    const handler = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  return (
    <div ref={ref} className="relative">
      <div className="relative">
        <input
          type="text"
          value={query}
          onChange={e => { setQuery(e.target.value); setOpen(true); onChange(e.target.value) }}
          onFocus={() => streets.length > 0 && setOpen(true)}
          placeholder="Start typing a street..."
          className="w-full border border-border rounded-xl px-4 py-3 pr-10 text-base outline-none focus:border-brand transition-colors"
        />
        {loading && <Spinner size="sm" tone="muted" className="absolute right-3 top-1/2 -translate-y-1/2" />}
      </div>
      {open && streets.length > 0 && (
        <div className="absolute top-full left-0 right-0 mt-1 bg-white border border-border rounded-xl shadow-lg z-[70] overflow-hidden">
          <div className="max-h-56 overflow-y-auto overscroll-contain">
            {streets.map((s, i) => {
              const label = s.Present || s.SettlementStreetDescription || ''
              return (
                <button
                  key={s.SettlementStreetRef || i}
                  type="button"
                  onClick={() => { setQuery(label); onChange(label); setOpen(false) }}
                  className="w-full text-left px-4 py-2.5 text-sm hover:bg-brand-bg hover:text-brand transition-colors"
                >
                  {label}
                </button>
              )
            })}
          </div>
        </div>
      )}
      {query.trim().length >= 2 && !loading && streets.length === 0 && (
        <p className="text-xs text-text-secondary mt-1">Street not found — enter the name manually.</p>
      )}
    </div>
  )
}

// Dropshipping parcel from one supplier: city + Nova Poshta branch (we don't deliver
// dropshipping by courier). Own useNPWarehouses — so the hook is per-supplier: different
// suppliers narrow the list differently.
function DropSupplierPicker({ index, supplier, value, onChange, error }: {
  index: number
  supplier: Supplier
  value: { city: string; cityRef: string; warehouse: string }
  onChange: (v: { city: string; cityRef: string; warehouse: string }) => void
  error?: string
}) {
  const mode: WarehouseMode = supplier.requires_cargo_branch ? 'cargo' : 'branch'
  const { warehouses, loading, error: whError } = useNPWarehouses(value.cityRef, mode)
  return (
    // The line separates a parcel from the previous one. Before parcel 1 there is none — the card
    // heading
    // is already there, and an extra line would cut it off from its own content.
    <div className="border-t border-border pt-6">
      <p className="font-bold text-base text-text-primary mb-3">Parcel {index}</p>
      <div className="flex flex-col gap-4">
        <div>
          <label className="block text-sm font-bold text-text-secondary mb-1.5">City</label>
          <CitySearch value={value.city} onChange={(val, ref) => onChange({ city: val, cityRef: ref, warehouse: '' })} />
        </div>
        {value.cityRef && (
          <div>
            <label className="block text-sm font-bold text-text-secondary mb-1.5">
              {mode === 'cargo' ? 'Cargo branch' : 'Branch'}
            </label>
            <WarehouseSearch warehouses={warehouses} value={value.warehouse} onChange={val => onChange({ ...value, warehouse: val })} loading={loading} error={whError} mode={mode} />
          </div>
        )}
      </div>
      {error && <p className="text-xs text-danger mt-1">{error}</p>}
    </div>
  )
}

// Translate the backend's technical errors into user-friendly ones.
// The backend sends e.g. '"Potassium phosphate, 1 kg" out of stock (have 1, need 3)' (the quoted name
// is a product name).
function localizeOrderError(msg: string): string {
  if (/out of stock/i.test(msg)) {
    const name = msg.match(/"(.+?)"/)?.[1]
    const have = msg.match(/have (\d+)/)?.[1]
    if (have != null) {
      return name
        ? `Sorry, “${name}” has only ${have} pcs left. Please reduce the quantity in your cart.`
        : `Stock has decreased — only ${have} pcs available. Please reduce the quantity in your cart.`
    }
    return name
      ? `Sorry, “${name}” is currently out of stock. Remove the item or reduce the quantity.`
      : 'Some items are not available in the required quantity. Please check your cart.'
  }
  return msg
}

// ─── Checkout page ────────────────────────────────────────────────────────────

export default function CheckoutPage() {
  const { items, total, clearCart, removeItem, updateQty, minOrderAmount, belowMinOrder } = useCart()
  const { user } = useAuth()
  const { openAuth } = useUI()
  const navigate = useNavigate()
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set())
  const toggleGroup = (key: string) => {
    setCollapsedGroups(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  // A cart with courses only — no delivery needed, the form is simplified.
  const onlyCourses = items.length > 0 && items.every(i => i.kind === 'course')
  // A course in a mixed cart also needs an explanation of "where to watch it" — but
  // delivery/address
  // for such an order can't be removed (the products are real).
  const hasCourses = items.some(i => i.kind === 'course')

  // Analytics: checkout start (once per entry to checkout with a non-empty cart).
  const beganCheckout = useRef(false)
  useEffect(() => {
    if (beganCheckout.current || items.length === 0) return
    beganCheckout.current = true
    trackBeginCheckout(toAnalyticsItems(items))
  }, [items])

  // Dropshipping (bradas) goes only to a Nova Poshta branch — the supplier does not do address
  // delivery. If ALL physical products are dropshipping, we don't show the Nova Poshta method
  // choice.
  const _phys = items.filter(i => i.kind !== 'course')
  // The item ships via DROPSHIPPING (a separate supplier parcel), not from own stock: pure
  // dropshipping (bradas)
  // always; hybrid — ONLY when the quantity exceeds own stock (consistent with the backend
  // OrderItem.was_dropshipped: hybrid → dropshipping when stock<qty). A hybrid in stock ships in
  // the own
  // parcel — we don't make the customer enter a 2nd Nova Poshta address for nothing. Before
  // reconcile stock=undefined
  // → treated as 0 (a safe over-ask until the live stock level is verified).
  const isDropItem = (i: CartItem) =>
    i.shippingOrigin === 'bradas' ||
    (i.shippingOrigin === 'hybrid' && i.qty > (i.stock ?? 0))
  // Dropshipping suppliers in the cart. Each supplier = a separate Nova Poshta branch parcel.
  const dropSuppliers = (() => {
    const m = new Map<string, Supplier>()
    _phys.forEach(i => { if (isDropItem(i) && i.supplier) m.set(i.supplier.code, i.supplier) })
    return [...m.values()]
  })()
  const hasDrop = dropSuppliers.length > 0
  const hasOwn = _phys.some(i => !isDropItem(i))   // own + bundle + in-stock hybrid → own parcel
  const allDrop = _phys.length > 0 && !hasOwn                      // pure dropshipping only
  const mixedShipment = hasDrop && hasOwn

  const [form, setForm] = useState({
    name: user ? [user.first_name, user.last_name].filter(Boolean).join(' ') : '',
    phone: user?.phone || '',
    email: user?.email || '',
    city: '', cityRef: '', warehouse: '',
    // Nova Poshta method: 'warehouse' — to a branch/parcel locker, 'address' — courier to an
    // address.
    npMode: 'warehouse' as 'warehouse' | 'address', street: '', house: '', flat: '',
    // Dropshipping parcels: Nova Poshta branch address per supplier (key = supplier.code).
    dropAddrs: {} as Record<string, { city: string; cityRef: string; warehouse: string }>,
    // Monobank is the default method: online payment without a manual transfer/screenshot.
    payment: 'monobank' as 'monobank' | 'bank_transfer',
    comment: '',
  })
  const [bankOption, setBankOption] = useState<'site' | 'contact'>('site')

  // Whether there are active payment details for this order's amount (we determine the needed
  // threshold
  // among ALL thresholds, and show only the active ones — same as on the payment page).
  const { paymentCards, thresholds } = useSiteSettings()
  const hasSiteCards = (() => {
    if (!paymentCards) return true  // still loading — optimistically keep the option
    const all = thresholds.length ? thresholds : paymentCards.map(c => c.threshold)
    if (!all.length) return false
    const applicable = all.filter(t => t <= total)
    const chosen = applicable.length ? Math.max(...applicable) : Math.min(...all)
    return paymentCards.some(c => c.threshold === chosen)
  })()

  // Options for receiving payment details. "On the site" — only if there is something to
  // show; "to the
  // phone" — for non-courses (or as a fallback when there is nothing on the site).
  const bankOptions = (() => {
    const opts: { value: 'site' | 'contact'; label: string }[] = []
    if (hasSiteCards) opts.push({ value: 'site', label: 'Show the payment details on the site' })
    if (!onlyCourses) opts.push({ value: 'contact', label: 'Send me the payment details to my phone number' })
    if (opts.length === 0) opts.push({ value: 'contact', label: 'Send me the payment details to my phone number' })
    return opts
  })()

  // The selected option became unavailable (e.g. the payment details were hidden) → switch to the
  // first available one.
  useEffect(() => {
    if (!bankOptions.some(o => o.value === bankOption)) setBankOption(bankOptions[0].value)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasSiteCards, onlyCourses])

  useEffect(() => {
    if (user) {
      setForm(f => ({
        ...f,
        name: f.name || [user.first_name, user.last_name].filter(Boolean).join(' '),
        phone: f.phone || user.phone,
        email: f.email || user.email,
        city: f.city || user.np_city || '',
        cityRef: f.cityRef || user.np_city_ref || '',
        warehouse: f.warehouse || user.np_warehouse || '',
      }))
    }
  }, [user])
  // Off by default: we show all parcels as they are, and don't decide the address on
  // the customer's behalf. The checkbox is their explicit choice to merge everything into one
  // branch.
  const [sameWarehouse, setSameWarehouse] = useState(false)
  // A courier address can't be given to a dropshipping parcel (the supplier ships only to
  // branches), so in address mode each parcel is filled in separately.
  const shareWarehouse = sameWarehouse && form.npMode === 'warehouse'
  const dropKey = dropSuppliers.map(s => s.code).join(',')

  // Keep the dropshipping parcel addresses equal to the main one while the checkbox is on. We write
  // directly into form (rather than substituting at submit) so the customer SEES in the fields what
  // will
  // actually be shipped. Return f unchanged when nothing changed — otherwise
  // setForm would cause extra re-renders on every pass.
  useEffect(() => {
    if (!shareWarehouse || !form.city || !form.warehouse) return
    setForm(f => {
      const src = { city: f.city, cityRef: f.cityRef, warehouse: f.warehouse }
      const next = { ...f.dropAddrs }
      let changed = false
      for (const code of dropKey ? dropKey.split(',') : []) {
        const cur = next[code]
        if (!cur || cur.city !== src.city || cur.cityRef !== src.cityRef || cur.warehouse !== src.warehouse) {
          next[code] = { ...src }
          changed = true
        }
      }
      return changed ? { ...f, dropAddrs: next } : f
    })
  }, [shareWarehouse, form.city, form.cityRef, form.warehouse, dropKey])

  const [consent, setConsent] = useState(false)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(false)
  // Synchronous guard against double submit: setLoading is async, so between two
  // fast clicks (double-tap) both handlers would manage to create an order each.
  const submittingRef = useRef(false)
  const [success, setSuccess] = useState<string | null>(null)
  const [showComment, setShowComment] = useState(false)

  // The strictest restriction among the cart's dropshipping suppliers. A cargo branch
  // accepts regular parcels too, but not vice versa, so we narrow to it.
  const dropStrictest: WarehouseMode =
    dropSuppliers.some(s => s.requires_cargo_branch) ? 'cargo' : 'branch'
  // The main parcel takes on these restrictions in two cases: a dropshipping-only cart
  // (there are no separate pickers) and the "all to one branch" checkbox
  // (dropshipping goes to the same
  // address). Otherwise the restriction would be bypassed: the customer would pick a parcel locker
  // for their own
  // parcel, and the dropshipping would go there too.
  const ownMode: WarehouseMode =
    allDrop || (shareWarehouse && hasDrop) ? dropStrictest : 'all'
  const { warehouses, loading: warehousesLoading, error: warehousesError } = useNPWarehouses(form.cityRef, ownMode)

  const set = (k: string, v: string) => setForm(f => ({ ...f, [k]: v }))

  // Phone mask: always the format "+38 0XX XXX XX XX"
  const validate = () => {
    const e: Record<string, string> = {}
    if (!form.name.trim()) e.name = "Enter your name"
    // For courses the phone is not required — except when the user chose
    // bank_transfer + the option "send the payment details to the phone" (send payment details to the
    // phone) (then it is absolutely needed).
    const phoneRequired = !onlyCourses || (form.payment === 'bank_transfer' && bankOption === 'contact')
    // A full phone is required (10 local digits with the leading zero) — otherwise the manager
    // can't reach the customer
    // and the order is effectively lost. PhoneInput lets incomplete input through too.
    if (phoneRequired) {
      if (!form.phone.trim()) e.phone = 'Enter your phone number'
      else if (toLocalDigits(form.phone).length !== 10) e.phone = 'Enter the full phone number'
    }
    // For courses email is required — the access instructions will be sent there.
    if (onlyCourses) {
      if (!form.email.trim()) e.email = 'Enter your email — the course access instructions will be sent to it'
    } else {
      if (!form.city) e.city = 'Choose a city'
      else if (form.npMode === 'warehouse') {
        if (!form.warehouse) e.warehouse = 'Choose a branch'
      } else {
        if (!form.street.trim()) e.street = 'Choose a street'
        if (!form.house.trim()) e.house = 'Enter the house number'
      }
      // Dropshipping parcels: each supplier (Bradas, ARS…) ships as a separate Nova Poshta parcel
      // to its own branch. Dropshipping is always branch-only (we don't deliver by courier).
      if (mixedShipment && !shareWarehouse) dropSuppliers.forEach(s => {
        const a = form.dropAddrs[s.code]
        if (!a || !a.city || !a.warehouse) e[`drop_${s.code}`] = `Choose a Nova Poshta branch for “${s.name}”`
      })
    }
    if (!consent) e.consent = 'You must accept the terms'
    setErrors(e)
    return Object.keys(e).length === 0
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (submittingRef.current) return
    if (belowMinOrder) {
      setErrors({ submit: `Minimum order amount — ${formatPrice(minOrderAmount)} UAH.` })
      return
    }
    if (!validate()) return
    submittingRef.current = true
    setLoading(true)

    const deliveryAddress = onlyCourses
      ? '— digital item —'
      : (form.npMode === 'warehouse'
          ? `${form.city}, ${form.warehouse}`
          // The marker "Address delivery —" lets the manager in the CRM and in
          // the email immediately
          // understand that it is a courier to an address, not a branch.
          : `Address delivery — ${form.city}, ${form.street}, house ${form.house.trim()}${form.flat.trim() ? `, apt. ${form.flat.trim()}` : ''}`)

    // Dropshipping parcels: Nova Poshta branch address per supplier (Bradas, ARS…).
    const dropShipments = dropSuppliers.map(s => {
      // With allDrop there are no separate dropshipping blocks — dropshipping ships to the main
      // address.
      const a = form.dropAddrs[s.code]
      const addr = a && a.city ? `${a.city}, ${a.warehouse}` : deliveryAddress
      return { supplier_code: s.code, address: addr }
    })

    try {
      // "On the site" was chosen, but there are no active payment details for this
      // amount (hidden) →
      // effectively we take the "to the phone" branch: success instead of an empty
      // payment page.
      const bankResolved: 'site' | 'contact' =
        form.payment === 'bank_transfer' && bankOption === 'site' && !hasSiteCards ? 'contact' : bankOption
      const res = await client.post('/orders/create/', {
        customer_name: form.name,
        customer_phone: form.phone ? '+38' + toLocalDigits(form.phone) : '',
        customer_email: form.email,
        delivery_type: 'nova_poshta',
        delivery_address: deliveryAddress,
        drop_delivery_address: dropShipments[0]?.address || '',
        drop_shipments: dropShipments,
        payment_method: form.payment,
        // The customer asks to send the payment details to the phone → a separate queue for the
        // manager in the CRM.
        payment_contact_requested: form.payment === 'bank_transfer' && bankResolved === 'contact',
        comment: form.comment,
        // Visit source (ad utm) - for the Analyst; without tags we don't send the field.
        attribution: getAttribution(),
        items: items.map(i => ({
          variant_id: i.variantId,
          slug: i.productSlug,
          is_bundle: i.isBundle || false,
          is_course: i.kind === 'course',
          qty: i.qty,
          price: roundPrice(i.price),
        })),
      })
      // Order snapshot for the purchase event: read on success/payment
      // by a separate load (including after the redirect to Monobank). We save it
      // BEFORE clearCart, while items are still intact.
      saveCheckoutSnapshot(String(res.data.order_number), toAnalyticsItems(items), Math.round(total))
      // Monobank — redirect to the payment page.
      // Clear the cart ONLY if payment_url was received and the redirect is guaranteed.
      if (form.payment === 'monobank') {
        if (res.data.payment_url) {
          clearCart()
          window.location.href = res.data.payment_url
        } else {
          // The order is created but payment is unavailable — don't touch the cart.
          setErrors({ submit: 'The order was created, but the payment page could not be opened. Try paying later in the “My orders” section.' })
          setLoading(false)
        }
        return
      }

      clearCart()

      if (form.payment === 'bank_transfer') {
        if (bankResolved === 'site') {
          // t — guest access token for payment actions (IDOR protection);
          // email — so that on the payment page we can offer the guest to create an account
          // and not lose the order (otherwise it disappears after the tab is closed).
          const tokenQ = res.data.token ? `&t=${encodeURIComponent(res.data.token)}` : ''
          const emailQ = form.email ? `&email=${encodeURIComponent(form.email)}` : ''
          navigate(`/checkout/payment?order_id=${res.data.order_number}&total=${Math.round(total)}${tokenQ}${emailQ}`)
        } else {
          const emailQ = form.email ? `&email=${encodeURIComponent(form.email)}` : ''
          navigate(`/checkout/success?order_id=${res.data.order_number}&contact=1${emailQ}`)
        }
        return
      }

      setSuccess(res.data.order_number)
    } catch (err) {
      // Parse DRF validation (400/422) → field errors + a general one.
      const ax = err as { response?: { status?: number; data?: unknown } }
      const status = ax.response?.status
      const data = ax.response?.data
      const next: Record<string, string> = {}

      // Map of backend keys to our form fields.
      const fieldMap: Record<string, string> = {
        customer_name: 'name',
        customer_phone: 'phone',
        customer_email: 'email',
        delivery_address: 'warehouse',
        payment_method: 'payment',
      }

      if (data && typeof data === 'object' && !Array.isArray(data)) {
        for (const [key, val] of Object.entries(data as Record<string, unknown>)) {
          const msg = Array.isArray(val) ? String(val[0] ?? '') : typeof val === 'string' ? val : ''
          if (!msg) continue
          // General backend errors (`error`/`detail`/`non_field_errors`) are not a form field —
          // show them as a general message, not "Check the highlighted
          // fields".
          if (key === 'error' || key === 'detail' || key === 'non_field_errors') {
            next.submit = localizeOrderError(msg)
          } else {
            next[fieldMap[key] ?? key] = msg
          }
        }
      }

      if (Object.keys(next).length === 0) {
        next.submit = status === 503
          ? 'The service is temporarily unavailable. Try again in a minute.'
          : status && status >= 500
            ? 'Server error. Please try again.'
            : 'Error while sending. Check your details and try again.'
      } else if (!next.submit) {
        next.submit = 'Check the highlighted fields'
      }
      setErrors(next)
    } finally {
      setLoading(false)
      submittingRef.current = false
    }
  }

  if (success) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-12 sm:py-20 text-center">
        <div className="w-16 h-16 bg-brand-bg rounded-full flex items-center justify-center mx-auto mb-6">
          <Check size={32} className="text-brand" />
        </div>
        <h1 className="text-xl sm:text-2xl font-bold text-gray-800 mb-2">Thank you for your order!</h1>
        <p className="text-text-muted mb-1">Order number: <span className="font-bold text-gray-800">#{success}</span></p>
        <p className="text-gray-400 text-sm mb-8">
          {onlyCourses
            ? 'Once the payment is credited, the course will appear in the “My courses” section.'
            : hasCourses
              // Mixed order: products ship by mail, the course goes straight to the account.
              // Previously this was silent, and course buyers simply didn't know where to go.
              ? "We will contact you about the delivery of the goods. Once the payment is credited, the course will appear in the “My courses” section."
              : "We will contact you shortly"}
        </p>
        <div className="flex flex-wrap items-center justify-center gap-3">
          {hasCourses && (
            <Button as={Link} to="/my-courses">
              To my courses
            </Button>
          )}
          <Button as={Link} to="/catalog" variant={hasCourses ? 'secondary' : 'primary'}>
            Continue shopping
          </Button>
        </div>
      </div>
    )
  }

  if (items.length === 0) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-12 sm:py-20 text-center">
        <p className="text-text-muted mb-4">Your cart is empty</p>
        <Link to="/catalog" className="text-brand font-bold hover:underline">To the catalog</Link>
      </div>
    )
  }

  return (
    <div className="bg-surface-muted">
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
      {/* Breadcrumb */}
      <nav className="flex items-center gap-1 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        <ChevronRight size={14} />
        <span className="text-gray-600">Checkout</span>
      </nav>

      <form onSubmit={submit}>
        <div className="flex gap-5 lg:gap-8 items-start flex-col lg:flex-row">

          {/* Left column — form */}
          <div className="w-full lg:flex-1 flex flex-col gap-5 sm:gap-6">

            {/* Contact details */}
            <Card variant="section" className="p-5 sm:p-6">
              <h2 className="font-bold text-lg text-text-primary mb-5">Contact details</h2>
              <div className="flex flex-col gap-4">
                <Input
                  label="Full name"
                  type="text"
                  value={form.name}
                  onChange={e => set('name', e.target.value)}
                  placeholder="Last name, first name, patronymic"
                  error={errors.name}
                />
                <PhoneInput
                  label="Phone"
                  value={form.phone}
                  onChange={v => set('phone', v)}
                  error={errors.phone}
                />
                <Input
                  label="Email"
                  type="email"
                  value={form.email}
                  onChange={e => set('email', e.target.value)}
                  placeholder="email@example.com"
                  error={errors.email}
                />
              </div>
            </Card>

            {/* A course is tied to an account, so the backend returns 401 for a guest. Previously
                this only surfaced on submit as a single line of red text — the buyer
                just left. Now we say it right away and show where to click.
                Authorization is a modal on top of the checkout: navigating to /login would lose
                the already filled name/phone/email and the chosen branch. */}
            {hasCourses && !user && (
              <Card variant="section" className="p-5 sm:p-6 border-amber-200 bg-amber-50">
                <h2 className="font-bold text-lg text-text-primary mb-2">Account required</h2>
                <p className="text-sm text-text-muted mb-4">
                  The course opens in your account, so you need to sign in
                  or sign up to buy it. The details you have filled in will stay in place.
                </p>
                <div className="flex flex-wrap gap-3">
                  <Button size="sm" onClick={() => openAuth('login', form.email)}>Sign in</Button>
                  <Button variant="secondary" size="sm" onClick={() => openAuth('register', form.email)}>
                    Sign up
                  </Button>
                </div>
              </Card>
            )}

            {/* Delivery — skipped for courses */}
            {onlyCourses ? (
              <Card variant="section" className="p-5 sm:p-6">
                <h2 className="font-bold text-lg text-text-primary mb-2">No delivery needed</h2>
                <p className="text-sm text-text-muted">
                  A course is a digital item. After payment it will appear under{' '}
                  <Link to="/my-courses" className="text-brand font-bold hover:underline">“My courses”</Link>.
                </p>
              </Card>
            ) : (
            <Card variant="section" className="p-5 sm:p-6">
              {/* There is a single carrier — we simply name it in the heading. */}
              <h2 className="font-bold text-lg text-text-primary mb-5">Delivery by Nova Poshta</h2>
              {/* A courier address cannot be given to a dropshipping parcel (the supplier ships
                  only to a branch) — in address mode there is no choice, parcels are always separate. */}
              {hasDrop && (
                <p className="text-xs text-text-muted -mt-3 mb-5">
                  Some parcels have restrictions on branches.
                </p>
              )}
              {mixedShipment && form.npMode === 'warehouse' && (
                <label className="flex items-start gap-2.5 cursor-pointer mb-5">
                  <input
                    type="checkbox"
                    checked={sameWarehouse}
                    onChange={e => setSameWarehouse(e.target.checked)}
                    className="mt-0.5 shrink-0 accent-brand"
                  />
                  <span className="text-sm text-text-secondary">All parcels to one branch</span>
                </label>
              )}
              {mixedShipment && !shareWarehouse && (
                <p className="font-bold text-base text-text-primary mb-3">Parcel 1</p>
              )}

              <div className="flex flex-col gap-4">
                <div>
                  <label className="block text-sm font-bold text-text-secondary mb-1.5">City</label>
                  <CitySearch value={form.city} onChange={(val, ref) => setForm(f => ({ ...f, city: val, cityRef: ref, warehouse: '', street: '', house: '', flat: '' }))} />
                  {errors.city && <p className="text-xs text-danger mt-1">{errors.city}</p>}
                </div>
                {form.cityRef && (
                  <>
                    {/* Nova Poshta method: to a branch or by courier to an address. For pure dropshipping
                        (allDrop) there is no address delivery — the supplier ships only to a branch. */}
                    {!allDrop && (
                      <div className="flex bg-surface-muted rounded-2xl p-1">
                        {(['warehouse', 'address'] as const).map(m => (
                          <button key={m} type="button"
                            onClick={() => setForm(f => ({ ...f, npMode: m, warehouse: '', street: '', house: '', flat: '' }))}
                            className={`flex-1 py-2 rounded-xl text-sm font-bold transition-colors ${form.npMode === m ? 'bg-white text-gray-800 shadow-sm' : 'text-gray-400 hover:text-gray-600'}`}>
                            {m === 'warehouse' ? 'Branch' : 'Address delivery'}
                          </button>
                        ))}
                      </div>
                    )}
                    {(allDrop || form.npMode === 'warehouse') ? (
                      <div>
                        <label className="block text-sm font-bold text-text-secondary mb-1.5">
                          {ownMode === 'cargo' ? 'Cargo branch' : ownMode === 'branch' ? 'Branch' : 'Branch / Parcel locker'}
                        </label>
                        <WarehouseSearch warehouses={warehouses} value={form.warehouse} onChange={val => set('warehouse', val)} loading={warehousesLoading} error={warehousesError} mode={ownMode} />
                        {errors.warehouse && <p className="text-xs text-danger mt-1">{errors.warehouse}</p>}
                      </div>
                    ) : (
                      <>
                        <div>
                          <label className="block text-sm font-bold text-text-secondary mb-1.5">Street</label>
                          <StreetSearch cityRef={form.cityRef} value={form.street} onChange={val => set('street', val)} />
                          {errors.street && <p className="text-xs text-danger mt-1">{errors.street}</p>}
                        </div>
                        <div className="flex gap-3">
                          <div className="flex-1">
                            <label className="block text-sm font-bold text-text-secondary mb-1.5">House</label>
                            <input type="text" value={form.house} onChange={e => set('house', e.target.value)}
                              placeholder="e.g. 10"
                              className="w-full border border-border rounded-xl px-4 py-3 text-base outline-none focus:border-brand transition-colors" />
                            {errors.house && <p className="text-xs text-danger mt-1">{errors.house}</p>}
                          </div>
                          <div className="flex-1">
                            <label className="block text-sm font-bold text-text-secondary mb-1.5">Apartment <span className="font-normal text-text-secondary/70">(optional)</span></label>
                            <input type="text" value={form.flat} onChange={e => set('flat', e.target.value)}
                              placeholder="e.g. 5"
                              className="w-full border border-border rounded-xl px-4 py-3 text-base outline-none focus:border-brand transition-colors" />
                          </div>
                        </div>
                      </>
                    )}
                  </>
                )}
              </div>

              {mixedShipment && !shareWarehouse && (
                <div className="mt-6 flex flex-col gap-6">
                  {dropSuppliers.map((s, idx) => (
                    <DropSupplierPicker
                      key={s.code}
                      index={idx + 2}
                      supplier={s}
                      value={form.dropAddrs[s.code] || { city: '', cityRef: '', warehouse: '' }}
                      // A manual edit = parcels are no longer identical → uncheck the checkbox,
                      // otherwise the sync effect would immediately overwrite what was entered.
                      onChange={v => {
                        setSameWarehouse(false)
                        setForm(f => ({ ...f, dropAddrs: { ...f.dropAddrs, [s.code]: v } }))
                      }}
                      error={errors[`drop_${s.code}`]}
                    />
                  ))}
                </div>
              )}
            </Card>
            )}

            {/* Payment */}
            <Card variant="section" className="p-5 sm:p-6">
              <h2 className="font-bold text-lg text-text-primary mb-5">Payment</h2>
              <div className="flex flex-col gap-3">
                <label className={`flex items-center gap-4 p-4 border rounded-xl transition-colors ${form.payment === 'monobank' ? 'border-gray-300 bg-gray-50 cursor-default' : 'border-gray-200 hover:bg-gray-100 cursor-pointer'}`}>
                  <input type="radio" name="payment" value="monobank" checked={form.payment === 'monobank'}
                    onChange={() => set('payment', 'monobank')} className="accent-gray-600" />
                  <div>
                    <p className="text-sm font-bold text-gray-800">Monobank</p>
                    <p className="text-xs text-gray-400">Online card payment or Apple/Google Pay</p>
                  </div>
                </label>
                <label className={`flex items-center gap-4 p-4 border rounded-xl transition-colors ${form.payment === 'bank_transfer' ? 'border-gray-300 bg-gray-50 cursor-default' : 'border-gray-200 hover:bg-gray-100 cursor-pointer'}`}>
                  <input type="radio" name="payment" value="bank_transfer" checked={form.payment === 'bank_transfer'}
                    onChange={() => set('payment', 'bank_transfer')} className="accent-gray-600" />
                  <div>
                    <p className="text-sm font-bold text-gray-800">Card transfer</p>
                    <p className="text-xs text-gray-400">PrivatBank or Monobank</p>
                  </div>
                </label>
                {/* We show the choice only when there is something to choose (>1 option). If
                    the details on the site are hidden — only "to the phone" remains,
                    and the radio is not needed (there is no choice). */}
                {form.payment === 'bank_transfer' && bankOptions.length > 1 && (
                  <div className="flex flex-col gap-2 pl-4 border-l-2 border-gray-100 ml-2">
                    {bankOptions.map(opt => (
                      <label key={opt.value} className={`flex items-center gap-3 p-3 rounded-xl border transition-colors text-sm ${bankOption === opt.value ? 'border-gray-300 bg-gray-50 text-gray-800 cursor-default' : 'border-gray-200 text-gray-500 hover:bg-gray-100 cursor-pointer'}`}>
                        <input type="radio" name="bankOption" value={opt.value} checked={bankOption === opt.value}
                          onChange={() => setBankOption(opt.value as 'site' | 'contact')} className="accent-gray-600" />
                        {opt.label}
                      </label>
                    ))}
                  </div>
                )}
              </div>
            </Card>

            {/* Comment */}
            {!showComment ? (
              <button
                type="button"
                onClick={() => setShowComment(true)}
                className="w-full flex items-center gap-3 bg-surface border border-border rounded-2xl shadow-card-mid px-5 py-4 text-sm font-bold text-text-muted hover:text-text-secondary transition-colors"
              >
                <MessageSquare size={18} className="shrink-0" />
                Add a comment to the order
              </button>
            ) : (
              <Card variant="section" className="p-5 sm:p-6">
                <div className="flex items-center justify-between mb-5">
                  <h2 className="font-bold text-lg text-text-primary">Order comment</h2>
                  <button type="button" onClick={() => { setShowComment(false); set('comment', '') }} className="text-text-muted hover:text-text-secondary transition-colors">
                    <X size={18} />
                  </button>
                </div>
                <textarea value={form.comment} onChange={e => set('comment', e.target.value)}
                  placeholder="Wishes, clarifications..."
                  rows={3}
                  autoFocus
                  className="w-full border border-border rounded-xl px-4 py-3 text-base outline-none focus:border-brand transition-colors resize-none" />
              </Card>
            )}
          </div>

          {/* Right column — summary */}
          <div className="w-full lg:w-96 lg:shrink-0 lg:sticky lg:top-[calc(var(--header-h,80px)+1rem)]">
            <div className="bg-white border border-border rounded-2xl shadow-card-low flex flex-col">
              {/* Header */}
              <div className="px-5 py-4 bg-white border-b border-border rounded-t-2xl shrink-0">
                <h2 className="font-bold text-lg text-text-primary">Your order</h2>
              </div>

              {/* Items */}
              <div className="flex flex-col overflow-y-auto overscroll-contain max-h-[420px] [&>*:last-child]:border-b-0 [&>*:last-child>*:last-child]:border-b-0">
                {(() => {
                  const groupsMap = new Map<ShippingOrigin | 'course', CartItem[]>()
                  for (const it of items) {
                    const key = (it.kind === 'course' ? 'course' : (it.shippingOrigin || 'own')) as ShippingOrigin | 'course'
                    if (!groupsMap.has(key)) groupsMap.set(key, [])
                    groupsMap.get(key)!.push(it)
                  }
                  const physicalGroups = Array.from(groupsMap.entries()).filter(([k]) => k !== 'course') as [ShippingOrigin, CartItem[]][]
                  const showHeaders = physicalGroups.length > 1
                  const renderItem = (item: CartItem) => (
                    <CartItemBase
                      key={item.key}
                      item={item}
                      mode="checkout"
                      onQtyChange={v => updateQty(item.key, v)}
                      onRemove={() => removeItem(item.key)}
                      extra={item.isBundle ? (
                        <BundleComposition slug={item.productSlug} />
                      ) : undefined}
                    />
                  )
                  return (
                    <>
                      {physicalGroups.map(([origin, list], i) => {
                        const isOpen = !collapsedGroups.has(origin)
                        return (
                          <div key={origin}>
                            {showHeaders && (
                              <ShipmentGroupHeader
                                index={i + 1}
                                total={physicalGroups.length}
                                open={isOpen}
                                onToggle={() => toggleGroup(origin)}
                              />
                            )}
                            {(isOpen || !showHeaders) && list.map(renderItem)}
                          </div>
                        )
                      })}
                      {(groupsMap.get('course') || []).map(renderItem)}
                    </>
                  )
                })()}
              </div>

              {/* Footer */}
              <div className="px-5 py-4 border-t border-border bg-white rounded-b-2xl shrink-0">
                <div className="flex justify-between items-center mb-2">
                  <span className="text-base text-text-secondary">Total due:</span>
                  <span className="text-2xl font-bold text-gray-800">{formatPrice(total)} UAH</span>
                </div>

                {!onlyCourses && (
                  <p className="mb-4 text-xs text-text-muted leading-relaxed">
                    Delivery — by Nova Poshta, paid by the recipient
                  </p>
                )}

                <label className="flex items-start gap-2.5 cursor-pointer mb-4">
                  <input
                    type="checkbox"
                    checked={consent}
                    onChange={e => { setConsent(e.target.checked); setErrors(prev => ({ ...prev, consent: '' })) }}
                    className="mt-0.5 shrink-0 accent-brand"
                  />
                  <span className="text-xs text-text-muted leading-relaxed">
                    I agree to the{' '}
                    <a href="/privacy" target="_blank" className="text-brand hover:underline">Privacy Policy</a>
                    {' '}and{' '}
                    <a href="/offer" target="_blank" className="text-brand hover:underline">Public Offer</a>
                    , and I consent to the processing of my personal data
                  </span>
                </label>
                {errors.consent && <p className="text-xs text-danger mb-3">{errors.consent}</p>}

                {belowMinOrder && (
                  <p className="text-sm text-danger mb-4 text-center leading-snug">
                    Minimum order amount — {formatPrice(minOrderAmount)} UAH.
                  </p>
                )}
                {errors.submit && <p className="text-sm text-danger mb-4 text-center">{errors.submit}</p>}

                <Button type="submit" variant="primary" size="lg" fullWidth loading={loading} disabled={belowMinOrder}>
                  {loading ? 'Sending...' : 'Confirm order'}
                </Button>
              </div>
            </div>
          </div>

        </div>
      </form>
    </div>
    </div>
  )
}
