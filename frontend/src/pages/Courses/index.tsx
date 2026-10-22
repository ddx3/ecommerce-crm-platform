import { useState, useEffect } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight, GraduationCap, Clock, PlayCircle } from 'lucide-react'
import { getCourses } from '../../api/courses'
import type { CourseListItem } from '../../types'
import { formatPrice } from '../../utils/price'
import { CENTER_ON_CAPS } from '../../components/ProductLabel'

export default function CoursesPage() {
  const [courses, setCourses] = useState<CourseListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const PAGE_SIZE = 12
  const totalPages = Math.ceil(total / PAGE_SIZE)

  useEffect(() => {
    setLoading(true)
    getCourses({ page, page_size: PAGE_SIZE })
      .then(data => { setCourses(data.results); setTotal(data.count) })
      .catch(e => { console.error('Courses error:', e); setCourses([]) })
      .finally(() => setLoading(false))
  }, [page])

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
      <nav className="flex items-center gap-2 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand">Home</Link>
        <ChevronRight size={14} />
        <span className="text-gray-700 font-medium">Courses</span>
      </nav>

      {loading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 sm:gap-6">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="rounded-2xl bg-gray-100 animate-pulse aspect-[4/3]" />
          ))}
        </div>
      ) : courses.length === 0 ? (
        <div className="text-center py-14 sm:py-20">
          <div className="w-20 h-20 rounded-full bg-brand-bg text-brand flex items-center justify-center mx-auto mb-4"><GraduationCap size={36} /></div>
          <p className="text-lg sm:text-xl font-bold text-gray-700">No courses yet</p>
        </div>
      ) : (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 sm:gap-6">
            {courses.map(c => <CourseCard key={c.id} course={c} />)}
          </div>
          {totalPages > 1 && (
            <div className="flex items-center justify-center gap-1 mt-8">
              <button
                onClick={() => setPage(p => p - 1)}
                disabled={page === 1}
                className="w-9 h-9 flex items-center justify-center rounded-xl border border-gray-200 text-gray-500 hover:border-brand hover:text-brand disabled:opacity-30 transition-colors"
              >
                <ChevronRight size={16} className="rotate-180" />
              </button>
              {Array.from({ length: totalPages }, (_, i) => i + 1).map(p => (
                <button
                  key={p}
                  onClick={() => setPage(p)}
                  className={`w-9 h-9 flex items-center justify-center rounded-xl text-sm font-bold leading-none pt-px transition-colors ${
                    page === p ? 'bg-brand text-white' : 'border border-gray-200 text-gray-600 hover:border-brand hover:text-brand'
                  }`}
                >
                  {p}
                </button>
              ))}
              <button
                onClick={() => setPage(p => p + 1)}
                disabled={page === totalPages}
                className="w-9 h-9 flex items-center justify-center rounded-xl border border-gray-200 text-gray-500 hover:border-brand hover:text-brand disabled:opacity-30 transition-colors"
              >
                <ChevronRight size={16} />
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function CourseCard({ course }: { course: CourseListItem }) {
  const hasDiscount = course.old_price && Number(course.old_price) > Number(course.price)

  return (
    <Link
      to={`/course/${course.slug}`}
      className="group block bg-surface rounded-2xl overflow-hidden border border-border shadow-card-low hover:shadow-card-hover"
    >
      <div className="overflow-hidden bg-gradient-to-br from-brand-bg to-[#d7ebd0] relative">
        {course.cover_url ? (
          <img
            src={course.cover_url}
            alt={course.name}
            className="w-full h-auto block"
          />
        ) : (
          <div className="aspect-[16/10] w-full flex items-center justify-center">
            <GraduationCap size={56} className="text-brand/30" />
          </div>
        )}
        <div className="absolute bottom-2.5 left-2.5 flex items-center gap-2">
          <span className="bg-white/95 text-text-secondary text-xs font-bold px-2.5 py-1 rounded-lg inline-flex items-center gap-1 leading-none">
            <PlayCircle size={12} className="shrink-0 block" />
            <span style={CENTER_ON_CAPS}>{course.total_lessons} lessons</span>
          </span>
          {course.total_duration_minutes > 0 && (
            <span className="bg-white/95 text-text-secondary text-xs font-bold px-2.5 py-1 rounded-lg inline-flex items-center gap-1 leading-none">
              <Clock size={12} className="shrink-0 block" />
              <span style={CENTER_ON_CAPS}>{formatDuration(course.total_duration_minutes)}</span>
            </span>
          )}
        </div>
      </div>
      <div className="px-4 pt-3 pb-4 flex flex-col gap-2">
        <h3 className="text-base font-bold text-text-primary line-clamp-2">
          {course.name}
        </h3>
        <div className="flex items-baseline gap-2 mt-2">
          <span className="text-xl font-bold text-text-primary">{formatPrice(course.price)} UAH</span>
          {hasDiscount && (
            <span className="text-sm text-text-muted line-through">{formatPrice(course.old_price)} UAH</span>
          )}
        </div>
      </div>
    </Link>
  )
}

function formatDuration(minutes: number): string {
  if (minutes < 60) return `${minutes} min`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m > 0 ? `${h} h ${m} min` : `${h} h`
}
