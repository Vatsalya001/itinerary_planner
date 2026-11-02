import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { linkClass } from '../components/ui'

const NODES: Array<[string, string, string]> = [
  ['guard_input', 'code', 'Input bounds (body size, budget, 30–720 minutes, list sizes, text length), Unicode normalisation, and rejecting anything that is not leisure planning.'],
  ['parse_preferences', 'LLM, structured output', 'Free text and form fields become normalised preferences: city, budget, currency, start, duration, mood, energy, interests, hard constraints and soft preferences. A rule-based parser is the fallback.'],
  ['resolve_location', 'code', 'City to coordinates, timezone, country and currency, with aliases such as Bengaluru and Bangalore.'],
  ['check_clarity', 'code', 'Looks only for blocking gaps: no city, ambiguous currency, contradictory or unmappable hard constraints, city not found. At most one round of questions; after that, visible defaults.'],
  ['ask_clarification', 'code + interrupt', 'Pauses the run with up to two questions and quick replies, then resumes with your answers.'],
  ['build_context', 'code', 'The coming Saturday in the city’s timezone, start and end, meal and daylight windows, pacing for your energy, and the forecast for the day: rain probability, temperature and sunset.'],
  ['plan_agent', 'LLM with tools', 'Searches, composes, checks and submits a plan.'],
  ['tools', 'code', 'Runs the tools, records every returned place or event in a per-run ledger, emits trace events and enforces per-tool caps.'],
  ['validate_plan', 'code', 'Rebuilds the plan from the ledger, computes the schedule and costs, and runs every check below.'],
  ['fallback_planner', 'code', 'A search over the same candidates (gathering places and events itself if needed) with the same constraints and pacing, used when the model fails or repairs run out.'],
  ['explain_infeasible', 'code', 'Which constraint removed what, the cheapest workable budget and time found, and one-tap relaxations.'],
  ['finalize', 'code', 'Builds the result, saves the plan for share links, writes the run record and deletes the paused-run checkpoint.'],
]

const TOOLS: Array<[string, string, string]> = [
  ['search_places', 'Up to 8 normalised places: name, category, distance, price range and its basis, rating, hours on the day, diet and accessibility evidence, crowd hint, source.', '6'],
  ['search_events', 'Events on the target date with title, start, venue, price if listed and source link.', '2'],
  ['estimate_travel', 'Minutes, mode and fare range between consecutive stops.', '4'],
  ['check_plan', 'Computed schedule, totals and violations for a draft, with no side effects.', '4'],
  ['submit_plan', 'Ends the loop and sends the plan to validation.', '3 (first try + 2 repairs)'],
]

const CHECKS: Array<[string, string]> = [
  ['Budget', 'Worst case (stop costs + fares + 10% contingency) must be within your per-person budget.'],
  ['Time', 'Elapsed time from the first stop to the last must fit the time you have.'],
  ['Opening hours', 'No visit may overlap a known closed period; unknown hours give a warning, not a pass.'],
  ['Diet', 'Strict diets need positive evidence; contrary evidence is a conflict.'],
  ['Accessibility', 'A wheelchair constraint needs positive evidence.'],
  ['Crowds', 'With “avoid crowds”, no likely-busy stop at its scheduled time (a heuristic, labelled as such).'],
  ['Pacing', 'Stops, walking and long legs within the profile for your energy level.'],
  ['Daylight and weather', 'No outdoor walk after sunset; an outdoor stop on a rainy day needs a Plan B.'],
  ['Events', 'Event start must be on the target date in the city’s timezone.'],
  ['Grounding', 'Every stop must come from a tool result in this run and carry a source; no duplicates; within 40 km of the city.'],
  ['Prose', 'Explanations may not contain prices, durations or clock times; those come from data.'],
  ['Constraints kept', 'Every hard constraint you gave must still be enforced, even if the parser dropped it.'],
]

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="space-y-3 rounded-2xl border border-stone-200 bg-white p-4 shadow-sm sm:p-6">
      <h2 id={id} className="text-xl font-semibold text-stone-900">
        {title}
      </h2>
      {children}
    </section>
  )
}

