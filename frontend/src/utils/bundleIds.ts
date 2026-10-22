/**
 * A bundle no longer has 'variants': on the storefront it has a single id - Bundle.id.
 * The localStorage of people who visited before the update holds cart/favorites rows with
 * variantId = the old variant pk (and a key composed from it). Here, once per
 * browser, we pull the current id by slug and rewrite such rows, so the bundle
 * doesn't get duplicated in the cart and the heart on the bundle page lights up.
 */
interface BundleLine {
  key: string
  productSlug: string
  variantId: number
  isBundle?: boolean
}

/** slug -> current bundle id. null - nothing to do (already normalized or there are no bundles). */
export async function resolveBundleIds(items: BundleLine[], flag: string): Promise<Map<string, number> | null> {
  try { if (localStorage.getItem(flag)) return null } catch { return null }
  const setFlag = () => { try { localStorage.setItem(flag, '1') } catch { /* ignore */ } }
  const slugs = Array.from(new Set(items.filter(i => i.isBundle).map(i => i.productSlug)))
  if (slugs.length === 0) { setFlag(); return null }
  const { getBundle } = await import('../api/catalog')
  const ids = new Map<string, number>()
  let transient = false
  await Promise.all(slugs.map(async slug => {
    try {
      const data = await getBundle(slug)
      if (typeof data?.id === 'number') ids.set(slug, data.id)
    } catch (e) {
      // 404 (the bundle was removed) is also an answer; on a network failure we try again next
      // time.
      if ((e as { response?: { status?: number } })?.response?.status !== 404) transient = true
    }
  }))
  if (!transient) setFlag()
  return ids.size ? ids : null
}

/** Pure rewrite of rows to the current ids; duplicates (an old + a new row of the same bundle) are merged. */
export function remapBundleLines<T extends BundleLine>(
  prev: T[],
  ids: Map<string, number>,
  makeKey: (slug: string, id: number) => string,
  merge: (a: T, b: T) => T,
): T[] {
  const out = new Map<string, T>()
  let order = 0
  for (const it of prev) {
    const id = it.isBundle ? ids.get(it.productSlug) : undefined
    const line = id != null && it.variantId !== id
      ? { ...it, variantId: id, key: makeKey(it.productSlug, id) }
      : it
    const k = line.key || `__${order++}`
    const have = out.get(k)
    out.set(k, have ? merge(have, line) : line)
  }
  return Array.from(out.values())
}
