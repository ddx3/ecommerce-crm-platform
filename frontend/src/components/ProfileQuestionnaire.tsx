import { useMemo, useState } from 'react'
import { Check } from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import { apiUpdateProfile } from '../api/auth'
import type {
  AuthUser, Gender, ReferralSource, ReferralSocial, Interest,
} from '../api/auth'
import { cn } from '../utils/cn'
import Button from './ui/Button'
import {
  GENDER_OPTIONS,
  REFERRAL_SOURCE_OPTIONS,
  REFERRAL_SOCIAL_OPTIONS,
  INTEREST_OPTIONS,
  MONTHS_UA,
  QUESTIONNAIRE_TOTAL_FIELDS,
} from '../constants/questionnaire'

/**
 * Questionnaire form. 6 logical fields. The style follows Profile/index.tsx.
 */

export type QuestionnaireMode = 'modal' | 'inline-card' | 'onboarding'

interface Props {
  mode: QuestionnaireMode
  initial?: Partial<AuthUser>
  onSaved?: () => void
  onSkip?: () => void
  onSnoozeForever?: () => void
  onSnooze7?: () => void
  showProgress?: boolean
}

// ─── Option cards (radio/checkbox) ───────────────────────────────────────
const OPT_BASE =
  'flex items-center gap-3 px-4 py-3 rounded-xl border bg-white cursor-pointer ' +
  'transition-[box-shadow,border-color,background-color] select-none'
const OPT_IDLE = 'border-gray-200 hover:border-brand/30 hover:shadow-card-low'
const OPT_ACTIVE = 'border-brand bg-brand-bg shadow-card-low'

function RadioCard<T extends string>({
  name,
  value,
  selected,
  onSelect,
  label,
}: {
  name: string
  value: T
  selected: T | ''
  onSelect: (v: T) => void
  label: string
}) {
  const active = selected === value
  return (
    <label className={cn(OPT_BASE, active ? OPT_ACTIVE : OPT_IDLE)}>
      <input
        type="radio"
        name={name}
        value={value}
        checked={active}
        onChange={() => onSelect(value)}
        className="accent-brand shrink-0"
      />
      <span className={cn('text-sm leading-tight', active ? 'font-bold text-gray-800' : 'text-gray-600')}>
        {label}
      </span>
    </label>
  )
}

function CheckboxCard({
  checked,
  onChange,
  label,
}: {
  checked: boolean
  onChange: () => void
  label: string
}) {
  return (
    <label className={cn(OPT_BASE, checked ? OPT_ACTIVE : OPT_IDLE)}>
      <input type="checkbox" checked={checked} onChange={onChange} className="sr-only" />
      <span
        className={cn(
          'w-5 h-5 rounded-md border flex items-center justify-center transition-colors shrink-0',
          checked ? 'bg-brand border-brand' : 'border-gray-300 bg-white',
        )}
      >
        {checked && <Check size={13} strokeWidth={3.5} className="text-white" />}
      </span>
      <span className={cn('text-sm leading-tight', checked ? 'font-bold text-gray-800' : 'text-gray-600')}>
        {label}
      </span>
    </label>
  )
}

// ─── Date of birth: 3 selects day/month/year ────────────────────────────

function parseBirth(iso: string | null | undefined): { d: string; m: string; y: string } {
  if (!iso) return { d: '', m: '', y: '' }
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})$/)
  if (!m) return { d: '', m: '', y: '' }
  return { y: m[1], m: String(parseInt(m[2], 10)), d: String(parseInt(m[3], 10)) }
}

function joinBirth(d: string, m: string, y: string): string | null {
  if (!d || !m || !y) return null
  const dd = d.padStart(2, '0')
  const mm = m.padStart(2, '0')
  return `${y}-${mm}-${dd}`
}

const CURRENT_YEAR = new Date().getFullYear()
const YEARS = Array.from({ length: CURRENT_YEAR - 1920 + 1 }, (_, i) => CURRENT_YEAR - i)
const DAYS = Array.from({ length: 31 }, (_, i) => i + 1)

function BirthDatePicker({
  value,
  onChange,
}: {
  value: string | null
  onChange: (iso: string | null) => void
}) {
  const initial = parseBirth(value)
  const [d, setD] = useState(initial.d)
  const [m, setM] = useState(initial.m)
  const [y, setY] = useState(initial.y)

  const update = (nd: string, nm: string, ny: string) => {
    setD(nd); setM(nm); setY(ny)
    onChange(joinBirth(nd, nm, ny))
  }

  const selectCls =
    'w-full border border-gray-200 rounded-xl px-3 py-3 text-base outline-none ' +
    'focus:border-brand transition-colors bg-white appearance-none'

  return (
    <div className="grid grid-cols-3 gap-2.5">
      <select value={d} onChange={e => update(e.target.value, m, y)} className={selectCls}>
        <option value="">Day</option>
        {DAYS.map(n => <option key={n} value={n}>{n}</option>)}
      </select>
      <select value={m} onChange={e => update(d, e.target.value, y)} className={selectCls}>
        <option value="">Month</option>
        {MONTHS_UA.map((label, i) => (
          <option key={i + 1} value={i + 1}>{label}</option>
        ))}
      </select>
      <select value={y} onChange={e => update(d, m, e.target.value)} className={selectCls}>
        <option value="">Year</option>
        {YEARS.map(n => <option key={n} value={n}>{n}</option>)}
      </select>
    </div>
  )
}

