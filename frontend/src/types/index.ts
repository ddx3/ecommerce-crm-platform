export interface Category {
  id: number
  name: string
  slug: string
  image_url: string | null
  parent_id: number | null
  children: Category[]
  sort_order: number
}

export interface Label {
  name: string
  color: string
  slug: string
}

export interface ProductImage {
  id: number
  url: string
  display_url?: string | null
  is_main: boolean
  sort_order: number
  variant: number | null
}

export interface ProductCharacteristic {
  name: string
  value: string
  color_hex?: string
  sort_order: number
}

export interface ProductComposition {
  name: string
  value: string
  sort_order: number
}

export interface AttributeValueRef {
  id: number
  attribute_id: number
  attribute_name: string
  value: string
  color_hex: string
  is_color: boolean
}

export interface ProductVariant {
  id: number
  name: string
  sku: string | null
  price: number
  old_price: number | null
  discount_percent: number | null
  discount_amount: number | null
  effective_discount: number | null
  stock: number
  is_active: boolean
  images: ProductImage[]
  active_labels: Label[]
  characteristics: ProductCharacteristic[]
  attribute_values: AttributeValueRef[]
  description: string
  shipping_origin: ShippingOrigin
  supplier?: Supplier | null
}

export interface PairedProduct {
  id: number
  slug: string
  name: string
  packaging_kind: 'factory' | 'own'
}

/** Supplier of a dropshipping/hybrid item. requires_cargo_branch — its parcel is accepted
 * only by Nova Poshta cargo branches (oversized items: agrofibre, mesh).
 */
export interface Supplier {
  code: string
  name: string
  requires_cargo_branch?: boolean
}

export type ShippingOrigin = 'own' | 'bradas' | 'hybrid'

export interface ProductListVariant {
  id: number
  name: string
  price: number
  stock: number
  sku: string | null
  attribute_values: AttributeValueRef[]
  image: string | null
  image_thumb?: string | null
  shipping_origin: ShippingOrigin
  supplier?: Supplier | null
  old_price: number | null
  discount_percent: number | null
  discount_amount: number | null
  effective_discount: number | null
  active_labels: Label[]
}

export interface ProductListItem {
  id: number
  name: string
  slug: string
  brand_name: string | null
  brand_slug: string | null
  main_image: string | null
  main_image_thumb?: string | null
  min_price: number | null
  old_price: number | null
  has_discount: boolean
  labels: Label[]
  variants: ProductListVariant[]
  packaging_kind: 'factory' | 'own'
  paired: ProductListItem | null
  // Only as part of a bundle: false → the product is hidden, the card is not clickable and cannot
  // be bought separately.
  is_active?: boolean
}

export interface ProductDetail {
  id: number
  name: string
  slug: string
  description: string
  application_norms: string
  brand_name: string | null
  brand_slug: string | null
  categories: Category[]
  images: ProductImage[]
  variants: ProductVariant[]
  characteristics: ProductCharacteristic[]
  compositions: ProductComposition[]
  meta_title: string
  meta_description: string
  packaging_kind: 'factory' | 'own'
  paired_product: PairedProduct | null
  reviews_count: number
  avg_rating: number | null
}

export interface BundleItem {
  product_name: string
  product_slug: string
  variant_id: number
  variant_name: string
  quantity: number
  image: string | null
  product: ProductListItem
}

export interface BundleListItem {
  id: number
  name: string
  slug: string
  sku: string | null
  main_image: string | null
  main_image_thumb?: string | null
  price: number | null
  old_price: number | null
  stock: number
  active_labels: Label[]
  is_active: boolean
}

export interface BundleDetail {
  id: number
  name: string
  slug: string
  sku: string | null
  price: number | null
  old_price: number | null
  stock: number
  description: string
  application_norms: string
  is_active: boolean
  items: BundleItem[]
  images: ProductImage[]
  active_labels: Label[]
  meta_title: string
  meta_description: string
  reviews_count: number
  avg_rating: number | null
}

export interface PaginatedResponse<T> {
  count: number
  next: string | null
  previous: string | null
  results: T[]
}

export interface VariantOrderRef {
  product_id: number
  variant_id: number
}

export interface ProductListResponse extends PaginatedResponse<ProductListItem> {
  variants_count: number
  // Present only with ?as=variant: the order of variant cards on the page
  // (globally sorted — in-stock first, out-of-stock at the end).
  variant_order?: VariantOrderRef[]
}

export interface BlogPostListItem {
  id: number
  slug: string
  title: string
  preview_image: string | null
  excerpt: string
  created_at: string
}

export interface BlogPostDetail extends BlogPostListItem {
  content: string
  youtube_url: string
  updated_at: string
  meta_title: string
  meta_description: string
}

// ───── Courses ─────

export interface CourseListItem {
  id: number
  name: string
  slug: string
  short_description: string
  cover_url: string | null
  instructor_name: string
  price: number
  old_price: number | null
  total_lessons: number
  total_duration_minutes: number
}

export interface CourseLessonProgram {
  id: number
  title: string
  lesson_type: 'video' | 'text'
  duration_minutes: number | null
  is_preview: boolean
  sort_order: number
  poster_url: string | null
}

export interface CourseChapter {
  time: number
  title: string
}

export interface CourseModule {
  id: number
  title: string
  sort_order: number
  lessons: CourseLessonProgram[]
  /** Submodules: a curriculum can have several levels (1 → 1.1 → 1.1.1). */
  children?: CourseModule[]
  /** A node without subsections and without other lessons — shown as a single lesson row.
   * This field was added later: the old backend does not return it.
   */
  is_leaf?: boolean
  /** The node's own lesson (same title as the module): an intro video or text
   * of the section itself. It is not duplicated in the lessons list.
   */
  own_lesson?: CourseLessonProgram | null
}

/** Course product — the same card as in the catalog, plus a recommended packaging. */
export interface CourseRelatedProduct extends ProductListItem {
  recommended_variant_id?: number
}

export interface CourseDetail {
  id: number
  name: string
  slug: string
  short_description: string
  description: string
  cover_url: string | null
  instructor_name: string
  price: number
  old_price: number | null
  total_lessons: number
  total_duration_minutes: number
  meta_title: string
  meta_description: string
  modules: CourseModule[]
  has_access: boolean
  related_products: CourseRelatedProduct[]
  /** Recommended bundles. The field was added later — the old backend does not return it. */
  related_bundles?: BundleListItem[]
  full_video_url: string | null
  full_video_poster_url: string | null
  /** Whether the course has a full video. The link itself is delivered only to the buyer. */
  has_full_video?: boolean
  full_video_chapters: CourseChapter[]
}

export interface LessonContent {
  id: number
  title: string
  lesson_type: 'video' | 'text'
  video_url: string
  video_file_url: string | null
  poster_url: string | null
  video_start: number | null
  video_end: number | null
  content: string
  duration_minutes: number | null
  is_preview: boolean
  sort_order: number
  module_id: number
  module_title: string
  course_slug: string
  course_name: string
}

export interface MyCourse {
  id: number
  course: CourseListItem
  purchased_at: string
}

export interface PendingCourse {
  course: {
    id: number
    name: string
    slug: string
    cover_url: string | null
    price: string
  }
  order_number: string
  order_payment_method: string
  created_at: string
}

export interface MyCoursesResponse {
  purchased: MyCourse[]
  pending: PendingCourse[]
}
