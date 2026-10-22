import { useAuth } from '../context/AuthContext'
import { useUI } from '../context/UIContext'
import { apiDismissQuestionnaire } from '../api/auth'
import { Modal } from './ui'
import ProfileQuestionnaire from './ProfileQuestionnaire'
import { useQuestionnairePrompt } from '../hooks/useQuestionnairePrompt'

/**
 * Questionnaire modal — two scenarios:
 *  - panel === 'questionnaire' — onboarding right after registration.
 *  - useQuestionnairePrompt — pop-up on the Nth login.
 *
 * The modal style follows AuthModal / StockNotifyModal:
 * branded «Magiya Roslyn» header, centered subtitle, form.
 */
export default function QuestionnairePrompt() {
  const { user, token } = useAuth()
  const { panel, close } = useUI()
  const prompt = useQuestionnairePrompt()

  const onboarding = panel === 'questionnaire'
  const popup = !onboarding && prompt.shouldShow

  if (!user) return null
  if (!onboarding && !popup) return null

  const handleClose = () => {
    if (onboarding) close()
    else prompt.close()
  }

  const handleDone = () => {
    if (onboarding) close()
    else prompt.close()
  }

  const handleSnooze7 = async () => {
    if (token) {
      try { await apiDismissQuestionnaire('snooze7') } catch { /* offline: ok */ }
    }
    prompt.snooze7()
  }

  const handleDismissForever = async () => {
    if (token) {
      try { await apiDismissQuestionnaire('forever') } catch { /* offline: ok */ }
    }
    prompt.dismissForever()
  }

  return (
    <Modal open onClose={handleClose} size="lg" ariaLabel="Gardener profile">
      <p className="text-center font-bold text-brand text-xl sm:text-2xl mb-2 sm:mb-3">Magiya Roslyn</p>
      <p className="text-center text-sm text-gray-500 mb-5 sm:mb-6 leading-relaxed">
        {onboarding
          ? 'Thank you for signing up! Tell us a little about yourself — it helps us show you better products.'
          : 'Tell us a little about yourself — it helps us show you better products. All fields are optional.'}
      </p>

      <ProfileQuestionnaire
        mode={onboarding ? 'onboarding' : 'modal'}
        showProgress
        onSaved={handleDone}
        onSkip={handleDone}
        onSnooze7={onboarding ? undefined : handleSnooze7}
        onSnoozeForever={onboarding ? undefined : handleDismissForever}
      />
    </Modal>
  )
}
