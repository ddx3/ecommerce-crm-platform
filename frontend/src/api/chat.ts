import client from './client'

export interface ChatAttachment {
  url: string
  name: string
  kind: 'image' | 'file'
  ext: string
  size: number | null
}

export interface ChatMessage {
  id: number
  conversation_id: number
  sender_type: 'customer' | 'manager' | 'system' | 'bot'
  sender_name: string
  text: string
  attachment: ChatAttachment | null
  created_at: string
  edited_at: string | null
}

export interface ChatConversation {
  id: number
  display_name: string
  status: 'open' | 'closed'
  user_id: number | null
  guest_id: string
  guest_name?: string
  guest_email?: string
  guest_phone?: string
  assigned_to_id: number | null
  created_at: string
  last_message_at: string | null
  unread_for_manager: number
  unread_for_customer: number
}

const GUEST_KEY = 'chat_guest_id'

export function getGuestId(): string {
  return localStorage.getItem(GUEST_KEY) || ''
}

export function setGuestId(id: string) {
  if (id) localStorage.setItem(GUEST_KEY, id)
}

function guestHeader(): Record<string, string> {
  const g = getGuestId()
  return g ? { 'X-Chat-Guest': g } : {}
}

export interface StartArgs {
  guest_name?: string
  guest_email?: string
  guest_phone?: string
  // true — create the conversation (first message); otherwise only fetch the existing one
  create?: boolean
}

export async function startConversation(args: StartArgs = {}) {
  const guest_id = getGuestId()
  const { data } = await client.post<{
    conversation: ChatConversation | null
    guest_id: string
    new_guest: boolean
  }>('/chat/start/', { guest_id, ...args })
  setGuestId(data.guest_id)
  return data
}

export async function fetchHistory(conversationId: number, before?: number) {
  const params: Record<string, string> = { limit: '50' }
  if (before) params.before = String(before)
  const { data } = await client.get<{ messages: ChatMessage[]; has_more: boolean }>(
    `/chat/conversations/${conversationId}/messages/`,
    { params, headers: { ...guestHeader() } },
  )
  return data
}

export async function sendMessageRest(conversationId: number, text: string) {
  // The backend returns {messages: [...]} (an array). The old {message} is supported too.
  const { data } = await client.post<{ messages?: ChatMessage[]; message?: ChatMessage }>(
    `/chat/conversations/${conversationId}/messages/`,
    { text },
    { headers: { ...guestHeader() } },
  )
  return data.messages?.[0] ?? data.message ?? null
}

export async function sendAttachments(conversationId: number, files: File[], text?: string) {
  const fd = new FormData()
  if (text && text.trim()) fd.append('text', text.trim())
  files.forEach(f => fd.append('file', f))
  const { data } = await client.post<{ messages?: ChatMessage[]; message?: ChatMessage }>(
    `/chat/conversations/${conversationId}/messages/`,
    fd,
    { headers: { ...guestHeader(), 'Content-Type': 'multipart/form-data' } },
  )
  return data.messages || (data.message ? [data.message] : [])
}

export async function markRead(conversationId: number) {
  await client.post(`/chat/conversations/${conversationId}/read/`, {}, {
    headers: { ...guestHeader() },
  })
}

export function buildWsUrl(conversationId: number): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const params = new URLSearchParams()
  const token = localStorage.getItem('token')
  if (token) params.set('token', token)
  const guest = getGuestId()
  if (guest) params.set('guest', guest)
  return `${proto}//${window.location.host}/ws/chat/${conversationId}/?${params.toString()}`
}
