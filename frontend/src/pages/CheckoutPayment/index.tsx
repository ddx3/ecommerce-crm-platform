import { useSearchParams, Link } from 'react-router-dom'
import { Copy, Check, Upload, FileIcon } from 'lucide-react'
import { useState, useRef, useEffect } from 'react'
import { Card } from '../../components/ui'
import Button from '../../components/ui/Button'
import client from '../../api/client'
import { useSiteSettings } from '../../context/SiteSettingsContext'
import GuestSavePrompt from '../../components/GuestSavePrompt'
import { flushPurchase } from '../../lib/analytics'

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  const handle = () => {
    navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }
  return (
    <button
      onClick={handle}
      aria-label="Copy card number"
      className="w-9 h-9 sm:w-7 sm:h-7 inline-flex items-center justify-center shrink-0 text-gray-400 hover:text-gray-600 transition-colors"
    >
      {copied ? <Check size={15} className="text-brand" /> : <Copy size={15} />}
    </button>
  )
}

function CardBlock({ bank, card, name }: { bank: string; card: string; name: string }) {
  return (
    <Card variant="action" className="p-5">
      <p className="text-xs text-text-muted uppercase tracking-wide mb-3">{bank}</p>
      <div className="flex items-center gap-1 mb-1">
        <span className="text-lg font-bold text-text-primary tracking-widest">{card}</span>
        <CopyButton text={card.replace(/\s/g, '')} />
      </div>
      <p className="text-sm text-text-muted">{name}</p>
    </Card>
  )
}

