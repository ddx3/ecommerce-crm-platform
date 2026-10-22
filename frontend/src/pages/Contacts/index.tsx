import { Link } from 'react-router-dom'
import { ChevronRight, Phone, Clock, Mail } from 'lucide-react'
import { Card } from '../../components/ui'

function IgIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="#c13584" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="2" width="20" height="20" rx="5" ry="5"/>
      <path d="M16 11.37A4 4 0 1 1 12.63 8 4 4 0 0 1 16 11.37z"/>
      <line x1="17.5" y1="6.5" x2="17.51" y2="6.5"/>
    </svg>
  )
}

function FbIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="#1877f2">
      <path d="M24 12.07C24 5.41 18.63 0 12 0S0 5.41 0 12.07C0 18.1 4.39 23.1 10.13 24v-8.44H7.08v-3.49h3.04V9.41c0-3.02 1.8-4.7 4.54-4.7 1.31 0 2.68.24 2.68.24v2.97h-1.51c-1.49 0-1.95.93-1.95 1.88v2.27h3.32l-.53 3.49h-2.79V24C19.62 23.1 24 18.1 24 12.07z"/>
    </svg>
  )
}

function YtIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="#ff0000">
      <path d="M23.5 6.2a3 3 0 0 0-2.1-2.1C19.5 3.5 12 3.5 12 3.5s-7.5 0-9.4.6A3 3 0 0 0 .5 6.2 31 31 0 0 0 0 12a31 31 0 0 0 .5 5.8 3 3 0 0 0 2.1 2.1c1.9.6 9.4.6 9.4.6s7.5 0 9.4-.6a3 3 0 0 0 2.1-2.1A31 31 0 0 0 24 12a31 31 0 0 0-.5-5.8z"/>
      <polygon fill="white" points="9.75 15.02 15.5 12 9.75 8.98 9.75 15.02"/>
    </svg>
  )
}

function TikTokIcon() {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="#000000">
      <path d="M19.59 6.69a4.83 4.83 0 0 1-3.77-4.25V2h-3.45v13.67a2.89 2.89 0 0 1-2.88 2.5 2.89 2.89 0 0 1-2.89-2.89 2.89 2.89 0 0 1 2.89-2.89c.28 0 .54.04.79.1V9.01a6.34 6.34 0 0 0-.79-.05 6.34 6.34 0 0 0-6.34 6.34 6.34 6.34 0 0 0 6.34 6.34 6.34 6.34 0 0 0 6.33-6.34V8.69a8.18 8.18 0 0 0 4.78 1.52V6.75a4.85 4.85 0 0 1-1.01-.06z"/>
    </svg>
  )
}


const SOCIALS = [
  { IconEl: IgIcon, bg: '#fdf0f8', label: 'Instagram blog', name: '@example_shop', href: 'https://instagram.com/example_shop/' },
  { IconEl: IgIcon, bg: '#fdf0f8', label: 'Instagram shop', name: '@example_shop', href: 'https://www.instagram.com/example_shop/' },
  { IconEl: FbIcon, bg: '#eef3ff', label: 'Facebook', name: 'Shop page', href: 'https://www.facebook.com/example.shop/' },
  { IconEl: YtIcon, bg: '#fff0f0', label: 'YouTube', name: '«Magiya Roslyn»', href: 'https://www.youtube.com/channel/UCi1g0RuH5SysT_iO7xeFo5A' },
  { IconEl: TikTokIcon, bg: '#f0f0f0', label: 'TikTok', name: '@magic_plants_', href: 'https://www.tiktok.com/@magic_plants_' },
]

const SCHEDULE = [
  { day: 'Monday – Friday', hours: '10:00 – 18:00' },
  { day: 'Saturday, Sunday', hours: 'Days off' },
]

export default function ContactsPage() {
  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">

      <nav className="flex items-center gap-1 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        <ChevronRight size={14} />
        <span className="text-gray-600">Contacts</span>
      </nav>

      <h1 className="text-2xl sm:text-3xl font-bold text-gray-800 mb-6 sm:mb-8">Contacts</h1>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 sm:gap-8">

        <div className="lg:col-span-2 space-y-6">

          {/* Get in touch */}
          <Card variant="section" className="p-5 sm:p-6">
            <h2 className="text-lg font-bold text-text-primary mb-5">Contact us</h2>
            <div className="space-y-3">
              <a href="tel:+380000000000"
                className="flex items-center gap-4 p-4 rounded-xl border border-border hover:border-gray-300 hover:shadow-card-low">
                <div className="w-10 h-10 rounded-xl bg-brand-bg flex items-center justify-center shrink-0">
                  <Phone size={18} className="text-brand" />
                </div>
                <div>
                  <p className="text-xs text-text-muted mb-0.5">Phone / Viber / Telegram</p>
                  <p className="font-bold text-text-primary">+380000000000</p>
                </div>
              </a>

              <a href="mailto:shop@example.com"
                className="flex items-center gap-4 p-4 rounded-xl border border-border hover:border-gray-300 hover:shadow-card-low">
                <div className="w-10 h-10 rounded-xl bg-blue-50 flex items-center justify-center shrink-0">
                  <Mail size={18} className="text-blue-500" />
                </div>
                <div>
                  <p className="text-xs text-text-muted mb-0.5">Email</p>
                  <p className="font-bold text-text-primary">shop@example.com</p>
                </div>
              </a>
            </div>
          </Card>

          {/* Working hours */}
          <Card variant="section" className="p-5 sm:p-6">
            <div className="flex items-center gap-2 mb-5">
              <Clock size={18} className="text-brand" />
              <h2 className="text-lg font-bold text-text-primary">Working hours</h2>
            </div>
            <div>
              {SCHEDULE.map(({ day, hours }) => (
                <div key={day} className="flex items-center justify-between py-3 border-b border-border last:border-0">
                  <span className="text-sm text-text-secondary">{day}</span>
                  <span className={`text-sm font-bold ${hours === 'Day off' ? 'text-text-muted' : 'text-text-primary'}`}>{hours}</span>
                </div>
              ))}
            </div>
          </Card>


        </div>

        {/* Sidebar: social networks */}
        <div>
          <Card variant="section" className="p-5 sm:p-6">
            <h2 className="text-lg font-bold text-text-primary mb-5">Find us online</h2>
            <div className="space-y-2">
              {SOCIALS.map(({ IconEl, bg, label, name, href }) => (
                <a key={name} href={href} target="_blank" rel="noopener noreferrer"
                  className="flex items-center gap-3 p-2.5 rounded-xl border border-border hover:border-gray-300 hover:shadow-card-low">
                  <div className="w-9 h-9 rounded-lg flex items-center justify-center shrink-0" style={{ background: bg }}>
                    <IconEl />
                  </div>
                  <div>
                    <p className="text-xs text-text-muted mb-0.5">{label}</p>
                    <p className="font-bold text-text-primary">{name}</p>
                  </div>
                </a>
              ))}
            </div>
          </Card>
        </div>

      </div>
    </div>
  )
}
