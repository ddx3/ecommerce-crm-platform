import { Link } from 'react-router-dom'
import { Heart, ChevronRight } from 'lucide-react'
import { useLiked } from '../../context/LikedContext'
import { useCart } from '../../context/CartContext'
import { useUI } from '../../context/UIContext'
import Button from '../../components/ui/Button'
import { roundPrice, formatPrice } from '../../utils/price'

export default function LikedPage() {
  const { items, toggle } = useLiked()
  const { addItem } = useCart()
  const { openCart } = useUI()

  if (items.length === 0) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
        <nav className="flex items-center gap-1 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6">
          <Link to="/" className="hover:text-brand transition-colors">Home</Link>
          <ChevronRight size={14} />
          <span className="text-gray-600">Favorites</span>
        </nav>
        <div className="text-center py-6 sm:py-12">
        <div className="w-20 h-20 rounded-full bg-red-50 text-red-500 flex items-center justify-center mx-auto mb-6"><Heart size={36} /></div>
        <h1 className="text-xl sm:text-2xl font-bold text-text-secondary mb-2">No favorites yet</h1>
        <p className="text-text-muted mb-8">Add products by clicking the heart on a product card</p>
        <Button as={Link} to="/catalog">
          Go to the catalog
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
        <span className="text-gray-600">Favorites</span>
      </nav>
      <h1 className="text-xl sm:text-2xl font-bold text-text-primary mb-5 sm:mb-8">Favorites ({items.length})</h1>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3 sm:gap-4">
        {items.map(item => (
          <div key={item.key} className="bg-surface border border-border rounded-2xl shadow-card-low overflow-hidden hover:shadow-card-hover">
            <Link to={item.isCourse ? `/course/${item.productSlug}` : `/product/${item.productSlug}`}>
              <div className="aspect-square bg-surface-muted overflow-hidden">
                {item.image
                  ? <img src={item.image} alt={item.variantName} className="w-full h-full object-contain p-3" />
                  : <div className="w-full h-full" />
                }
              </div>
              <div className="px-4 pt-3 pb-2">
                <p className="text-sm font-bold text-text-primary line-clamp-2 leading-snug">{item.variantName}</p>
                <p className="font-bold text-base text-gray-900 mt-1">{item.price ? `${formatPrice(item.price)} UAH` : '—'}</p>
              </div>
            </Link>
            <div className="px-4 pb-4 flex gap-2">
              <button
                onClick={() => { if (item.price) { addItem({ productSlug: item.productSlug, productName: item.productName, variantId: item.variantId, variantName: item.variantName, sku: item.sku, price: roundPrice(item.price), image: item.image, ...(item.isCourse ? { kind: 'course' as const } : {}) }); openCart() } }}
                className="flex-1 h-8 text-xs font-bold bg-brand hover:bg-brand-hover text-white rounded-xl transition-colors"
              >
                Add to cart
              </button>
              <button
                onClick={() => toggle(item)}
                className="w-8 h-8 flex items-center justify-center border border-red-200 text-red-400 hover:bg-red-50 rounded-xl transition-colors"
              >
                <Heart size={13} fill="currentColor" />
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
