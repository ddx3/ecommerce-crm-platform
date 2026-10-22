import { Link } from 'react-router-dom'
import { ChevronRight, ShieldCheck, Award, Leaf, Users } from 'lucide-react'
import { Card } from '../../components/ui'
import Button from '../../components/ui/Button'


const VALUES = [
  {
    icon: ShieldCheck,
    title: 'Original products only',
    desc: 'All products are certified and supplied directly by the manufacturers.',
  },
  {
    icon: Award,
    title: 'Extensive experience',
    desc: 'Years of practice growing ornamental and fruit gardens.',
  },
  {
    icon: Leaf,
    title: 'Results guaranteed',
    desc: 'Video guides and tips help you use the products correctly.',
  },
  {
    icon: Users,
    title: 'A community of gardeners',
    desc: 'Tens of thousands of followers with whom we share our experience every day.',
  },
]

export default function AboutPage() {
  return (
    <div className="max-w-[1440px] mx-auto px-4 sm:px-6 lg:px-8 py-6 sm:py-8">

      {/* Breadcrumb */}
      <nav className="flex items-center gap-1 text-xs sm:text-sm text-gray-400 mb-4 sm:mb-6">
        <Link to="/" className="hover:text-brand transition-colors">Home</Link>
        <ChevronRight size={14} />
        <span className="text-gray-600">About us</span>
      </nav>

      {/* Hero */}
      <div className="rounded-2xl sm:rounded-3xl overflow-hidden mb-8 sm:mb-10 relative" style={{
        background: 'linear-gradient(135deg, #1a5c2a 0%, #2d8a45 60%, #3aad58 100%)',
        backgroundImage: 'linear-gradient(135deg, #1a5c2a 0%, #2d8a45 60%, #3aad58 100%), radial-gradient(rgba(255,255,255,0.07) 1px, transparent 1px)',
        backgroundSize: 'cover, 24px 24px',
      }}>
        <div className="flex flex-col lg:flex-row lg:items-center">
          <div className="px-6 py-8 sm:px-12 sm:py-14 flex-1">
            <div className="inline-flex items-center gap-2 bg-white/20 text-white text-xs sm:text-sm font-medium px-3 sm:px-4 py-1 sm:py-1.5 rounded-full mb-4 sm:mb-5">
              <Leaf size={14} />
              Online store
            </div>
            <h1 className="text-3xl sm:text-4xl font-bold text-white mb-5 sm:mb-8 leading-tight">Magiya Roslyn</h1>
            <p className="text-white/80 text-sm sm:text-lg leading-relaxed">
              Gardening products: fertilizers, growth stimulants and plant protection products. Only original, certified products from the manufacturers at fair prices.
            </p>
          </div>
          <div className="lg:w-80 px-6 pb-8 sm:px-12 sm:pb-14 lg:py-14 flex flex-col gap-5 sm:gap-6">
            {[
              { num: '500+', label: 'products in the catalog' },
              { num: '10+', label: 'years of gardening experience' },
              { num: '1000+', label: 'happy customers' },
            ].map(({ num, label }) => (
              <div key={label} className="border-l-2 border-white/30 pl-4 sm:pl-5">
                <p className="text-2xl sm:text-3xl font-bold text-white">{num}</p>
                <p className="text-white/60 text-xs sm:text-sm mt-0.5">{label}</p>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Who we are + Consultation */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-5 sm:gap-6 mb-5 sm:mb-6">
        <Card variant="section" className="p-5 sm:p-6">
          <h2 className="text-lg sm:text-xl font-bold text-text-primary mb-4">Who we are</h2>
          <div className="space-y-4 text-text-secondary leading-relaxed">
            <p>We have extensive experience growing both ornamental and fruit gardens. We know how to grow healthy, beautiful plants and get a rich, high-quality harvest!</p>
            <p>We gladly share our experience on various platforms — our video guides and tips will help you make the right choice and use the products properly in your garden and vegetable plot.</p>
            <p className="font-bold text-brand">We wish you a good harvest and healthy plants!</p>
          </div>
        </Card>

        <Card variant="section" className="p-5 sm:p-6 flex flex-col justify-between">
          <div>
            <h2 className="text-xl sm:text-2xl font-bold text-text-primary mb-3">Consultation</h2>
            <p className="text-text-muted leading-relaxed mb-6">
              Not sure which product to pick? Tell us about your task and we will find a solution.
            </p>
            <div className="space-y-2.5">
              {[
                'We will choose a product for your task',
                'We will identify the disease or pest',
                'We will recommend application rates and timing',
                'We will advise how to care for your plants',
              ].map(item => (
                <div key={item} className="flex items-center gap-3 bg-brand-bg/40 border border-brand/15 rounded-xl px-4 py-2.5">
                  <div className="w-5 h-5 rounded-full bg-brand-bg flex items-center justify-center shrink-0">
                    <svg viewBox="0 0 12 12" width="10" height="10" fill="none">
                      <path d="M2 6l2.5 2.5L10 3.5" stroke="currentColor" className="text-brand" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                    </svg>
                  </div>
                  <span className="text-text-secondary text-sm">{item}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="mt-6">
            <Button as={Link} to="/contacts" size="sm" fullWidth>
              Contact us
            </Button>
          </div>
        </Card>
      </div>

      {/* Benefits */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-6">
        {VALUES.map(({ icon: Icon, title, desc }) => (
          <Card key={title} variant="section" className="p-5 sm:p-6">
            <div className="w-10 h-10 rounded-xl bg-brand-bg flex items-center justify-center mb-3">
              <Icon size={20} className="text-brand" />
            </div>
            <p className="font-bold text-text-primary mb-1">{title}</p>
            <p className="text-sm text-text-muted leading-relaxed">{desc}</p>
          </Card>
        ))}
      </div>
    </div>
  )
}
