/** A structured view over the existing JSON authoring document, not a second resource. */
import { useState } from 'react'
import { Alert, Button, FormField, Input } from '../../../components/ui'
import { pt } from '../../../i18n/pageMessages'

type Rate = { rate: { value: string; unit: string } }
type Catalog = Record<string, Rate>
type Props = {
  mode: 'profile' | 'overrides'
  text: string
  onChange: (text: string) => void
  profileId?: string | null
  profileCharges?: Catalog
}

function readDoc(text: string): Record<string, unknown> | null {
  if (!text.trim()) return null
  try {
    const doc: unknown = JSON.parse(text)
    return doc && typeof doc === 'object' && !Array.isArray(doc) ? doc as Record<string, unknown> : null
  } catch {
    return null
  }
}

function readCharges(doc: Record<string, unknown> | null): Catalog | null {
  const source = doc?.demand_charges
  if (source === undefined) return {}
  if (!source || typeof source !== 'object' || Array.isArray(source)) return null
  for (const item of Object.values(source)) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null
    const rate = (item as { rate?: unknown }).rate
    if (!rate || typeof rate !== 'object' || Array.isArray(rate)
      || typeof (rate as Rate['rate']).value !== 'string'
      || typeof (rate as Rate['rate']).unit !== 'string') return null
  }
  return source as Catalog
}

export default function DemandRateEditor({ mode, text, onChange, profileId, profileCharges = {} }: Props) {
  const [newId, setNewId] = useState('')
  const [newRate, setNewRate] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const doc = readDoc(text)
  const validCharges = readCharges(doc)
  const charges = validCharges ?? {}
  const currency = mode === 'profile'
    ? ((doc?.profile as { currency?: string } | undefined)?.currency ?? '—')
    : Object.values(profileCharges)[0]?.rate.unit.split('/')[0] ?? '—'

  function replaceCharges(next: Catalog) {
    // Overrides have an existing empty-document state (404); start its canonical
    // authoring envelope, preserving the exact Profile identity selected by user.
    const base = doc ?? (mode === 'overrides' && profileId ? {
      schema: 'ies.finance-overrides', schema_version: '4.0.0', profile_ref: { id: profileId },
    } : null)
    if (!base) {
      setMessage(pt('ies.finance.demand_document_first'))
      return
    }
    onChange(JSON.stringify({ ...base, demand_charges: next }, null, 2))
    setMessage(null)
  }

  function editRate(id: string, value: string, unit: string) {
    const next = { ...charges }
    if (mode === 'overrides' && value === '') delete next[id]
    else next[id] = { rate: { value, unit } }
    replaceCharges(next)
  }

  const rows = mode === 'profile' ? charges : profileCharges
  return (
    <div style={{ marginTop: 'var(--ies-space-3)', padding: 'var(--ies-space-3)', border: '1px solid var(--ies-color-border)', borderRadius: 8 }}>
      <h3>{pt(mode === 'profile' ? 'ies.finance.demand_profile_title' : 'ies.finance.demand_override_title')}</h3>
      <p>{pt(mode === 'profile' ? 'ies.finance.demand_profile_help' : 'ies.finance.demand_override_help')}</p>
      {message && <Alert variant="error">{message}</Alert>}
      {text.trim() && (!doc || !validCharges) && <Alert variant="error">{pt('ies.finance.invalid_json')}</Alert>}
      {Object.keys(rows).length === 0 && <p>{pt('ies.finance.demand_empty')}</p>}
      {Object.entries(rows).sort(([a], [b]) => a.localeCompare(b)).map(([id, entry]) => {
        const unit = mode === 'profile' ? entry.rate.unit : profileCharges[id].rate.unit
        const value = mode === 'profile' ? entry.rate.value : charges[id]?.rate.value ?? ''
        return (
          <div key={id} className="ies-flex" style={{ marginTop: 'var(--ies-space-2)', alignItems: 'end', gap: 'var(--ies-space-2)', flexWrap: 'wrap' }}>
            <div style={{ flex: '1 1 180px' }}>
              <FormField label={`${id} (${unit})`} htmlFor={`${mode}-demand-${id}`}>
                <Input id={`${mode}-demand-${id}`} value={value}
                  placeholder={mode === 'overrides' ? `${pt('ies.finance.demand_inherit')}: ${entry.rate.value}` : undefined}
                  onChange={(e) => editRate(id, e.target.value, unit)} disabled={!!text.trim() && (!doc || !validCharges)} />
              </FormField>
            </div>
            <Button variant="secondary" size="sm" disabled={!!text.trim() && (!doc || !validCharges)}
              onClick={() => { const next = { ...charges }; delete next[id]; replaceCharges(next) }}>
              {pt(mode === 'profile' ? 'ies.finance.demand_remove' : 'ies.finance.demand_revert')}
            </Button>
          </div>
        )
      })}
      {mode === 'profile' && (
        <div className="ies-flex" style={{ marginTop: 'var(--ies-space-3)', alignItems: 'end', flexWrap: 'wrap', gap: 'var(--ies-space-2)' }}>
          <FormField label={pt('ies.finance.demand_id')} htmlFor="finance-demand-new-id">
            <Input id="finance-demand-new-id" value={newId} onChange={(e) => setNewId(e.target.value)} />
          </FormField>
          <FormField label={`${pt('ies.finance.demand_rate')} (${currency}/kW)`} htmlFor="finance-demand-new-rate">
            <Input id="finance-demand-new-rate" value={newRate} onChange={(e) => setNewRate(e.target.value)} />
          </FormField>
          <Button variant="secondary" disabled={!!text.trim() && (!doc || !validCharges)} onClick={() => {
            const id = newId.trim()
            if (!id || !newRate || currency === '—' || Object.hasOwn(charges, id)) {
              setMessage(pt('ies.finance.demand_add_invalid'))
              return
            }
            replaceCharges({ ...charges, [id]: { rate: { value: newRate, unit: `${currency}/kW` } } })
            setNewId('')
            setNewRate('')
          }}>{pt('ies.finance.demand_add')}</Button>
        </div>
      )}
    </div>
  )
}
