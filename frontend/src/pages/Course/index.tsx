import { useEffect, useState } from 'react'
import { Link, useParams, useNavigate } from 'react-router-dom'
import {
  ChevronRight, ChevronLeft, ChevronDown, GraduationCap, Clock, PlayCircle,
  FileText, ShoppingCart, CheckCircle2, Lock, Heart,
} from 'lucide-react'
import { getCourse } from '../../api/courses'
import { sanitizeHtml } from '../../utils/sanitize'
import type {
  CourseDetail, CourseModule, CourseLessonProgram, CourseRelatedProduct, BundleListItem,
} from '../../types'
import { programItems, isLeafNode, lessonCount, ownLesson, hasVisibleContent } from '../../utils/courseProgram'
import { ProductCard, BundleCard } from '../Catalog'
import { useCart } from '../../context/CartContext'
import { useLiked } from '../../context/LikedContext'
import { useUI } from '../../context/UIContext'
import { formatPrice } from '../../utils/price'
import { trackViewItem } from '../../lib/analytics'
import { Card } from '../../components/ui'
import Button from '../../components/ui/Button'
import Seo from '../../components/Seo'

/** Lesson row in the program. A lesson is a node without subsections, so the same row
 *  is drawn both for a lesson inside a section and for a root lesson.
 */
function LessonRow({
  lesson: l, num, courseSlug, hasAccess,
}: {
  lesson: CourseLessonProgram
  num: string
  courseSlug: string
  hasAccess: boolean
}) {
  const Icon = l.lesson_type === 'video' ? PlayCircle : FileText
  const inner = (
    <>
      <span className="text-xs text-gray-400 shrink-0 tabular-nums">{num}.</span>
      <Icon size={16} className="text-gray-400" />
      <span className="flex-1 min-w-0 text-sm text-gray-700 line-clamp-1">{l.title}</span>
      {l.duration_minutes && (
        <span className="text-xs text-gray-400 whitespace-nowrap">{l.duration_minutes} min</span>
      )}
      {!hasAccess && <Lock size={14} className="text-gray-400 shrink-0" />}
    </>
  )
  return hasAccess ? (
    <Link
      to={`/course/${courseSlug}/learn/${l.id}`}
      className="flex items-center gap-3 px-4 py-2.5 hover:bg-brand-bg transition-colors"
    >
      {inner}
    </Link>
  ) : (
    <div
      aria-disabled="true"
      title="Available after purchasing the course"
      className="flex items-center gap-3 px-4 py-2.5 cursor-not-allowed text-gray-400"
    >
      {inner}
    </div>
  )
}

/** Program node: without subsections it is a lesson in a single row, with subsections —
 *  a section that expands. A section can also have its own lesson (an intro video) —
 *  then its title is a link to that lesson. Numbering is continuous (1 → 1.1 → 1.1.1).
 */
