import { Link } from 'react-router-dom'
import { X, Heart, Trash2, GraduationCap } from 'lucide-react'
import { useUI } from '../context/UIContext'
import { useLiked } from '../context/LikedContext'
import { useCart } from '../context/CartContext'
import { Drawer, IconButton, Button, EmptyState } from './ui'
import BundleComposition from './BundleComposition'
import { roundPrice, formatPrice } from '../utils/price'

export default function LikedDrawer() {
  const { panel, close, openCart } = useUI()
  const { items, toggle, clear } = useLiked()
  const { addItem } = useCart()
  const open = panel === 'liked'

  return (
    <Drawer open={open} onClose={close} ariaLabel="Favorites">
      <div className="flex items-center justify-between px-6 py-5 border-b border-border bg-white shrink-0">
        <div className="flex items-baseline gap-2">
          <span className="font-bold text-lg text-text-primary leading-none">Favorites</span>
          <button
            type="button"
            onClick={clear}
            className={`text-xs text-gray-400 hover:text-danger transition-colors ${items.length > 0 ? '' : 'invisible'}`}
          >
            (clear)
          </button>
        </div>
        <IconButton onClick={close} aria-label="Close favorites">
          <X size={20} />
        </IconButton>
      </div>

      <div className="flex-1 overflow-y-auto overscroll-contain" style={{ direction: 'rtl', overflowAnchor: 'none' }}>
        {items.length === 0 ? (
          <EmptyState
            className="h-full"
            icon={<Heart size={56} />}
            iconClassName="bg-red-50 text-red-500"
            title="No favorites yet"
          />
        ) : (
          <div className="flex flex-col" style={{ direction: 'ltr' }}>
            {items.map(item => {
              const href = item.isCourse
                ? `/course/${item.productSlug}`
                : item.isBundle ? `/bundle/${item.productSlug}` : `/product/${item.productSlug}`
              return (
                <div key={item.key} className="relative px-4 py-3 border-b border-border flex flex-col gap-3">
                  <button
                    type="button"
                    onClick={() => toggle(item)}
                    aria-label={`Remove ${item.variantName} from favorites`}
                    className="absolute bottom-3 right-4 text-text-disabled hover:text-danger transition-colors"
                  >
                    <Trash2 size={18} />
                  </button>
                  <div className="flex gap-3">
                    <Link
                      to={href}
                      onClick={close}
                      className={`group relative block shrink-0 self-start ${item.isCourse ? 'w-28' : 'w-20 h-20'} rounded-xl overflow-hidden bg-white ring-1 ring-border hover:ring-border-strong transition-colors`}
                    >
                      {item.image
                        ? (
                          <div className={item.isCourse ? 'relative w-full' : 'relative w-full h-full'}>
                            <img src={item.image} alt={item.variantName} className={item.isCourse ? 'w-full h-auto block object-contain' : 'w-full h-full object-contain p-1.5'} />
                            <div className="absolute inset-0 bg-black/0 group-hover:bg-black/10 transition-colors duration-300" />
                          </div>
                        )
                        : <div className="w-full h-full bg-white" />
                      }
                    </Link>
                    <div className="flex-1 min-w-0 flex flex-col justify-between self-stretch">
                      <Link
                        to={href}
                        onClick={close}
                        className="text-sm font-bold text-text-primary hover:text-brand transition-colors line-clamp-2 leading-snug block"
                      >
                        {item.isCourse ? (item.productName || item.variantName) : item.variantName}
                      </Link>
                      <div className="flex items-baseline justify-between gap-2 mt-2">
                        <span className="text-xs text-text-muted truncate">{item.isCourse ? '' : (item.sku ?? '')}</span>
                        <p className="text-base font-bold text-gray-900 leading-none">
                          {item.price ? `${formatPrice(item.price)} UAH` : '—'}
                        </p>
                      </div>
                    </div>
                  </div>
                  {item.isCourse && (
                    <p className="text-base text-brand font-bold flex items-center gap-1.5">
                      <GraduationCap size={18} /> Digital course
                    </p>
                  )}
                  {item.isBundle && (
                    <BundleComposition slug={item.productSlug} onClose={close} />
                  )}
                  <div className="flex items-center">
                    <Button
                      variant="primary"
                      size="sm"
                      disabled={!item.price}
                      onClick={() => {
                        if (item.price) {
                          addItem({ productSlug: item.productSlug, productName: item.productName, variantId: item.variantId, variantName: item.variantName, sku: item.sku, price: roundPrice(item.price), image: item.image, isBundle: item.isBundle, ...(item.isCourse ? { kind: 'course' as const } : {}) })
                          openCart()
                        }
                      }}
                    >
                      Add to cart
                    </Button>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>
    </Drawer>
  )
}
