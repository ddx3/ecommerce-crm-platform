import { createContext, useContext, useState, useEffect, type ReactNode } from 'react'
import client from '../api/client'
import { DEFAULT_MIN_ORDER_AMOUNT } from '../utils/price'

export interface PaymentCardInfo {
  bank: string
  card: string
  name: string
  /** The card is shown to the customer if the order total is not less than this threshold. */
  threshold: number
}

interface SiteSettingsContextType {
  /** Minimum order amount (UAH) — set in CRM → Settings. */
  minOrderAmount: number
  /** Payment details as a flat list (active only, each with its own threshold). null until loaded. */
  paymentCards: PaymentCardInfo[] | null
  /** All group thresholds (including hidden ones) — to determine the needed threshold by the amount. */
  thresholds: number[]
  /** Settings have been loaded from the backend (otherwise the defaults apply). */
  loaded: boolean
}

const SiteSettingsContext = createContext<SiteSettingsContextType | null>(null)

export function SiteSettingsProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SiteSettingsContextType>({
    minOrderAmount: DEFAULT_MIN_ORDER_AMOUNT,
    paymentCards: null,
    thresholds: [],
    loaded: false,
  })

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const { data } = await client.get('/site-settings/')
        if (cancelled) return
        setState({
          minOrderAmount: Number(data.min_order_amount) || DEFAULT_MIN_ORDER_AMOUNT,
          paymentCards: Array.isArray(data.payment_cards) ? data.payment_cards : [],
          thresholds: Array.isArray(data.thresholds) ? data.thresholds : [],
          loaded: true,
        })
      } catch {
        // Backend unavailable — keep the defaults, but treat it as "loaded"
        // so the UI does not hang in a waiting state.
        if (!cancelled) setState(s => ({ ...s, loaded: true }))
      }
    })()
    return () => { cancelled = true }
  }, [])

  return <SiteSettingsContext.Provider value={state}>{children}</SiteSettingsContext.Provider>
}

export const useSiteSettings = () => {
  const ctx = useContext(SiteSettingsContext)
  if (!ctx) throw new Error('useSiteSettings must be used within SiteSettingsProvider')
  return ctx
}
