import type { ReactNode } from 'react'
import { cn } from '../../utils/cn'

interface Props {
  icon?: ReactNode
  title: string
  description?: string
  action?: ReactNode
  className?: string
  /** Color of the circle under the icon. Defaults to the standard light green
   * (bg-brand-bg text-brand), no exceptions. Pass it only when there is a clear reason
   * to deviate from the standard (e.g. the Favorites heart is red).
   */
  iconClassName?: string
}

export default function EmptyState({ icon, title, description, action, className, iconClassName }: Props) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-4 text-center px-8 py-12', className)}>
      {icon && <div className={cn('w-20 h-20 rounded-full flex items-center justify-center', iconClassName || 'bg-brand-bg text-brand')}>{icon}</div>}
      <p className="text-lg font-bold text-text-secondary">{title}</p>
      {description && <p className="text-sm text-text-muted max-w-xs">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}
