import { Link } from 'react-router-dom'
import { btnPrimary } from '../components/ui'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

export default function NotFound() {
  useDocumentTitle('Page not found · Perfect Saturday Planner')
  return (
    <div className="space-y-4 py-10 text-center">
      <h1 className="text-2xl font-bold text-stone-900">Page not found</h1>
      <p className="text-stone-700">There’s nothing at this address.</p>
      <Link to="/" className={btnPrimary}>
        Plan a Saturday
      </Link>
    </div>
  )
}
