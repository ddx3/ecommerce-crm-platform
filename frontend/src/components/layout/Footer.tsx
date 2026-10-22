import { Link } from 'react-router-dom'

function InstagramIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect width="20" height="20" x="2" y="2" rx="5"/>
      <circle cx="12" cy="12" r="4"/>
      <circle cx="17.5" cy="6.5" r="0.5" fill="currentColor" stroke="none"/>
    </svg>
  )
}

function FacebookIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <path d="M18 2h-3a5 5 0 0 0-5 5v3H7v4h3v8h4v-8h3l1-4h-4V7a1 1 0 0 1 1-1h3z"/>
    </svg>
  )
}

function YoutubeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <path d="M22.54 6.42a2.78 2.78 0 0 0-1.95-1.96C18.88 4 12 4 12 4s-6.88 0-8.59.46A2.78 2.78 0 0 0 1.46 6.42 29 29 0 0 0 1 12a29 29 0 0 0 .46 5.58a2.78 2.78 0 0 0 1.95 1.96C5.12 20 12 20 12 20s6.88 0 8.59-.46a2.78 2.78 0 0 0 1.95-1.96A29 29 0 0 0 23 12a29 29 0 0 0-.46-5.58z"/>
      <polygon fill="white" points="9.75 15.02 15.5 12 9.75 8.98 9.75 15.02"/>
    </svg>
  )
}

function TikTokIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
      <path d="M19.59 6.69a4.83 4.83 0 0 1-3.77-4.25V2h-3.45v13.67a2.89 2.89 0 0 1-2.88 2.5 2.89 2.89 0 0 1-2.89-2.89 2.89 2.89 0 0 1 2.89-2.89c.28 0 .54.04.79.1V9.01a6.32 6.32 0 0 0-.79-.05 6.34 6.34 0 0 0-6.34 6.34 6.34 6.34 0 0 0 6.34 6.34 6.34 6.34 0 0 0 6.33-6.34V8.69a8.18 8.18 0 0 0 4.78 1.52V6.75a4.85 4.85 0 0 1-1.01-.06z"/>
    </svg>
  )
}

const SOCIALS = [
  { icon: <InstagramIcon />, href: 'https://www.instagram.com/example_shop/', label: 'Instagram' },
  { icon: <FacebookIcon />, href: 'https://www.facebook.com/example.shop/', label: 'Facebook' },
  { icon: <YoutubeIcon />, href: 'https://www.youtube.com/channel/UCi1g0RuH5SysT_iO7xeFo5A', label: 'YouTube' },
  { icon: <TikTokIcon />, href: 'https://www.tiktok.com/@magic_plants_', label: 'TikTok' },
]

const INFO_LINKS = [
  { label: 'About us', to: '/about' },
  { label: 'Payment and delivery', to: '/delivery' },
  { label: 'Reviews', to: '/reviews' },
  { label: 'Contacts', to: '/contacts' },
]

const LEGAL_LINKS = [
  { label: 'Privacy policy', to: '/privacy' },
  { label: 'Public offer', to: '/offer' },
  { label: 'Returns and exchanges', to: '/returns' },
]

export default function Footer() {
  return (
    <footer className="mt-auto" style={{ backgroundColor: '#2a1f15' }}>

      {/* Main */}
      <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-10 sm:py-14">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-8 sm:gap-10 lg:gap-10">

          {/* Brand */}
          <div>
            <div className="font-bold text-2xl text-white mb-2 tracking-tight">Magiya Roslyn</div>
            <p className="text-white/50 text-sm leading-relaxed mb-6">
              Experience, knowledge and a little magic
            </p>

            {/* Socials */}
            <div className="flex flex-wrap gap-2">
              {SOCIALS.map(s => (
                <a
                  key={s.href}
                  href={s.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={s.label}
                  className="w-9 h-9 rounded-xl flex items-center justify-center text-white/60 hover:text-white transition-all"
                  style={{ backgroundColor: 'rgba(255,255,255,0.07)' }}
                  onMouseEnter={e => (e.currentTarget.style.backgroundColor = 'rgba(255,255,255,0.14)')}
                  onMouseLeave={e => (e.currentTarget.style.backgroundColor = 'rgba(255,255,255,0.07)')}
                >
                  {s.icon}
                </a>
              ))}
            </div>
          </div>

          {/* Info */}
          <div>
            <h4 className="text-xs font-bold uppercase tracking-widest text-white/35 mb-5">Information</h4>
            <ul className="space-y-3">
              {INFO_LINKS.map(({ label, to }) => (
                <li key={to}>
                  <Link to={to} className="text-sm text-white/60 hover:text-white transition-colors">
                    {label}
                  </Link>
                </li>
              ))}
            </ul>
          </div>

          {/* Legal */}
          <div>
            <h4 className="text-xs font-bold uppercase tracking-widest text-white/35 mb-5">Important</h4>
            <ul className="space-y-3">
              {LEGAL_LINKS.map(({ label, to }) => (
                <li key={to}>
                  <Link to={to} className="text-sm text-white/60 hover:text-white transition-colors">
                    {label}
                  </Link>
                </li>
              ))}
            </ul>
          </div>

          {/* Contacts */}
          <div>
            <h4 className="text-xs font-bold uppercase tracking-widest text-white/35 mb-5">Contacts</h4>
            <div className="space-y-4">
              <a
                href="tel:+380000000000"
                className="flex items-center gap-3 group"
              >
                <div className="w-9 h-9 rounded-xl flex items-center justify-center shrink-0 transition-all" style={{ backgroundColor: 'rgba(255,255,255,0.07)' }}>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="text-white/60 group-hover:text-white transition-colors">
                    <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.36 12a19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 3.11 1h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.09 8.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 21 16z"/>
                  </svg>
                </div>
                <div>
                  <p className="text-xs text-white/35 mb-0.5">Phone</p>
                  <p className="text-sm text-white/70 group-hover:text-white transition-colors font-medium">+380000000000</p>
                </div>
              </a>

              <a
                href="mailto:shop@example.com"
                className="flex items-center gap-3 group"
              >
                <div className="w-9 h-9 rounded-xl flex items-center justify-center shrink-0 transition-all" style={{ backgroundColor: 'rgba(255,255,255,0.07)' }}>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" className="text-white/60 group-hover:text-white transition-colors">
                    <rect width="20" height="16" x="2" y="4" rx="2"/>
                    <path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>
                  </svg>
                </div>
                <div className="min-w-0">
                  <p className="text-xs text-white/35 mb-0.5">Email</p>
                  <p className="text-sm text-white/70 group-hover:text-white transition-colors font-medium break-all">shop@example.com</p>
                </div>
              </a>

            </div>
          </div>

        </div>
      </div>

      {/* Bottom bar */}
      <div style={{ borderTop: '1px solid rgba(255,255,255,0.06)' }}>
        <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-5 text-center">
          <span className="text-sm text-white/30">© {new Date().getFullYear()} Magiya Roslyn</span>
        </div>
      </div>

    </footer>
  )
}
