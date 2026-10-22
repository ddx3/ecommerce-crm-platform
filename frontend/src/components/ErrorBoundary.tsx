import { Component } from 'react'
import type { ReactNode, ErrorInfo } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // In production we could send to Sentry — for now we log to the console
    console.error('App crashed:', error, info)
  }

  handleReset = () => {
    this.setState({ hasError: false, error: null })
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="max-w-[640px] mx-auto px-4 py-12 sm:py-20 text-center">
          <h1 className="text-2xl sm:text-3xl font-bold text-[#1A5C2A] mb-4">Something went wrong</h1>
          <p className="text-gray-500 mb-6">
            An error occurred on this page. Try reloading it or come back later.
          </p>
          <div className="flex gap-3 justify-center">
            <button
              type="button"
              onClick={() => window.location.reload()}
              className="px-5 py-2 bg-[#1A5C2A] text-white rounded hover:bg-[#144a22]"
            >
              Reload
            </button>
            <button
              type="button"
              onClick={this.handleReset}
              className="px-5 py-2 border border-gray-300 rounded hover:bg-gray-50"
            >
              Dismiss error
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