// Utility for counting filled fields (6 logical)
function countFilled(s: {
  gender: Gender
  birth_date: string | null
  gardening_text: string
  interests: Interest[]
  referral_source: ReferralSource
  wishes: string
}) {
  let n = 0
  if (s.gender) n++
  if (s.birth_date) n++
  if (s.gardening_text.trim()) n++
  if (s.interests.length > 0) n++
  if (s.referral_source) n++
  if (s.wishes.trim()) n++
  return n
}

// ─── Component ────────────────────────────────────────────────────────────

export default function ProfileQuestionnaire({
  mode,
  initial,
  onSaved,
  onSkip,
  onSnoozeForever,
  onSnooze7,
  showProgress,
}: Props) {
  const { user, token, setAuth } = useAuth()
  const source = initial ?? user ?? undefined

  const [gender, setGender] = useState<Gender>((source?.gender as Gender) || '')
  const [birthDate, setBirthDate] = useState<string | null>(source?.birth_date || null)
  const [gardeningText, setGardeningText] = useState(source?.gardening_text || '')
  const [interests, setInterests] = useState<Interest[]>(source?.interests || [])
  const [referralSource, setReferralSource] = useState<ReferralSource>(
    (source?.referral_source as ReferralSource) || '',
  )
  const [referralSocial, setReferralSocial] = useState<ReferralSocial>(
    (source?.referral_social as ReferralSocial) || '',
  )
  const [referralOther, setReferralOther] = useState(source?.referral_other || '')
  const [wishes, setWishes] = useState(source?.wishes || '')

  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [savedFlash, setSavedFlash] = useState(false)

  const filled = useMemo(
    () =>
      countFilled({
        gender,
        birth_date: birthDate,
        gardening_text: gardeningText,
        interests,
        referral_source: referralSource,
        wishes,
      }),
    [gender, birthDate, gardeningText, interests, referralSource, wishes],
  )

  const toggleInterest = (k: Interest) => {
    setInterests(prev => (prev.includes(k) ? prev.filter(x => x !== k) : [...prev, k]))
  }

  const handleSave = async () => {
    if (!token) return
    setError('')
    setSaving(true)
    try {
      // If source != 'social' — clear referral_social. If != 'other' and != 'social/other' — clear
      // the text.
      const wantSocial = referralSource === 'social' ? referralSocial : ''
      const wantOther =
        referralSource === 'other' ? referralOther.trim() :
        (referralSource === 'social' && referralSocial === 'other') ? referralOther.trim() : ''

      const updated = await apiUpdateProfile({
        gender,
        birth_date: birthDate,
        gardening_text: gardeningText.trim(),
        interests,
        referral_source: referralSource,
        referral_social: wantSocial,
        referral_other: wantOther,
        wishes: wishes.trim(),
      })
      setAuth(updated)
      setSavedFlash(true)
      setTimeout(() => setSavedFlash(false), 2500)
      onSaved?.()
    } catch (e: any) {
      setError(e?.message || 'Save error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="flex flex-col gap-5">
      {showProgress && (
        <div>
          <div className="flex items-center justify-between text-xs text-gray-500 mb-1.5">
            <span>Completed</span>
            <span className="font-bold text-gray-700">{filled} of {QUESTIONNAIRE_TOTAL_FIELDS}</span>
          </div>
          <div className="h-2 bg-gray-100 rounded-full overflow-hidden">
            <div
              className="h-full bg-brand transition-all"
              style={{ width: `${(filled / QUESTIONNAIRE_TOTAL_FIELDS) * 100}%` }}
            />
          </div>
        </div>
      )}

      {/* Gender */}
      <div>
        <label className="block text-sm font-bold text-gray-500 mb-2">Gender</label>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2.5">
          {GENDER_OPTIONS.map(o => (
            <RadioCard key={o.key} name="gender" value={o.key} selected={gender} onSelect={setGender} label={o.label} />
          ))}
        </div>
      </div>

      {/* Date of birth */}
      <div>
        <label className="block text-sm font-bold text-gray-500 mb-1.5">Date of birth</label>
        <BirthDatePicker value={birthDate} onChange={setBirthDate} />
        <p className="text-xs text-gray-400 mt-1.5">So we can greet you and give birthday discounts</p>
      </div>

      {/* What you grow */}
      <div>
        <label className="block text-sm font-bold text-gray-500 mb-1.5">What do you grow</label>
        <textarea
          value={gardeningText}
          onChange={e => setGardeningText(e.target.value)}
          placeholder="Describe it in your own words — tomatoes, roses, a lawn, a greenhouse, etc. The more detail, the better our recommendations."
          rows={4}
          maxLength={2000}
          className="w-full border border-gray-200 rounded-xl px-4 py-3 text-base outline-none focus:border-brand transition-colors resize-y leading-relaxed"
        />
      </div>

      {/* Interests (product categories) */}
      <div>
        <label className="block text-sm font-bold text-gray-500 mb-2">What are you interested in at the shop</label>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2.5">
          {INTEREST_OPTIONS.map(o => (
            <CheckboxCard
              key={o.key}
              checked={interests.includes(o.key)}
              onChange={() => toggleInterest(o.key)}
              label={o.label}
            />
          ))}
        </div>
      </div>

      {/* How you found us — with a nested choice */}
      <div>
        <label className="block text-sm font-bold text-gray-500 mb-2">How did you hear about the shop</label>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2.5">
          {REFERRAL_SOURCE_OPTIONS.map(o => (
            <RadioCard
              key={o.key}
              name="referral_source"
              value={o.key}
              selected={referralSource}
              onSelect={v => {
                setReferralSource(v)
                if (v !== 'social') setReferralSocial('')
                if (v !== 'social' && v !== 'other') setReferralOther('')
              }}
              label={o.label}
            />
          ))}
        </div>

        {/* Sub-choice: which social network */}
        {referralSource === 'social' && (
          <div className="mt-3 pl-3 border-l-2 border-brand/30">
            <p className="text-xs font-bold text-gray-500 mb-2">Which social network exactly?</p>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2.5">
              {REFERRAL_SOCIAL_OPTIONS.map(o => (
                <RadioCard
                  key={o.key}
                  name="referral_social"
                  value={o.key}
                  selected={referralSocial}
                  onSelect={v => {
                    setReferralSocial(v)
                    if (v !== 'other') setReferralOther('')
                  }}
                  label={o.label}
                />
              ))}
            </div>
            {referralSocial === 'other' && (
              <input
                type="text"
                value={referralOther}
                onChange={e => setReferralOther(e.target.value)}
                placeholder="Name of the social network"
                maxLength={200}
                className="mt-3 w-full border border-gray-200 rounded-xl px-4 py-3 text-base outline-none focus:border-brand transition-colors"
              />
            )}
          </div>
        )}

        {/* Sub-choice: which exact "other" */}
        {referralSource === 'other' && (
          <input
            type="text"
            value={referralOther}
            onChange={e => setReferralOther(e.target.value)}
            placeholder="Tell us where exactly"
            maxLength={200}
            className="mt-3 w-full border border-gray-200 rounded-xl px-4 py-3 text-base outline-none focus:border-brand transition-colors"
          />
        )}
      </div>

      {/* Wishes */}
      <div>
        <label className="block text-sm font-bold text-gray-500 mb-1.5">What would you like to see in our shop</label>
        <textarea
          value={wishes}
          onChange={e => setWishes(e.target.value)}
          placeholder="Which products are missing? What should we improve? Any wishes."
          rows={3}
          maxLength={2000}
          className="w-full border border-gray-200 rounded-xl px-4 py-3 text-base outline-none focus:border-brand transition-colors resize-y leading-relaxed"
        />
      </div>

      {error && <p className="text-sm text-red-500">{error}</p>}
      {savedFlash && <p className="text-sm text-brand font-bold">Saved</p>}

      <div className="flex flex-wrap items-center gap-3 pt-1">
        <Button
          size="lg"
          onClick={handleSave}
          disabled={saving}
          loading={saving}
        >
          Save
        </Button>

        {(mode === 'modal' || mode === 'onboarding') && onSkip && (
          <button
            type="button"
            onClick={onSkip}
            className="border border-gray-200 hover:border-gray-400 hover:bg-gray-50 text-gray-700 font-bold px-7 py-3 rounded-2xl transition-colors text-base"
          >
            Skip
          </button>
        )}

        {mode === 'modal' && onSnooze7 && (
          <button
            type="button"
            onClick={onSnooze7}
            className="text-gray-500 hover:text-gray-700 underline text-sm"
          >
            Remind me later
          </button>
        )}

        {mode === 'modal' && onSnoozeForever && (
          <button
            type="button"
            onClick={onSnoozeForever}
            className="text-gray-500 hover:text-gray-700 underline text-sm"
          >
            Don't show again
          </button>
        )}
      </div>
    </div>
  )
}
