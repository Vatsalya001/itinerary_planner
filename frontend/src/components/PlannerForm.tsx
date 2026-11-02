import { useId, useState, type ReactNode, type Ref } from 'react'
import {
  CONSTRAINT_TOGGLES,
  CURRENCIES,
  DURATION_OPTIONS,
  INTEREST_PRESETS,
  LIMITS,
  restoreConstraint,
  toggleConstraint as toggledConstraint,
  type DirtyFields,
  type FieldKey,
  type FormState,
  type ToggleId,
} from '../lib/form'
import { fmtDuration, humanize } from '../lib/format'
import { constraintName } from '../lib/labels'
import { Icon } from './Icon'
import { btnPrimary, btnSecondary, inputClass } from './ui'

export type FormErrors = Partial<Record<FieldKey | 'freeText' | 'form', string>>

function Field({
  id,
  label,
  hint,
  error,
  overriding,
  children,
}: {
  id: string
  label: string
  hint?: string
  error?: string
  overriding?: boolean
  children: ReactNode
}) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="flex items-center gap-2 text-sm font-medium text-stone-800">
        {label}
        {overriding ? (
          <span className="rounded-full bg-orange-100 px-1.5 py-0.5 text-[11px] font-semibold text-orange-900">edited</span>
        ) : null}
      </label>
      {children}
      {hint ? (
        <p id={`${id}-hint`} className="text-xs text-stone-600">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={`${id}-error`} className="text-xs font-medium text-red-800">
          {error}
        </p>
      ) : null}
    </div>
  )
}

