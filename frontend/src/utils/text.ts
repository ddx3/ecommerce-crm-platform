// Don't leave an orphan: a short last chunk of a name (unit of measure — "g", "l",
// "ml", "pcs", etc.; the Ukrainian storefront data has "г", "л", "мл", "шт") is glued with a
// non-breaking space to the previous word so that, for example, "100 g" doesn't break with
// "g" falling onto its own line.
export function noWidow(s: string): string {
  return s.replace(/\s+(\S{1,3})\s*$/u, ' $1')
}
