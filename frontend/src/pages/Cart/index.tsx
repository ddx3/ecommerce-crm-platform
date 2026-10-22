import { Link } from 'react-router-dom'
import { ShoppingCart } from 'lucide-react'
import { useCart } from '../../context/CartContext'
import { Card, CartItemBase } from '../../components/ui'
import Button from '../../components/ui/Button'
import { formatPrice } from '../../utils/price'

export default function CartPage() {
  const { items, removeItem, updateQty, total, clearCart, minOrderAmount, belowMinOrder } = useCart()

  if (items.length === 0) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-12 sm:py-20 text-center">
        <div className="w-20 h-20 rounded-full bg-brand-bg text-brand flex items-center justify-center mx-auto mb-6"><ShoppingCart size={36} /></div>
        <h1 className="text-xl sm:text-2xl font-bold text-text-secondary mb-8">Your cart is empty</h1>
        <Button as={Link} to="/catalog">
          Go to the catalog
        </Button>
      </div>
    )
  }

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
      <div className="flex items-center justify-between mb-5 sm:mb-8">
        <h1 className="text-xl sm:text-2xl font-bold text-text-primary">Cart</h1>
        <button onClick={clearCart} className="text-xs sm:text-sm text-text-muted hover:text-danger transition-colors">
          Clear cart
        </button>
      </div>

      <div className="flex gap-5 lg:gap-8 items-start flex-col lg:flex-row">
        {/* Items */}
        <div className="w-full lg:flex-1 flex flex-col gap-3">
          {items.map(item => (
            <CartItemBase
              key={item.key}
              item={item}
              mode="page"
              onQtyChange={v => updateQty(item.key, v)}
              onRemove={() => removeItem(item.key)}
            />
          ))}
        </div>

        {/* Summary */}
        <Card variant="section" className="w-full lg:w-80 lg:shrink-0 p-5 sm:p-6 lg:sticky lg:top-[calc(var(--header-h,80px)+1rem)]">
          <h2 className="font-bold text-lg text-text-primary mb-4">Total</h2>
          <div className="flex justify-between items-center mb-2 text-base text-text-secondary">
            <span>Items:</span>
            <span className="font-bold">{items.reduce((s, i) => s + i.qty, 0)} pcs</span>
          </div>
          <div className="flex justify-between items-center pt-3 border-t border-border mt-3">
            <span className="text-lg font-bold text-text-primary">Total due:</span>
            <span className="text-2xl font-bold text-text-primary">{formatPrice(total)} UAH</span>
          </div>
          {belowMinOrder && (
            <p className="mt-4 text-sm text-danger text-center leading-snug">
              Minimum order amount — {formatPrice(minOrderAmount)} UAH.
            </p>
          )}
          {belowMinOrder ? (
            <Button size="lg" fullWidth disabled className="mt-4">
              Place order
            </Button>
          ) : (
            <Button as={Link} to="/checkout" size="lg" fullWidth className="mt-6">
              Place order
            </Button>
          )}
          <Link to="/catalog" className="mt-3 w-full flex items-center justify-center text-sm text-text-muted hover:text-brand transition-colors py-2">
            Continue shopping
          </Link>
        </Card>
      </div>
    </div>
  )
}
