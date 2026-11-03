import { NavLink, Outlet, useMatch } from 'react-router-dom'
import { useEffect } from 'react'
import Planner from '../pages/Planner'

const navClass = ({ isActive }: { isActive: boolean }) =>
  `rounded-lg px-2.5 py-1.5 text-sm font-medium transition-colors ${
    isActive ? 'bg-orange-100 text-orange-900' : 'text-stone-700 hover:bg-stone-100 hover:text-stone-900'
  }`

export function Layout() {
  const home = useMatch('/') !== null
  const shared = useMatch('/p/:id') !== null
  useEffect(() => { if (home) document.title = 'Perfect Saturday Planner' }, [home])
  const wide = useMatch('/evals') !== null
  const width = wide ? 'max-w-6xl' : 'max-w-4xl'
  return (
    <div className="flex min-h-dvh flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:rounded-lg focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:font-semibold focus:text-orange-900 focus:shadow"
      >
        Skip to content
      </a>
      <header className="border-b border-stone-200 bg-white/90 backdrop-blur">
        <div className={`mx-auto flex ${width} flex-wrap items-center justify-between gap-2 px-4 py-3`}>
          <NavLink to="/" className="flex items-center gap-2 font-bold text-stone-900" end>
            <img src="/favicon.svg" alt="" className="h-7 w-7" width={28} height={28} />
            <span>Perfect Saturday</span>
          </NavLink>
          <nav aria-label="Main">
            <ul className="flex flex-wrap items-center gap-1">
              <li>
                <NavLink to="/" end className={navClass}>
                  Plan
                </NavLink>
              </li>
              <li>
                <NavLink to="/how-it-works" className={navClass}>
                  How it works
                </NavLink>
              </li>
              <li>
                <NavLink to="/evals" className={navClass}>
                  Evals
                </NavLink>
              </li>
            </ul>
          </nav>
        </div>
      </header>
      <main id="main" tabIndex={-1} className={`mx-auto w-full ${width} flex-1 px-4 py-6 sm:py-10`}>
        <div hidden={!home}><Planner active={home} /></div>
        {shared ? <h1 className="sr-only">Shared Saturday plan</h1> : null}
        <Outlet />
      </main>
      <footer className="border-t border-stone-200 bg-white">
        <div className={`mx-auto ${width} space-y-1 px-4 py-5 text-xs text-stone-600`}>
          <p>
            Places from <span translate="no">Google Maps</span> or a curated snapshot (© OpenStreetMap contributors). City,
            timezone and weather from Open-Meteo.
          </p>
          <p>Travel times, fares and crowd levels are estimates. Prices and hours can change; check before you go.</p>
        </div>
      </footer>
    </div>
  )
}
