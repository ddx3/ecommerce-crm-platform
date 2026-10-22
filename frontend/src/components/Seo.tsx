import { useEffect } from 'react'

/**
 * Client-side management of SEO tags during SPA navigation.
 *
 * The server shell (Django) injects <head> on the first load — for bots
 * and social networks. But on navigation inside the SPA (no reload) the head
 * does not update by itself. This component upserts the same tags (does NOT duplicate — updates
 * existing ones,
 * creates missing ones), so <title>/description/canonical always match the page.
 *
 * We do NOT set JSON-LD here — only the shell holds it (single source, no duplicates).
 */

const SITE_NAME = 'Magiya Roslyn'

function upsertMeta(attr: 'name' | 'property', key: string, content: string) {
  let el = document.head.querySelector<HTMLMetaElement>(`meta[${attr}="${key}"]`)
  if (!el) {
    el = document.createElement('meta')
    el.setAttribute(attr, key)
    document.head.appendChild(el)
  }
  el.setAttribute('content', content)
}

function upsertLink(rel: string, href: string) {
  let el = document.head.querySelector<HTMLLinkElement>(`link[rel="${rel}"]`)
  if (!el) {
    el = document.createElement('link')
    el.setAttribute('rel', rel)
    document.head.appendChild(el)
  }
  el.setAttribute('href', href)
}

export interface SeoProps {
  title: string
  description?: string
  /** Path for canonical (e.g. /product/slug). Defaults to the current path without the query. */
  canonicalPath?: string
  image?: string | null
  noindex?: boolean
  ogType?: string
}

export default function Seo({
  title,
  description,
  canonicalPath,
  image,
  noindex = false,
  ogType = 'website',
}: SeoProps) {
  useEffect(() => {
    const fullTitle = title.includes(SITE_NAME) ? title : `${title} | ${SITE_NAME}`
    document.title = fullTitle

    upsertMeta('name', 'robots', noindex ? 'noindex,follow' : 'index,follow')
    if (description) upsertMeta('name', 'description', description)

    const origin = window.location.origin
    const canonical = origin + (canonicalPath ?? window.location.pathname)
    upsertLink('canonical', canonical)

    upsertMeta('property', 'og:type', ogType)
    upsertMeta('property', 'og:site_name', SITE_NAME)
    upsertMeta('property', 'og:title', fullTitle)
    upsertMeta('property', 'og:url', canonical)
    if (description) upsertMeta('property', 'og:description', description)
    if (image) upsertMeta('property', 'og:image', image)
  }, [title, description, canonicalPath, image, noindex, ogType])

  return null
}
