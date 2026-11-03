import { DEGRADATION_NOTES, DEGRADATION_TITLES, classifyDegradation, type DegradationKind } from '../lib/labels'
import type { Degradation } from '../lib/trace'
import { Notice } from './ui'

const ORDER: DegradationKind[] = ['planner', 'checked', 'model', 'parser', 'data']

/**
 * One notice per kind of degradation, so a model timeout never reads as "reduced data" and vice versa.
 * Accepts classified trace degradations or the plain strings from PlanResult.degradations.
 */
export function DegradationNotices({
  items,
  exclude = [],
}: {
  items: Array<Degradation | string>
  exclude?: DegradationKind[]
}) {
  const classified = items.map((d) => (typeof d === 'string' ? { text: d, kind: classifyDegradation(d) } : d))
  const groups = ORDER.filter((k) => !exclude.includes(k))
    .map((kind) => ({ kind, texts: [...new Set(classified.filter((d) => d.kind === kind).map((d) => d.text))] }))
    .filter((g) => g.texts.length)
  if (!groups.length) return null
  return (
    <div className="space-y-2">
      {groups.map((g) => (
        <Notice key={g.kind} tone={g.kind === 'data' ? 'info' : 'warn'} title={DEGRADATION_TITLES[g.kind]}>
          {DEGRADATION_NOTES[g.kind] ? <p>{DEGRADATION_NOTES[g.kind]}</p> : null}
          {g.texts.length === 1 ? (
            <p>{g.texts[0]}</p>
          ) : (
            <ul className="list-disc pl-5">
              {g.texts.map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
          )}
        </Notice>
      ))}
    </div>
  )
}
