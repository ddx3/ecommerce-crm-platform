import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** Concatenate classNames with conditional logic + Tailwind dedupe. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}
