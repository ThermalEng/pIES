/**
 * ISSUE-03: explicit instance cost and metered price bindings.
 * No financial assumptions or device-to-finance inference. Both complete
 * sections are saved together through the existing optimistic draft commands.
 */
import { useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import { Link } from 'react-router-dom'

import { Alert, Button, Card, FormField, Input, Select, Spinner } from '../components/ui'
import { getBindingDraft, saveBindings } from '../features/assembly-bindings'
import type { BindingDraft, CostBindingDto, DriverTargetDto, TariffBindingDto } from '../features/assembly-bindings'
import { getEffectiveFinance } from '../features/finance'
import { listDeviceTypes } from '../features/modeling'
import { ApiError } from '../types'
import type { DeviceTypeSpec } from '../types'
import { errorMessage } from '../i18n'
import { pt } from '../i18n/pageMessages'

type Aggregation = '' | 'integrate_positive' | 'sum_positive'
type DriverRow = {
  id: number
  name: string
  source: '' | 'property' | 'interface'
  target: string
  aggregation: Aggregation
}
type CostRow = {
  instance: string
  mode: '' | 'none' | 'costed'
  financeType: string
  drivers: DriverRow[]
}
type TariffRow = {
  id: number
  bindingId: string
  price: string
  instance: string
  interfaceId: string
  aggregation: Aggregation
}
type EffectiveCatalog = {
  financeTypes: Record<string, Record<string, unknown>>
  prices: Record<string, { carrier: string; direction: string }>
}

const AGGREGATIONS: Exclude<Aggregation, ''>[] = ['integrate_positive', 'sum_positive']

function errorDetails(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null
  const parts = [err.code, err.location ? JSON.stringify(err.location) : '', Object.keys(err.params).length ? JSON.stringify(err.params) : '']
  return parts.filter(Boolean).join(' · ')
}

/** Only for suggestions: read declared finance drivers, never choose a driver binding. */
function declaredDrivers(catalog: EffectiveCatalog | null, financeType: string): string[] {
  const entry = catalog?.financeTypes[financeType]
  if (!entry) return []
  const names = new Set<string>()
  for (const key of ['upfront_capex', 'annual_fixed_om', 'period_variable_om']) {
    const component = entry[key] as { linear?: Record<string, unknown> } | undefined
    for (const name of Object.keys(component?.linear ?? {})) names.add(name)
  }
  return [...names].sort()
}

export default function EconomicBindingsPage() {
  const { id } = useParams()
  const projectId = id === undefined ? undefined : Number(id)
  const counter = useRef(0)
  const newId = () => ++counter.current
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [revision, setRevision] = useState<number | null>(null)
  const [devices, setDevices] = useState<BindingDraft['devices']>([])
  const [descriptors, setDescriptors] = useState<DeviceTypeSpec[]>([])
  const [catalog, setCatalog] = useState<EffectiveCatalog | null>(null)
  const [costs, setCosts] = useState<CostRow[]>([])
  const [orphans, setOrphans] = useState<string[]>([])
  const [tariffs, setTariffs] = useState<TariffRow[]>([])
  const [busy, setBusy] = useState<'save' | 'reload' | null>(null)
  const [conflict, setConflict] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [formDetail, setFormDetail] = useState<string | null>(null)

  function deviceSpec(instance: string): DeviceTypeSpec | undefined {
    const type = devices.find((d) => d.name === instance)?.device_type
    return descriptors.find((d) => d.type_id === type)
  }

  function applyDraft(draft: BindingDraft) {
    const names = new Set(draft.devices.map((d) => d.name))
    setDevices(draft.devices)
    setCosts(draft.devices.map((device) => {
      const binding = draft.instances[device.name]
      return {
        instance: device.name,
        mode: binding?.type ?? '',
        financeType: binding?.type === 'costed' ? binding.finance_type : '',
        drivers: binding?.type === 'costed' ? Object.entries(binding.drivers).map(([name, target]) => ({
          id: newId(),
          name,
          source: 'property' in target ? 'property' as const : 'interface' as const,
          target: 'property' in target ? target.property : target.interface,
          aggregation: 'aggregation' in target ? target.aggregation : '' as const,
        })) : [],
      }
    }))
    setOrphans(Object.keys(draft.instances).filter((name) => !names.has(name)))
    setTariffs(Object.entries(draft.tariffs).map(([bindingId, binding]) => ({
      id: newId(),
      bindingId,
      price: binding.price,
      instance: binding.instance,
      interfaceId: binding.interface,
      aggregation: binding.aggregation,
    })))
    setRevision(draft.revision)
    setConflict(false)
    setNotice(null)
    setFormError(null)
    setFormDetail(null)
  }

  async function reload(project: number, cancelled: () => boolean) {
    const [draft, specs, effective] = await Promise.all([
      getBindingDraft(project),
      listDeviceTypes(project),
      getEffectiveFinance(project).catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 404) return null
        throw err
      }),
    ])
    if (cancelled()) return
    setDescriptors(specs)
    setCatalog(effective ? {
      financeTypes: effective.effective_finance_config.finance_types as EffectiveCatalog['financeTypes'],
      prices: effective.effective_finance_config.energy_prices as EffectiveCatalog['prices'],
    } : null)
    applyDraft(draft)
  }

  useEffect(() => {
    if (projectId === undefined) { setLoading(false); return }
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    void reload(projectId, () => cancelled).catch((err: unknown) => {
      if (!cancelled) setLoadError(errorMessage(err))
    }).finally(() => {
      if (!cancelled) setLoading(false)
    })
    return () => { cancelled = true }
    // Project workbench remounts on projectId change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  function updateCost(instance: string, change: Partial<CostRow>) {
    setCosts((all) => all.map((c) => c.instance === instance ? { ...c, ...change } : c))
  }
  function updateDriver(instance: string, id: number, change: Partial<DriverRow>) {
    setCosts((all) => all.map((c) => c.instance === instance
      ? { ...c, drivers: c.drivers.map((d) => d.id === id ? { ...d, ...change } : d) }
      : c))
  }
  function updateTariff(id: number, change: Partial<TariffRow>) {
    setTariffs((all) => all.map((r) => r.id === id ? { ...r, ...change } : r))
  }

  function buildPayload(): { instances: Record<string, CostBindingDto>; tariffs: Record<string, TariffBindingDto> } | null {
    if (orphans.length > 0) {
      setFormError(pt('ies.bindings.orphans_error'))
      return null
    }
    const instances: Record<string, CostBindingDto> = {}
    for (const cost of costs) {
      if (!cost.mode) {
        setFormError(pt('ies.bindings.unassigned', { instance: cost.instance }))
        return null
      }
      if (cost.mode === 'none') { instances[cost.instance] = { type: 'none' }; continue }
      if (!cost.financeType) {
        setFormError(pt('ies.bindings.finance_required', { instance: cost.instance }))
        return null
      }
      const drivers: Record<string, DriverTargetDto> = {}
      for (const row of cost.drivers) {
        const name = row.name.trim(), target = row.target.trim()
        if (!name || !target || !row.source || (row.source === 'interface' && !row.aggregation) || Object.hasOwn(drivers, name)) {
          setFormError(pt('ies.bindings.driver_invalid', { instance: cost.instance }))
          return null
        }
        drivers[name] = row.source === 'property'
          ? { property: target }
          : { interface: target, aggregation: row.aggregation as Exclude<Aggregation, ''> }
      }
      instances[cost.instance] = { type: 'costed', finance_type: cost.financeType, drivers }
    }

    const bindings: Record<string, TariffBindingDto> = {}
    for (const row of tariffs) {
      const name = row.bindingId.trim()
      if (!name || !row.price || !row.instance || !row.interfaceId.trim() || !row.aggregation || Object.hasOwn(bindings, name)) {
        setFormError(pt('ies.bindings.tariff_invalid'))
        return null
      }
      bindings[name] = {
        price: row.price,
        instance: row.instance,
        interface: row.interfaceId.trim(),
        aggregation: row.aggregation,
      }
    }
    setFormError(null)
    setFormDetail(null)
    return { instances, tariffs: bindings }
  }

  async function save() {
    if (projectId === undefined || revision === null || !catalog || busy) return
    const payload = buildPayload()
    if (!payload) return
    setBusy('save')
    setNotice(null)
    setConflict(false)
    try {
      await saveBindings(projectId, revision, payload.instances, payload.tariffs)
      // Read back the actual saved draft, not a locally fabricated saved state.
      const draft = await getBindingDraft(projectId)
      applyDraft(draft)
      setNotice(pt('ies.bindings.saved', { revision: draft.revision }))
    } catch (err) {
      setConflict(err instanceof ApiError && err.status === 409)
      setFormError(errorMessage(err))
      setFormDetail(errorDetails(err))
    } finally {
      setBusy(null)
    }
  }

  async function reloadLatest() {
    if (projectId === undefined || busy) return
    setBusy('reload')
    try {
      await reload(projectId, () => false)
    } catch (err) {
      setFormError(errorMessage(err))
      setFormDetail(errorDetails(err))
    } finally {
      setBusy(null)
    }
  }

  if (projectId === undefined) return <Alert variant="error">{pt('ies.bindings.no_project')}</Alert>
  if (loading) return <div className="ies-page-placeholder"><Spinner size="lg" /></div>
  if (loadError) return <Alert variant="error">{loadError}</Alert>

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--ies-space-4)' }}>
      <div className="ies-page-header">
        <div>
          <h1 className="ies-page-title">{pt('ies.bindings.title')}</h1>
          <p className="ies-page-subtitle">{pt('ies.bindings.revision', { revision: revision ?? '—' })}</p>
        </div>
        <div className="ies-flex">
          <Button variant="secondary" onClick={() => void reloadLatest()} loading={busy === 'reload'} disabled={busy !== null}>{pt('ies.bindings.reload')}</Button>
          <Button onClick={() => void save()} loading={busy === 'save'} disabled={busy !== null || !catalog || devices.length === 0}>{pt('ies.bindings.save')}</Button>
        </div>
      </div>
      <p>{pt('ies.bindings.intro')}</p>
      {catalog === null && (
        <Alert variant="warning">
          {pt('ies.bindings.no_effective')} <Link to={'/projects/' + projectId + '/finance'}>{pt('ies.bindings.finance_link')}</Link>
        </Alert>
      )}
      {notice && <Alert variant="success">{notice}</Alert>}
      {formError && <Alert variant="error" title={formError}>{formDetail}</Alert>}
      {conflict && <Alert variant="warning">{pt('ies.bindings.conflict')}</Alert>}
      {devices.length === 0 && <Alert variant="info">{pt('ies.bindings.no_devices')}</Alert>}
      {orphans.length > 0 && (
        <Card title={pt('ies.bindings.orphans')}>
          <p>{pt('ies.bindings.orphans_hint')}</p>
          {orphans.map((name) => (
            <div key={name} className="ies-flex">
              <strong>{name}</strong>
              <Button size="sm" variant="secondary" disabled={busy !== null}
                onClick={() => setOrphans((current) => current.filter((v) => v !== name))}>
                {pt('ies.bindings.remove')}
              </Button>
            </div>
          ))}
        </Card>
      )}

      <fieldset disabled={busy !== null} style={{ border: 0, margin: 0, padding: 0, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 'var(--ies-space-4)' }}>
        <Card title={pt('ies.bindings.cost_title')}>
          <p>{pt('ies.bindings.cost_help')}</p>
          {costs.map((cost) => {
            const device = devices.find((d) => d.name === cost.instance)
            const spec = deviceSpec(cost.instance)
            const driverNames = declaredDrivers(catalog, cost.financeType)
            return (
              <div key={cost.instance} style={{ borderTop: '1px solid var(--ies-color-border)', marginTop: 'var(--ies-space-3)', paddingTop: 'var(--ies-space-3)' }}>
                <h3>{cost.instance} <small>({device?.device_type}, {device?.kind})</small></h3>
                <FormField label={pt('ies.bindings.cost_mode')} htmlFor={'binding-mode-' + cost.instance} required>
                  <Select id={'binding-mode-' + cost.instance} value={cost.mode}
                    onChange={(e) => updateCost(cost.instance, { mode: e.target.value as CostRow['mode'] })}>
                    <option value="">{pt('ies.bindings.choose')}</option>
                    <option value="none">{pt('ies.bindings.none')}</option>
                    <option value="costed">{pt('ies.bindings.costed')}</option>
                  </Select>
                </FormField>
                {cost.mode === 'costed' && <>
                  <FormField label={pt('ies.bindings.finance_type')} htmlFor={'binding-finance-' + cost.instance} required>
                    <Select id={'binding-finance-' + cost.instance} value={cost.financeType}
                      onChange={(e) => updateCost(cost.instance, { financeType: e.target.value })}>
                      <option value="">{pt('ies.bindings.choose')}</option>
                      {cost.financeType && !Object.hasOwn(catalog?.financeTypes ?? {}, cost.financeType) &&
                        <option value={cost.financeType}>{cost.financeType}</option>}
                      {Object.keys(catalog?.financeTypes ?? {}).sort().map((name) => <option key={name} value={name}>{name}</option>)}
                    </Select>
                  </FormField>
                  <p>{pt('ies.bindings.available_drivers', { names: driverNames.join(', ') || '—' })}</p>
                  {cost.drivers.map((row) => (
                    <div key={row.id} style={{ borderTop: '1px dashed var(--ies-color-border)', paddingTop: 'var(--ies-space-2)' }}>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 'var(--ies-space-2)' }}>
                        <FormField label={pt('ies.bindings.driver_name')} htmlFor={'binding-driver-' + row.id} required>
                          <Input id={'binding-driver-' + row.id} list={'binding-driver-choices-' + cost.instance} value={row.name}
                            onChange={(e) => updateDriver(cost.instance, row.id, { name: e.target.value })} />
                        </FormField>
                        <FormField label={pt('ies.bindings.driver_source')} htmlFor={'binding-source-' + row.id} required>
                          <Select id={'binding-source-' + row.id} value={row.source}
                            onChange={(e) => updateDriver(cost.instance, row.id, { source: e.target.value as DriverRow['source'], target: '', aggregation: '' })}>
                            <option value="">{pt('ies.bindings.choose')}</option>
                            <option value="property">property</option>
                            <option value="interface">interface</option>
                          </Select>
                        </FormField>
                        <FormField label={pt('ies.bindings.driver_target')} htmlFor={'binding-target-' + row.id} required>
                          <Input id={'binding-target-' + row.id} value={row.target}
                            list={row.source === 'property' ? 'binding-props-' + cost.instance : 'binding-ports-' + cost.instance}
                            onChange={(e) => updateDriver(cost.instance, row.id, { target: e.target.value })} />
                        </FormField>
                        {row.source === 'interface' && <FormField label={pt('ies.bindings.aggregation')} htmlFor={'binding-agg-' + row.id} required>
                          <Select id={'binding-agg-' + row.id} value={row.aggregation}
                            onChange={(e) => updateDriver(cost.instance, row.id, { aggregation: e.target.value as Aggregation })}>
                            <option value="">{pt('ies.bindings.choose')}</option>
                            {AGGREGATIONS.map((a) => <option key={a} value={a}>{a}</option>)}
                          </Select>
                        </FormField>}
                      </div>
                      <Button size="sm" variant="secondary" onClick={() => updateCost(cost.instance, { drivers: cost.drivers.filter((d) => d.id !== row.id) })}>{pt('ies.bindings.remove_driver')}</Button>
                    </div>
                  ))}
                  <datalist id={'binding-driver-choices-' + cost.instance}>{driverNames.map((n) => <option key={n} value={n} />)}</datalist>
                  <datalist id={'binding-props-' + cost.instance}>{Object.keys(spec?.parameters ?? {}).map((n) => <option key={n} value={n} />)}</datalist>
                  <datalist id={'binding-ports-' + cost.instance}>{(spec?.ports ?? []).map((p) => <option key={p.name} value={p.name} />)}</datalist>
                  <Button variant="secondary" size="sm" onClick={() => updateCost(cost.instance, { drivers: [...cost.drivers, { id: newId(), name: '', source: '', target: '', aggregation: '' }] })}>{pt('ies.bindings.add_driver')}</Button>
                </>}
              </div>
            )
          })}
        </Card>

        <Card title={pt('ies.bindings.tariff_title')}>
          <p>{pt('ies.bindings.tariff_help')}</p>
          {tariffs.map((row) => {
            const spec = deviceSpec(row.instance)
            return (
              <div key={row.id} style={{ borderTop: '1px solid var(--ies-color-border)', marginTop: 'var(--ies-space-3)', paddingTop: 'var(--ies-space-3)' }}>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(175px, 1fr))', gap: 'var(--ies-space-2)' }}>
                  <FormField label={pt('ies.bindings.binding_id')} htmlFor={'tariff-id-' + row.id} required>
                    <Input id={'tariff-id-' + row.id} value={row.bindingId} onChange={(e) => updateTariff(row.id, { bindingId: e.target.value })} />
                  </FormField>
                  <FormField label={pt('ies.bindings.price')} htmlFor={'tariff-price-' + row.id} required>
                    <Select id={'tariff-price-' + row.id} value={row.price} onChange={(e) => updateTariff(row.id, { price: e.target.value })}>
                      <option value="">{pt('ies.bindings.choose')}</option>
                      {row.price && !Object.hasOwn(catalog?.prices ?? {}, row.price) && <option value={row.price}>{row.price}</option>}
                      {Object.entries(catalog?.prices ?? {}).sort(([a], [b]) => a.localeCompare(b)).map(([id, p]) =>
                        <option key={id} value={id}>{id} ({p.carrier}, {p.direction})</option>)}
                    </Select>
                  </FormField>
                  <FormField label={pt('ies.bindings.instance')} htmlFor={'tariff-instance-' + row.id} required>
                    <Select id={'tariff-instance-' + row.id} value={row.instance} onChange={(e) => updateTariff(row.id, { instance: e.target.value, interfaceId: '' })}>
                      <option value="">{pt('ies.bindings.choose')}</option>
                      {row.instance && !devices.some((d) => d.name === row.instance) && <option value={row.instance}>{row.instance}</option>}
                      {devices.map((d) => <option key={d.name} value={d.name}>{d.name}</option>)}
                    </Select>
                  </FormField>
                  <FormField label={pt('ies.bindings.interface')} htmlFor={'tariff-interface-' + row.id} required
                    hint={(spec?.ports ?? []).filter((p) => p.direction === 'in' || p.direction === 'out').map((p) => p.name).join(', ') || undefined}>
                    <Input id={'tariff-interface-' + row.id} list={'tariff-ports-' + row.id} value={row.interfaceId}
                      onChange={(e) => updateTariff(row.id, { interfaceId: e.target.value })} />
                  </FormField>
                  <FormField label={pt('ies.bindings.aggregation')} htmlFor={'tariff-agg-' + row.id} required>
                    <Select id={'tariff-agg-' + row.id} value={row.aggregation} onChange={(e) => updateTariff(row.id, { aggregation: e.target.value as Aggregation })}>
                      <option value="">{pt('ies.bindings.choose')}</option>
                      {AGGREGATIONS.map((a) => <option key={a} value={a}>{a}</option>)}
                    </Select>
                  </FormField>
                </div>
                <datalist id={'tariff-ports-' + row.id}>{(spec?.ports ?? []).filter((p) => p.direction === 'in' || p.direction === 'out').map((p) => <option key={p.name} value={p.name} />)}</datalist>
                <Button size="sm" variant="secondary" onClick={() => setTariffs((all) => all.filter((v) => v.id !== row.id))}>{pt('ies.bindings.remove')}</Button>
              </div>
            )
          })}
          <div style={{ marginTop: 'var(--ies-space-3)' }}>
            <Button variant="secondary" onClick={() => setTariffs((all) => [...all, { id: newId(), bindingId: '', price: '', instance: '', interfaceId: '', aggregation: '' }])}>{pt('ies.bindings.add_tariff')}</Button>
          </div>
        </Card>
      </fieldset>
    </div>
  )
}
