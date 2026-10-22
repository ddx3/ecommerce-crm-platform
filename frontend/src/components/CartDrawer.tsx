import { useState } from 'react'
import { Link } from 'react-router-dom'
import { X, ShoppingCart } from 'lucide-react'
import { useUI } from '../context/UIContext'
import { useCart } from '../context/CartContext'
import { Drawer, IconButton, EmptyState, CartItemBase } from './ui'
import ShipmentGroupHeader from './ShipmentGroupHeader'
import BundleComposition from './BundleComposition'
import type { ShippingOrigin } from '../types'
import type { CartItem } from '../context/CartContext'
import { formatPrice } from '../utils/price'

export default function CartDrawer() {
  const { panel, close } = useUI()
  const { items, removeItem, updateQty, total, clearCart, minOrderAmount, belowMinOrder } = useCart()
  const open = panel === 'cart'
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set())
  const toggleGroup = (key: string) => {
    setCollapsedGroups(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <Drawer open={open} onClose={close} ariaLabel="Cart">
      <div className="flex items-center justify-between px-6 py-5 border-b border-border bg-white shrink-0">
        <div className="flex items-baseline gap-2">
          <span className="font-bold text-lg text-text-primary leading-none">Cart</span>
          <button
            type="button"
            onClick={clearCart}
            className={`text-xs text-gray-400 hover:text-danger transition-colors ${items.length > 0 ? '' : 'invisible'}`}
          >
            (clear)
          </button>
        </div>
        <IconButton onClick={close} aria-label="Close cart">
          <X size={20} />
        </IconButton>
      </div>

      <div className="flex-1 overflow-y-auto overscroll-contain" style={{ direction: 'rtl', overflowAnchor: 'none' }}>
        {items.length === 0 ? (
          <EmptyState
            className="h-full"
            icon={<ShoppingCart size={56} />}
            title="Your cart is empty"
            action={
              <Link
                to="/catalog"
                onClick={close}
                className="inline-flex items-center justify-center bg-brand text-white font-bold px-6 py-3 rounded-2xl hover:bg-brand-hover active:bg-brand-active transition-colors"
              >
                To the catalog
              </Link>
            }
          />
        ) : (
          <div className="flex flex-col" style={{ direction: 'ltr' }}>
            {(() => {
              // Group items by shippingOrigin (courses form a separate "virtual" group).
              const groupsMap = new Map<ShippingOrigin | 'course', CartItem[]>()
              for (const it of items) {
                const key = (it.kind === 'course' ? 'course' : (it.shippingOrigin || 'own')) as ShippingOrigin | 'course'
                if (!groupsMap.has(key)) groupsMap.set(key, [])
                groupsMap.get(key)!.push(it)
              }
              // Courses are not counted as a parcel (intangible) — only physical items are counted.
              const physicalGroups = Array.from(groupsMap.entries()).filter(([k]) => k !== 'course') as [ShippingOrigin, CartItem[]][]
              const showHeaders = physicalGroups.length > 1
              const renderItem = (item: CartItem) => (
                <CartItemBase
                  key={item.key}
                  item={item}
                  mode="drawer"
                  onQtyChange={v => updateQty(item.key, v)}
                  onRemove={() => removeItem(item.key)}
                  onLinkClick={close}
                  extra={item.isBundle ? (
                    <BundleComposition slug={item.productSlug} onClose={close} />
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
                  {/* Courses — at the end, without a header */}
                  {(groupsMap.get('course') || []).map(renderItem)}
                </>
              )
            })()}
          </div>
        )}
      </div>

      {items.length > 0 && (
        <div className="shrink-0 px-6 py-5 border-t border-border bg-white">
          <div className="flex justify-between items-center mb-2">
            <span className="text-base text-text-secondary">Total due:</span>
            <span className="text-2xl font-bold text-text-primary">{formatPrice(total)} UAH</span>
          </div>
          {belowMinOrder && (
            <p className="mb-3 text-sm text-danger text-center leading-snug">
              Minimum order amount — {formatPrice(minOrderAmount)} UAH.
            </p>
          )}
          {belowMinOrder ? (
            <span
              aria-disabled="true"
              className="w-full h-13 inline-flex items-center justify-center bg-gray-300 text-white font-bold rounded-2xl text-base cursor-not-allowed select-none"
            >
              Place order
            </span>
          ) : (
            <Link
              to="/checkout"
              onClick={close}
              className="w-full h-13 inline-flex items-center justify-center bg-brand text-white font-bold rounded-2xl hover:bg-brand-hover active:bg-brand-active transition-colors text-base focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/50"
            >
              Place order
            </Link>
          )}
        </div>
      )}
    </Drawer>
  )
}
