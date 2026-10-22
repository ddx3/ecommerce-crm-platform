import DOMPurify from 'dompurify'

const ALLOWED_TAGS = [
  'a', 'b', 'i', 'em', 'strong', 'u', 'p', 'br', 'span', 'div',
  'ul', 'ol', 'li', 'blockquote', 'pre', 'code',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'table', 'thead', 'tbody', 'tr', 'td', 'th',
  'img', 'figure', 'figcaption',
  'sub', 'sup', 'hr', 's', 'strike',
]

const ALLOWED_ATTR = [
  'href', 'src', 'alt', 'title', 'target', 'rel',
  'width', 'height', 'class', 'style',
  'colspan', 'rowspan',
]

export function sanitizeHtml(html: string | null | undefined): string {
  if (!html) return ''
  const clean = DOMPurify.sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ALLOW_DATA_ATTR: false,
    FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed', 'link', 'meta'],
    FORBID_ATTR: ['onerror', 'onload', 'onclick', 'onmouseover'],
  })
  return wrapTables(clean)
}

// Tables from the description (Summernote) have their own widths and nowrap headers — on
// mobile they are wider than the screen. Wrap each in .table-scroll, which gives
// horizontal scroll (finger swipe) instead of overflowing the viewport.
function wrapTables(html: string): string {
  if (!html.includes('<table') || typeof document === 'undefined') return html
  const tpl = document.createElement('template')
  tpl.innerHTML = html
  tpl.content.querySelectorAll('table').forEach(table => {
    if (table.parentElement?.classList.contains('table-scroll')) return
    const wrap = document.createElement('div')
    wrap.className = 'table-scroll'
    table.replaceWith(wrap)
    wrap.appendChild(table)
  })
  return tpl.innerHTML
}