function ModuleNode({
  module: m, num, courseSlug, hasAccess, openModules, onToggle, depth = 0,
}: {
  module: CourseModule
  num: string
  courseSlug: string
  hasAccess: boolean
  openModules: Set<number>
  onToggle: (id: number) => void
  depth?: number
}) {
  const card = 'bg-surface border border-border rounded-xl shadow-card-low overflow-hidden'
  const indent = depth ? { marginLeft: depth * 16 } : undefined
  const own = ownLesson(m)

  if (isLeafNode(m) && own) {
    return (
      <div className={card} style={indent}>
        <LessonRow lesson={own} num={num} courseSlug={courseSlug} hasAccess={hasAccess} />
      </div>
    )
  }

  const isOpen = openModules.has(m.id)
  const items = programItems(m, num)
  const titleInner = (
    <>
      <span className="tabular-nums shrink-0">{num}.</span>
      {own && <PlayCircle size={16} className="text-gray-400 shrink-0" />}
      <span className="truncate">{m.title}</span>
      {own && !hasAccess && <Lock size={14} className="text-gray-400 shrink-0" />}
    </>
  )
  const titleCls = 'flex-1 min-w-0 flex items-center gap-1.5 font-bold text-gray-900 text-left'
  return (
    <div className={card} style={indent}>
      <div className="flex items-center gap-3 px-4 py-3 bg-surface">
        {own && hasAccess ? (
          <Link to={`/course/${courseSlug}/learn/${own.id}`} className={`${titleCls} hover:text-brand transition-colors`}>
            {titleInner}
          </Link>
        ) : (
          <button onClick={() => onToggle(m.id)} className={titleCls}>{titleInner}</button>
        )}
        <span className="text-xs text-gray-400 whitespace-nowrap">{lessonCount(m)} lessons</span>
        <button
          onClick={() => onToggle(m.id)}
          aria-expanded={isOpen}
          aria-label={isOpen ? 'Collapse' : 'Expand'}
          className="shrink-0 p-1 -mr-1 text-gray-400 hover:text-gray-600 transition-colors"
        >
          <ChevronDown size={18} className={`transition-transform ${isOpen ? 'rotate-180' : ''}`} />
        </button>
      </div>
      {isOpen && (
        <div className="border-t border-gray-100 divide-y divide-gray-50">
          {items.map(it => (
            it.kind === 'lesson' ? (
              <LessonRow
                key={`l${it.lesson.id}`}
                lesson={it.lesson}
                num={it.num}
                courseSlug={courseSlug}
                hasAccess={hasAccess}
              />
            ) : (
              <div key={`m${it.module.id}`} className="p-2">
                <ModuleNode
                  module={it.module}
                  num={it.num}
                  courseSlug={courseSlug}
                  hasAccess={hasAccess}
                  openModules={openModules}
                  onToggle={onToggle}
                  depth={depth + 1}
                />
              </div>
            )
          ))}
        </div>
      )}
    </div>
  )
}

/** Course products — a program block above the modules, in the form of the same kind of node:
 *  a header with a counter and an arrow, inside — catalog cards (the same as on the
 *  storefront: price, availability, pack selection, purchase).
 */
function ProductsNode({
  products, bundles, isOpen, onToggle,
}: {
  products: CourseRelatedProduct[]
  bundles: BundleListItem[]
  isOpen: boolean
  onToggle: () => void
}) {
  const total = products.length + bundles.length
  return (
    /* Without overflow-hidden: a catalog card grows downward on hover, and the block
        would clip the last row.
     */
    <div className="bg-surface border border-border rounded-xl shadow-card-low">
      {/* The collapsed block is just the header, so its bottom must be rounded too. */}
      <div className={`flex items-center gap-3 px-4 py-3 bg-surface ${isOpen ? 'rounded-t-xl' : 'rounded-xl'}`}>
        <button
          onClick={onToggle}
          className="flex-1 min-w-0 flex items-center gap-1.5 font-bold text-gray-900 text-left"
        >
          <span className="truncate">Course products</span>
        </button>
        <span className="text-xs text-gray-400 whitespace-nowrap">{total} products</span>
        <button
          onClick={onToggle}
          aria-expanded={isOpen}
          aria-label={isOpen ? 'Collapse' : 'Expand'}
          className="shrink-0 p-1 -mr-1 text-gray-400 hover:text-gray-600 transition-colors"
        >
          <ChevronDown size={18} className={`transition-transform ${isOpen ? 'rotate-180' : ''}`} />
        </button>
      </div>
      {isOpen && (
        <div className="border-t border-gray-100 p-3 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
          {products.map(p => (
            <ProductCard
              key={`p${p.id}`}
              product={p}
              scope="course"
              /* We recommend a specific pack — the card starts with exactly that one. */
              expectedVariantId={p.recommended_variant_id}
            />
          ))}
          {bundles.map(b => (
            <BundleCard key={`b${b.id}`} bundle={b} />
          ))}
        </div>
      )}
    </div>
  )
}

