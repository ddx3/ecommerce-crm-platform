import { useState, useEffect } from 'react'
import type { FormEvent, TextareaHTMLAttributes } from 'react'
import { User, MessageSquare, MessageSquarePlus } from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import { useUI } from '../context/UIContext'
import type { SiteReview } from '../api/catalog'
import { postReviewReply } from '../api/catalog'
import { Button, Card, Input } from './ui'
import { cn } from '../utils/cn'

function formatDate(iso: string) {
  const d = new Date(iso)
  return d.toLocaleDateString('uk-UA', { day: '2-digit', month: '2-digit', year: 'numeric' })
    + ' at ' + d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit' })
}

function userDisplayName(user: { first_name?: string; last_name?: string; email?: string } | null): string {
  if (!user) return ''
  return [user.first_name, user.last_name].filter(Boolean).join(' ') || user.email || 'User'
}

// Textarea styled like the design system's Input (same wrapper/border/focus).
function Textarea({ label, error, className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement> & { label?: string; error?: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      {label && <label className="block text-sm font-bold text-text-secondary">{label}</label>}
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

interface ReviewReplyResponse {
  id: number
  parent_id: number | null
  detail?: string
}

interface PostReviewResponse {
  detail?: string
}

interface Props {
  slug: string
  getReviews: (slug: string) => Promise<SiteReview[]>
  postReview: (slug: string, data: { rating: number; text: string; variant_id?: number; guest_name?: string; guest_email?: string }) => Promise<PostReviewResponse>
  onCountLoad?: (count: number) => void
  variantId?: number
  variantName?: string
}

type FlatReply = { id: number; author_name: string; text: string; created_at: string; parent_id: number | null; is_admin?: boolean }

function ReplyForm({ reviewId, replyId, onSuccess, onCancel }: {
  reviewId: number
  replyId?: number
  onSuccess: (reply: FlatReply) => void
  onCancel: () => void
}) {
  const { user } = useAuth()
  const [text, setText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [done, setDone] = useState('')

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (!text.trim()) return
    setSubmitting(true)
    try {
      const res = await postReviewReply(reviewId, { text, reply_id: replyId }) as ReviewReplyResponse
      const isApproved = res?.detail?.includes('published')
      if (isApproved) {
        onSuccess({ id: res.id, author_name: userDisplayName(user), text, created_at: new Date().toISOString(), parent_id: res.parent_id, is_admin: !!(user?.is_staff || user?.is_superuser) })
      } else {
        setDone(res?.detail || 'Sent')
      }
      setText('')
    } finally {
      setSubmitting(false)
    }
  }

  if (done) return <p className="mt-2 text-sm text-text-muted">{done}</p>

  return (
    <form onSubmit={handleSubmit} className="mt-3 space-y-3">
      <Textarea value={text} onChange={e => setText(e.target.value)}
        rows={2} placeholder="Your reply..." autoFocus />
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

function ReplyNode({ reply, allReplies, reviewId, canReply, onAdd, depth = 0 }: {
  reply: FlatReply
  allReplies: FlatReply[]
  reviewId: number
  canReply: boolean
  onAdd: (r: FlatReply) => void
  depth?: number
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
            <button type="button" onClick={handleReplyClick} className="mt-1 text-sm font-medium text-brand hover:underline">
              {canReply ? 'Reply' : 'Log in to reply'}
            </button>
          )}
        </div>
      </div>
      {replyOpen && (
        <div className="pl-4 mt-2">
          <ReplyForm reviewId={reviewId} replyId={reply.id}
            onSuccess={r => { onAdd(r); setReplyOpen(false) }}
            onCancel={() => setReplyOpen(false)} />
        </div>
      )}
      {children.length > 0 && (
        <div className="mt-3 ml-4 space-y-3">
          {children.map(child => (
            <ReplyNode key={child.id} reply={child} allReplies={allReplies} reviewId={reviewId}
              canReply={canReply} onAdd={onAdd} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  )
}

function CommentItem({ review, canReply }: {
  review: SiteReview
  canReply: boolean
}) {
  const { openAuth } = useUI()
  const [replyOpen, setReplyOpen] = useState(false)
  const [pendingReply, setPendingReply] = useState(false)
  const [localReplies, setLocalReplies] = useState<FlatReply[]>(
    review.replies.map(r => ({ ...r, parent_id: (r as { parent_id?: number | null }).parent_id ?? null }))
  )

  // After login (the guest clicked "Log in to reply") — open the form
  // right away.
  useEffect(() => {
    if (canReply && pendingReply) { setReplyOpen(true); setPendingReply(false) }
  }, [canReply, pendingReply])

  const handleReplyClick = () => canReply ? setReplyOpen(true) : (setPendingReply(true), openAuth('login'))

  const addReply = (r: FlatReply) => setLocalReplies(prev => [...prev, r])

  const rootReplies = localReplies.filter(r => r.parent_id === review.id || r.parent_id === null)

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
            {review.variant_name && (
              <span className="text-xs font-medium text-brand bg-brand/10 px-2 py-0.5 rounded-md">{review.variant_name}</span>
            )}
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
            <button type="button" onClick={handleReplyClick} className="mt-2 text-sm font-medium text-brand hover:underline">
              {canReply ? 'Reply' : 'Log in to reply'}
            </button>
          )}
          {replyOpen && (
            <ReplyForm reviewId={review.id}
              onSuccess={r => { addReply(r); setReplyOpen(false) }}
              onCancel={() => setReplyOpen(false)} />
          )}
        </div>
      </div>
    </Card>
  )
}

export default function ReviewsAndQuestions({ slug, getReviews, postReview, onCountLoad, variantId }: Props) {
  const { user } = useAuth()
  const [reviews, setReviews] = useState<SiteReview[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [text, setText] = useState('')
  const [guestName, setGuestName] = useState('')
  const [guestEmail, setGuestEmail] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [submitMsg, setSubmitMsg] = useState('')
  const [errors, setErrors] = useState<Record<string, string>>({})

  useEffect(() => {
    getReviews(slug)
      .then(data => { setReviews(data); onCountLoad?.(data.length) })
      .catch(() => { /* ignore */ })
      .finally(() => setLoading(false))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug])

  const openForm = () => {
    setShowForm(true)
    setSubmitted(false)
  }

  const closeForm = () => {
    setShowForm(false)
    setSubmitted(false)
    setText('')
    setGuestName('')
    setGuestEmail('')
    setErrors({})
  }

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    const errs: Record<string, string> = {}
    if (!text.trim()) errs.text = 'Enter some text'
    if (!user && !guestName.trim()) errs.guestName = 'Enter your name'
    if (Object.keys(errs).length) { setErrors(errs); return }
    setErrors({})
    setSubmitting(true)
    try {
      const res = await postReview(slug, {
        rating: 5, text, variant_id: variantId,
        ...(!user && { guest_name: guestName.trim(), guest_email: guestEmail.trim() }),
      })
      setSubmitMsg(res?.detail || '')
      setSubmitted(true)
      setText('')
      if (user?.is_staff || user?.is_superuser) {
        setReviews(prev => [...prev, {
          id: Date.now(),
          author_name: userDisplayName(user),
          rating: 5,
          text,
          created_at: new Date().toISOString(),
          replies: [],
        }])
        setShowForm(false)
        setSubmitted(false)
      }
    } catch {
      setErrors({ general: 'Error while sending. Please try again.' })
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div>
      {showForm && (
        <Card variant="section" className="mb-4 p-5 sm:p-6">
          {submitted ? (
            <div className="text-center py-4">
              <p className="font-bold text-text-primary mb-1">{submitMsg || 'Comment sent!'}</p>
              <Button type="button" variant="secondary" size="sm" className="mt-3" onClick={closeForm}>
                Close
              </Button>
            </div>
          ) : (
            <form onSubmit={handleSubmit} className="space-y-4">
              {!user && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <Input
                    label="Your name"
                    value={guestName}
                    onChange={e => setGuestName(e.target.value)}
                    placeholder="Name"
                    error={errors.guestName}
                  />
                  <Input
                    label="Email (optional)"
                    type="email"
                    value={guestEmail}
                    onChange={e => setGuestEmail(e.target.value)}
                    placeholder="you@example.com"
                  />
                </div>
              )}
              <Textarea
                value={text}
                onChange={e => setText(e.target.value)}
                rows={4}
                placeholder="Write your comment..."
                error={errors.text}
              />
              {errors.general && <p className="text-sm text-danger">{errors.general}</p>}
              <div className="flex gap-3">
                <Button type="submit" loading={submitting} disabled={!text.trim()}>
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

      {loading ? (
        <div className="space-y-4">{[1, 2].map(i => <div key={i} className="h-24 bg-surface-muted rounded-2xl animate-pulse" />)}</div>
      ) : reviews.length === 0 ? (
        !showForm && (
          <div className="py-12 flex flex-col items-center text-center">
            <div className="w-14 h-14 rounded-full bg-brand-bg text-brand flex items-center justify-center mb-3">
              <MessageSquare size={22} />
            </div>
            <p className="text-sm font-bold text-text-primary mb-1">No comments yet</p>
            <p className="text-xs text-text-muted mb-4">Be the first to share your thoughts</p>
            <Button type="button" iconLeft={<MessageSquarePlus size={16} />} onClick={openForm}>
              Write a comment
            </Button>
          </div>
        )
      ) : (
        <>
          <div className="space-y-4">
            {reviews.map(r => (
              <CommentItem key={r.id} review={r} canReply={!!user} />
            ))}
          </div>
          {!showForm && (
            <div className="mt-6 flex justify-center">
              <Button type="button" iconLeft={<MessageSquarePlus size={16} />} onClick={openForm}>
                Write a comment
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