export function PlannerForm({
  form,
  dirty,
  errors,
  busy,
  onChange,
  onSubmit,
  onCancel,
  onReset,
  textareaRef,
}: {
  form: FormState
  dirty: DirtyFields
  errors: FormErrors
  busy: boolean
  onChange: (next: FormState, field?: FieldKey) => void
  onSubmit: () => void
  onCancel: () => void
  onReset: () => void
  textareaRef?: Ref<HTMLTextAreaElement>
}) {
  const [open, setOpen] = useState(false)
  const [customInterest, setCustomInterest] = useState('')
  const uid = useId()
  const id = (name: string) => `${uid}-${name}`
  const textMode = form.freeText.trim() !== ''
  const marked = (k: FieldKey) => textMode && Boolean(dirty[k])

  const set = <K extends keyof FormState>(key: K, value: FormState[K], field?: FieldKey) =>
    onChange({ ...form, [key]: value }, field)

  const toggleInterest = (interest: string) => {
    const has = form.interests.includes(interest)
    set('interests', has ? form.interests.filter((i) => i !== interest) : [...form.interests, interest], 'interests')
  }

  const addCustomInterest = () => {
    const v = customInterest.trim().slice(0, LIMITS.interest)
    if (!v) return
    if (!form.interests.some((i) => i.toLowerCase() === v.toLowerCase())) set('interests', [...form.interests, v], 'interests')
    setCustomInterest('')
  }

  const toggleConstraint = (tid: ToggleId) => onChange(toggledConstraint(form, tid), 'constraints')

  const interestChoices = [...INTEREST_PRESETS, ...form.interests.filter((i) => !INTEREST_PRESETS.includes(i))]
  const durationChoices = DURATION_OPTIONS.some((d) => String(d.minutes) === form.availableMinutes)
    ? DURATION_OPTIONS
    : [...DURATION_OPTIONS, { minutes: Number(form.availableMinutes), label: fmtDuration(Number(form.availableMinutes)) }]

  return (
    <form
      noValidate
      aria-label="Plan your Saturday"
      className="space-y-4 rounded-2xl border border-stone-200 bg-white p-4 shadow-sm sm:p-6"
      onSubmit={(e) => {
        e.preventDefault()
        onSubmit()
      }}
    >
      <div className="space-y-1.5">
        <label htmlFor={id('free')} className="block text-lg font-semibold text-stone-900">
          Describe your Saturday
        </label>
        <textarea
          id={id('free')}
          ref={textareaRef}
          rows={5}
          maxLength={LIMITS.freeText}
          value={form.freeText}
          aria-describedby={`${id('free')}-hint${errors.freeText ? ` ${id('free')}-error` : ''}`}
          aria-invalid={errors.freeText ? true : undefined}
          className={`${inputClass} resize-y leading-relaxed`}
          onChange={(e) => set('freeText', e.target.value)}
        />
        <div className="flex justify-between gap-3 text-xs text-stone-600">
          <p id={`${id('free')}-hint`}>
            How you feel, where you are, your budget and time, what you like and anything to avoid.
          </p>
          <span aria-hidden="true">{`${form.freeText.length}/${LIMITS.freeText}`}</span>
        </div>
        {errors.freeText ? (
          <p id={`${id('free')}-error`} className="text-xs font-medium text-red-800">
            {errors.freeText}
          </p>
        ) : null}
      </div>

      <div>
        <button
          type="button"
          className="inline-flex items-center gap-1 rounded-lg py-1 text-sm font-semibold text-orange-800 hover:text-orange-900"
          aria-expanded={open}
          aria-controls={id('details')}
          onClick={() => setOpen((o) => !o)}
        >
          <Icon name="chevronDown" className={`h-4 w-4 transition-transform ${open ? 'rotate-180' : ''}`} />
          {open ? 'Hide the details' : 'Fine-tune the details'}
        </button>

        <div id={id('details')} hidden={!open} className="mt-3 space-y-5">
          <p className="rounded-xl bg-stone-50 p-3 text-sm text-stone-700">
            {textMode
              ? 'Fields you change here override what the planner reads from your description. Clear the description to plan from these fields alone.'
              : 'The description is empty, so the planner will use exactly these fields.'}
          </p>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field id={id('city')} label="City" overriding={marked('city')} error={errors.city}>
              <input
                id={id('city')}
                className={inputClass}
                value={form.city}
                maxLength={LIMITS.city}
                autoComplete="address-level2"
                onChange={(e) => set('city', e.target.value, 'city')}
              />
            </Field>
            <Field id={id('hood')} label="Neighbourhood (optional)" overriding={marked('neighbourhood')}>
              <input
                id={id('hood')}
                className={inputClass}
                value={form.neighbourhood}
                maxLength={LIMITS.neighbourhood}
                placeholder="e.g. Indiranagar"
                onChange={(e) => set('neighbourhood', e.target.value, 'neighbourhood')}
              />
            </Field>

            <div className="grid grid-cols-[1fr_auto] gap-2">
              <Field id={id('budget')} label="Budget per person" overriding={marked('budget')} error={errors.budget}>
                <input
                  id={id('budget')}
                  className={inputClass}
                  type="number"
                  inputMode="numeric"
                  min={0}
                  max={LIMITS.budgetMax}
                  step={50}
                  value={form.budget}
                  aria-invalid={errors.budget ? true : undefined}
                  aria-describedby={errors.budget ? `${id('budget')}-error` : undefined}
                  onChange={(e) => set('budget', e.target.value, 'budget')}
                />
              </Field>
              <Field id={id('currency')} label="Currency" overriding={marked('currency')}>
                <select
                  id={id('currency')}
                  className={inputClass}
                  value={form.currency}
                  onChange={(e) => set('currency', e.target.value, 'currency')}
                >
                  <option value="">From city</option>
                  {CURRENCIES.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
              </Field>
            </div>

            <div className="grid grid-cols-2 gap-2">
              <Field
                id={id('start')}
                label="Start time"
                overriding={Boolean(dirty.startTime)}
                hint={dirty.startTime ? undefined : 'Default 3:00 PM'}
              >
                <input
                  id={id('start')}
                  className={inputClass}
                  type="time"
                  value={form.startTime}
                  aria-describedby={dirty.startTime ? undefined : `${id('start')}-hint`}
                  onChange={(e) => set('startTime', e.target.value, 'startTime')}
                />
              </Field>
              <Field id={id('time')} label="Time you have" overriding={marked('available')}>
                <select
                  id={id('time')}
                  className={inputClass}
                  value={form.availableMinutes}
                  onChange={(e) => set('availableMinutes', e.target.value, 'available')}
                >
                  {durationChoices.map((d) => (
                    <option key={d.minutes} value={String(d.minutes)}>
                      {d.label}
                    </option>
                  ))}
                </select>
              </Field>
            </div>

            <Field id={id('mood')} label="Mood" overriding={marked('mood')}>
              <input
                id={id('mood')}
                className={inputClass}
                value={form.mood}
                maxLength={LIMITS.mood}
                onChange={(e) => set('mood', e.target.value, 'mood')}
              />
            </Field>
            <Field id={id('group')} label="Group size" overriding={marked('groupSize')} error={errors.groupSize}>
              <input
                id={id('group')}
                className={inputClass}
                type="number"
                inputMode="numeric"
                min={LIMITS.groupMin}
                max={LIMITS.groupMax}
                value={form.groupSize}
                aria-invalid={errors.groupSize ? true : undefined}
                onChange={(e) => set('groupSize', e.target.value, 'groupSize')}
              />
            </Field>
          </div>

          <fieldset className="space-y-2">
            <legend className="flex items-center gap-2 text-sm font-medium text-stone-800">
              Interests
              {marked('interests') ? (
                <span className="rounded-full bg-orange-100 px-1.5 py-0.5 text-[11px] font-semibold text-orange-900">edited</span>
              ) : null}
            </legend>
            <div className="flex flex-wrap gap-2">
              {interestChoices.map((interest) => {
                const on = form.interests.includes(interest)
                return (
                  <button
                    key={interest}
                    type="button"
                    aria-pressed={on}
                    onClick={() => toggleInterest(interest)}
                    className={`inline-flex items-center gap-1 rounded-full border px-3 py-1.5 text-sm font-medium transition-colors ${
                      on
                        ? 'border-orange-700 bg-orange-700 text-white'
                        : 'border-stone-300 bg-white text-stone-800 hover:border-orange-400 hover:bg-orange-50'
                    }`}
                  >
                    {on ? <Icon name="check" className="h-3.5 w-3.5" /> : null}
                    {humanize(interest)}
                  </button>
                )
              })}
            </div>
            <div className="flex gap-2">
              <label htmlFor={id('custom-interest')} className="sr-only">
                Add another interest
              </label>
              <input
                id={id('custom-interest')}
                className={inputClass}
                placeholder="Add another interest"
                value={customInterest}
                maxLength={LIMITS.interest}
                onChange={(e) => setCustomInterest(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    addCustomInterest()
                  }
                }}
              />
              <button type="button" className={btnSecondary} onClick={addCustomInterest}>
                Add
              </button>
            </div>
            {errors.interests ? <p className="text-xs font-medium text-red-800">{errors.interests}</p> : null}
          </fieldset>

          <fieldset className="space-y-2">
            <legend className="flex items-center gap-2 text-sm font-medium text-stone-800">
              Must-haves
              {marked('constraints') ? (
                <span className="rounded-full bg-orange-100 px-1.5 py-0.5 text-[11px] font-semibold text-orange-900">edited</span>
              ) : null}
            </legend>
            <p className="text-xs text-stone-600">
              These are hard constraints: a stop only counts if there is positive evidence for it. Unknown is never treated as yes.
            </p>
            <div className="grid gap-2 sm:grid-cols-2">
              {CONSTRAINT_TOGGLES.map((t) => (
                <label
                  key={t.id}
                  className="flex cursor-pointer items-center gap-2 rounded-xl border border-stone-200 px-3 py-2 text-sm text-stone-800 hover:bg-stone-50 has-[:checked]:border-orange-300 has-[:checked]:bg-orange-50"
                >
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-orange-700"
                    checked={form.toggles[t.id]}
                    onChange={() => toggleConstraint(t.id)}
                  />
                  {t.label}
                </label>
              ))}
            </div>
            <Field
              id={id('extra')}
              label="Anything else? (optional)"
              hint="Separate with commas, e.g. include a sit-down meal, no malls"
              error={errors.constraints}
            >
              <input
                id={id('extra')}
                className={inputClass}
                value={form.extraConstraints}
                aria-describedby={`${id('extra')}-hint`}
                onChange={(e) => set('extraConstraints', e.target.value, 'constraints')}
              />
            </Field>
          </fieldset>

          <button type="button" className="text-sm font-medium text-stone-700 underline underline-offset-2" onClick={onReset}>
            Reset to the example
          </button>
        </div>
      </div>

      {form.relaxed.length ? (
        <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
          <p className="font-medium">Dropped to find a plan (kept for your next plans):</p>
          <ul className="mt-1 flex flex-wrap gap-2">
            {form.relaxed.map((key) => (
              <li key={key} className="inline-flex items-center gap-2 rounded-full border border-amber-300 bg-white py-0.5 pl-3 pr-1">
                <span>{constraintName(key)}</span>
                <button
                  type="button"
                  className="rounded-full px-2 py-0.5 text-xs font-semibold text-orange-800 hover:bg-orange-50"
                  aria-label={`Bring back ${constraintName(key)}`}
                  onClick={() => onChange(restoreConstraint(form, key))}
                >
                  Bring back
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {errors.form ? (
        <p role="alert" className="text-sm font-medium text-red-800">
          {errors.form}
        </p>
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <button type="submit" className={btnPrimary} disabled={busy}>
          {busy ? (
            <>
              <Icon name="spinner" className="h-4 w-4 motion-safe:animate-spin" />
              Planning…
            </>
          ) : (
            'Plan my Saturday'
          )}
        </button>
        {busy ? (
          <button type="button" className={btnSecondary} onClick={onCancel}>
            Stop
          </button>
        ) : null}
      </div>
    </form>
  )
}
