/**
 * ISSUE-05: project PlanningConfig editor (separate from CalculationConfig).
 * The saved revision is project-wide; the effective finance snapshot must exist
 * before the backend accepts the first save.
 */
import { useEffect, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'

import { Alert, Button, Card, EmptyState, FormField, Input, Select, Spinner, Textarea } from '../components/ui'
import { getPlanningConfig, savePlanningConfig } from '../features/config'
import { EMPTY_PLANNING, planningFromYaml, planningToYaml } from '../features/config/planningDocument'
import type { PlanningConfigBody, PlanningConstraintDto, PlanningVariableDto } from '../features/config'
import { getModel } from '../features/modeling'
import { errorMessage } from '../i18n'
import { pt } from '../i18n/pageMessages'
import { ApiError } from '../types'

type Sense = '' | 'minimize' | 'maximize'
type VariableRow = {
  id: number
  name: string
  device_ref: string
  parameter: string
  lower_bound: string
  upper_bound: string
  unit: string
  manual?: boolean
}
type ConstraintRow = {
  id: number
  name: string
  type: string
  expression: string
  enabled: boolean
  references?: PlanningConstraintDto['references']
}

const CONSTRAINT_TYPES = [
  'load_balance',
  'capacity_limit',
  'co2_limit',
  'annual_purchase_cost_limit',
  'ratio',
  'step_relation',
]

export default function PlanningPage() {
  const { id } = useParams()
  const projectId = id === undefined ? undefined : Number(id)
  const nextRowId = useRef(0)
  const newId = () => ++nextRowId.current
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [revision, setRevision] = useState<number | null>(null)
  const [sense, setSense] = useState<Sense>('')
  const [expression, setExpression] = useState('')
  const [variables, setVariables] = useState<VariableRow[]>([])
  const [constraints, setConstraints] = useState<ConstraintRow[]>([])
  const [baseConfig, setBaseConfig] = useState<PlanningConfigBody | null>(null)
  const [view, setView] = useState<'form' | 'document'>('form')
  const [documentText, setDocumentText] = useState(planningToYaml(EMPTY_PLANNING))
  const [saving, setSaving] = useState(false)
  const [reloading, setReloading] = useState(false)
  const [conflict, setConflict] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [devices, setDevices] = useState<Array<{ ref: string; name: string }>>([])
  const [devicesError, setDevicesError] = useState<string | null>(null)

  function applyConfig(body: PlanningConfigBody | null, rev: number | null) {
    setBaseConfig(body)
    setDocumentText(planningToYaml(body ?? EMPTY_PLANNING))
    populateForm(body)
    setRevision(rev)
    setConflict(false)
    setFormError(null)
    setNotice(null)
  }

  function populateForm(body: PlanningConfigBody | null) {
    const rawSense = body?.objective.sense
    setSense(rawSense === 'minimize' || rawSense === 'maximize' ? rawSense : '')
    setExpression(body?.objective.expression ?? '')
    setVariables(Object.entries(body?.variables ?? {}).map(([name, value]) => ({
      id: newId(),
      name,
      device_ref: value.device_ref,
      parameter: value.parameter,
      lower_bound: value.lower_bound ?? '',
      upper_bound: value.upper_bound ?? '',
      unit: value.unit ?? '',
    })))
    setConstraints(Object.entries(body?.constraints ?? {}).map(([name, value]) => ({
      id: newId(),
      name,
      type: value.type,
      expression: value.expression,
      enabled: value.enabled !== false,
      references: value.references,
    })))
  }

  useEffect(() => {
    if (projectId === undefined) {
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    void getPlanningConfig(projectId).then(
      (saved) => {
        if (!cancelled) applyConfig(saved.planning_config, saved.revision)
      },
      (err: unknown) => {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 404) {
          applyConfig(null, null)
        } else {
          setLoadError(errorMessage(err))
        }
      },
    ).finally(() => { if (!cancelled) setLoading(false) })
    void getModel(projectId).then(
      (res) => {
        if (!cancelled) {
          setDevices(res.model.devices.map((d) => ({
            ref: d.ref,
            name: d.display_name,
          })))
        }
      },
      (err: unknown) => {
        if (!cancelled) setDevicesError(errorMessage(err))
      },
    )
    return () => { cancelled = true }
    // Component instance is re-mounted by project workbench when projectId changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId])

  function updateVariable(rowId: number, patch: Partial<VariableRow>) {
    setVariables((current) => current.map((row) => row.id === rowId ? { ...row, ...patch } : row))
  }

  function updateConstraint(rowId: number, patch: Partial<ConstraintRow>) {
    setConstraints((current) => current.map((row) => row.id === rowId ? { ...row, ...patch } : row))
  }

  function buildConfig(): PlanningConfigBody | null {
    if (!sense || !expression.trim() || variables.length === 0) {
      setFormError(pt('ies.planning.required'))
      return null
    }
    const variableMap: Record<string, PlanningVariableDto> = {}
    for (const row of variables) {
      const name = row.name.trim()
      if (!name || !row.device_ref.trim() || !row.parameter.trim() || Object.hasOwn(variableMap, name)) {
        setFormError(pt('ies.planning.variable_invalid'))
        return null
      }
      variableMap[name] = {
        device_ref: row.device_ref.trim(),
        parameter: row.parameter.trim(),
        lower_bound: row.lower_bound.trim() || null,
        upper_bound: row.upper_bound.trim() || null,
        unit: row.unit.trim() || null,
      }
    }
    const constraintMap: Record<string, PlanningConstraintDto> = {}
    for (const row of constraints) {
      const name = row.name.trim()
      if (!name || !row.type || !row.expression.trim() || Object.hasOwn(constraintMap, name)) {
        setFormError(pt('ies.planning.constraint_invalid'))
        return null
      }
      constraintMap[name] = {
        type: row.type,
        expression: row.expression.trim(),
        enabled: row.enabled,
        ...(row.type === 'step_relation' && row.references ? { references: row.references } : {}),
      }
    }
    setFormError(null)
    return {
      ...(baseConfig ?? EMPTY_PLANNING),
      objective: { ...(baseConfig?.objective ?? {}), sense, expression: expression.trim() },
      variables: variableMap,
      constraints: constraintMap,
    }
  }

  function serializeFormState(): PlanningConfigBody {
    const variableMap: Record<string, PlanningVariableDto> = {}
    const usedVarNames = new Set<string>()
    variables.forEach((row, index) => {
      let key = row.name.trim()
      if (!key || usedVarNames.has(key)) {
        key = key ? `${key}_${index + 1}` : `variable_${index + 1}`
      }
      usedVarNames.add(key)
      variableMap[key] = {
        device_ref: row.device_ref.trim(),
        parameter: row.parameter.trim(),
        lower_bound: row.lower_bound.trim() || null,
        upper_bound: row.upper_bound.trim() || null,
        unit: row.unit.trim() || null,
      }
    })
    const constraintMap: Record<string, PlanningConstraintDto> = {}
    const usedConNames = new Set<string>()
    constraints.forEach((row, index) => {
      let key = row.name.trim()
      if (!key || usedConNames.has(key)) {
        key = key ? `${key}_${index + 1}` : `constraint_${index + 1}`
      }
      usedConNames.add(key)
      constraintMap[key] = {
        type: row.type || '',
        expression: row.expression.trim(),
        enabled: row.enabled !== false,
        ...(row.type === 'step_relation' && row.references ? { references: row.references } : {}),
      }
    })
    return {
      ...(baseConfig ?? EMPTY_PLANNING),
      objective: { ...(baseConfig?.objective ?? {}), sense, expression: expression.trim() },
      variables: variableMap,
      ...(constraints.length > 0 || (baseConfig?.constraints && Object.keys(baseConfig.constraints).length > 0)
        ? { constraints: constraintMap }
        : {}),
    }
  }

  function switchView(target: 'form' | 'document') {
    if (target === view || saving || reloading) return
    if (target === 'document') {
      const state = serializeFormState()
      setBaseConfig(state)
      setDocumentText(planningToYaml(state))
    } else {
      try {
        const parsed = planningFromYaml(documentText)
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed) ||
            !parsed.objective || typeof parsed.objective !== 'object' || Array.isArray(parsed.objective) ||
            (parsed.variables !== undefined && (typeof parsed.variables !== 'object' || Array.isArray(parsed.variables))) ||
            (parsed.constraints !== undefined && (typeof parsed.constraints !== 'object' || Array.isArray(parsed.constraints)))) {
          throw new Error('Planning YAML root needs an objective mapping')
        }
        setBaseConfig(parsed)
        populateForm(parsed)
      } catch (err) {
        setFormError(String(err instanceof Error ? err.message : err))
        return
      }
    }
    setFormError(null)
    setView(target)
  }

  async function handleSave() {
    if (projectId === undefined) return
    let body: PlanningConfigBody
    if (view === 'document') {
      try {
        body = planningFromYaml(documentText)
      } catch (err) {
        setFormError(String(err instanceof Error ? err.message : err))
        return
      }
    } else {
      const form = buildConfig()
      if (!form) return
      body = form
    }
    setSaving(true)
    setNotice(null)
    setConflict(false)
    try {
      const saved = await savePlanningConfig(projectId, body, revision)
      applyConfig(saved.planning_config, saved.revision)
      setNotice(pt('ies.planning.saved', { revision: saved.revision }))
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setConflict(true)
        setFormError(pt('ies.planning.conflict'))
      } else {
        setFormError(errorMessage(err))
      }
    } finally {
      setSaving(false)
    }
  }

  async function handleReload() {
    if (projectId === undefined) return
    setReloading(true)
    try {
      const saved = await getPlanningConfig(projectId)
      applyConfig(saved.planning_config, saved.revision)
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        applyConfig(null, null)
      } else {
        setFormError(errorMessage(err))
      }
    } finally {
      setReloading(false)
    }
  }

  if (projectId === undefined) return <EmptyState icon="info" title={pt('ies.planning.no_project')} />
  if (loading) return <div className="ies-page-placeholder"><Spinner size="lg" /></div>
  if (loadError) return <div className="ies-page-placeholder"><Alert variant="error">{loadError}</Alert></div>

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--ies-space-4)' }}>
      <div className="ies-page-header">
        <div>
          <h1 className="ies-page-title">{pt('ies.planning.title')}</h1>
          <p className="ies-page-subtitle">
            {revision === null ? pt('ies.planning.unsaved') : pt('ies.planning.revision', { revision })}
          </p>
        </div>
        <Button onClick={() => void handleSave()} loading={saving} disabled={reloading}>
          {pt('ies.planning.save')}
        </Button>
      </div>
      <p>{pt('ies.planning.intro')}</p>
      <div className="ies-flex" role="group" aria-label={pt('ies.planning.view_label')}>
        <Button variant={view === 'form' ? 'primary' : 'secondary'} disabled={saving || reloading}
          onClick={() => switchView('form')}>{pt('ies.planning.form_tab')}</Button>
        <Button variant={view === 'document' ? 'primary' : 'secondary'} disabled={saving || reloading}
          onClick={() => switchView('document')}>{pt('ies.planning.document_tab')}</Button>
      </div>
      {notice && <Alert variant="success">{notice}</Alert>}
      {formError && <Alert variant="error">{formError}</Alert>}
      {conflict && (
        <Button variant="secondary" onClick={() => void handleReload()} loading={reloading} disabled={saving}>
          {pt('ies.planning.reload')}
        </Button>
      )}

      {/* A saved/reloaded revision replaces the entire form. Prevent edits in flight. */}
      <fieldset
        disabled={saving || reloading}
        style={{ border: 0, margin: 0, padding: 0, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 'var(--ies-space-4)' }}
      >
      {view === 'document' ? (
        <Card title={pt('ies.planning.document_tab')}>
          <FormField label={pt('ies.planning.document_label')} htmlFor="planning-yaml"
            hint={pt('ies.planning.document_hint')}>
            <Textarea id="planning-yaml" value={documentText} spellCheck={false}
              onChange={(e) => setDocumentText(e.target.value)} rows={24} />
          </FormField>
        </Card>
      ) : (<>
      <Card title={pt('ies.planning.objective')}>
        <FormField label={pt('ies.planning.sense')} htmlFor="planning-sense" required>
          <Select id="planning-sense" value={sense} onChange={(e) => setSense(e.target.value as Sense)}>
            <option value="">{pt('ies.planning.choose')}</option>
            <option value="minimize">{pt('ies.planning.minimize')}</option>
            <option value="maximize">{pt('ies.planning.maximize')}</option>
          </Select>
        </FormField>
        <FormField label={pt('ies.planning.expression')} htmlFor="planning-expression" required
          hint={pt('ies.planning.expression_hint')}>
          <Textarea id="planning-expression" value={expression} onChange={(e) => setExpression(e.target.value)}
            rows={3} />
        </FormField>
      </Card>

      <Card title={pt('ies.planning.variables')}>
        <p>{pt('ies.planning.variables_help')}</p>
        {variables.map((row, index) => (
          <div key={row.id} style={{ borderTop: '1px solid var(--ies-color-border)', paddingTop: 'var(--ies-space-3)', marginTop: 'var(--ies-space-3)' }}>
            <div className="ies-flex" style={{ justifyContent: 'space-between' }}>
              <strong>{pt('ies.planning.variable_number', { number: index + 1 })}</strong>
              <Button size="sm" variant="secondary" onClick={() => setVariables((current) => current.filter((x) => x.id !== row.id))}>
                {pt('ies.planning.remove')}
              </Button>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 'var(--ies-space-3)' }}>
              <FormField label={pt('ies.planning.name')} htmlFor={'planning-var-name-' + row.id} required>
                <Input id={'planning-var-name-' + row.id} value={row.name} onChange={(e) => updateVariable(row.id, { name: e.target.value })} />
              </FormField>
              <FormField label={pt('ies.planning.device_ref')} htmlFor={'planning-var-device-' + row.id} required>
                {devicesError ? (
                  <div>
                    <span className="ies-form-message" style={{ color: 'var(--ies-color-error)', display: 'block', marginBottom: 'var(--ies-space-1)' }}>
                      {devicesError}
                    </span>
                    <Input
                      id={'planning-var-device-' + row.id}
                      value={row.device_ref}
                      placeholder="手动输入实例引用 (如 pv_new)"
                      onChange={(e) => updateVariable(row.id, { device_ref: e.target.value })}
                    />
                  </div>
                ) : devices.length > 0 && !row.manual ? (
                  <div>
                    <Select
                      id={'planning-var-device-' + row.id}
                      value={row.device_ref}
                      onChange={(e) => updateVariable(row.id, { device_ref: e.target.value })}
                    >
                      <option value="">{pt('ies.planning.choose')}</option>
                      {row.device_ref && !devices.some((d) => d.ref === row.device_ref) && (
                        <option value={row.device_ref}>{`${row.device_ref} (当前引用)`}</option>
                      )}
                      {devices.map((d) => (
                        <option key={d.ref} value={d.ref}>
                          {d.name === d.ref ? d.name : `${d.name} (${d.ref})`}
                        </option>
                      ))}
                    </Select>
                    <div style={{ marginTop: 'var(--ies-space-1)' }}>
                      <button
                        type="button"
                        className="ies-link-button"
                        style={{ fontSize: 'var(--ies-font-size-xs)' }}
                        onClick={() => updateVariable(row.id, { manual: true })}
                      >
                        手动输入引用
                      </button>
                    </div>
                  </div>
                ) : (
                  <div>
                    <Input
                      id={'planning-var-device-' + row.id}
                      value={row.device_ref}
                      placeholder="输入实例引用 (如 pv_new)"
                      onChange={(e) => updateVariable(row.id, { device_ref: e.target.value })}
                    />
                    {devices.length > 0 && (
                      <div style={{ marginTop: 'var(--ies-space-1)' }}>
                        <button
                          type="button"
                          className="ies-link-button"
                          style={{ fontSize: 'var(--ies-font-size-xs)' }}
                          onClick={() => updateVariable(row.id, { manual: false })}
                        >
                          从设备列表选择
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </FormField>
              <FormField label={pt('ies.planning.parameter')} htmlFor={'planning-var-param-' + row.id} required>
                <Input id={'planning-var-param-' + row.id} value={row.parameter} onChange={(e) => updateVariable(row.id, { parameter: e.target.value })} />
              </FormField>
              <FormField label={pt('ies.planning.lower')} htmlFor={'planning-var-lower-' + row.id}>
                <Input id={'planning-var-lower-' + row.id} value={row.lower_bound} onChange={(e) => updateVariable(row.id, { lower_bound: e.target.value })} />
              </FormField>
              <FormField label={pt('ies.planning.upper')} htmlFor={'planning-var-upper-' + row.id}>
                <Input id={'planning-var-upper-' + row.id} value={row.upper_bound} onChange={(e) => updateVariable(row.id, { upper_bound: e.target.value })} />
              </FormField>
              <FormField label={pt('ies.planning.unit')} htmlFor={'planning-var-unit-' + row.id}>
                <Input id={'planning-var-unit-' + row.id} value={row.unit} onChange={(e) => updateVariable(row.id, { unit: e.target.value })} />
              </FormField>
            </div>
          </div>
        ))}
        <div style={{ marginTop: 'var(--ies-space-3)' }}>
          <Button variant="secondary" onClick={() => setVariables((current) => [...current, {
            id: newId(), name: '', device_ref: '', parameter: '', lower_bound: '', upper_bound: '', unit: '',
          }])}>{pt('ies.planning.add_variable')}</Button>
        </div>
      </Card>

      <Card title={pt('ies.planning.constraints')}>
        <p>{pt('ies.planning.constraints_help')}</p>
        {constraints.map((row, index) => (
          <div key={row.id} style={{ borderTop: '1px solid var(--ies-color-border)', paddingTop: 'var(--ies-space-3)', marginTop: 'var(--ies-space-3)' }}>
            <div className="ies-flex" style={{ justifyContent: 'space-between' }}>
              <strong>{pt('ies.planning.constraint_number', { number: index + 1 })}</strong>
              <Button size="sm" variant="secondary" onClick={() => setConstraints((current) => current.filter((x) => x.id !== row.id))}>
                {pt('ies.planning.remove')}
              </Button>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 'var(--ies-space-3)' }}>
              <FormField label={pt('ies.planning.name')} htmlFor={'planning-con-name-' + row.id} required>
                <Input id={'planning-con-name-' + row.id} value={row.name} onChange={(e) => updateConstraint(row.id, { name: e.target.value })} />
              </FormField>
              <FormField label={pt('ies.planning.constraint_type')} htmlFor={'planning-con-type-' + row.id} required>
                <Select id={'planning-con-type-' + row.id} value={row.type} onChange={(e) => updateConstraint(row.id, { type: e.target.value })}>
                  <option value="">{pt('ies.planning.choose')}</option>
                  {CONSTRAINT_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
                </Select>
              </FormField>
              <FormField label={pt('ies.planning.constraint_enabled')} htmlFor={'planning-con-enabled-' + row.id}>
                <Select id={'planning-con-enabled-' + row.id} value={row.enabled ? 'true' : 'false'} onChange={(e) => updateConstraint(row.id, { enabled: e.target.value === 'true' })}>
                  <option value="true">{pt('ies.planning.enabled')}</option>
                  <option value="false">{pt('ies.planning.disabled')}</option>
                </Select>
              </FormField>
            </div>
            <FormField label={pt('ies.planning.expression')} htmlFor={'planning-con-expression-' + row.id} required>
              <Textarea id={'planning-con-expression-' + row.id} value={row.expression}
                onChange={(e) => updateConstraint(row.id, { expression: e.target.value })} rows={2} />
            </FormField>
            {row.type === 'step_relation' && (
              <p>{pt('ies.planning.step_references_hint')}</p>
            )}
          </div>
        ))}
        <div style={{ marginTop: 'var(--ies-space-3)' }}>
          <Button variant="secondary" onClick={() => setConstraints((current) => [...current, {
            id: newId(), name: '', type: '', expression: '', enabled: true,
          }])}>{pt('ies.planning.add_constraint')}</Button>
        </div>
      </Card>
      </>)}
      </fieldset>
    </div>
  )
}
