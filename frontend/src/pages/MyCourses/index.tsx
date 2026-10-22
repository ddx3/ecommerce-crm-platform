import { useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ChevronRight, GraduationCap, PlayCircle, Clock, Hourglass } from 'lucide-react'
import { getMyCourses } from '../../api/courses'
import type { MyCourse, PendingCourse } from '../../types'
import { useAuth } from '../../context/AuthContext'
import { Card } from '../../components/ui'
import Button from '../../components/ui/Button'
import { CENTER_ON_CAPS } from '../../components/ProductLabel'

export default function MyCoursesPage() {
  const { token } = useAuth()
  const navigate = useNavigate()
  const [purchased, setPurchased] = useState<MyCourse[]>([])
  const [pending, setPending] = useState<PendingCourse[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    // On a hard refresh the token is read from localStorage synchronously, while user is still null
    // (being validated by apiMe).
    // Redirect to login ONLY when there is definitely no token, otherwise a logged-in user bounces
    // to /login before the profile loads.
    if (!token) {
      navigate('/login', { replace: true })
      return
    }
    setLoading(true)
    getMyCourses()
      .then(data => { setPurchased(data.purchased); setPending(data.pending) })
      .catch(e => { console.error('My courses error:', e); setPurchased([]); setPending([]) })
      .finally(() => setLoading(false))
  }, [token, navigate])

  if (!token) return null

  const isEmpty = purchased.length === 0 && pending.length === 0

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">
      <nav className="flex items-center gap-2 text-xs sm:text-sm text-text-muted mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        <ChevronRight size={14} />
        <span className="text-text-secondary font-medium">My courses</span>
      </nav>
      <div className="flex items-center justify-between mb-6 sm:mb-8">
        <h1 className="text-xl sm:text-2xl font-bold text-gray-900">
          My courses{purchased.length > 0 && <span className="text-gray-400 font-bold ml-2">({purchased.length})</span>}
        </h1>
      </div>

      {loading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 sm:gap-6">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="rounded-2xl bg-surface-muted animate-pulse aspect-[4/3]" />
          ))}
        </div>
      ) : isEmpty ? (
        <div className="text-center py-14 sm:py-20">
          <div className="w-20 h-20 rounded-full bg-brand-bg text-brand flex items-center justify-center mx-auto mb-4"><GraduationCap size={36} /></div>
          <p className="text-lg sm:text-xl font-bold text-text-secondary mb-2">You have not purchased any courses yet</p>
          <Button as={Link} to="/courses">
            To the course catalog
          </Button>
        </div>
      ) : (
        <>
          {/* Pending — orders being processed */}
          {pending.length > 0 && (
            <section className="mb-8">
              <h2 className="text-base sm:text-lg font-bold text-text-secondary mb-3 flex items-center gap-2">
                <Hourglass size={18} className="text-amber-500" /> Awaiting payment confirmation
              </h2>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 sm:gap-4">
                {pending.map(p => (
                  <div
                    key={`${p.order_number}_${p.course.id}`}
                    className="bg-amber-50 border border-amber-200 rounded-2xl p-4 flex gap-3"
                  >
                    <div className="w-20 h-20 shrink-0 bg-white rounded-xl overflow-hidden flex items-center justify-center">
                      {p.course.cover_url ? (
                        <img src={p.course.cover_url} alt={p.course.name} className="w-full h-full object-cover" />
                      ) : (
                        <GraduationCap size={32} className="text-amber-300" />
                      )}
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-bold text-text-primary line-clamp-2">{p.course.name}</p>
                      <p className="text-xs text-amber-700 mt-1">
                        Order <Link to="/orders" className="font-bold hover:underline">#{p.order_number}</Link>
                      </p>
                      <p className="text-xs text-text-muted mt-1">
                        Access will appear automatically once the payment is credited.
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* Purchased — available courses */}
          {purchased.length > 0 && (
            <section>
              {pending.length > 0 && (
                <h2 className="text-base sm:text-lg font-bold text-text-secondary mb-3">Available courses</h2>
              )}
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 sm:gap-6">
                {purchased.map(p => (
                  <Card
                    key={p.id}
                    as={Link}
                    // eslint-disable-next-line @typescript-eslint/no-explicit-any
                    {...({ to: `/course/${p.course.slug}/learn` } as any)}
                    variant="listing"
                    className="block overflow-hidden"
                  >
                    <div className="overflow-hidden bg-gradient-to-br from-brand-bg to-[#d7ebd0] relative">
                      {p.course.cover_url ? (
                        <img
                          src={p.course.cover_url}
                          alt={p.course.name}
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
                          <span style={CENTER_ON_CAPS}>{p.course.total_lessons} lessons</span>
                        </span>
                        {p.course.total_duration_minutes > 0 && (
                          <span className="bg-white/95 text-text-secondary text-xs font-bold px-2.5 py-1 rounded-lg inline-flex items-center gap-1 leading-none">
                            <Clock size={12} className="shrink-0 block" />
                            <span style={CENTER_ON_CAPS}>{formatDuration(p.course.total_duration_minutes)}</span>
                          </span>
                        )}
                      </div>
                    </div>
                    <div className="px-4 pt-3 pb-4 flex flex-col gap-2">
                      <h3 className="text-base font-bold text-text-primary line-clamp-2">
                        {p.course.name}
                      </h3>
                      <p className="text-xs text-text-disabled mt-2">Purchased: {new Date(p.purchased_at).toLocaleDateString('uk-UA')}</p>
                    </div>
                  </Card>
                ))}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  )
}

function formatDuration(minutes: number): string {
  if (minutes < 60) return `${minutes} min`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m > 0 ? `${h} h ${m} min` : `${h} h`
}
