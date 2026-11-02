import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { linkClass } from '../components/ui'

// Every statement on this page describes backend/app (graph/build.py, graph/nodes/*, domain/*) as documented in
// docs/LLD.md sections 3, 4 and 7. Keep it in step with the code; make no claims the code does not back.
const NODES: Array<[string, string, string]> = [
  [
    'guard_input',
    'code',
    'Cleans the request: Unicode normalisation, removal of control, zero-width and direction-override characters, and per-field length limits. Clamps the time you have to 30–720 minutes, with a visible note when it changes it. Flags instruction-like text (such as “ignore previous instructions”) in the trace, and adds a note when you ask it to book, pay or contact someone. It never rejects a request: malformed, oversized or rate-limited requests are refused by the API (HTTP 400, 413 or 429) before the graph starts.',
  ],
  [
    'parse_preferences',
    'rules + LLM',
    'The rule-based parser always reads your request. When you write a free-text description (and the planner is not the no-LLM baseline), the model reads it too, grounded in your words; requests made only with the form fields skip this model call because the fields are already exact. The model can only keep interests and hard constraints that your text supports, never re-adds a constraint you dropped, and its time and budget count only when your text mentions a time or an amount. The two readings are merged: fields you filled in win, then the model, then the rules, and hard constraints are combined. If the model fails or times out, the rule-based reading is used and the trace says so.',
  ],
  [
    'resolve_location',
    'code',
    'Looks up the city in a built-in list of cities first, then with Open-Meteo geocoding: coordinates, timezone, country and currency. A transient failure is retried once.',
  ],
  [
    'check_clarity',
    'code',
    'Asks only about blocking gaps: a missing or unknown city, contradictory constraints, an ambiguous currency, missing budget and time when the city is also missing, or a constraint it can’t map. At most two questions, asked once. If the city still can’t be found, it says so instead of guessing.',
  ],
  [
    'ask_clarification',
    'code + interrupt',
    'Pauses the run with the questions and quick replies. Your answers (up to 200 characters each) are folded into the request and parsing runs again.',
  ],
  [
    'build_context',
    'code',
    'Applies visible defaults (such as a 3:00 PM start), then sets up the day: the coming Saturday in the city’s timezone, the start and end, meal windows, pacing for your energy, and the forecast (rain probability, temperature, sunset). With under 15 seconds left it goes straight to the rule-based planner.',
  ],
  [
    'plan_agent',
    'LLM with tools',
    'The model takes a turn: it calls tools or replies. A model error, timeout, refusal or provider rate limit, the turn cap, or running low on time hands over to the rule-based planner. A turn that ends without submitting a plan gets one nudge; a second one hands over too.',
  ],
  [
    'tools',
    'code',
    'Runs the model’s tool calls, records every place or event they return in this run’s ledger, enforces per-tool caps and counts protocol errors (malformed calls, unknown tools, IDs no tool returned).',
  ],
  [
    'validate_plan',
    'code',
    'Rebuilds the submitted plan from the ledger, computes the schedule and costs, and runs every check below. A failing plan goes back to the model with one-line fixes while submissions (three in total) and time remain; otherwise the rule-based planner takes over.',
  ],
  [
    'fallback_planner',
    'code',
    'If the model stopped for time or turns, or didn’t submit, after a draft it checked had passed, that draft is checked again and used. Otherwise a deterministic search plans over the places and events found so far, running its own searches if needed, with the same constraints and pacing. A plan it builds is shown as a backup plan; if nothing fits, explain_infeasible runs.',
  ],
  [
    'explain_infeasible',
    'code',
    'Finds what blocks a plan by retrying with the budget ignored, more time, other start times, each constraint dropped and each pacing rule relaxed, then names the blocking constraints and offers the changes that work as one-tap options.',
  ],
  [
    'finalize',
    'code',
    'Checks the plan again from the ledger (a plan that fails is never shown), builds the result with names for the places left out, and emits the run metrics and the result. After it, the API saves shareable plans and the run log, and the runner deletes the finished run’s checkpoint.',
  ],
]

