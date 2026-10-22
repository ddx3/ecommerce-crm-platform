import { useState, useRef, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { User, ChevronRight } from 'lucide-react'
import { useAuth } from '../../context/AuthContext'
import { useUI } from '../../context/UIContext'
import { apiUpdateProfile } from '../../api/auth'
import client from '../../api/client'
import { Card } from '../../components/ui'
import Button from '../../components/ui/Button'
import ProfileQuestionnaire from '../../components/ProfileQuestionnaire'

interface NPCity { Ref: string; Present: string }
interface NPWarehouse { Ref: string; Description: string }

function useCities(query: string) {
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

function useWarehouses(cityRef: string) {
  const [warehouses, setWarehouses] = useState<NPWarehouse[]>([])
  useEffect(() => {
    if (!cityRef) { setWarehouses([]); return }
    client.get('/nova-poshta/', { params: { method: 'getWarehouses', city_ref: cityRef } })
      .then(r => setWarehouses(r.data?.data || []))
      .catch(() => setWarehouses([]))
  }, [cityRef])
  return warehouses
}

function CitySearch({ value, onChange }: { value: string; onChange: (val: string, ref: string) => void }) {
  const [query, setQuery] = useState(value)
  const [open, setOpen] = useState(false)
  const { cities, loading } = useCities(query)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => { setQuery(value) }, [value])
  useEffect(() => {
    const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])

  return (
    <div ref={ref} className="relative">
      <div className="relative">
        <input type="text" value={query}
          onChange={e => { setQuery(e.target.value); setOpen(true); onChange('', '') }}
          onFocus={() => cities.length > 0 && setOpen(true)}
          placeholder="Start typing a city..."
          className="w-full border border-gray-200 rounded-xl px-4 py-3 pr-10 text-base outline-none focus:border-brand transition-colors"
        />
        {loading && <div className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 border-2 border-gray-200 border-t-brand rounded-full animate-spin" />}
      </div>
      {open && cities.length > 0 && (
        <div className="absolute top-full left-0 right-0 mt-1 bg-white border border-gray-200 rounded-xl shadow-lg z-[70] overflow-hidden">
          <div className="max-h-56 overflow-y-auto overscroll-contain">
            {cities.map(city => (
              <button key={city.Ref} type="button"
                onClick={() => { setQuery(city.Present); onChange(city.Present, city.Ref); setOpen(false) }}
                className="w-full text-left px-4 py-2.5 text-sm hover:bg-brand-bg hover:text-brand transition-colors">
                {city.Present}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function WarehouseSearch({ warehouses, value, onChange }: { warehouses: NPWarehouse[]; value: string; onChange: (val: string) => void }) {
  const [query, setQuery] = useState(value)
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const filtered = query.trim() ? warehouses.filter(w => w.Description.toLowerCase().includes(query.toLowerCase())) : warehouses

  useEffect(() => { setQuery(value) }, [value])
  useEffect(() => {
    const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false) }
    document.addEventListener('mousedown', h)
    return () => document.removeEventListener('mousedown', h)
  }, [])

  if (warehouses.length === 0) return null

  return (
    <div ref={ref} className="relative">
      <input type="text" value={query}
        onChange={e => { setQuery(e.target.value); setOpen(true); onChange('') }}
        onFocus={() => setOpen(true)}
        placeholder="Branch or parcel locker..."
        className="w-full border border-gray-200 rounded-xl px-4 py-3 text-base outline-none focus:border-brand transition-colors"
      />
      {open && filtered.length > 0 && (
        <div className="absolute top-full left-0 right-0 mt-1 bg-white border border-gray-200 rounded-xl shadow-lg z-[70] overflow-hidden">
          <div className="max-h-56 overflow-y-auto overscroll-contain">
            {filtered.slice(0, 300).map(w => (
              <button key={w.Ref} type="button"
                onClick={() => { setQuery(w.Description); onChange(w.Description); setOpen(false) }}
                className="w-full text-left px-4 py-2.5 text-sm hover:bg-brand-bg hover:text-brand transition-colors">
                {w.Description}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

export default function ProfilePage() {
  const { user, token, setAuth } = useAuth()
  const { openAuth } = useUI()
  const [form, setForm] = useState({
    pib: [user?.first_name, user?.last_name].filter(Boolean).join(' '),
    email: user?.email || '',
    phone: (user?.phone || '').replace(/^\+38/, '').replace(/\D/g, '').replace(/^(\d{3})(\d{3})(\d{2})(\d{2})$/, '$1 $2 $3 $4').trim(),
    np_city: user?.np_city || '',
    np_city_ref: user?.np_city_ref || '',
    np_warehouse: user?.np_warehouse || '',
    new_password: '',
    confirm_password: '',
  })
  const [loading, setLoading] = useState(false)
  const [success, setSuccess] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (user) {
      setForm(f => ({
        ...f,
        pib: [user.first_name, user.last_name].filter(Boolean).join(' '),
        email: user.email,
        phone: (user.phone || '').replace(/^\+38/, '').replace(/\D/g, '').replace(/^(\d{3})(\d{3})(\d{2})(\d{2})$/, '$1 $2 $3 $4').trim(),
        np_city: user.np_city,
        np_city_ref: user.np_city_ref,
        np_warehouse: user.np_warehouse,
      }))
    }
  }, [user])

  const warehouses = useWarehouses(form.np_city_ref)

  const set = (k: string, v: string) => { setForm(f => ({ ...f, [k]: v })); setSuccess(false); setError('') }

  // Whether the form differs from the saved data — so "Save" isn't offered without
  // changes.
  const fmtPhone = (p?: string | null) => (p || '').replace(/^\+38/, '').replace(/\D/g, '').replace(/^(\d{3})(\d{3})(\d{2})(\d{2})$/, '$1 $2 $3 $4').trim()
  const dirty = !!form.new_password || !!form.confirm_password || (!!user && (
    form.pib !== [user.first_name, user.last_name].filter(Boolean).join(' ') ||
    form.email !== (user.email || '') ||
    form.phone !== fmtPhone(user.phone) ||
    form.np_city !== (user.np_city || '') ||
    form.np_warehouse !== (user.np_warehouse || '')
  ))

  const handlePhone = (v: string) => {
    const digits = v.replace(/\D/g, '').slice(0, 10)
    let formatted = digits
    if (digits.length > 3) formatted = digits.slice(0, 3) + ' ' + digits.slice(3)
    if (digits.length > 6) formatted = formatted.slice(0, 7) + ' ' + formatted.slice(7)
    if (digits.length > 8) formatted = formatted.slice(0, 10) + ' ' + formatted.slice(10)
    set('phone', formatted)
  }

  const handleSubmit = async () => {
    setError(''); setSuccess(false)
    if ((form.new_password || form.confirm_password) && form.new_password !== form.confirm_password) {
      setError('Passwords do not match'); return
    }
    if (!token) return
    setLoading(true)
    try {
      const parts = form.pib.trim().split(/\s+/)
      const data: any = {
        first_name: parts[0] || '', last_name: parts.slice(1).join(' '),
        email: form.email.trim().toLowerCase(),
        phone: form.phone ? `+38${form.phone.replace(/\s/g, '')}` : '',
        np_city: form.np_city, np_city_ref: form.np_city_ref,
        np_warehouse: form.np_warehouse,
      }
      if (form.new_password) data.new_password = form.new_password
      const { _tokens, ...updated } = await apiUpdateProfile(data)
      // Password change → the backend returned a fresh token pair (old sessions invalidated).
      if (_tokens) setAuth(updated, _tokens.access, _tokens.refresh)
      else setAuth(updated)
      setForm(f => ({ ...f, new_password: '', confirm_password: '' }))
      setSuccess(true)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  // While the token is being validated (token exists but user isn't loaded yet) — a spinner.
  if (token && !user) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-12 sm:py-20 flex justify-center">
        <div className="w-10 h-10 border-4 border-gray-200 border-t-brand rounded-full animate-spin" />
      </div>
    )
  }

  if (!user) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
        <nav className="flex items-center gap-1 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6">
          <Link to="/" className="hover:text-brand transition-colors">Home</Link>
          <ChevronRight size={14} />
          <span className="text-gray-600">Personal details</span>
        </nav>
        <h1 className="text-xl sm:text-2xl font-bold text-gray-900 mb-6 sm:mb-8">Personal details</h1>
        <div className="flex flex-col items-center justify-center py-32 text-center">
          <div className="w-20 h-20 rounded-full bg-brand-bg flex items-center justify-center mb-5">
            <User size={36} className="text-brand" />
          </div>
          <p className="text-xl font-bold text-gray-700 mb-6">Sign in to your account</p>
          <Button onClick={() => openAuth('login')}>
            Sign in
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
      <nav className="flex items-center gap-1 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        <ChevronRight size={14} />
        <span className="text-gray-600">Personal details</span>
      </nav>
      <h1 className="text-xl sm:text-2xl font-bold text-gray-900 mb-6 sm:mb-8">Personal details</h1>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 sm:gap-6 items-start">

        {/* Left column — personal details + password */}
        <div className="flex flex-col gap-5 sm:gap-6">
          <Card variant="section" className="p-5 sm:p-6 flex flex-col gap-5">
            <p className="font-bold text-gray-700">Personal details</p>
            <div>
              <label className="block text-sm font-bold text-gray-500 mb-1.5">Full name</label>
              <input type="text" value={form.pib} onChange={e => set('pib', e.target.value)}
                placeholder="Full name"
                className="w-full border border-gray-200 rounded-xl px-4 py-3 text-base outline-none focus:border-brand transition-colors" />
            </div>
            <div>
              <label className="block text-sm font-bold text-gray-500 mb-1.5">Email</label>
              <input type="email" value={form.email} onChange={e => set('email', e.target.value)}
                className="w-full border border-gray-200 rounded-xl px-4 py-3 text-base outline-none focus:border-brand transition-colors" />
            </div>
            <div>
              <label className="block text-sm font-bold text-gray-500 mb-1.5">Phone number</label>
              <div className="flex items-center border border-border rounded-xl overflow-hidden focus-within:border-brand transition-colors">
                <span className="px-3 py-3 text-base text-gray-500 bg-gray-50 border-r border-gray-200 shrink-0">+38</span>
                <input type="tel" value={form.phone} onChange={e => handlePhone(e.target.value)}
                  placeholder="000 000 00 00"
                  className="flex-1 px-3 py-3 text-base outline-none bg-white" />
              </div>
            </div>
          </Card>

          <Card variant="section" className="p-5 sm:p-6 flex flex-col gap-5">
            <p className="font-bold text-gray-700">Change password</p>
            <div>
              <label className="block text-sm font-bold text-gray-500 mb-1.5">New password</label>
              <input type="password" value={form.new_password} onChange={e => set('new_password', e.target.value)}
                placeholder="Leave empty to keep it unchanged"
                className="w-full border border-gray-200 rounded-xl px-4 py-3 text-base outline-none focus:border-brand transition-colors" />
            </div>
            <div>
              <label className="block text-sm font-bold text-gray-500 mb-1.5">Confirm password</label>
              <input type="password" value={form.confirm_password} onChange={e => set('confirm_password', e.target.value)}
                placeholder="••••••••"
                className={`w-full border rounded-xl px-4 py-3 text-base outline-none focus:border-brand transition-colors ${form.confirm_password && form.confirm_password !== form.new_password ? 'border-red-300' : 'border-gray-200'}`} />
            </div>
          </Card>
        </div>

        {/* Right column — delivery addresses */}
        <div className="flex flex-col gap-5 sm:gap-6">
          <Card variant="section" className="p-5 sm:p-6 flex flex-col gap-5">
            <p className="font-bold text-gray-700">Delivery address</p>
            <div>
              <label className="block text-sm font-bold text-gray-500 mb-1.5">City</label>
              <CitySearch value={form.np_city} onChange={(val, ref) => setForm(f => ({ ...f, np_city: val, np_city_ref: ref, np_warehouse: '' }))} />
            </div>
            {form.np_city_ref && (
              <div>
                <label className="block text-sm font-bold text-gray-500 mb-1.5">Branch / Parcel locker</label>
                <WarehouseSearch warehouses={warehouses} value={form.np_warehouse} onChange={val => set('np_warehouse', val)} />
              </div>
            )}
          </Card>
        </div>

      </div>

      {error && <p className="text-sm text-red-500 mt-4">{error}</p>}
      {success && <p className="text-sm text-brand font-medium mt-4">Saved</p>}

      <Button onClick={handleSubmit} disabled={loading || !dirty} loading={loading} size="lg" className="mt-6">
        Save
      </Button>

      {/* ─── Questionnaire: gardener profile — temporarily hidden ─── */}
      {false && (
        <Card variant="section" className="p-5 sm:p-6 flex flex-col gap-5 mt-5 sm:mt-6">
          <div>
            <p className="font-bold text-gray-700">Gardener profile</p>
            <p className="text-sm text-gray-500 mt-1">
              Fill in as much as you like — it helps us pick better products for you. All fields are optional.
            </p>
          </div>
          <ProfileQuestionnaire mode="inline-card" showProgress />
        </Card>
      )}
    </div>
  )
}
