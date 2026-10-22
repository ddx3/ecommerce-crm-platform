import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { CartProvider } from './context/CartContext'
import { LikedProvider } from './context/LikedContext'
import { UIProvider } from './context/UIContext'
import { AuthProvider } from './context/AuthContext'
import { SiteSettingsProvider } from './context/SiteSettingsContext'
import Layout from './components/layout/Layout'
import ErrorBoundary from './components/ErrorBoundary'
import MultiShipmentConfirmModal from './components/MultiShipmentConfirmModal'
import ScrollToTop from './components/ScrollToTop'
import AnalyticsTracker from './components/AnalyticsTracker'

// Home — eager: it is the first page, no point in lazy loading
import HomePage from './pages/Home'

// Other pages — lazy loading (reduces the initial bundle)
const CatalogPage = lazy(() => import('./pages/Catalog'))
const ProductPage = lazy(() => import('./pages/Product'))
const BundlePage = lazy(() => import('./pages/Bundle'))
const CheckoutPage = lazy(() => import('./pages/Checkout'))
const CheckoutSuccessPage = lazy(() => import('./pages/CheckoutSuccess'))
const CheckoutPaymentPage = lazy(() => import('./pages/CheckoutPayment'))
const LoginPage = lazy(() => import('./pages/Login'))
const LikedPage = lazy(() => import('./pages/Liked'))
const OrdersPage = lazy(() => import('./pages/Orders'))
const ProfilePage = lazy(() => import('./pages/Profile'))
const ReviewsPage = lazy(() => import('./pages/Reviews'))
const AboutPage = lazy(() => import('./pages/About'))
const ContactsPage = lazy(() => import('./pages/Contacts'))
const DeliveryPage = lazy(() => import('./pages/Delivery'))
const ResetPasswordPage = lazy(() => import('./pages/ResetPassword'))
const PrivacyPage = lazy(() => import('./pages/Privacy'))
const OfferPage = lazy(() => import('./pages/Offer'))
const ReturnsPage = lazy(() => import('./pages/Returns'))
const BlogPage = lazy(() => import('./pages/Blog'))
const BlogPostPage = lazy(() => import('./pages/BlogPost'))
const CoursesPage = lazy(() => import('./pages/Courses'))
const CoursePage = lazy(() => import('./pages/Course'))
const CourseLearnPage = lazy(() => import('./pages/CourseLearn'))
const MyCoursesPage = lazy(() => import('./pages/MyCourses'))
const MaintenancePage = lazy(() => import('./pages/Maintenance'))

function NotFound() {
  return (
    <div className="max-w-[1440px] mx-auto px-4 py-12 sm:py-20 text-center">
      <h1 className="text-4xl sm:text-5xl font-bold text-[#1A5C2A] mb-4">404</h1>
      <p className="text-gray-500 text-base sm:text-lg">Page not found</p>
    </div>
  )
}

function PageFallback() {
  return (
    <div className="min-h-[60vh] flex items-center justify-center">
      <div className="w-10 h-10 border-4 border-gray-200 border-t-[#1a5c2a] rounded-full animate-spin" />
    </div>
  )
}

export default function App() {
  return (
    <ErrorBoundary>
    <AuthProvider>
    <SiteSettingsProvider>
    <CartProvider>
      <LikedProvider>
        <UIProvider>
        <BrowserRouter>
          <ScrollToTop />
          <AnalyticsTracker />
          <Suspense fallback={<PageFallback />}>
            <Routes>
              {/* /maintenance — rendered without the Layout (no header/footer). */}
              <Route path="/maintenance" element={<MaintenancePage />} />
              <Route element={<Layout />}>
                <Route path="/" element={<HomePage />} />
                <Route path="/catalog" element={<CatalogPage />} />
                <Route path="/product/:slug" element={<ProductPage />} />
                <Route path="/bundle/:slug" element={<BundlePage />} />
                <Route path="/checkout" element={<CheckoutPage />} />
                <Route path="/checkout/success" element={<CheckoutSuccessPage />} />
                <Route path="/checkout/payment" element={<CheckoutPaymentPage />} />
                <Route path="/login" element={<LoginPage />} />
                <Route path="/liked" element={<LikedPage />} />
                <Route path="/orders" element={<OrdersPage />} />
                <Route path="/profile" element={<ProfilePage />} />
                <Route path="/reviews" element={<ReviewsPage />} />
                <Route path="/about" element={<AboutPage />} />
                <Route path="/contacts" element={<ContactsPage />} />
                <Route path="/delivery" element={<DeliveryPage />} />
                <Route path="/reset-password" element={<ResetPasswordPage />} />
                <Route path="/privacy" element={<PrivacyPage />} />
                <Route path="/offer" element={<OfferPage />} />
                <Route path="/returns" element={<ReturnsPage />} />
                <Route path="/blog" element={<BlogPage />} />
                <Route path="/blog/:slug" element={<BlogPostPage />} />
                <Route path="/courses" element={<CoursesPage />} />
                <Route path="/course/:slug" element={<CoursePage />} />
                <Route path="/course/:slug/learn" element={<CourseLearnPage />} />
                <Route path="/course/:slug/learn/:lessonId" element={<CourseLearnPage />} />
                <Route path="/my-courses" element={<MyCoursesPage />} />
                <Route path="*" element={<NotFound />} />
              </Route>
            </Routes>
          </Suspense>
          <MultiShipmentConfirmModal />
        </BrowserRouter>
        </UIProvider>
      </LikedProvider>
    </CartProvider>
    </SiteSettingsProvider>
    </AuthProvider>
    </ErrorBoundary>
  )
}
