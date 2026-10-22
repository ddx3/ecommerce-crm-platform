import client from './client'
import type { Category, ProductListItem, ProductDetail, PaginatedResponse, ProductListResponse, BundleListItem, BundleDetail } from '../types'

export const getCategories = () =>
  client.get<Category[]>('/categories/').then(r => r.data)

export const getCategoriesFlat = () =>
  client.get<Category[]>('/categories/flat/').then(r => r.data)

export interface ProductsParams {
  category?: string
  search?: string
  brand?: string
  ordering?: 'price' | '-price' | 'new'
  label?: string
  exclude_label?: string
  in_stock?: '1'
  price_min?: string
  price_max?: string
  attrs?: string
  exclude?: string
  page?: number
  page_size?: number
  all?: '1'
  slugs?: string
  as?: 'variant'
}

export const getProducts = (params?: ProductsParams) =>
  client.get<ProductListResponse>('/products/', { params }).then(r => r.data)

export interface CatalogFacets {
  brands: { slug: string; name: string }[]
  labels: { slug: string; name: string; color: string }[]
  attributes: { id: number; name: string; is_color: boolean; values: { id: number; value: string; color_hex: string }[] }[]
  price: { min: number | null; max: number | null }
}

export const getCatalogFacets = (category?: string) =>
  client.get<CatalogFacets>('/products/facets/', { params: category ? { category } : undefined }).then(r => r.data)

export const getProduct = (slug: string) =>
  client.get<ProductDetail>(`/products/${slug}/`).then(r => r.data)

export const getBoughtTogether = (slug: string, limit = 4) =>
  client
    .get<{ results: ProductListItem[] }>(`/products/${slug}/bought-together/`, { params: { limit } })
    .then(r => r.data.results)

export const getBundles = (params?: { search?: string; category?: string; product_slug?: string; page?: number; page_size?: number }) =>
  client.get<PaginatedResponse<BundleListItem>>('/bundles/', { params }).then(r => r.data)

export const getBundle = (slug: string) =>
  client.get<BundleDetail>(`/bundles/${slug}/`).then(r => r.data)

export interface SearchResult {
  variants: {
    product_slug: string; product_name: string; variant_name: string
    sku: string | null; price: number | null; stock: number; in_stock: boolean; image: string | null
  }[]
  bundles: { sku: string; slug: string; name: string; image: string | null; price: number | null; stock: number; in_stock: boolean }[]
}

export const searchItems = (q: string, category?: string) =>
  client.get<SearchResult>('/search/', { params: category ? { q, category } : { q } }).then(r => r.data)

export interface SiteReviewReply {
  id: number
  author_name: string
  text: string
  created_at: string
  parent_id: number | null
  is_admin?: boolean
}

export interface SiteReview {
  id: number
  author_name: string
  rating: number
  text: string
  created_at: string
  replies: SiteReviewReply[]
  variant_name?: string | null
}

export const getSiteReviews = () =>
  client.get<SiteReview[]>('/reviews/').then(r => r.data)

type GuestFields = { guest_name?: string; guest_email?: string }

export const postSiteReview = (data: { rating: number; text: string } & GuestFields) =>
  client.post('/reviews/', data).then(r => r.data)

export const postReviewReply = (reviewId: number, data: { text: string; reply_id?: number }) =>
  client.post(`/reviews/${reviewId}/reply/`, { ...data, rating: 5 }).then(r => r.data)

export const getProductReviews = (slug: string) =>
  client.get<SiteReview[]>(`/products/${slug}/reviews/`).then(r => r.data)

export const postProductReview = (slug: string, data: { rating: number; text: string; variant_id?: number } & GuestFields) =>
  client.post(`/products/${slug}/reviews/`, data).then(r => r.data)

export const getBundleReviews = (slug: string) =>
  client.get<SiteReview[]>(`/bundles/${slug}/reviews/`).then(r => r.data)

export const postBundleReview = (slug: string, data: { rating: number; text: string } & GuestFields) =>
  client.post(`/bundles/${slug}/reviews/`, data).then(r => r.data)

export type StockNotifyKind = 'variant' | 'bundle'

export const notifyStock = (id: number, email: string, kind: StockNotifyKind = 'variant') =>
  client.post('/stock-notify/', { [kind === 'bundle' ? 'bundle_id' : 'variant_id']: id, email }).then(r => r.data)

export interface Brand {
  id: number
  name: string
  slug: string
  logo: string | null
}

export const getBrands = () =>
  client.get<Brand[]>('/brands/').then(r => r.data)

// ─── Favorites (server-side storage of likes for demand analytics) ───
export type FavoriteKind = 'variant' | 'bundle' | 'course'
export interface FavoriteSyncItem { kind: FavoriteKind; id: number }

export const favoriteSet = (device_id: string, kind: FavoriteKind, id: number, liked: boolean) =>
  client.post('/favorites/set/', { device_id, kind, id, liked }).then(r => r.data)

export const favoriteSync = (device_id: string, items: FavoriteSyncItem[]) =>
  client.post('/favorites/sync/', { device_id, items }).then(r => r.data)