export default function CoursePage() {
  const { slug } = useParams<{ slug: string }>()
  const [course, setCourse] = useState<CourseDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [openModules, setOpenModules] = useState<Set<number>>(new Set())
  const [openProducts, setOpenProducts] = useState(false)   // collapsed, like the sections
  // A section where everything is hidden is not shown on the storefront (an empty accordion).
  const visibleModules = (course?.modules ?? []).filter(hasVisibleContent)
  const cart = useCart()
  const liked = useLiked()
  const { openCart } = useUI()
  const navigate = useNavigate()

  useEffect(() => {
    if (!slug) return
    setLoading(true)
    setError(null)
    getCourse(slug)
      .then(data => {
        setCourse(data)
        // item_id/category — as in add_to_cart and begin_checkout (`course_<id>`, 'course'),
        // otherwise GA4 won't stitch the view step to the rest of the course funnel.
        trackViewItem({ id: `course_${data.id}`, name: data.name,
                        price: data.price ?? 0, category: 'course' })
        // Expand the first section by default (the root lesson is not expandable)
        const firstSection = data.modules.find(m => !isLeafNode(m))
        if (firstSection) setOpenModules(new Set([firstSection.id]))
      })
      .catch(e => {
        console.error('Course load error:', e)
        setError(e?.response?.status === 404 ? 'Course not found' : 'Loading error')
      })
      .finally(() => setLoading(false))
  }, [slug])

  const toggleModule = (id: number) => {
    setOpenModules(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id); else next.add(id)
      return next
    })
  }

  const handleBuy = () => {
    if (!course) return
    cart.addItem({
      productSlug: course.slug,
      productName: course.name,
      variantId: course.id,
      variantName: course.name,
      sku: `COURSE-${course.id}`,
      price: Number(course.price),
      image: course.cover_url,
      kind: 'course',
    })
    openCart()
  }

  const likeKey = course ? `${course.slug}_${course.id}` : ''
  const isLiked = course ? liked.isLiked(likeKey) : false
  const toggleLike = () => {
    if (!course) return
    liked.toggle({
      key: likeKey,
      productSlug: course.slug,
      productName: course.name,
      variantId: course.id,
      variantName: course.name,
      sku: `COURSE-${course.id}`,
      price: Number(course.price),
      image: course.cover_url,
      isCourse: true,
    })
  }

  const shopCount = course
    ? course.related_products.length + (course.related_bundles?.length ?? 0)
    : 0

  if (loading) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
        <div className="animate-pulse space-y-4">
          <div className="h-8 w-2/3 bg-gray-100 rounded" />
          <div className="h-64 bg-gray-100 rounded-2xl" />
          <div className="h-32 bg-gray-100 rounded-2xl" />
        </div>
      </div>
    )
  }

  if (error || !course) {
    return (
      <div className="max-w-[1440px] mx-auto px-4 py-20 text-center">
        <div className="w-20 h-20 rounded-full bg-brand-bg text-brand flex items-center justify-center mx-auto mb-4"><GraduationCap size={36} /></div>
        <p className="text-xl font-bold text-gray-700">{error || 'Course not found'}</p>
        <Link to="/courses" className="inline-flex items-center gap-1 mt-4 text-brand font-bold hover:underline">
          <ChevronLeft size={18} /> Back to courses
        </Link>
      </div>
    )
  }

  const hasDiscount = course.old_price && Number(course.old_price) > Number(course.price)

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
      <Seo
        title={course.meta_title || course.name}
        description={course.meta_description || course.short_description}
        canonicalPath={`/course/${course.slug}`}
        image={course.cover_url}
      />
      <nav className="flex items-center gap-2 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand">Home</Link>
        <ChevronRight size={14} />
        <Link to="/courses" className="hover:text-brand">Courses</Link>
        <ChevronRight size={14} />
        <span className="text-gray-700 font-medium line-clamp-1">{course.name}</span>
      </nav>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_380px] gap-6 lg:gap-8">
        {/* ── Left column: content ── */}
        <div>
          <div className="aspect-[16/9] rounded-2xl overflow-hidden bg-gradient-to-br from-brand-bg to-[#d7ebd0] mb-6">
            {course.cover_url ? (
              <img src={course.cover_url} alt={course.name} className="w-full h-full object-cover" />
            ) : (
              <div className="w-full h-full flex items-center justify-center">
                <GraduationCap size={80} className="text-brand/30" />
              </div>
            )}
          </div>

          <h1 className="text-2xl sm:text-3xl lg:text-4xl font-bold text-gray-900 mb-3">{course.name}</h1>

          <div className="flex flex-wrap items-center gap-3 text-sm text-gray-600 mb-6">
            <span className="flex items-center gap-1.5">
              <PlayCircle size={16} className="shrink-0" /> {course.total_lessons} lessons
            </span>
            {course.total_duration_minutes > 0 && (
              <span className="flex items-center gap-1.5">
                <Clock size={16} className="shrink-0" /> {formatDuration(course.total_duration_minutes)}
              </span>
            )}
          </div>

          {/* short_description is an automatic excerpt of the description (first ~200 characters),
              needed by the catalog. Next to the full description it was printed a second time. */}
          {course.short_description && !course.description && (
            <p className="text-base sm:text-lg text-gray-700 mb-6 leading-relaxed">{course.short_description}</p>
          )}

          {course.description && (
            <div
              className="prose prose-sm sm:prose-base max-w-none mb-8 text-gray-700"
              dangerouslySetInnerHTML={{ __html: sanitizeHtml(course.description) }}
            />
          )}

          {/* Program */}
          <h2 className="text-xl sm:text-2xl font-bold text-gray-900 mb-4">Course program</h2>
          <div className="space-y-2 mb-8">
            {/* Full version — a separate block before the modules */}
            {course.has_full_video !== false && course.full_video_poster_url && (() => {
              const inner = (
                <>
                  <img
                    src={course.full_video_poster_url!}
                    alt=""
                    className="w-20 h-12 object-cover rounded shrink-0 bg-gray-100"
                  />
                  <div className="flex-1 min-w-0">
                    <div className="font-bold text-gray-900">Full course video</div>
                    <div className="text-xs text-gray-400 mt-0.5">
                      {course.total_duration_minutes > 0
                        ? `${formatDuration(course.total_duration_minutes)} of continuous viewing`
                        : 'Watch without lesson breaks'}
                    </div>
                  </div>
                  {course.has_access ? (
                    <PlayCircle size={20} className="text-brand shrink-0" />
                  ) : (
                    <Lock size={16} className="text-gray-400 shrink-0" />
                  )}
                </>
              )
              return (
                <div className="bg-surface border border-border rounded-xl shadow-card-low overflow-hidden">
                  {course.has_access ? (
                    <Link
                      to={`/course/${course.slug}/learn/full`}
                      className="flex items-center gap-3 px-4 py-3 hover:bg-brand-bg transition-colors"
                    >
                      {inner}
                    </Link>
                  ) : (
                    <div
                      aria-disabled="true"
                      title="Available after purchasing the course"
                      className="flex items-center gap-3 px-4 py-3 cursor-not-allowed"
                    >
                      {inner}
                    </div>
                  )}
                </div>
              )
            })()}
            {shopCount > 0 && (
              <ProductsNode
                products={course.related_products}
                bundles={course.related_bundles ?? []}
                isOpen={openProducts}
                onToggle={() => setOpenProducts(v => !v)}
              />
            )}
            {visibleModules.length === 0 && (
              <p className="text-gray-400 italic">The program will appear soon.</p>
            )}
            {visibleModules.map((m, mIdx) => (
              <ModuleNode
                key={m.id}
                module={m}
                num={String(mIdx + 1)}
                courseSlug={course.slug}
                hasAccess={course.has_access}
                openModules={openModules}
                onToggle={toggleModule}
              />
            ))}
          </div>

        </div>

        {/* ── Right column: price/CTA (sticky) ── */}
        <aside className="lg:sticky lg:top-[calc(var(--header-h,80px)+1rem)] lg:self-start">
          <Card variant="action" className="p-5 sm:p-6">
            {course.has_access ? (
              <>
                <div className="flex items-center gap-2 text-brand font-bold mb-4">
                  <CheckCircle2 size={20} className="shrink-0" /> Course purchased
                </div>
                <Button
                  onClick={() => navigate(`/course/${course.slug}/learn`)}
                  fullWidth
                  className="mb-4"
                  iconLeft={<PlayCircle size={20} />}
                >
                  Start learning
                </Button>
                <ul className="space-y-2 text-sm text-text-secondary">
                  <li className="flex items-start gap-2">
                    <CheckCircle2 size={16} className="text-brand shrink-0 mt-0.5" />
                    <span>All lessons are unlocked — {course.total_lessons} {course.total_duration_minutes > 0 ? `(${formatDuration(course.total_duration_minutes)})` : ''}</span>
                  </li>
                  <li className="flex items-start gap-2">
                    <CheckCircle2 size={16} className="text-brand shrink-0 mt-0.5" />
                    <span>Notes with dosages under each video</span>
                  </li>
                  <li className="flex items-start gap-2">
                    <CheckCircle2 size={16} className="text-brand shrink-0 mt-0.5" />
                    <span>Lifetime access, watch whenever it suits you</span>
                  </li>
                </ul>
              </>
            ) : (
              <>
                <div className="flex items-baseline gap-3 mb-4">
                  <span className="text-3xl font-bold text-gray-900">{formatPrice(course.price)} UAH</span>
                  {hasDiscount && (
                    <span className="text-lg text-gray-400 line-through">{formatPrice(course.old_price)} UAH</span>
                  )}
                </div>

                <div className="flex gap-3 mb-3">
                  <Button
                    onClick={handleBuy}
                    className="flex-1"
                    iconLeft={<ShoppingCart size={20} />}
                  >
                    Buy the course
                  </Button>
                  <button
                    onClick={toggleLike}
                    aria-label={isLiked ? 'Remove from favorites' : 'Add to favorites'}
                    title={isLiked ? 'Remove from favorites' : 'Add to favorites'}
                    className={`w-12 shrink-0 flex items-center justify-center rounded-xl border-2 transition-colors ${isLiked ? 'border-red-300 bg-red-50 text-red-500' : 'border-gray-200 text-gray-400 hover:border-red-300 hover:text-red-400'}`}
                  >
                    <Heart size={20} fill={isLiked ? 'currentColor' : 'none'} />
                  </button>
                </div>

                <ul className="space-y-2 text-sm text-text-secondary">
                  <li className="flex items-start gap-2">
                    <CheckCircle2 size={16} className="text-brand shrink-0 mt-0.5" />
                    <span>Lifetime access after purchase</span>
                  </li>
                  <li className="flex items-start gap-2">
                    <CheckCircle2 size={16} className="text-brand shrink-0 mt-0.5" />
                    <span>Watch whenever you like, no limits</span>
                  </li>
                  <li className="flex items-start gap-2">
                    <CheckCircle2 size={16} className="text-brand shrink-0 mt-0.5" />
                    <span>Pay by card online or by bank transfer</span>
                  </li>
                </ul>
              </>
            )}
          </Card>
        </aside>
      </div>
    </div>
  )
}

function formatDuration(minutes: number): string {
  if (minutes < 60) return `${minutes} min`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m > 0 ? `${h} h ${m} min` : `${h} h`
}
