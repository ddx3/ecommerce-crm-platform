import { useEffect, useRef, useState, useCallback } from 'react'
import { MessageCircle, X, Send, Paperclip, FileText } from 'lucide-react'
import { Spinner } from './ui'
import { useAuth } from '../context/AuthContext'
import {
  type ChatConversation,
  type ChatMessage,
  buildWsUrl,
  fetchHistory,
  markRead,
  sendAttachments,
  sendMessageRest,
  startConversation,
} from '../api/chat'

const STORAGE_OPEN = 'chat_widget_open'

function formatTime(iso: string): string {
  const d = new Date(iso)
  return d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit' })
}

function formatBytes(n: number | null): string {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

const MAX_UPLOAD = 10 * 1024 * 1024
const MAX_FILES = 3

interface StagedItem {
  file: File
  thumbUrl: string | null
}

export default function ChatWidget() {
  const { user } = useAuth()
  const [open, setOpen] = useState<boolean>(() => localStorage.getItem(STORAGE_OPEN) === '1')
  const [conv, setConv] = useState<ChatConversation | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [text, setText] = useState('')
  const [unread, setUnread] = useState(0)
  const [connecting, setConnecting] = useState(false)
  const [staged, setStaged] = useState<StagedItem[]>([])
  const [lightbox, setLightbox] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)

  const wsRef = useRef<WebSocket | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)
  const reconnectTimer = useRef<number | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const panelRef = useRef<HTMLDivElement | null>(null)

  // Persist widget open state
  useEffect(() => {
    localStorage.setItem(STORAGE_OPEN, open ? '1' : '0')
  }, [open])

  // Allow opening the chat from outside (e.g. from the hero CTA on the home page).
  useEffect(() => {
    const onOpen = () => setOpen(true)
    window.addEventListener('open-chat', onOpen)
    return () => window.removeEventListener('open-chat', onOpen)
  }, [])

  // If widget was restored as open from localStorage — kick off conversation
  useEffect(() => {
    if (open && !conv && !connecting) {
      ensureConversation().catch(() => {})
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // On open: only FETCH the existing conversation. We don't create a new one — it
  // appears only when the user actually sends the first message.
  const ensureConversation = useCallback(async () => {
    if (conv) return conv
    setConnecting(true)
    try {
      const data = await startConversation()
      if (data.conversation) {
        setConv(data.conversation)
        const hist = await fetchHistory(data.conversation.id)
        setMessages(hist.messages)
        setHasMore(hist.has_more)
        return data.conversation
      }
      // No conversation yet — a clean start, straight into the chat (without a contacts form)
      setMessages([])
      setHasMore(false)
      return null
    } finally {
      setConnecting(false)
    }
  }, [conv])

  // A guest wrote, then logged in in the same window — re-link the current
  // conversation to the account (the backend merges guest_id → user itself) and fetch
  // the history under the new id if the conversation was replaced.
  useEffect(() => {
    if (!user || !conv || conv.user_id) return
    startConversation().then(async data => {
      if (!data.conversation) return
      setConv(data.conversation)
      if (data.conversation.id !== conv.id) {
        const hist = await fetchHistory(data.conversation.id)
        setMessages(hist.messages)
        setHasMore(hist.has_more)
      }
    }).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user])

  const loadOlder = useCallback(async () => {
    if (!conv || loadingMore || !hasMore || messages.length === 0) return
    setLoadingMore(true)
    const list = listRef.current
    const prevHeight = list?.scrollHeight ?? 0
    const prevTop = list?.scrollTop ?? 0
    try {
      const oldest = messages[0]
      const data = await fetchHistory(conv.id, oldest.id)
      if (data.messages.length) {
        setMessages(prev => [...data.messages, ...prev])
        setHasMore(data.has_more)
        // Restore scroll position after prepend
        requestAnimationFrame(() => {
          if (list) list.scrollTop = list.scrollHeight - prevHeight + prevTop
        })
      } else {
        setHasMore(false)
      }
    } finally {
      setLoadingMore(false)
    }
  }, [conv, hasMore, loadingMore, messages])

  // Detect scroll-to-top to load older
  useEffect(() => {
    const list = listRef.current
    if (!list) return
    const onScroll = () => {
      if (list.scrollTop < 40 && hasMore && !loadingMore) {
        loadOlder()
      }
    }
    list.addEventListener('scroll', onScroll, { passive: true })
    return () => list.removeEventListener('scroll', onScroll)
  }, [hasMore, loadingMore, loadOlder, conv?.id])

  // WebSocket lifecycle — only when widget is open AND we have a conversation
  useEffect(() => {
    if (!open || !conv) return

    let cancelled = false

    const connect = () => {
      if (cancelled) return
      const ws = new WebSocket(buildWsUrl(conv.id))
      wsRef.current = ws

      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data)
          if (data.type === 'message') {
            const msg: ChatMessage = data.payload
            setMessages(prev => {
              if (prev.some(m => m.id === msg.id)) return prev
              return [...prev, msg]
            })
            if (msg.sender_type !== 'customer') {
              setUnread(u => (open ? 0 : u + 1))
            }
          } else if (data.type === 'message_updated') {
            const msg: ChatMessage = data.payload
            setMessages(prev => prev.map(m => (m.id === msg.id ? msg : m)))
          } else if (data.type === 'message_deleted') {
            const delId = data.payload.id
            setMessages(prev => prev.filter(m => m.id !== delId))
          } else if (data.type === 'conversation_deleted') {
            // The conversation was deleted by a manager — just reset. We do NOT create a new
            // one: it appears only when the customer writes something again.
            setMessages([])
            setHasMore(false)
            setConv(null)
          }
        } catch {
          // ignore malformed WS payloads
        }
      }

      ws.onclose = () => {
        wsRef.current = null
        if (!cancelled) {
          // simple back-off reconnect
          reconnectTimer.current = window.setTimeout(connect, 2500)
        }
      }
      ws.onerror = () => {
        try { ws.close() } catch {
          // already closed
        }
      }
    }

    connect()
    return () => {
      cancelled = true
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current)
        reconnectTimer.current = null
      }
      if (wsRef.current) {
        wsRef.current.onclose = null
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [open, conv])

  // Auto-scroll on new messages
  useEffect(() => {
    if (!listRef.current) return
    listRef.current.scrollTop = listRef.current.scrollHeight
  }, [messages, open])

  // Block page scroll when wheel/touch happens inside the chat panel.
  // If a scrollable child can absorb the scroll — let it. Otherwise preventDefault.
  useEffect(() => {
    if (!open) return
    const panel = panelRef.current
    if (!panel) return
    const isScrollable = (el: HTMLElement | null): boolean => {
      while (el && el !== panel) {
        if (el.scrollHeight > el.clientHeight) {
          const oy = getComputedStyle(el).overflowY
          if (oy === 'auto' || oy === 'scroll') return true
        }
        el = el.parentElement
      }
      return false
    }
    const onWheel = (e: WheelEvent) => {
      if (!isScrollable(e.target as HTMLElement)) e.preventDefault()
    }
    const onTouchMove = (e: TouchEvent) => {
      if (!isScrollable(e.target as HTMLElement)) e.preventDefault()
    }
    panel.addEventListener('wheel', onWheel, { passive: false })
    panel.addEventListener('touchmove', onTouchMove, { passive: false })
    return () => {
      panel.removeEventListener('wheel', onWheel)
      panel.removeEventListener('touchmove', onTouchMove)
    }
  }, [open])

  // Mark read on open / on new manager message while open
  useEffect(() => {
    if (!open || !conv) return
    setUnread(0)
    markRead(conv.id).catch(() => {})
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: 'read' }))
    }
  }, [open, conv, messages.length])

  const handleOpen = async () => {
    setOpen(true)
    setUnread(0)
    if (!conv) await ensureConversation()
  }

  const clearStaged = () => {
    setStaged(prev => {
      prev.forEach(s => { if (s.thumbUrl) URL.revokeObjectURL(s.thumbUrl) })
      return []
    })
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  const removeStaged = (idx: number) => {
    setStaged(prev => {
      const next = prev.slice()
      const [removed] = next.splice(idx, 1)
      if (removed?.thumbUrl) URL.revokeObjectURL(removed.thumbUrl)
      return next
    })
  }

  const stageFiles = (files: File[]) => {
    setStaged(prev => {
      const next = prev.slice()
      let rejectedSize = 0
      for (const f of files) {
        if (next.length >= MAX_FILES) break
        if (f.size > MAX_UPLOAD) { rejectedSize++; continue }
        next.push({ file: f, thumbUrl: f.type.startsWith('image/') ? URL.createObjectURL(f) : null })
      }
      if (files.length > MAX_FILES - prev.length) {
        setTimeout(() => alert(`You can attach no more than ${MAX_FILES} files`), 0)
      }
      if (rejectedSize) setTimeout(() => alert('File size cannot exceed 10 MB'), 0)
      return next
    })
  }

  const handleSend = async () => {
    const t = text.trim()
    if (!t && staged.length === 0) return
    // Lazy creation: the first message creates the conversation (create:true).
    let activeConv = conv
    if (!activeConv) {
      try {
        const data = await startConversation({ create: true })
        if (!data.conversation) return
        activeConv = data.conversation
        setConv(activeConv)
      } catch {
        return
      }
    }
    setText('')
    if (staged.length > 0) {
      try {
        const msgs = await sendAttachments(activeConv.id, staged.map(s => s.file), t)
        setMessages(prev => {
          const seen = new Set(prev.map(m => m.id))
          return [...prev, ...msgs.filter(m => !seen.has(m.id))]
        })
        clearStaged()
      } catch {
        setText(t)
      }
      return
    }
    // A just-created conversation has no WS yet — the first message goes via REST,
    // later (when useEffect brings up the socket) — via WS.
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ action: 'message', text: t }))
    } else {
      try {
        const msg = await sendMessageRest(activeConv.id, t)
        if (!msg) return
        setMessages(prev => prev.some(m => m.id === msg.id) ? prev : [...prev, msg])
      } catch {
        setText(t)
      }
    }
  }

  return (
    <>
      {/* Floating button */}
      {!open && (
        <button
          onClick={handleOpen}
          type="button"
          className="fixed bottom-4 right-4 sm:bottom-6 sm:right-6 z-50 rounded-full bg-brand text-white shadow-xl hover:bg-brand-hover transition-colors flex items-center justify-center"
          style={{ width: 'clamp(48px, 14vw, 56px)', height: 'clamp(48px, 14vw, 56px)' }}
          aria-label="Open chat"
        >
          <MessageCircle size={26} strokeWidth={2.2} />
          {unread > 0 && (
            <span className="absolute -top-1 -right-1 min-w-[20px] h-5 px-1 rounded-full bg-red-500 text-white text-xs font-semibold flex items-center justify-center ring-2 ring-white">
              {unread > 9 ? '9+' : unread}
            </span>
          )}
        </button>
      )}

      {/* Chat panel */}
      {open && (
        <div ref={panelRef} className="fixed bottom-4 right-4 sm:bottom-6 sm:right-6 z-50 w-[380px] max-w-[calc(100vw-32px)] h-[560px] max-h-[calc(100vh-32px)] bg-white border border-gray-100 rounded-2xl shadow-2xl flex flex-col overflow-hidden">
          {/* Header */}
          <div className="bg-brand text-white px-5 py-3.5 flex items-center justify-between gap-3">
            <div className="flex items-center gap-3 min-w-0">
              <div className="w-9 h-9 rounded-full bg-white/15 flex items-center justify-center shrink-0">
                <MessageCircle size={18} strokeWidth={2.2} />
              </div>
              <div className="font-semibold text-[18px] leading-none min-w-0 truncate">Support</div>
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="p-1.5 rounded-xl shrink-0 text-white hover:text-white/70 transition-colors"
              aria-label="Close chat"
            >
              <X size={20} strokeWidth={2} />
            </button>
          </div>

          {/* Body */}
          <>
            <div ref={listRef} className="flex-1 overflow-y-auto overscroll-contain px-4 py-4 space-y-2 bg-gray-50">
              {connecting && messages.length === 0 && (
                <div className="flex items-center justify-center py-10">
                  <Spinner size="md" tone="brand" />
                </div>
              )}
              {!connecting && messages.length === 0 && (
                <div className="h-full flex flex-col items-center justify-center text-center px-4">
                  <p className="text-base font-bold text-text-primary mb-1">Hello!</p>
                  <p className="text-sm text-text-muted">Write to us — we will reply as soon as possible.</p>
                </div>
              )}
              {loadingMore && (
                <div className="text-center text-xs text-text-muted py-2">Loading…</div>
              )}
              {messages.map(msg => {
                const mine = msg.sender_type === 'customer'
                const att = msg.attachment
                const isImage = !!att && att.kind === 'image'
                const isFile = !!att && att.kind === 'file'
                const hasText = !!msg.text
                const tail = mine ? 'rounded-br-md' : 'rounded-bl-md'
                const time = (
                  <span className={`text-[10px] shrink-0 leading-none pb-[3px] ${mine ? 'text-[#1a5c2a]/55' : 'text-gray-400'}`}>
                    {formatTime(msg.created_at)}
                  </span>
                )

                // Image-only: image inside the same bubble as text messages
                // Time floats over bottom-right corner of the image
                if (isImage && !hasText) {
                  return (
                    <div key={msg.id} className={`flex ${mine ? 'justify-end' : 'justify-start'}`}>
                      <div className={`max-w-[82%] p-1.5 rounded-2xl ${tail} border shadow-sm ${
                        mine
                          ? 'bg-[#eef7ec] border-[#cfe7d2]'
                          : 'bg-white border-gray-100'
                      }`}>
                        <div className="relative">
                          <img
                            src={att.url}
                            alt={att.name}
                            onClick={() => setLightbox(att.url)}
                            className="block max-w-[240px] max-h-[240px] rounded-xl cursor-zoom-in object-cover"
                          />
                          <span className="absolute bottom-1.5 right-1.5 text-[10px] leading-none text-white px-1.5 py-0.5 rounded-full bg-black/45 backdrop-blur-sm">
                            {formatTime(msg.created_at)}
                          </span>
                        </div>
                      </div>
                    </div>
                  )
                }

                // Image + caption: image inside bubble with caption below — same wrapper as image-only
                if (isImage && hasText) {
                  return (
                    <div key={msg.id} className={`flex ${mine ? 'justify-end' : 'justify-start'}`}>
                      <div className={`max-w-[82%] p-1.5 rounded-2xl ${tail} border shadow-sm ${
                        mine
                          ? 'bg-[#eef7ec] border-[#cfe7d2] text-[#1a5c2a]'
                          : 'bg-white border-gray-100 text-gray-800'
                      }`}>
                        <img
                          src={att.url}
                          alt={att.name}
                          onClick={() => setLightbox(att.url)}
                          className="block max-w-[240px] max-h-[240px] w-full rounded-xl cursor-zoom-in object-cover"
                        />
                        <div className="flex items-end gap-2 px-1.5 pt-1.5 pb-0.5 text-sm">
                          <div className="whitespace-pre-wrap break-words flex-1 min-w-0">{msg.text}</div>
                          {time}
                        </div>
                      </div>
                    </div>
                  )
                }

                // File or plain text bubble
                return (
                  <div key={msg.id} className={`flex ${mine ? 'justify-end' : 'justify-start'}`}>
                    <div className={`max-w-[82%] rounded-2xl text-sm ${isFile ? 'p-1.5' : 'px-3.5 py-2'} ${
                      mine
                        ? 'bg-[#eef7ec] border border-[#cfe7d2] text-[#1a5c2a] rounded-br-md shadow-sm'
                        : 'bg-white border border-gray-100 text-gray-800 rounded-bl-md shadow-sm'
                    }`}>
                      {isFile && (
                        <a
                          href={att.url}
                          target="_blank"
                          rel="noreferrer"
                          download={att.name}
                          className={`flex items-center gap-2 px-2.5 py-2 rounded-xl min-w-[180px] max-w-[260px] no-underline ${
                            mine ? 'bg-white/70 hover:bg-white text-[#1a5c2a]' : 'bg-gray-50 hover:bg-gray-100 text-gray-700'
                          }`}
                        >
                          <FileText size={20} className="shrink-0 opacity-80" />
                          <div className="min-w-0 flex-1">
                            <div className="text-[12px] font-medium truncate">{att.name}</div>
                            {att.size != null && <div className="text-[10px] opacity-70 mt-0.5">{formatBytes(att.size)}</div>}
                          </div>
                        </a>
                      )}
                      {hasText ? (
                        <div className={`flex items-end gap-2 ${isFile ? 'mt-1.5 px-1.5 pb-0.5' : ''}`}>
                          <div className="whitespace-pre-wrap break-words flex-1 min-w-0">{msg.text}</div>
                          {time}
                        </div>
                      ) : (
                        <div className={`text-[10px] ${isFile ? 'px-1.5 pb-0.5' : 'mt-1'} ${mine ? 'text-[#1a5c2a]/55' : 'text-gray-400'} text-right`}>
                          {formatTime(msg.created_at)}
                        </div>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>

            {/* Input */}
            <div className="border-t border-gray-100 p-3 bg-white">
              {staged.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mb-2">
                  {staged.map((s, i) => (
                    <div key={i} className="relative inline-flex items-center gap-2 pl-1 pr-6 py-1 bg-gray-50 border border-gray-200 rounded-lg text-xs text-gray-700 max-w-[200px]">
                      {s.thumbUrl ? (
                        <img src={s.thumbUrl} alt="" className="w-6 h-6 rounded object-cover" />
                      ) : (
                        <FileText size={16} className="text-gray-500 shrink-0" />
                      )}
                      <span className="truncate" title={s.file.name}>{s.file.name}</span>
                      <button onClick={() => removeStaged(i)} className="absolute right-1 top-1/2 -translate-y-1/2 text-gray-400 hover:text-red-600 p-0.5" aria-label="Remove" type="button">
                        <X size={16} strokeWidth={2.2} />
                      </button>
                    </div>
                  ))}
                </div>
              )}
              <div className="flex gap-1 items-center bg-white border border-border rounded-xl p-1 focus-within:border-brand transition-colors">
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  className="w-9 h-9 rounded-xl flex items-center justify-center bg-gray-200 text-text-muted hover:bg-gray-300 hover:text-text-secondary transition-colors shrink-0"
                  aria-label="Attach files"
                  title={`Attach files (up to ${MAX_FILES})`}
                >
                  <Paperclip size={18} />
                </button>
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  className="hidden"
                  accept="image/*,application/pdf,.doc,.docx,.xls,.xlsx,.csv,.txt,.zip,.rar,.7z"
                  onChange={e => {
                    const fs = Array.from(e.target.files || [])
                    if (fs.length) stageFiles(fs)
                    e.target.value = ''
                  }}
                />
                <textarea
                  value={text}
                  onChange={e => setText(e.target.value)}
                  onKeyDown={e => {
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault()
                      handleSend()
                    }
                  }}
                  onPaste={e => {
                    const items = e.clipboardData?.items
                    if (!items) return
                    const pasted: File[] = []
                    for (let i = 0; i < items.length; i++) {
                      const it = items[i]
                      if (it.kind === 'file' && it.type.startsWith('image/')) {
                        const f = it.getAsFile()
                        if (!f) continue
                        const ext = (f.type.split('/')[1] || 'png').replace('jpeg', 'jpg')
                        const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
                        pasted.push(new File([f], `screenshot-${stamp}-${pasted.length + 1}.${ext}`, { type: f.type }))
                      }
                    }
                    if (pasted.length) {
                      e.preventDefault()
                      stageFiles(pasted)
                    }
                  }}
                  placeholder="Type a message…"
                  rows={1}
                  className="flex-1 resize-none px-2 py-2 text-sm bg-transparent focus:outline-none max-h-32 leading-snug placeholder:text-gray-400"
                />
                <button
                  type="button"
                  onClick={handleSend}
                  disabled={!text.trim() && staged.length === 0}
                  className="w-9 h-9 rounded-xl flex items-center justify-center bg-brand text-white hover:bg-brand-hover transition-colors shrink-0 disabled:bg-gray-200 disabled:text-text-muted"
                  aria-label="Send"
                >
                  <Send size={16} strokeWidth={2.4} className="-translate-x-[1px] translate-y-[1px]" />
                </button>
              </div>
            </div>
          </>
        </div>
      )}

      {lightbox && (
        <div
          onClick={() => setLightbox(null)}
          className="fixed inset-0 z-[60] bg-black/85 flex items-center justify-center cursor-zoom-out"
        >
          <img src={lightbox} alt="" className="max-w-[92vw] max-h-[92vh] object-contain rounded" />
        </div>
      )}
    </>
  )
}
