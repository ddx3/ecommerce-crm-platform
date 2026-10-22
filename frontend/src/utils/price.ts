// Single price rule on the storefront: always whole hryvnias (no kopecks).
// Consistent with the backend (orders: the final item price is quantized to an integer).

/** Round a price to an integer (mathematical round, 643.5 → 644). */
export const roundPrice = (n: number | string | null | undefined): number =>
  Math.round(Number(n) || 0)

/** Price display: an integer with thousands separators (Ukrainian locale), no kopecks. */
export const formatPrice = (n: number | string | null | undefined): string =>
  roundPrice(n).toLocaleString('uk-UA')

// Default minimum order amount — applies only until SiteSettingsContext has
// loaded the real value from the backend (CRM → Settings). Do not use
// directly in components: take minOrderAmount from useCart()/useSiteSettings().
export const DEFAULT_MIN_ORDER_AMOUNT = 250
