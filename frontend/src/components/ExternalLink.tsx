import type { ReactNode } from 'react'

export const NEW_TAB_HINT = '(opens in a new tab)'

/**
 * The only place the app opens links in a new tab. It always adds rel="noopener noreferrer" and a visually
 * hidden "(opens in a new tab)" so screen-reader users hear it in the link name. Callers pass https URLs only
 * (see lib/urls.ts). A test fails if target="_blank" appears anywhere else in src/.
 */
export function ExternalLink({
  href,
  className,
  children,
}: {
  href: string
  className?: string
  children: ReactNode
}) {
  return (
    <a className={className} href={href} target="_blank" rel="noopener noreferrer">
      {children}
      {/* Leading space inside the hidden span: nothing extra renders, and browsers keep it in the link name. */}
      <span className="sr-only">{` ${NEW_TAB_HINT}`}</span>
    </a>
  )
}
