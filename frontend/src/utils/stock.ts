import type { ShippingOrigin } from '../types'

/** Maximum quantity of a variant that can be put in the cart.
 * undefined = no limit. Mirrors the server stock check
 * (backend/orders/views.py): stock is strictly checked ONLY for own
 * variants (shipping_origin='own'). Dropshipping ('bradas') and hybrid ('hybrid')
 * are sold beyond stock (the excess is dropshipped) — no limit there.
 */
export function variantMaxQty(v: { stock?: number | null; shipping_origin?: ShippingOrigin }): number | undefined {
  if (v.shipping_origin === 'bradas' || v.shipping_origin === 'hybrid') return undefined
  return Math.max(0, v.stock ?? 0)
}

/** Whether a variant is available for purchase on the storefront.
 * Hybrid (50/50) is ALWAYS in stock (own stock ran out → we'll ship the rest via dropshipping).
 * own and pure dropshipping ('bradas') — only when stock>0: for dropshipping, stock = the
 * supplier's
 * stock level (synced from bradas), so 0 = nowhere to buy → "out of stock".
 * Mirrors the server's get_in_stock.
 */
export function variantInStock(v: { stock?: number | null; shipping_origin?: ShippingOrigin }): boolean {
  if (v.shipping_origin === 'hybrid') return true
  return (v.stock ?? 0) > 0
}
