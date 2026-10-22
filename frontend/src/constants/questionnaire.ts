// Single source of truth for questionnaire labels.
// The backend stores keys — the frontend renders the captions.
// Keep in sync with backend/users/models.py (choices)
// and backend/users/views.py (QUESTIONNAIRE_FIELDS / INTEREST_KEYS).

import type { Gender, ReferralSource, ReferralSocial, Interest } from '../api/auth'

export const GENDER_OPTIONS: { key: Gender; label: string }[] = [
  { key: 'female', label: 'Female' },
  { key: 'male', label: 'Male' },
  { key: 'na', label: 'Prefer not to say' },
]

export const REFERRAL_SOURCE_OPTIONS: { key: ReferralSource; label: string }[] = [
  { key: 'friends', label: 'From friends' },
  { key: 'social',  label: 'Social media' },
  { key: 'google',  label: 'Google / search' },
  { key: 'youtube', label: 'YouTube' },
  { key: 'ad',      label: 'Advertising' },
  { key: 'other',   label: 'Other' },
]

export const REFERRAL_SOCIAL_OPTIONS: { key: ReferralSocial; label: string }[] = [
  { key: 'instagram', label: 'Instagram' },
  { key: 'facebook',  label: 'Facebook' },
  { key: 'tiktok',    label: 'TikTok' },
  { key: 'other',     label: 'Other' },
]

export const INTEREST_OPTIONS: { key: Interest; label: string }[] = [
  { key: 'seeds',       label: 'Seeds' },
  { key: 'seedlings',   label: 'Seedlings' },
  { key: 'fertilizers', label: 'Fertilizers' },
  { key: 'protection',  label: 'Plant protection' },
  { key: 'tools',       label: 'Tools' },
  { key: 'indoor',      label: 'Houseplants' },
  { key: 'lawn',        label: 'Lawn / landscape' },
  { key: 'other',       label: 'Other' },
]

export const MONTHS_UA = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
]

// Number of logical questionnaire fields for the progress bar:
// gender, birth_date, gardening_text, interests, referral_source, wishes.
export const QUESTIONNAIRE_TOTAL_FIELDS = 6