const TOOLS: Array<[string, string, string]> = [
  [
    'search_places',
    'Up to four searches in one call (and, if asked, the day’s events), so the whole search takes one model request. Each place comes in a compact form: name, category, distance, price with its basis, rating, hours on the day, diet and wheelchair evidence, a likely-busy flag, whether the visit is mostly walking, typical visit length and source.',
    '3 calls, 8 searches',
  ],
  ['search_events', 'Events on the target date in the same form, with start time and price (also available through search_places).', '2'],
  ['estimate_travel', 'Mode, distance, minutes and fare range between consecutive stops. Optional; the model is told to skip it.', '4'],
  ['check_plan', 'Schedule, totals, errors and warnings for a draft, without submitting. Optional; submit_plan already checks.', '4'],
  ['submit_plan', 'Sends the plan to validate_plan. A rejected plan comes back with the computed schedule and a one-line fix (for example how many minutes to cut).', '3 (first try + 2 repairs)'],
]

const CHECKS: Array<[string, string]> = [
  ['Budget', 'Worst case (stop costs + fares + 10% contingency) must be within your per-person budget.'],
  ['Time', 'From your start time to the end of the last stop must fit the time you have.'],
  [
    'Opening hours',
    'No visit while a place is known to be closed, and no arriving after an event starts. Unknown hours give a warning; venues that are only worth visiting when a show is on need known hours.',
  ],
  ['Diet', 'Strict diets need positive evidence at every food stop; contrary evidence is a conflict.'],
  ['Accessibility', 'A wheelchair constraint needs positive evidence at every stop.'],
  ['No alcohol', 'No bars or breweries; a venue that serves alcohol gets a warning.'],
  ['Indoors / outdoors only', 'Every stop needs positive evidence of the setting.'],
  ['Must include / avoid', 'At least one stop satisfies each “include”; no stop matches an “avoid”.'],
  ['Crowds', 'With “avoid crowded places”, no likely-busy stop at its scheduled time (a heuristic, labelled as such).'],
  ['Pacing', 'Stops, walking, long legs and seated stops within the profile for your energy level.'],
  ['Visit lengths', 'Each visit stays within a sensible range for its category.'],
  [
    'Daylight and weather',
    'No outdoor walk ending after sunset. An outdoor stop when rain is likely gets a warning unless it has a valid indoor Plan B.',
  ],
  ['Events', 'An event must be on the target date in the city’s timezone.'],
  [
    'Grounding',
    'Every stop comes from a tool result in this run, appears once, is priced in the plan’s currency and is within 40 km of the city. Source links must be https, and an event needs an https source page.',
  ],
  ['Prose', 'Explanations may not state prices, durations, distances or clock times; those come from data.'],
  ['Constraints kept', 'Every hard constraint you gave is enforced, even if the parser missed it.'],
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
          A LangGraph agent chooses and explains the plan using place and event search tools, while deterministic code
          fetches the weather and owns the arithmetic, the checks and the fallback. No plan reaches you without passing
          those checks. Prices, clock times and addresses in a plan come from data, and the model’s explanations are
          checked so they can’t state them.
        </p>
      </div>

      <Section id="graph" title="The graph">
        <p className="text-stone-700">
          Each run goes through these steps, in this order where they apply. You can watch them live in “How I planned
          this” on the planner.
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
          The model decides which tools to call and in what order. Searches and travel estimates carry a short reason
          (required; optional for the plan tools), shown in the trace as the agent’s note. The model can only use places
          and events that a tool returned in this run.
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
          <li>By default, at most 10 model turns and a 60-second deadline per run.</li>
          <li>If a submitted plan fails a check, the problems go back to the model, which gets up to two repairs.</li>
          <li>
            If the model errors, times out, refuses or runs out of repairs, the rule-based planner takes over and the
            result says so.
          </li>
        </ul>
      </Section>

      <Section id="pacing" title="Pacing by energy">
        <p className="text-stone-700">
          Your mood maps to an energy level and a pacing profile, and the validator enforces it. Walking counts walk legs
          plus walking stops; the buffer is added between stops.
        </p>
        <div role="region" aria-label="Pacing profiles" tabIndex={0} className="overflow-x-auto">
          <table className="w-full min-w-[30rem] border-collapse text-sm">
            <thead className="text-left text-xs text-stone-700">
              <tr className="border-b border-stone-200">
                <th scope="col" className="py-2 pr-3 font-semibold">Energy</th>
                <th scope="col" className="py-2 pr-3 text-right font-semibold">Max stops</th>
                <th scope="col" className="py-2 pr-3 text-right font-semibold">Max walking</th>
                <th scope="col" className="py-2 pr-3 text-right font-semibold">Buffer</th>
                <th scope="col" className="py-2 pr-3 text-right font-semibold">Legs over 15 min</th>
                <th scope="col" className="py-2 text-right font-semibold">Seated stops</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-stone-100 text-stone-800">
              <tr>
                <th scope="row" className="py-2 pr-3 text-left font-medium">Low</th>
                <td className="py-2 pr-3 text-right tabular-nums">3</td>
                <td className="py-2 pr-3 text-right tabular-nums">45 min</td>
                <td className="py-2 pr-3 text-right tabular-nums">15 min</td>
                <td className="py-2 pr-3 text-right tabular-nums">at most 1</td>
                <td className="py-2 text-right tabular-nums">at least 1</td>
              </tr>
              <tr>
                <th scope="row" className="py-2 pr-3 text-left font-medium">Medium</th>
                <td className="py-2 pr-3 text-right tabular-nums">4</td>
                <td className="py-2 pr-3 text-right tabular-nums">75 min</td>
                <td className="py-2 pr-3 text-right tabular-nums">10 min</td>
                <td className="py-2 pr-3 text-right tabular-nums">at most 2</td>
                <td className="py-2 text-right tabular-nums">—</td>
              </tr>
              <tr>
                <th scope="row" className="py-2 pr-3 text-left font-medium">High</th>
                <td className="py-2 pr-3 text-right tabular-nums">6</td>
                <td className="py-2 pr-3 text-right tabular-nums">150 min</td>
                <td className="py-2 pr-3 text-right tabular-nums">10 min</td>
                <td className="py-2 pr-3 text-right tabular-nums">at most 3</td>
                <td className="py-2 text-right tabular-nums">—</td>
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
          <li>
            Your text, tool output and event names are passed to the model as data, not instructions; event descriptions
            are never read. Instruction-like sentences never set a field in the rule-based parser.
          </li>
          <li>The tools only search, estimate, check and submit. The planner can’t book, pay or contact anyone.</li>
          <li>Pages render text only (no raw HTML) and only link to https addresses.</li>
        </ul>
      </Section>

      <Section id="data" title="Where the data comes from">
        <ul className="list-disc space-y-2 pl-5 text-sm text-stone-800">
          <li>
            <span className="font-semibold">Places:</span> Google Places when a key is configured, shown with “Google
            Maps” attribution and a link to each place. Otherwise, or when Google fails, a curated snapshot built from
            OpenStreetMap (© OpenStreetMap contributors), badged as curated; it covers Bengaluru only.
          </li>
          <li>
            <span className="font-semibold">City, timezone and weather:</span> a built-in list of cities, then Open-Meteo
            geocoding; the forecast comes from Open-Meteo.
          </li>
          <li>
            <span className="font-semibold">Events:</span> when enabled, a web search limited to an allow-list of sites,
            then structured event data read in code from at most three pages that robots.txt allows. Events link back to
            their page; check before you go.
          </li>
          <li>
            <span className="font-semibold">Travel:</span> distance-based estimates with per-city speeds, pickup times and
            fare tables (or a default table). Always labelled as estimates.
          </li>
          <li>
            <span className="font-semibold">Costs:</span> a listed price range when there is one, otherwise a band for the
            price level, a ticket price for events, free where the data says so, or a typical range for the category.
            Each cost shows its basis.
          </li>
          <li>
            <span className="font-semibold">Crowds:</span> there is no live crowd data. “Likely busy” is a heuristic:
            popularity (from the number of ratings, or curated) combined with the category’s typical Saturday busy hours.
          </li>
        </ul>
      </Section>

      <Section id="failures" title="When something goes wrong">
        <ul className="list-disc space-y-1 pl-5 text-sm text-stone-800">
          <li>
            A request the API can’t accept (malformed, too large, or over the rate limit) gets a clear error before any
            planning starts.
          </li>
          <li>Model failure or invalid submissions: a backup plan from the rule-based planner, with the reason shown.</li>
          <li>
            Places unavailable: the curated snapshot where it covers the city, badged; otherwise an honest “try again”
            message.
          </li>
          <li>Weather or events unavailable: the plan goes ahead and says what wasn’t checked.</li>
          <li>
            Nothing satisfies your hard constraints: the blocking constraints, the cheapest workable budget or shortest
            time found, and one-tap changes.
          </li>
        </ul>
      </Section>

      <Section id="privacy" title="Privacy">
        <p className="text-sm text-stone-800">
          No accounts. Your description is sent to the AI model to read your request, and is kept only in the run’s
          checkpoint, which is deleted when the run finishes, or after 24 hours if a question is never answered. It is
          never written to the logs. Shared plans don’t store
          your description, mood or constraint wording, and expire after 30 days. Feedback comments are kept for up to 90
          days. Optional LangSmith tracing is off by default and hides inputs and outputs in production.
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
