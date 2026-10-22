import axios from 'axios'
import client from './client'
import { getAntibotToken, TOKEN_FIELD, HONEYPOT_FIELD } from './antibot'

const BASE = '/api/auth'

// Extract the error text from an axios response (the backend returns {error}) or from an Error.
function apiErr(e: unknown, fallback: string): Error {
  if (axios.isAxiosError(e)) return new Error(e.response?.data?.error || fallback)
  return e instanceof Error ? e : new Error(fallback)
}

// These calls go through fetch (not through the axios client), so the anti-bot fields
// are added manually.
async function withAntibot<T extends object>(data: T): Promise<T & Record<string, string>> {
  const token = await getAntibotToken()
  return { ...data, [HONEYPOT_FIELD]: '', ...(token ? { [TOKEN_FIELD]: token } : {}) }
}

export type Gender = 'female' | 'male' | 'na' | ''
export type ReferralSource =
  | 'friends' | 'social' | 'google' | 'youtube' | 'ad' | 'other' | ''
export type ReferralSocial =
  | 'instagram' | 'facebook' | 'tiktok' | 'other' | ''
export type Interest =
  | 'seeds' | 'seedlings' | 'fertilizers' | 'protection'
  | 'tools' | 'indoor' | 'lawn' | 'other'

export interface AuthUser {
  id: number
  email: string
  phone: string
  first_name: string
  last_name: string
  np_city: string
  np_city_ref: string
  np_warehouse: string
  // Questionnaire
  gender?: Gender
  birth_date?: string | null   // 'YYYY-MM-DD'
  gardening_text?: string
  interests?: Interest[]
  referral_source?: ReferralSource
  referral_social?: ReferralSocial
  referral_other?: string
  wishes?: string
  questionnaire_dismissed_at?: string | null
  questionnaire_filled_count?: number
  is_staff?: boolean
  is_superuser?: boolean
}

export interface AuthResponse {
  user: AuthUser
  access: string
  refresh: string
}

export async function apiRegister(data: {
  email: string
  password: string
  name: string
  phone: string
}): Promise<AuthResponse> {
  const res = await fetch(`${BASE}/register/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(await withAntibot(data)),
  })
  const json = await res.json()
  if (!res.ok) throw new Error(json.error || 'Registration error')
  return json
}

export async function apiLogin(data: {
  identifier: string
  password: string
}): Promise<AuthResponse> {
  const res = await fetch(`${BASE}/login/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  })
  const json = await res.json()
  if (!res.ok) throw new Error(json.error || 'Sign-in error')
  return json
}

export async function apiMe(): Promise<AuthUser> {
  // Via the axios client: the request interceptor adds the token, auto-refresh on 401.
  const res = await client.get('/auth/me/')
  return res.data
}

/** Response to a profile change: when the password changes, the backend attaches a fresh token pair. */
export type ProfileUpdateResponse = AuthUser & {
  _tokens?: { access: string; refresh: string }
}

export async function apiUpdateProfile(
  data: Partial<AuthUser & {
    new_password: string
    questionnaire_dismissed: boolean
    questionnaire_snooze: number
  }>,
): Promise<ProfileUpdateResponse> {
  try {
    const res = await client.patch('/auth/profile/', data)
    return res.data
  } catch (e) {
    throw apiErr(e, 'Save error')
  }
}

/** Closes the questionnaire pop-up: mode='forever' — never show again, 'snooze7' — for 7 days. */
export async function apiDismissQuestionnaire(mode: 'forever' | 'snooze7'): Promise<AuthUser> {
  const payload =
    mode === 'forever'
      ? { questionnaire_dismissed: true }
      : { questionnaire_snooze: 7 }
  return apiUpdateProfile(payload)
}

export interface OrderItem {
  /** Only for bundle items (for downloading the PDF instruction). */
  id?: number
  name: string
  sku: string
  slug: string
  is_bundle: boolean
  /** A course is a digital item: it leads not to the catalog but to "My courses". */
  is_course?: boolean
  course_slug?: string
  quantity: number
  price: string
  image: string
  composition?: { name: string; sku: string; slug: string; quantity: number; image: string }[]
  /** An electronic instruction exists for exactly the composition that arrived in this order. */
  has_instruction?: boolean
}

export interface UserOrder {
  id: number
  order_number: string
  status: string
  status_display: string
  payment_status: string
  payment_status_display: string
  payment_method: string
  delivery_type: string
  delivery_type_display: string
  delivery_address: string
  comment: string
  total_price: string
  created_at: string
  items: OrderItem[]
}

export async function apiPasswordResetRequest(email: string): Promise<void> {
  const res = await fetch(`${BASE}/password-reset/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(await withAntibot({ email })),
  })
  const json = await res.json()
  if (!res.ok) throw new Error(json.error || 'Error')
}

export async function apiPasswordResetConfirm(uid: string, token: string, password: string): Promise<void> {
  const res = await fetch(`${BASE}/password-reset/confirm/`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ uid, token, password }),
  })
  const json = await res.json()
  if (!res.ok) throw new Error(json.error || 'Error')
}

export async function apiMyOrders(): Promise<UserOrder[]> {
  const res = await client.get('/orders/my/')
  return res.data
}

/** Opens the PDF instruction of a bundle item in a new tab (it can also be printed there). */
export async function apiOpenOrderInstruction(orderNumber: string, itemId: number): Promise<void> {
  const res = await client.get(`/orders/my/${orderNumber}/instruction/${itemId}/`, { responseType: 'blob' })
  const url = URL.createObjectURL(res.data)
  window.open(url, '_blank')
  // The object URL stays alive while the tab is open; we release it with a small margin.
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

/** Changes the payment method of a still unpaid order. */
export async function apiChangePaymentMethod(orderNumber: string, paymentMethod: string): Promise<void> {
  try {
    await client.post(`/orders/${orderNumber}/payment-method/`, { payment_method: paymentMethod })
  } catch (e) {
    throw apiErr(e, 'Could not change the payment method')
  }
}