export default function CheckoutPaymentPage() {
  const [params] = useSearchParams()
  const orderNumber = params.get('order_id') || ''
  const total = params.get('total') || ''
  // t — guest access token for payment actions (IDOR protection).
  const token = params.get('t') || ''
  const email = (params.get('email') || '').trim()

  // Purchase: order created (card prepayment) — send purchase from the snapshot. Idempotent.
  useEffect(() => {
    if (orderNumber) flushPurchase(orderNumber)
  }, [orderNumber])

  const { paymentCards, thresholds } = useSiteSettings()
  // null until the payment details have loaded — show a loader instead of cards.
  // We determine the needed threshold among ALL thresholds (including hidden groups),
  // and show only the active cards of that threshold. If they are hidden — empty,
  // we do NOT substitute another group (its details are sent personally).
  const cards = (() => {
    if (!paymentCards) return null
    const all = thresholds.length ? thresholds : paymentCards.map(c => c.threshold)
    if (!all.length) return []
    const sum = Number(total) || 0
    const applicable = all.filter(t => t <= sum)
    const chosen = applicable.length ? Math.max(...applicable) : Math.min(...all)
    return paymentCards.filter(c => c.threshold === chosen)
  })()
  // No active payment details for this amount (hidden) → we'll send the details to the
  // phone; don't show the card payment page.
  const cardsEmpty = cards !== null && cards.length === 0

  const [requested, setRequested] = useState(false)
  const [requesting, setRequesting] = useState(false)
  const [requestError, setRequestError] = useState(false)

  const handleRequestDetails = async () => {
    if (!orderNumber) return
    setRequesting(true)
    setRequestError(false)
    try {
      // The axios client adds JWT (for logged-in users) and anti-bot fields; guest
      // orders (user=null) are accepted by the backend by order_number + public_token.
      await client.post(`/orders/${orderNumber}/request-details/`, { token })
      setRequested(true)
    } catch {
      setRequestError(true)
    } finally {
      setRequesting(false)
    }
  }

  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploaded, setUploaded] = useState(false)
  const [alreadyPaid, setAlreadyPaid] = useState(false)
  // null — no error; a string — the specific reason (shown to the customer).
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [drag, setDrag] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  // Consistent with the backend (MAX_UPLOAD_BYTES = 8 MB) — catch an oversized file
  // before sending, to avoid sending megabytes for nothing and to give a clear error.
  const MAX_UPLOAD = 8 * 1024 * 1024

  const handleFile = (f: File) => {
    setUploadError(null)
    if (f.size > MAX_UPLOAD) {
      setUploadError('File is too large — 8 MB maximum. Compress it or take a screenshot.')
      return
    }
    setFile(f)
    setPreview(f.type.startsWith('image/') ? URL.createObjectURL(f) : null)
    setUploaded(false)
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    setDrag(false)
    const f = e.dataTransfer.files[0]
    if (f) handleFile(f)  // accept PDF too, not only images
  }

  const handleUpload = async () => {
    if (!file || !orderNumber) return
    setUploading(true)
    setUploadError(null)
    const fd = new FormData()
    fd.append('screenshot', file)
    if (token) fd.append('token', token)
    try {
      const res = await client.post(`/orders/${orderNumber}/screenshot/`, fd, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      setAlreadyPaid(!!res.data?.already_paid)
      setUploaded(true)
    } catch (e) {
      const err = e as { response?: { status?: number; data?: { error?: string } } }
      const status = err.response?.status
      console.error('screenshot upload failed', status, err.response?.data)
      // The backend returns a clear text in {error} — show it; otherwise go by status.
      let msg = err.response?.data?.error || 'Could not send the screenshot. Please try again.'
      if (status === 429) msg = 'Too many attempts. Wait a minute and try again.'
      else if (status === 404) msg = 'Order not found or the link has expired.'
      setUploadError(msg)
    } finally {
      setUploading(false)
    }
  }

  return (
    <div className="max-w-lg mx-auto px-4 sm:px-6 py-8 sm:py-12">
      <h1 className="text-xl sm:text-2xl font-bold text-gray-900 mb-5 sm:mb-6">
        {cardsEmpty ? 'Order placed' : 'Payment details'}
      </h1>

      {cardsEmpty ? (
        <p className="text-sm text-gray-700 mb-6">
          We will send you the payment details to your phone number.
        </p>
      ) : (
        <div className="flex flex-col gap-3 mb-6">
          {cards === null ? (
            <div className="flex justify-center py-10">
              <div className="w-8 h-8 border-4 border-gray-200 border-t-brand rounded-full animate-spin" />
            </div>
          ) : (
            cards.map(c => (
              <CardBlock key={c.card} bank={c.bank} card={c.card} name={c.name} />
            ))
          )}
        </div>
      )}

      {!cardsEmpty && (<>
      <div className="bg-amber-50 border border-amber-100 rounded-2xl px-5 py-4 text-sm text-amber-800 mb-8">
        <p className="font-bold mb-1">Important!</p>
        <p>In the payment purpose, please enter: <span className="font-medium">for garden and vegetable plot goods according to order #{orderNumber}</span></p>
        <p className="mt-2">After paying, please send us a screenshot or the receipt.</p>
      </div>

      <div className="mb-8">
        <p className="text-base font-bold text-gray-800 mb-3">Send a payment screenshot</p>
        <div
          onClick={() => !preview && inputRef.current?.click()}
          onDragOver={e => { e.preventDefault(); setDrag(true) }}
          onDragLeave={() => setDrag(false)}
          onDrop={handleDrop}
          className={`border-2 border-dashed rounded-2xl transition-colors ${drag ? 'border-brand bg-brand-bg' : 'border-border bg-surface-muted'} ${!file ? 'cursor-pointer hover:border-border-strong' : ''}`}
        >
          {file ? (
            <div className="relative p-3">
              {preview
                ? <img src={preview} alt="screenshot" className="w-full max-h-64 object-contain rounded-xl" />
                : <div className="flex items-center gap-3 py-4 px-2">
                    <FileIcon size={28} className="text-gray-400 shrink-0" />
                    <p className="text-sm text-gray-700 truncate">{file.name}</p>
                  </div>
              }
              <button
                onClick={() => { setFile(null); setPreview(null); setUploaded(false); setUploadError(null) }}
                aria-label="Remove file"
                className="absolute top-4 right-4 bg-white border border-gray-200 rounded-full w-9 h-9 flex items-center justify-center text-gray-400 hover:text-gray-600 transition-colors text-base"
              >×</button>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center py-10 gap-2 text-gray-400">
              <Upload size={28} className="text-gray-300" />
              <p className="text-sm">Click or drag a screenshot / PDF here</p>
            </div>
          )}
        </div>
        <input ref={inputRef} type="file" accept="image/*,.pdf" className="hidden" onChange={e => { const f = e.target.files?.[0]; if (f) handleFile(f) }} />
        {file && !uploaded && (
          <Button
            onClick={handleUpload}
            disabled={uploading}
            fullWidth
            className="mt-3"
            iconLeft={<Upload size={16} />}
          >
            {uploading ? 'Sending...' : 'Send'}
          </Button>
        )}
        {uploaded && (
          <div className="mt-3 flex items-center gap-2 text-brand text-sm font-medium justify-center text-center">
            <Check size={16} className="shrink-0" />
            {alreadyPaid ? 'The order is already paid. Thank you for the receipt!' : 'Screenshot received, thank you!'}
          </div>
        )}
        {uploadError && (
          <p className="mt-3 text-sm text-red-600 text-center">{uploadError}</p>
        )}
      </div>

      <div className="mb-6">
        <button
          onClick={handleRequestDetails}
          disabled={requesting || requested}
          className="w-full border border-gray-200 hover:enabled:border-gray-400 hover:enabled:bg-gray-50 text-gray-800 font-bold py-3 rounded-2xl transition-colors text-sm disabled:cursor-default"
        >
          {requested ? 'Request sent, we will contact you' : requesting ? '...' : 'Send me the payment details by phone'}
        </button>
        {requestError && (
          <p className="mt-2 text-sm text-red-600 text-center">
            Could not send the request. Please try again.
          </p>
        )}
      </div>
      </>)}

      <GuestSavePrompt email={email} />

      <div className="flex gap-3">
        <Button as={Link} to="/orders" className="flex-1">
          My orders
        </Button>
        <Link to="/catalog" className="flex-1 text-center border border-gray-200 hover:border-gray-400 hover:bg-gray-50 text-gray-700 font-bold py-3 rounded-2xl transition-colors">
          Go to catalog
        </Link>
      </div>
    </div>
  )
}