export default function HowItWorks() {
  useDocumentTitle('How it works · Perfect Saturday Planner')
  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <h1 className="text-3xl font-bold tracking-tight text-stone-900">How it works</h1>
        <p className="max-w-3xl text-stone-700">
          A LangGraph agent chooses and explains the plan using place, weather and event tools, while deterministic code
          owns the arithmetic, the checks and the fallback. No plan reaches you without passing those checks. The model
          never produces a price, a clock time, an address or a validation verdict that you see.
        </p>
      </div>

      <Section id="graph" title="The graph">
        <p className="text-stone-700">
          Each run goes through these steps. You can watch them live in “How I planned this” on the planner.
        </p>
        <ol className="space-y-3">
          {NODES.map(([name, kind, text], i) => (
            <li key={name} className="flex gap-3">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-orange-100 text-xs font-bold text-orange-900">
                {i + 1}
              </span>
              <div className="min-w-0">
                <p className="flex flex-wrap items-center gap-2">
                  <code className="rounded bg-stone-100 px-1.5 py-0.5 font-mono text-sm text-stone-900">{name}</code>
                  <span className="text-xs text-stone-600">{kind}</span>
                </p>
                <p className="text-sm text-stone-800">{text}</p>
              </div>
            </li>
          ))}
        </ol>
      </Section>

      <Section id="tools" title="The agent's tools">
        <p className="text-stone-700">
          The model decides which tools to call and in what order. Every call carries a short reason, shown in the trace
          as the agent’s note. The model can only use places and events that a tool returned in this run.
        </p>
        <div role="region" aria-label="Agent tools" tabIndex={0} className="overflow-x-auto">
          <table className="w-full min-w-[32rem] border-collapse text-sm">
            <thead className="text-left text-xs text-stone-700">
              <tr className="border-b border-stone-200">
                <th scope="col" className="py-2 pr-3 font-semibold">Tool</th>
                <th scope="col" className="py-2 pr-3 font-semibold">Returns</th>
                <th scope="col" className="py-2 font-semibold whitespace-nowrap">Cap per run</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-stone-100">
              {TOOLS.map(([name, text, cap]) => (
                <tr key={name}>
                  <th scope="row" className="py-2 pr-3 text-left align-top">
                    <code className="font-mono text-stone-900">{name}</code>
                  </th>
                  <td className="py-2 pr-3 text-stone-800">{text}</td>
                  <td className="py-2 text-stone-800">{cap}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <ul className="list-disc space-y-1 pl-5 text-sm text-stone-800">
          <li>At most 10 model turns and a 60-second run deadline.</li>
          <li>If a submitted plan fails a check, the violations go back to the model, which gets up to two repairs.</li>
          <li>If the model errors, times out, refuses or runs out of repairs, the deterministic planner takes over and the result says so.</li>
        </ul>
      </Section>

      <Section id="pacing" title="Pacing by energy">
        <p className="text-stone-700">Your mood maps to a pacing profile in code, and the validator enforces it.</p>
        <div role="region" aria-label="Pacing profiles" tabIndex={0} className="overflow-x-auto">
          <table className="w-full min-w-[30rem] border-collapse text-sm">
            <thead className="text-left text-xs text-stone-700">
              <tr className="border-b border-stone-200">
                <th scope="col" className="py-2 pr-3 font-semibold">Energy</th>
                <th scope="col" className="py-2 pr-3 text-right font-semibold">Max stops</th>
                <th scope="col" className="py-2 pr-3 text-right font-semibold">Max walking</th>
                <th scope="col" className="py-2 pr-3 text-right font-semibold">Buffer</th>
                <th scope="col" className="py-2 font-semibold">Other</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-stone-100 text-stone-800">
              <tr>
                <th scope="row" className="py-2 pr-3 text-left font-medium">Low</th>
                <td className="py-2 pr-3 text-right tabular-nums">3</td>
                <td className="py-2 pr-3 text-right tabular-nums">45 min</td>
                <td className="py-2 pr-3 text-right tabular-nums">15 min</td>
                <td className="py-2">At least one seated stop; at most one leg over 15 min</td>
              </tr>
              <tr>
                <th scope="row" className="py-2 pr-3 text-left font-medium">Medium</th>
                <td className="py-2 pr-3 text-right tabular-nums">4</td>
                <td className="py-2 pr-3 text-right tabular-nums">75 min</td>
                <td className="py-2 pr-3 text-right tabular-nums">10 min</td>
                <td className="py-2">—</td>
              </tr>
              <tr>
                <th scope="row" className="py-2 pr-3 text-left font-medium">High</th>
                <td className="py-2 pr-3 text-right tabular-nums">6</td>
                <td className="py-2 pr-3 text-right tabular-nums">150 min</td>
                <td className="py-2 pr-3 text-right tabular-nums">10 min</td>
                <td className="py-2">—</td>
              </tr>
            </tbody>
          </table>
        </div>
      </Section>

      <Section id="checks" title="Guardrails and checks">
        <p className="text-stone-700">
          The validator recomputes everything from tool data and never trusts totals from the model. The same checks
          run on agent plans and backup plans.
        </p>
        <dl className="grid gap-x-4 gap-y-2 text-sm sm:grid-cols-[10rem_1fr]">
          {CHECKS.map(([name, text]) => (
            <div key={name} className="contents">
              <dt className="font-semibold text-stone-900">{name}</dt>
              <dd className="text-stone-800">{text}</dd>
            </div>
          ))}
        </dl>
        <ul className="list-disc space-y-1 pl-5 text-sm text-stone-800">
          <li>Unknown is never treated as yes: hard constraints need positive evidence.</li>
          <li>Your text, tool output and event titles are passed to the model as data, not instructions.</li>
          <li>The tools are read-only. The planner can’t book, pay or contact anyone.</li>
          <li>Pages render text only (no raw HTML) and only link to https addresses.</li>
        </ul>
      </Section>

      <Section id="data" title="Where the data comes from">
        <ul className="list-disc space-y-2 pl-5 text-sm text-stone-800">
          <li>
            <span className="font-semibold">Places:</span> Google Places, shown with “Google Maps” attribution and a link to
            each place on Google Maps. When that is unavailable, a curated Bangalore snapshot built from OpenStreetMap
            (© OpenStreetMap contributors) and official venue sites, badged as curated.
          </li>
          <li>
            <span className="font-semibold">City, timezone and weather:</span> Open-Meteo geocoding and forecast.
          </li>
          <li>
            <span className="font-semibold">Events:</span> a web search limited to an allow-list of sites, then structured
            event data read in code from at most three pages. Events link back to their source; check before you go.
          </li>
          <li>
            <span className="font-semibold">Travel:</span> distance-based estimates per city and mode, with fares from a
            per-city table. Always labelled as estimates.
          </li>
          <li>
            <span className="font-semibold">Costs:</span> a listed price range when available, otherwise a price-level
            band, a ticket price for events, free for parks and walks, or the category’s upper default. Each cost shows
            its basis.
          </li>
          <li>
            <span className="font-semibold">Crowds:</span> there is no live crowd data. “Likely busy” is a heuristic from
            rating count, category and weekend peak hours.
          </li>
        </ul>
      </Section>

      <Section id="failures" title="When something goes wrong">
        <ul className="list-disc space-y-1 pl-5 text-sm text-stone-800">
          <li>Model failure or invalid submissions: a backup plan, with the reason shown.</li>
          <li>Places unavailable: the curated snapshot where available, badged; otherwise an honest message.</li>
          <li>Weather or events unavailable: the plan goes ahead and says what wasn’t checked.</li>
          <li>Nothing satisfies your hard constraints: the blocking constraint, the cheapest workable budget or time, and one-tap changes.</li>
        </ul>
      </Section>

      <Section id="privacy" title="Privacy">
        <p className="text-sm text-stone-800">
          No accounts. Your free text lives only in the run’s checkpoint, which is deleted when the run finishes, and is
          never logged. Shared plans hold no free text and expire after 30 days.
        </p>
      </Section>

      <p className="text-sm text-stone-700">
        See how different models compare on the{' '}
        <Link className={linkClass} to="/evals">
          evals page
        </Link>
        .
      </p>
    </div>
  )
}
