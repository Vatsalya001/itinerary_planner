import type { RouteObject } from 'react-router-dom'
import { Layout } from './components/Layout'
import NotFound from './pages/NotFound'
import SharedPlan from './pages/SharedPlan'

export const routes: RouteObject[] = [
  {
    element: <Layout />,
    hydrateFallbackElement: (
      <p role="status" className="p-8 text-center text-stone-700">
        Loading…
      </p>
    ),
    children: [
      { path: '/', element: null },
      { path: '/p/:id', element: <SharedPlan /> },
      // Secondary pages load on demand to keep the planner's first load small.
      { path: '/evals', lazy: () => import('./pages/EvalsLeaderboard').then((m) => ({ Component: m.default })) },
      { path: '/how-it-works', lazy: () => import('./pages/HowItWorks').then((m) => ({ Component: m.default })) },
      { path: '*', element: <NotFound /> },
    ],
  },
]
