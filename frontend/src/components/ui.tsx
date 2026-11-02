import type { ReactNode } from 'react'
import { Icon, type IconName } from './Icon'

export const btnPrimary =
  'inline-flex items-center justify-center gap-2 rounded-xl bg-orange-700 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-orange-800 disabled:cursor-not-allowed disabled:opacity-60'

export const btnSecondary =
  'inline-flex items-center justify-center gap-2 rounded-xl border border-stone-300 bg-white px-3.5 py-2 text-sm font-medium text-stone-800 transition-colors hover:border-stone-400 hover:bg-stone-50 disabled:cursor-not-allowed disabled:opacity-60'

export const linkClass = 'font-medium text-orange-800 underline decoration-orange-300 underline-offset-2 hover:decoration-orange-700'

export const cardClass = 'rounded-2xl border border-stone-200 bg-white p-4 shadow-sm sm:p-5'

export const inputClass =
  'block w-full rounded-xl border border-stone-300 bg-white px-3 py-2 text-base text-stone-900 placeholder:text-stone-500 hover:border-stone-400 sm:text-sm'

type Tone = 'neutral' | 'green' | 'amber' | 'red' | 'blue' | 'orange' | 'violet'

const TONES: Record<Tone, string> = {
  neutral: 'border-stone-200 bg-stone-100 text-stone-700',
  green: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  amber: 'border-amber-200 bg-amber-50 text-amber-900',
  red: 'border-red-200 bg-red-50 text-red-800',
  blue: 'border-sky-200 bg-sky-50 text-sky-900',
  orange: 'border-orange-200 bg-orange-50 text-orange-900',
  violet: 'border-violet-200 bg-violet-50 text-violet-900',
}

export function Badge({ tone = 'neutral', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}
    >
      {children}
    </span>
  )
}

const NOTICE_TONES: Record<'info' | 'warn' | 'error' | 'success', { box: string; icon: IconName }> = {
  info: { box: 'border-sky-200 bg-sky-50 text-sky-950', icon: 'info' },
  warn: { box: 'border-amber-300 bg-amber-50 text-amber-950', icon: 'alert' },
  error: { box: 'border-red-200 bg-red-50 text-red-950', icon: 'alert' },
  success: { box: 'border-emerald-200 bg-emerald-50 text-emerald-950', icon: 'check' },
}

export function Notice({
  tone = 'info',
  title,
  children,
  role,
}: {
  tone?: 'info' | 'warn' | 'error' | 'success'
  title?: ReactNode
  children?: ReactNode
  role?: 'status' | 'alert'
}) {
  const t = NOTICE_TONES[tone]
  return (
    <div role={role} className={`flex gap-3 rounded-xl border p-3 text-sm ${t.box}`}>
      <Icon name={t.icon} className="mt-0.5 h-4 w-4 shrink-0" />
      <div className="min-w-0 space-y-1">
        {title ? <p className="font-semibold">{title}</p> : null}
        {children}
      </div>
    </div>
  )
}

export function SectionHeading({ children, id }: { children: ReactNode; id?: string }) {
  return (
    <h3 id={id} className="text-base font-semibold text-stone-900">
      {children}
    </h3>
  )
}
