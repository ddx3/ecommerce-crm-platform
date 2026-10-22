import { createContext, useContext, useState, type ReactNode } from 'react'
import type { StockNotifyKind } from '../api/catalog'

type Panel = 'auth' | 'cart' | 'liked' | 'stock-notify' | 'questionnaire' | null

interface UIContextType {
  panel: Panel
  authTab: 'login' | 'register'
  /** Pre-filled email for the auth form (e.g. a guest saving their order). */
  authEmail: string
  stockNotifyTarget: { id: number; kind: StockNotifyKind } | null
  openAuth: (tab?: 'login' | 'register', email?: string) => void
  openCart: () => void
  openLiked: () => void
  openStockNotify: (id: number, kind?: StockNotifyKind) => void
  openQuestionnaire: () => void
  close: () => void
}

const UIContext = createContext<UIContextType | null>(null)

export function UIProvider({ children }: { children: ReactNode }) {
  const [panel, setPanel] = useState<Panel>(null)
  const [authTab, setAuthTab] = useState<'login' | 'register'>('login')
  const [authEmail, setAuthEmail] = useState('')
  const [stockNotifyTarget, setStockNotifyTarget] = useState<{ id: number; kind: StockNotifyKind } | null>(null)

  const openAuth = (tab: 'login' | 'register' = 'login', email = '') => { setAuthTab(tab); setAuthEmail(email); setPanel('auth') }
  const openCart = () => setPanel('cart')
  const openLiked = () => setPanel('liked')
  const openStockNotify = (id: number, kind: StockNotifyKind = 'variant') => { setStockNotifyTarget({ id, kind }); setPanel('stock-notify') }
  const openQuestionnaire = () => setPanel('questionnaire')
  const close = () => setPanel(null)

  return (
    <UIContext.Provider value={{ panel, authTab, authEmail, stockNotifyTarget, openAuth, openCart, openLiked, openStockNotify, openQuestionnaire, close }}>
      {children}
    </UIContext.Provider>
  )
}

export const useUI = () => {
  const ctx = useContext(UIContext)
  if (!ctx) throw new Error('useUI must be used within UIProvider')
  return ctx
}
