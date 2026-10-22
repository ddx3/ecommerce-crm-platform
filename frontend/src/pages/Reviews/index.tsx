import { useState, useEffect, useRef, useMemo } from 'react'
import type { TextareaHTMLAttributes } from 'react'
import { Link } from 'react-router-dom'
import { Star, User, ChevronRight, ChevronDown, MessageSquarePlus } from 'lucide-react'
import { getSiteReviews, postSiteReview, postReviewReply, type SiteReview } from '../../api/catalog'
import { useAuth } from '../../context/AuthContext'
import { useUI } from '../../context/UIContext'
import { Button, Card, Input } from '../../components/ui'
import { cn } from '../../utils/cn'

// Logged-in user's name for optimistic display (the server takes it from its own side anyway).
function userDisplayName(user: { first_name?: string; last_name?: string; email?: string } | null): string {
  if (!user) return ''
  return [user.first_name, user.last_name].filter(Boolean).join(' ') || user.email || 'User'
}

// Textarea styled like the design system's Input (same wrapper/border/focus).
function Textarea({ error, className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement> & { error?: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className={cn(
        'w-full bg-white border rounded-xl overflow-hidden transition-colors focus-within:border-brand',
        error ? 'border-danger/60' : 'border-border',
      )}>
        <textarea
          className={cn('w-full px-4 py-3 text-base outline-none bg-transparent resize-none placeholder:text-gray-400', className)}
          {...rest}
        />
      </div>
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  )
}

function Stars({ value, size = 16 }: { value: number; size?: number }) {
  return (
    <span className="flex gap-0.5">
      {[1, 2, 3, 4, 5].map(i => (
        <Star key={i} size={size} className={value >= i ? 'text-[#e8a020]' : 'text-gray-200'} fill={value >= i ? '#e8a020' : 'none'} />
      ))}
    </span>
  )
}

function StarPicker({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  const [hovered, setHovered] = useState(0)
  return (
    <span className="flex gap-1">
      {[1, 2, 3, 4, 5].map(i => (
        <button key={i} type="button" onClick={() => onChange(i)}
          onMouseEnter={() => setHovered(i)} onMouseLeave={() => setHovered(0)}>
          <Star size={28} className={(hovered || value) >= i ? 'text-[#e8a020]' : 'text-gray-200'}
            fill={(hovered || value) >= i ? '#e8a020' : 'none'} />
        </button>
      ))}
    </span>
  )
}

function formatDate(iso: string) {
  const d = new Date(iso)
  return d.toLocaleDateString('uk-UA', { day: '2-digit', month: '2-digit', year: 'numeric' })
    + ' at ' + d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit' })
}

type FlatReply = { id: number; author_name: string; text: string; created_at: string; parent_id?: number | null; is_admin?: boolean }

function ReplyForm({ reviewId, replyId, onSuccess, onCancel }: {
  reviewId: number
  replyId?: number
  onSuccess: (r: FlatReply) => void
  onCancel: () => void
}) {
  const { user } = useAuth()
  const [text, setText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [done, setDone] = useState('')
  const [error, setError] = useState('')

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!text.trim()) return
    setSubmitting(true)
    setError('')
    try {
      const res = await postReviewReply(reviewId, { text, reply_id: replyId })
      const isApproved = res?.detail?.includes('published')
      if (isApproved) {
        onSuccess({ id: res.id, author_name: userDisplayName(user), text, created_at: new Date().toISOString(), parent_id: res.parent_id, is_admin: !!(user?.is_staff || user?.is_superuser) })
      } else {
        setDone(res?.detail || 'Sent')
      }
      setText('')
    } catch {
      setError('Could not send the reply. Please try again.')
    } finally {
      setSubmitting(false)
    }
  }

  if (done) return <p className="mt-2 text-sm text-text-muted">{done}</p>

  return (
    <form onSubmit={handleSubmit} className="mt-3 space-y-3">
      <Textarea value={text} onChange={e => setText(e.target.value)}
        rows={2} placeholder="Your reply..." autoFocus error={error} />
      <div className="flex gap-2">
        <Button type="submit" size="sm" loading={submitting} disabled={!text.trim()} className="leading-none">
          Send
        </Button>
        <Button type="button" variant="secondary" size="sm" onClick={onCancel}
          className="bg-gray-100 text-gray-600 border-0 hover:bg-gray-200 hover:text-text-primary leading-none">
          Cancel
        </Button>
      </div>
    </form>
  )
}

function ReplyNode({ reply, allReplies, reviewId, canReply, onAdd }: {
  reply: FlatReply
  allReplies: FlatReply[]
  reviewId: number
  canReply: boolean
  onAdd: (r: FlatReply) => void
}) {
  const { openAuth } = useUI()
  const [replyOpen, setReplyOpen] = useState(false)
  const [pendingReply, setPendingReply] = useState(false)
  const children = allReplies.filter(r => r.parent_id === reply.id)

  // After login (the guest clicked "Log in to reply") — open the form
  // right away.
  useEffect(() => {
    if (canReply && pendingReply) { setReplyOpen(true); setPendingReply(false) }
  }, [canReply, pendingReply])

  const handleReplyClick = () => canReply ? setReplyOpen(true) : (setPendingReply(true), openAuth('login'))

  return (
    <div>
      <div className="flex gap-3 pl-4 border-l-2 border-brand/20">
        <div className="w-7 h-7 rounded-full bg-brand flex items-center justify-center shrink-0">
          <User size={13} className="text-white" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className={`text-sm font-bold ${reply.is_admin ? 'text-brand' : 'text-text-primary'}`}>
              {reply.is_admin ? 'Magiya Roslyn' : reply.author_name}
            </span>
            <span className="text-xs text-text-muted">{formatDate(reply.created_at)}</span>
          </div>
          <p className="text-sm text-text-secondary mt-0.5">{reply.text}</p>
          {!replyOpen && (
            <button onClick={handleReplyClick} className="mt-1 text-sm font-medium text-brand hover:underline">
              {canReply ? 'Reply' : 'Log in to reply'}
            </button>
          )}
        </div>
      </div>
      {replyOpen && (
        <div className="pl-4 mt-2">
          <ReplyForm reviewId={reviewId} replyId={reply.id}
            onSuccess={r => { onAdd(r); setReplyOpen(false) }} onCancel={() => setReplyOpen(false)} />
        </div>
      )}
      {children.length > 0 && (
        <div className="mt-3 ml-4 space-y-3">
          {children.map(child => (
            <ReplyNode key={child.id} reply={child} allReplies={allReplies}
              reviewId={reviewId} canReply={canReply} onAdd={onAdd} />
          ))}
        </div>
      )}
    </div>
  )
}

function ReviewItem({ review, canReply }: {
  review: SiteReview
  canReply: boolean
}) {
  const { openAuth } = useUI()
  const [replyOpen, setReplyOpen] = useState(false)
  const [pendingReply, setPendingReply] = useState(false)
  const [localReplies, setLocalReplies] = useState<FlatReply[]>(
    review.replies.map(r => ({ ...r, parent_id: (r as any).parent_id ?? review.id }))
  )

  // After login (the guest clicked "Log in to reply") — open the form
  // right away.
  useEffect(() => {
    if (canReply && pendingReply) { setReplyOpen(true); setPendingReply(false) }
  }, [canReply, pendingReply])

  const handleReplyClick = () => canReply ? setReplyOpen(true) : (setPendingReply(true), openAuth('login'))

  const addReply = (r: FlatReply) => setLocalReplies(prev => [...prev, r])
  const rootReplies = localReplies.filter(r => r.parent_id === review.id)

  return (
    <Card variant="section" className="p-5">
      <div className="flex gap-3">
        <div className="w-10 h-10 rounded-full bg-brand-bg text-brand flex items-center justify-center shrink-0 mt-0.5">
          <User size={18} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3 flex-wrap mb-1.5">
            <span className="font-bold text-text-primary text-sm">{review.author_name}</span>
            <span className="text-xs text-text-muted">{formatDate(review.created_at)}</span>
            <Stars value={review.rating} size={14} />
          </div>
          <p className="text-sm text-text-secondary leading-relaxed whitespace-pre-line">{review.text}</p>

          {rootReplies.length > 0 && (
            <div className="mt-4 space-y-3">
              {rootReplies.map(reply => (
                <ReplyNode key={reply.id} reply={reply} allReplies={localReplies}
                  reviewId={review.id} canReply={canReply} onAdd={addReply} />
              ))}
            </div>
          )}

          {!replyOpen && (
            <button onClick={handleReplyClick} className="mt-2 text-sm font-medium text-brand hover:underline">
              {canReply ? 'Reply' : 'Log in to reply'}
            </button>
          )}
          {replyOpen && (
            <ReplyForm reviewId={review.id}
              onSuccess={r => { addReply(r); setReplyOpen(false) }} onCancel={() => setReplyOpen(false)} />
          )}
        </div>
      </div>
    </Card>
  )
}

type SortKey = 'date_desc' | 'date_asc' | 'rating_desc' | 'rating_asc'

const SORT_OPTIONS: { key: SortKey; label: string }[] = [
  { key: 'date_desc',   label: 'Newest first' },
  { key: 'date_asc',    label: 'Oldest first' },
  { key: 'rating_desc', label: 'Highest rating' },
  { key: 'rating_asc',  label: 'Lowest rating' },
]

const PAGE_SIZE = 10

export default function ReviewsPage() {
  const { user } = useAuth()
  const [reviews, setReviews] = useState<SiteReview[]>([])
  const [loading, setLoading] = useState(true)
  const [sort, setSort] = useState<SortKey>('date_desc')
  const [sortOpen, setSortOpen] = useState(false)
  const sortRef = useRef<HTMLDivElement>(null)
  const [page, setPage] = useState(1)
  const [showForm, setShowForm] = useState(false)
  const [form, setForm] = useState({ rating: 0, text: '', guestName: '', guestEmail: '' })
  const [submitting, setSubmitting] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [errors, setErrors] = useState<Record<string, string>>({})

  useEffect(() => {
    getSiteReviews().then(setReviews).catch(() => setReviews([])).finally(() => setLoading(false))
  }, [])

  // Reset to page 1 when the sort changes
  useEffect(() => { setPage(1) }, [sort])

  // Close the mobile sort dropdown on outside click
  useEffect(() => {
    if (!sortOpen) return
    const handler = (e: MouseEvent) => {
      if (sortRef.current && !sortRef.current.contains(e.target as Node)) setSortOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [sortOpen])

  // useMemo: typing into the form / opening the dropdown does not re-sort the whole array.
  const sorted = useMemo(() => [...reviews].sort((a, b) => {
    const dateDelta = new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
    switch (sort) {
      case 'date_asc':    return -dateDelta
      case 'rating_desc': return b.rating - a.rating || dateDelta
      case 'rating_asc':  return a.rating - b.rating || dateDelta
      default:            return dateDelta // 'date_desc'
    }
  }), [reviews, sort])

  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE))
  const paged = useMemo(() => sorted.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE), [sorted, page])

  const goToPage = (p: number) => {
    setPage(p)
    // Scroll to the list heading so the user sees the new content
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const avg = useMemo(() => reviews.length ? (reviews.reduce((s, r) => s + r.rating, 0) / reviews.length).toFixed(1) : null, [reviews])

  const openForm = () => {
    setShowForm(v => !v)
    setSubmitted(false)
  }

  const closeForm = () => {
    setShowForm(false)
    setSubmitted(false)
    setForm({ rating: 0, text: '', guestName: '', guestEmail: '' })
    setErrors({})
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const errs: Record<string, string> = {}
    if (!form.rating) errs.rating = 'Choose a rating'
    if (!form.text.trim()) errs.text = 'Enter the review text'
    if (!user && !form.guestName.trim()) errs.guestName = 'Enter your name'
    if (Object.keys(errs).length) { setErrors(errs); return }
    setErrors({})
    setSubmitting(true)
    try {
      await postSiteReview({
        rating: form.rating, text: form.text,
        ...(!user && { guest_name: form.guestName.trim(), guest_email: form.guestEmail.trim() }),
      })
      setSubmitted(true)
      setForm({ rating: 0, text: '', guestName: '', guestEmail: '' })
    } catch {
      setErrors({ general: 'Error while sending. Please try again.' })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">

      {/* Breadcrumb */}
      <nav className="flex items-center gap-1 text-xs sm:text-sm text-text-muted mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        <ChevronRight size={14} />
        <span className="text-text-secondary">Shop reviews</span>
      </nav>

      <h1 className="text-2xl sm:text-3xl font-bold text-text-primary mb-6 sm:mb-8">Shop reviews</h1>

      {/* Panel: rating + button + sorting */}
      <div className="flex items-center gap-3 sm:gap-4 mb-5 sm:mb-6 flex-wrap">
        {avg && (
          <div className="flex items-center gap-2">
            <span className="text-2xl sm:text-3xl font-bold text-text-primary">{avg}</span>
            <div>
              <Stars value={Math.round(Number(avg))} size={18} />
              <p className="text-xs text-text-muted mt-0.5">{reviews.length} reviews</p>
            </div>
          </div>
        )}

        <Button iconLeft={<MessageSquarePlus size={16} />} onClick={openForm}>
          Write a review
        </Button>

        {/* Mobile: custom dropdown in the site style */}
        <div ref={sortRef} className="relative w-full sm:hidden">
          <button
            type="button"
            onClick={() => setSortOpen(v => !v)}
            className="w-full flex items-center justify-between gap-2 bg-surface-muted text-text-secondary text-sm font-bold rounded-xl px-4 py-2.5 transition-colors hover:text-text-primary"
          >
            <span>{SORT_OPTIONS.find(o => o.key === sort)?.label}</span>
            <ChevronDown size={18} className={cn('text-text-muted transition-transform', sortOpen && 'rotate-180')} />
          </button>
          {sortOpen && (
            <div className="absolute top-full left-0 right-0 mt-1 bg-white rounded-xl border border-border shadow-card-high z-20 overflow-hidden">
              {SORT_OPTIONS.map(o => (
                <button
                  key={o.key}
                  type="button"
                  onClick={() => { setSort(o.key); setSortOpen(false) }}
                  className={cn(
                    'w-full text-left px-4 py-2.5 text-sm font-bold transition-colors',
                    sort === o.key
                      ? 'bg-brand-bg text-brand'
                      : 'text-text-secondary hover:bg-surface-muted',
                  )}
                >
                  {o.label}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Desktop: segmented control */}
        <div className="hidden sm:inline-flex sm:ml-auto items-center p-1 bg-surface-muted rounded-xl">
          {SORT_OPTIONS.map(o => (
            <button
              key={o.key}
              type="button"
              onClick={() => setSort(o.key)}
              className={cn(
                'shrink-0 px-4 py-1.5 rounded-lg text-sm font-bold whitespace-nowrap transition-all',
                sort === o.key
                  ? 'bg-white text-brand shadow-sm'
                  : 'text-text-muted hover:text-text-secondary',
              )}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>

      {/* Form (inline) */}
      {showForm && (
        <Card variant="section" className="mb-6 p-5 sm:p-6">
          {submitted ? (
            <div className="text-center py-4">
              <p className="font-bold text-text-primary mb-1">Thank you for your review!</p>
              <p className="text-sm text-text-muted">It will appear after moderation.</p>
              <Button variant="secondary" size="sm" className="mt-3" onClick={closeForm}>
                Close
              </Button>
            </div>
          ) : (
            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <StarPicker value={form.rating} onChange={v => setForm(f => ({ ...f, rating: v }))} />
                {errors.rating && <p className="text-xs text-danger mt-1">{errors.rating}</p>}
              </div>
              {!user && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <Input
                    label="Your name"
                    value={form.guestName}
                    onChange={e => setForm(f => ({ ...f, guestName: e.target.value }))}
                    placeholder="Name"
                    error={errors.guestName}
                  />
                  <Input
                    label="Email (optional)"
                    type="email"
                    value={form.guestEmail}
                    onChange={e => setForm(f => ({ ...f, guestEmail: e.target.value }))}
                    placeholder="you@example.com"
                  />
                </div>
              )}
              <Textarea
                value={form.text}
                onChange={e => setForm(f => ({ ...f, text: e.target.value }))}
                rows={4}
                placeholder="Share your impressions..."
                error={errors.text}
              />
              {errors.general && <p className="text-sm text-danger">{errors.general}</p>}
              <div className="flex gap-3">
                <Button type="submit" loading={submitting} disabled={!form.rating || !form.text.trim()}>
                  Send
                </Button>
                <Button type="button" variant="secondary" onClick={closeForm}
                  className="bg-gray-100 text-gray-600 border-0 hover:bg-gray-200 hover:text-text-primary">
                  Cancel
                </Button>
              </div>
            </form>
          )}
        </Card>
      )}

      {/* List of reviews */}
      {loading ? (
        <div className="space-y-4">
          {[1, 2, 3].map(i => <div key={i} className="h-28 bg-surface-muted rounded-2xl animate-pulse" />)}
        </div>
      ) : sorted.length === 0 ? (
        <div className="py-16 text-center text-text-muted">No reviews yet. Be the first!</div>
      ) : (
        <>
          <div className="space-y-4">
            {paged.map(r => (
              <ReviewItem key={r.id} review={r} canReply={!!user} />
            ))}
          </div>

          {totalPages > 1 && (
            <div className="flex flex-wrap items-center justify-center gap-1 mt-8">
              <button
                onClick={() => goToPage(Math.max(1, page - 1))}
                disabled={page === 1}
                className="w-9 h-9 flex items-center justify-center rounded-xl border border-border text-text-muted hover:border-brand hover:text-brand disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
              >
                <ChevronRight size={16} className="rotate-180" />
              </button>
              {Array.from({ length: totalPages }, (_, i) => i + 1)
                .filter(p => p === 1 || p === totalPages || Math.abs(p - page) <= 2)
                .reduce<(number | '...')[]>((acc, p, i, arr) => {
                  if (i > 0 && p - (arr[i - 1] as number) > 1) acc.push('...')
                  acc.push(p)
                  return acc
                }, [])
                .map((p, i) =>
                  p === '...' ? (
                    <span key={`dots-${i}`} className="w-9 h-9 flex items-center justify-center text-text-muted">…</span>
                  ) : (
                    <button
                      key={p}
                      onClick={() => goToPage(p)}
                      className={cn(
                        'w-9 h-9 flex items-center justify-center rounded-xl text-sm font-bold leading-none pt-px transition-colors',
                        page === p ? 'bg-brand text-white' : 'border border-border text-text-secondary hover:border-brand hover:text-brand',
                      )}
                    >
                      {p}
                    </button>
                  )
                )}
              <button
                onClick={() => goToPage(Math.min(totalPages, page + 1))}
                disabled={page === totalPages}
                className="w-9 h-9 flex items-center justify-center rounded-xl border border-border text-text-muted hover:border-brand hover:text-brand disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
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
