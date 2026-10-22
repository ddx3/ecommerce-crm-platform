import { Outlet } from 'react-router-dom'
import Header from './Header'
import Footer from './Footer'
import AuthModal from '../AuthModal'
import CartDrawer from '../CartDrawer'
import LikedDrawer from '../LikedDrawer'
import ChatWidget from '../ChatWidget'
import StockNotifyModal from '../StockNotifyModal'
import QuestionnairePrompt from '../QuestionnairePrompt'
export default function Layout() {
  return (
    <>
      <Header />
      <main className="flex-1">
        <Outlet />
      </main>
      <Footer />
      <AuthModal />
      <CartDrawer />
      <LikedDrawer />
      <ChatWidget />
      <StockNotifyModal />
      {/* The "gardener profile" questionnaire is temporarily hidden */}
      {false && <QuestionnairePrompt />}
    </>
  )
}
