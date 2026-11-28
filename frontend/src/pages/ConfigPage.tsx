/**
 * 计算配置页(0.12-A1 起 CalculationConfig revision 编辑器)。
 *
 * - 读取:GET /config → 最新 revision 全文填表; 404 即空白表单(时间限制取
 *   GET /deployment/defaults 公开初值并同一次原子填入, 其余字段留空不预填;
 *   初值加载失败即明确报错);
 * - 保存:PUT {calculation_config, expected_revision}(自有链乐观锁, 首次为 null);
 *   成功提示 revision; 409 保留表单仅提示冲突, 由用户主动重载; 422 展示错误信息;
 * - predictions/evaluators/options/outputs 为 JSON 文本域, 本地解析失败即
 *   阻断保存; 空 seed 按契约以 null 提交, 空 evaluators/predictions 为 [],
 *   空 options 为 {}。
 *
 * 挂载方式:路由 /projects/:id/config 由 useParams 取项目 id;
 * 也可由工作台以 <ConfigPage projectId={id} /> 方式嵌入。
 */

import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'

import {
  coerceSchemaFieldValue,
  extractSchemaFields,
  getCalcConfig,
  getDeploymentDefaults,
  getStageCatalog,
  saveCalcConfig,
} from '../features/config'
import type { SchemaFieldSpec, StageCatalogDto, StageCatalogEntryDto } from '../features/config'
import { Alert, Badge, Button, Card, Checkbox, EmptyState, FormField, Input, Select, Spinner, Textarea } from '../components/ui'
import { errorMessage, useI18n } from '../i18n'
import { pt } from '../i18n/pageMessages'
import { ApiError } from '../types'
import type { CalculationConfigBody } from '../types'

export interface ConfigPageProps {
  projectId?: number
}

/**
 * 空白新建表单: 除时间限制取统一部署配置公开初值外全部留空, 不冒充已填写
 * (mode/generator/solver/precision 等均须用户明确选择; schema/schema_version
 * 为协议常量, 由提交时固定填入, 不在表单呈现)。
 */
function emptyFormState(): {
  mode: string
  generator: string
  solver: string
  algorithm: string
  evaluatorsText: string
  precision: '' | 'low' | 'medium' | 'high'
  timeLimit: string
  seed: string
  outputsText: string
  predictionsText: string
  optionsText: string
} {
  return {
    mode: '',
    generator: '',
    solver: '',
    algorithm: '',
    evaluatorsText: '',
    precision: '',
    timeLimit: '',
    seed: '',
    outputsText: '',
    predictionsText: '',
    optionsText: '',
  }
}

/** 解析 JSON 文本域: 空字符串按缺省处理, 解析失败返回错误信息。 */
function parseJsonField(text: string, emptyValue: unknown): { value: unknown; error: string | null } {
  const trimmed = text.trim()
  if (!trimmed) return { value: emptyValue, error: null }
  try {
    return { value: JSON.parse(trimmed) as unknown, error: null }
  } catch {
    return { value: null, error: pt('ies.config.invalid_json') }
  }
}

function pretty(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (Array.isArray(value) && value.length === 0) return ''
  if (typeof value === 'object' && Object.keys(value as Record<string, unknown>).length === 0) return ''
  return JSON.stringify(value, null, 2)
}

export default function ConfigPage(props: ConfigPageProps) {
  const { t } = useI18n()
  const { id } = useParams()
  const pid = props.projectId ?? (id === undefined ? undefined : Number(id))

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState<number | null>(null)
  const [mode, setMode] = useState('')
  const [generator, setGenerator] = useState('')
  const [solver, setSolver] = useState('')
  const [algorithm, setAlgorithm] = useState('')
  const [evaluatorsText, setEvaluatorsText] = useState('')
  const [precision, setPrecision] = useState<'' | 'low' | 'medium' | 'high'>('')
  const [timeLimit, setTimeLimit] = useState('')
  const [seed, setSeed] = useState('')
  const [outputsText, setOutputsText] = useState('')
  const [predictionsText, setPredictionsText] = useState('')
  const [optionsText, setOptionsText] = useState('')
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'success' | 'error'; text: string } | null>(null)
  // 409 冲突后为 true: 表单保留用户编辑, 仅提供主动重载入口
  const [conflict, setConflict] = useState(false)
  const [reloading, setReloading] = useState(false)
  const [stageCatalog, setStageCatalog] = useState<StageCatalogDto | null>(null)
  const [catalogError, setCatalogError] = useState<string | null>(null)
  const [selectedPredictor, setSelectedPredictor] = useState('')
  const [predictorTarget, setPredictorTarget] = useState('')
  const [selectedEvaluator, setSelectedEvaluator] = useState('')

  function applyBody(body: CalculationConfigBody, rev: number | null) {
    setMode(body.mode ?? '')
    setGenerator(body.generator ?? '')
    setSolver(body.solver ?? '')
    setAlgorithm(body.algorithm ?? '')
    setEvaluatorsText(pretty(body.evaluators))
    setPrecision(body.precision === 'low' || body.precision === 'medium' || body.precision === 'high' ? body.precision : '')
    setTimeLimit(Number.isFinite(body.time_limit_seconds) ? String(body.time_limit_seconds) : '')
    setSeed(typeof body.seed === 'number' ? String(body.seed) : '')
    setOutputsText(pretty(body.outputs))
    setPredictionsText(pretty(body.predictions))
    setOptionsText(pretty(body.options))
    setRevision(rev)
    setFieldErrors({})
    setNotice(null)
    setConflict(false)
  }

  useEffect(() => {
    if (pid === undefined) {
      setLoading(false)
      return
    }
    const projectIdNum: number = pid
    let cancelled = false
    async function load() {
      setLoading(true)
      setError(null)
      try {
        const cfg = await getCalcConfig(projectIdNum)
        if (!cancelled) applyBody(cfg.calculation_config, cfg.revision)
      } catch (err) {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 404) {
          // 空白新建: 时间初值必须取统一部署配置公开值, 与其余字段同一次原子填入;
          // 取不到即明确报错, 不得静默留空冒充正常初值, 迟到响应不得覆盖用户已编辑值
          try {
            const d = await getDeploymentDefaults()
            if (cancelled) return
            const e = emptyFormState()
            setMode(e.mode)
            setGenerator(e.generator)
            setSolver(e.solver)
            setAlgorithm(e.algorithm)
            setEvaluatorsText(e.evaluatorsText)
            setPrecision(e.precision)
            setTimeLimit(String(d.calculation_defaults.time_limit_seconds))
            setSeed(e.seed)
            setOutputsText(e.outputsText)
            setPredictionsText(e.predictionsText)
            setOptionsText(e.optionsText)
            setRevision(null)
            setFieldErrors({})
            setNotice(null)
            setConflict(false)
          } catch (depErr) {
            if (!cancelled) setError(errorMessage(depErr))
          }
        } else {
          setError(errorMessage(err))
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    getStageCatalog()
      .then((cat) => {
        if (!cancelled) {
          setStageCatalog(cat)
          setCatalogError(null)
        }
      })
      .catch((catErr) => {
        if (!cancelled) setCatalogError(errorMessage(catErr))
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid])

  /** 组装提交体: 返回 body 或字段错误表(13 字段 CalculationConfig 契约)。 */
  function buildBody(): { body: CalculationConfigBody | null; errors: Record<string, string> } {
    const errors: Record<string, string> = {}
    if (!mode.trim()) errors.mode = t('ies.config.required')
    const generatorVal = generator.trim() || null
    const solverVal = solver.trim() || null
    const algorithmVal = algorithm.trim() || null
    if (algorithmVal) {
      if (generatorVal) errors.generator = t('ies.config.mode_conflict')
      if (solverVal) errors.solver = t('ies.config.mode_conflict')
    } else {
      if (!generatorVal) errors.generator = t('ies.config.required')
      if (!solverVal) errors.solver = t('ies.config.required')
    }
    if (!precision) errors.precision = t('ies.config.required')
    const limit = Number(timeLimit)
    if (!timeLimit.trim() || !Number.isInteger(limit) || limit <= 0) {
      errors.time_limit_seconds = t('ies.config.time_limit_invalid')
    }
    let seedValue: number | null = null
    if (seed.trim()) {
      const s = Number(seed)
      if (!Number.isInteger(s)) {
        errors.seed = t('ies.config.time_limit_invalid')
      } else {
        seedValue = s
      }
    }
    const outputs = parseJsonField(outputsText, [])
    if (outputs.error) {
      errors.outputs = outputs.error
    } else if (!Array.isArray(outputs.value) || outputs.value.length === 0) {
      errors.outputs = t('ies.config.outputs_required')
    }
    const predictions = parseJsonField(predictionsText, [])
    if (predictions.error) {
      errors.predictions = predictions.error
    } else if (!Array.isArray(predictions.value)) {
      errors.predictions = t('ies.config.invalid_json_array')
    }
    const evaluators = parseJsonField(evaluatorsText, [])
    if (evaluators.error) {
      errors.evaluators = evaluators.error
    } else if (!Array.isArray(evaluators.value)) {
      errors.evaluators = t('ies.config.invalid_json_array')
    }
    const options = parseJsonField(optionsText, {})
    if (options.error) {
      errors.options = options.error
    } else if (options.value === null || typeof options.value !== 'object' || Array.isArray(options.value)) {
      errors.options = t('ies.config.invalid_json_object')
    }
    if (Object.keys(errors).length > 0) return { body: null, errors }
    return {
      body: {
        schema: 'ies.calculation-config',
        schema_version: '1.0.0',
        mode: mode.trim(),
        predictions: predictions.value as CalculationConfigBody['predictions'],
        generator: generatorVal,
        solver: solverVal,
        algorithm: algorithmVal,
        evaluators: evaluators.value as CalculationConfigBody['evaluators'],
        precision: precision as 'low' | 'medium' | 'high',
        time_limit_seconds: limit,
        options: options.value as Record<string, unknown>,
        seed: seedValue,
        outputs: outputs.value as string[],
      },
      errors,
    }
  }

  /** 用户主动重载: 以最新 revision 覆盖当前表单(将丢弃未提交编辑)。 */
  async function handleReloadLatest() {
    if (pid === undefined) return
    setReloading(true)
    try {
      const latest = await getCalcConfig(pid)
      applyBody(latest.calculation_config, latest.revision)
    } catch (err) {
      setNotice({ kind: 'error', text: errorMessage(err) })
    } finally {
      setReloading(false)
    }
  }

  async function handleSave() {
    if (pid === undefined) return
    const { body, errors } = buildBody()
    setFieldErrors(errors)
    if (!body) return
    setSaving(true)
    setNotice(null)
    setConflict(false)
    try {
      const saved = await saveCalcConfig(pid, { calculation_config: body, expected_revision: revision })
      applyBody(saved.calculation_config, saved.revision)
      setNotice({ kind: 'success', text: pt('ies.config.saved_ok', { revision: saved.revision }) })
    } catch (err) {
      // 后端按已选生成器公开 descriptor 返回字段定位。保留整份编辑态，
      // 总览与字段旁同时展示，不把静态不相容留到任务运行后才发现。
      if (err instanceof ApiError && err.status === 422 && err.location?.field) {
        const field = err.location.field.split('.')[0].split('[')[0].split('/')[0]
        if (['generator', 'solver', 'algorithm', 'evaluators', 'mode', 'precision', 'outputs', 'options', 'predictions', 'seed', 'time_limit_seconds'].includes(field)) {
          setFieldErrors({ [field]: errorMessage(err) })
        }
      }
      if (err instanceof ApiError && err.status === 409) {
        // 他人已保存新 revision: 保留当前表单编辑, 仅提示冲突,
        // 由用户决定是否主动重载(重载将覆盖未提交编辑)
        setConflict(true)
        setNotice({ kind: 'error', text: t('ies.config.conflict') })
      } else {
        setNotice({ kind: 'error', text: errorMessage(err) })
      }
    } finally {
      setSaving(false)
    }
  }

  if (pid === undefined) {
    return <EmptyState icon="info" title={pt('ies.config.no_project')} />
  }

  if (loading) {
    return (
      <div className="ies-page-placeholder">
        <Spinner size="lg" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="ies-page-placeholder">
        <Alert variant="error" title={t('ies.common.load_failed', { reason: error })} />
      </div>
    )
  }

  return (
    <div className="ies-stack">
      <div className="ies-page-header">
        <div>
          <h1 className="ies-page-title">{t('ies.config.title')}</h1>
          <p className="ies-page-subtitle">
            {revision === null ? t('ies.config.unsaved') : `${t('ies.config.revision')}: v${revision}`}
          </p>
        </div>
        <div className="ies-flex">
          <Button onClick={() => void handleSave()} loading={saving}>
            {t('ies.config.save')}
          </Button>
        </div>
      </div>

      {notice ? (
        <Alert variant={notice.kind === 'success' ? 'success' : 'error'}>{notice.text}</Alert>
      ) : null}
      {conflict ? (
        <div className="ies-flex">
          <Button onClick={() => void handleReloadLatest()} loading={reloading}>
            {t('ies.config.reload_latest')}
          </Button>
        </div>
      ) : null}

      <Card title={pt('ies.config_eco.title')}>
        {catalogError ? (
          <Alert variant="warning" title={pt('ies.config_eco.load_failed', { reason: catalogError })} />
        ) : null}
        {stageCatalog ? (
          <StageCatalogPanel
            catalog={stageCatalog}
            generator={generator}
            solver={solver}
            algorithm={algorithm}
            predictionsText={predictionsText}
            evaluatorsText={evaluatorsText}
            optionsText={optionsText}
            selectedPredictor={selectedPredictor}
            predictorTarget={predictorTarget}
            selectedEvaluator={selectedEvaluator}
            onSelectGenerator={(ref) => {
              setGenerator(ref)
              if (ref) setAlgorithm('')
            }}
            onSelectSolver={(ref) => {
              setSolver(ref)
              if (ref) setAlgorithm('')
            }}
            onSelectAlgorithm={(ref) => {
              setAlgorithm(ref)
              if (ref) {
                setGenerator('')
                setSolver('')
              }
            }}
            onChangePredictor={setSelectedPredictor}
            onChangePredictorTarget={setPredictorTarget}
            onChangePredictionsText={setPredictionsText}
            onSelectEvaluator={setSelectedEvaluator}
            onChangeEvaluatorsText={setEvaluatorsText}
            onChangeOptionsText={setOptionsText}
          />
        ) : null}
      </Card>

      <Card title={t('ies.config.title')}>
        <div className="ies-form-grid">
          <FormField label={t('ies.config.mode')} htmlFor="calc-mode" required error={fieldErrors.mode}>
            <Input id="calc-mode" value={mode} onChange={(e) => setMode(e.target.value)} placeholder="planning" invalid={!!fieldErrors.mode} />
          </FormField>
          <FormField label={t('ies.config.generator')} htmlFor="calc-generator" required={!algorithm.trim()} error={fieldErrors.generator} hint={t('ies.config.hint_refs')}>
            <Input
              id="calc-generator"
              value={generator}
              onChange={(e) => setGenerator(e.target.value)}
              placeholder="ies.generator.highs_milp@1.0.0"
              invalid={!!fieldErrors.generator}
            />
          </FormField>
          <FormField label={t('ies.config.solver')} htmlFor="calc-solver" required={!algorithm.trim()} error={fieldErrors.solver} hint={t('ies.config.hint_refs')}>
            <Input
              id="calc-solver"
              value={solver}
              onChange={(e) => setSolver(e.target.value)}
              placeholder="ies.solver.highs@1.7.2"
              invalid={!!fieldErrors.solver}
            />
          </FormField>
          <FormField label={t('ies.config.algorithm')} htmlFor="calc-algorithm" error={fieldErrors.algorithm} hint={t('ies.config.hint_refs')}>
            <Input
              id="calc-algorithm"
              value={algorithm}
              onChange={(e) => setAlgorithm(e.target.value)}
              placeholder="acme.algorithm.capacity_search@1.2.0"
              invalid={!!fieldErrors.algorithm}
            />
          </FormField>
          <FormField label={t('ies.config.precision')} htmlFor="calc-precision" required error={fieldErrors.precision}>
            <Select id="calc-precision" value={precision} onChange={(e) => setPrecision(e.target.value as '' | 'low' | 'medium' | 'high')} invalid={!!fieldErrors.precision}>
              <option value="">—</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
            </Select>
          </FormField>
          <FormField label={t('ies.config.time_limit')} htmlFor="calc-time-limit" required error={fieldErrors.time_limit_seconds}>
            <Input
              id="calc-time-limit"
              value={timeLimit}
              onChange={(e) => setTimeLimit(e.target.value)}
              inputMode="numeric"
              invalid={!!fieldErrors.time_limit_seconds}
            />
          </FormField>
          <FormField label={t('ies.config.seed_optional')} htmlFor="calc-seed" error={fieldErrors.seed}>
            <Input id="calc-seed" value={seed} onChange={(e) => setSeed(e.target.value)} inputMode="numeric" invalid={!!fieldErrors.seed} />
          </FormField>
        </div>
        <FormField label={t('ies.config.outputs')} htmlFor="calc-outputs" required error={fieldErrors.outputs}>
          <Textarea id="calc-outputs" value={outputsText} onChange={(e) => setOutputsText(e.target.value)} rows={3} invalid={!!fieldErrors.outputs} />
        </FormField>
        <FormField label={t('ies.config.predictions')} htmlFor="calc-predictions" error={fieldErrors.predictions}>
          <Textarea
            id="calc-predictions"
            value={predictionsText}
            onChange={(e) => setPredictionsText(e.target.value)}
            rows={5}
            invalid={!!fieldErrors.predictions}
          />
        </FormField>
        <FormField label={t('ies.config.evaluators')} htmlFor="calc-evaluators" error={fieldErrors.evaluators} hint={t('ies.config.hint_evaluators')}>
          <Textarea
            id="calc-evaluators"
            value={evaluatorsText}
            onChange={(e) => setEvaluatorsText(e.target.value)}
            rows={4}
            placeholder='[{"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}}]'
            invalid={!!fieldErrors.evaluators}
          />
        </FormField>
        <FormField label={t('ies.config.options')} htmlFor="calc-options" error={fieldErrors.options} hint={t('ies.config.hint_options')}>
          <Textarea
            id="calc-options"
            value={optionsText}
            onChange={(e) => setOptionsText(e.target.value)}
            rows={4}
            placeholder='{"generator": {}, "solver": {}}'
            invalid={!!fieldErrors.options}
          />
        </FormField>
      </Card>
    </div>
  )
}

function SlotSchemaFields({
  idPrefix,
  schema,
  values,
  onChangeField,
}: {
  idPrefix: string
  schema: Record<string, unknown> | null
  values: Record<string, unknown>
  onChangeField: (spec: SchemaFieldSpec, nextVal: unknown) => void
}) {
  const fields = extractSchemaFields(schema)
  if (fields.length === 0) return null
  return (
    <div className="ies-form-grid">
      {fields.map((spec) => {
        const current = values[spec.name]
        const inputId = `${idPrefix}-${spec.name}`
        if (spec.type === 'boolean') {
          const checked = typeof current === 'boolean' ? current : Boolean(spec.defaultValue)
          return (
            <FormField key={spec.name} label={spec.title} htmlFor={inputId} hint={spec.description || undefined}>
              <Checkbox
                id={inputId}
                checked={checked}
                onChange={(val) => onChangeField(spec, val)}
                label={spec.title}
              />
            </FormField>
          )
        }
        const textVal = current !== undefined && current !== null
          ? String(current)
          : spec.defaultValue !== undefined && spec.defaultValue !== null
            ? String(spec.defaultValue)
            : ''
        if (spec.enumValues) {
          return (
            <FormField key={spec.name} label={spec.title} htmlFor={inputId} required={spec.required} hint={spec.description || undefined}>
              <Select
                id={inputId}
                value={textVal}
                onChange={(e) => onChangeField(spec, coerceSchemaFieldValue(spec, e.target.value))}
              >
                <option value="">—</option>
                {spec.enumValues.map((opt) => (
                  <option key={opt} value={opt}>{opt}</option>
                ))}
              </Select>
            </FormField>
          )
        }
        return (
          <FormField key={spec.name} label={spec.title} htmlFor={inputId} required={spec.required} hint={spec.description || undefined}>
            <Input
              id={inputId}
              value={textVal}
              inputMode={spec.type === 'number' || spec.type === 'integer' ? 'decimal' : undefined}
              onChange={(e) => onChangeField(spec, coerceSchemaFieldValue(spec, e.target.value))}
            />
          </FormField>
        )
      })}
    </div>
  )
}

function StageCatalogPanel(props: {
  catalog: StageCatalogDto
  generator: string
  solver: string
  algorithm: string
  predictionsText: string
  evaluatorsText: string
  optionsText: string
  selectedPredictor: string
  predictorTarget: string
  selectedEvaluator: string
  onSelectGenerator: (ref: string) => void
  onSelectSolver: (ref: string) => void
  onSelectAlgorithm: (ref: string) => void
  onChangePredictor: (ref: string) => void
  onChangePredictorTarget: (target: string) => void
  onChangePredictionsText: (next: string) => void
  onSelectEvaluator: (ref: string) => void
  onChangeEvaluatorsText: (next: string) => void
  onChangeOptionsText: (next: string) => void
}) {
  const {
    catalog,
    generator,
    solver,
    algorithm,
    predictionsText,
    evaluatorsText,
    optionsText,
    selectedPredictor,
    predictorTarget,
    selectedEvaluator,
    onSelectGenerator,
    onSelectSolver,
    onSelectAlgorithm,
    onChangePredictor,
    onChangePredictorTarget,
    onChangePredictionsText,
    onSelectEvaluator,
    onChangeEvaluatorsText,
    onChangeOptionsText,
  } = props

  const parsedOptions = (() => {
    const p = parseJsonField(optionsText, {})
    return p.value && typeof p.value === 'object' && !Array.isArray(p.value)
      ? (p.value as Record<string, Record<string, unknown>>)
      : {}
  })()

  const parsedEvaluators = (() => {
    const p = parseJsonField(evaluatorsText, [])
    return Array.isArray(p.value)
      ? (p.value as Array<{ evaluator?: string; parameters?: Record<string, unknown> }>)
      : []
  })()

  const parsedPredictions = (() => {
    const p = parseJsonField(predictionsText, [])
    return Array.isArray(p.value)
      ? (p.value as Array<{ target?: string; predictor?: string; parameters?: Record<string, unknown> }>)
      : []
  })()

  const updateSlotOption = (slot: 'generator' | 'solver' | 'algorithm', spec: SchemaFieldSpec, nextVal: unknown) => {
    const nextOptions: Record<string, Record<string, unknown>> = { ...parsedOptions }
    const slotObj = { ...(nextOptions[slot] ?? {}) }
    if (nextVal === undefined) delete slotObj[spec.name]
    else slotObj[spec.name] = nextVal
    nextOptions[slot] = slotObj
    onChangeOptionsText(JSON.stringify(nextOptions, null, 2))
  }

  const entryFor = (list: StageCatalogEntryDto[], ref: string) => list.find((e) => e.ref === ref) ?? null
  const activeSlots: Array<{ slot: 'generator' | 'solver' | 'algorithm'; entry: StageCatalogEntryDto }> = []
  const genEntry = generator ? entryFor(catalog.generators, generator) : null
  const solEntry = solver ? entryFor(catalog.solvers, solver) : null
  const algEntry = algorithm ? entryFor(catalog.algorithms, algorithm) : null
  if (genEntry) activeSlots.push({ slot: 'generator', entry: genEntry })
  if (solEntry) activeSlots.push({ slot: 'solver', entry: solEntry })
  if (algEntry) activeSlots.push({ slot: 'algorithm', entry: algEntry })

  const handleAddPredictor = () => {
    const predRef = selectedPredictor || catalog.predictors[0]?.ref || ''
    const target = predictorTarget.trim()
    if (!predRef || !target) return
    const next = [...parsedPredictions, { target, predictor: predRef, parameters: {} }]
    onChangePredictionsText(JSON.stringify(next, null, 2))
    onChangePredictorTarget('')
  }

  const handleAddEvaluator = () => {
    const evalRef = selectedEvaluator || catalog.evaluators[0]?.ref || ''
    if (!evalRef) return
    if (parsedEvaluators.some((item) => item?.evaluator === evalRef)) return
    const next = [...parsedEvaluators, { evaluator: evalRef, parameters: {} }]
    onChangeEvaluatorsText(JSON.stringify(next, null, 2))
  }

  const handleRemoveEvaluator = (idx: number) => {
    const next = parsedEvaluators.filter((_, i) => i !== idx)
    onChangeEvaluatorsText(next.length === 0 ? '' : JSON.stringify(next, null, 2))
  }

  const handleUpdateEvaluatorParam = (idx: number, spec: SchemaFieldSpec, nextVal: unknown) => {
    const next = parsedEvaluators.map((item, i) => {
      if (i !== idx) return item
      const params = { ...(item.parameters ?? {}) }
      if (nextVal === undefined) delete params[spec.name]
      else params[spec.name] = nextVal
      return { ...item, parameters: params }
    })
    onChangeEvaluatorsText(JSON.stringify(next, null, 2))
  }

  return (
    <div className="ies-stack">
      <div className="ies-form-grid">
        <FormField label={pt('ies.config_eco.pick_generator')} htmlFor="calc-catalog-generator">
          <Select id="calc-catalog-generator" value={generator} onChange={(e) => onSelectGenerator(e.target.value)}>
            <option value="">—</option>
            {catalog.generators.map((g) => (
              <option key={g.ref} value={g.ref}>
                {g.ref} ({g.source})
              </option>
            ))}
          </Select>
        </FormField>
        <FormField label={pt('ies.config_eco.pick_solver')} htmlFor="calc-catalog-solver">
          <Select id="calc-catalog-solver" value={solver} onChange={(e) => onSelectSolver(e.target.value)}>
            <option value="">—</option>
            {catalog.solvers.map((s) => (
              <option key={s.ref} value={s.ref}>
                {s.ref} ({s.source})
              </option>
            ))}
          </Select>
        </FormField>
        <FormField label={pt('ies.config_eco.pick_algorithm')} htmlFor="calc-catalog-algorithm">
          <Select id="calc-catalog-algorithm" value={algorithm} onChange={(e) => onSelectAlgorithm(e.target.value)}>
            <option value="">—</option>
            {catalog.algorithms.map((a) => (
              <option key={a.ref} value={a.ref}>
                {a.ref} ({a.source})
              </option>
            ))}
          </Select>
        </FormField>
      </div>

      {activeSlots.map(({ slot, entry }) => {
        if (extractSchemaFields(entry.parameters_schema).length === 0) return null
        return (
          <div key={slot} className="ies-section">
            <h3 className="ies-section-subtitle">
              {pt('ies.config_eco.slot_params', { slot, ref: entry.ref })}
            </h3>
            <SlotSchemaFields
              idPrefix={`calc-slot-${slot}`}
              schema={entry.parameters_schema}
              values={parsedOptions[slot] ?? {}}
              onChangeField={(spec, val) => updateSlotOption(slot, spec, val)}
            />
          </div>
        )
      })}

      {catalog.predictors.length > 0 ? (
        <div className="ies-form-row" style={{ alignItems: 'flex-end' }}>
          <FormField label={pt('ies.config_eco.pick_predictor')} htmlFor="calc-catalog-predictor">
            <Select
              id="calc-catalog-predictor"
              value={selectedPredictor || catalog.predictors[0]?.ref || ''}
              onChange={(e) => onChangePredictor(e.target.value)}
            >
              {catalog.predictors.map((p) => (
                <option key={p.ref} value={p.ref}>
                  {p.ref} ({p.source})
                </option>
              ))}
            </Select>
          </FormField>
          <FormField label={pt('ies.config_eco.predictor_target')} htmlFor="calc-catalog-predictor-target">
            <Input
              id="calc-catalog-predictor-target"
              value={predictorTarget}
              onChange={(e) => onChangePredictorTarget(e.target.value)}
              placeholder="load_elec"
            />
          </FormField>
          <Button size="sm" variant="secondary" onClick={handleAddPredictor}>
            {pt('ies.config_eco.add_predictor')}
          </Button>
        </div>
      ) : null}

      {catalog.evaluators.length > 0 ? (
        <div className="ies-stack">
          <div className="ies-form-row" style={{ alignItems: 'flex-end' }}>
            <FormField label={pt('ies.config_eco.pick_evaluator')} htmlFor="calc-catalog-evaluator">
              <Select
                id="calc-catalog-evaluator"
                value={selectedEvaluator || catalog.evaluators[0]?.ref || ''}
                onChange={(e) => onSelectEvaluator(e.target.value)}
              >
                {catalog.evaluators.map((ev) => (
                  <option key={ev.ref} value={ev.ref}>
                    {ev.ref} ({ev.source})
                  </option>
                ))}
              </Select>
            </FormField>
            <Button size="sm" variant="secondary" onClick={handleAddEvaluator}>
              {pt('ies.config_eco.add_evaluator')}
            </Button>
          </div>
          {parsedEvaluators.length > 0 ? (
            <div className="ies-stack">
              <h3 className="ies-section-subtitle">
                {pt('ies.config_eco.evaluators_list', { count: parsedEvaluators.length })}
              </h3>
              {parsedEvaluators.map((evRow, idx) => {
                const ref = evRow?.evaluator ?? ''
                const evSpec = ref ? entryFor(catalog.evaluators, ref) : null
                return (
                  <div key={`${ref}-${idx}`} className="ies-custom-card">
                    <div className="ies-custom-card__head">
                      <div className="ies-flex">
                        <strong>{ref}</strong>
                        {evSpec ? <Badge size="sm" variant="neutral" label={evSpec.source} /> : null}
                      </div>
                      <Button size="sm" variant="danger" onClick={() => handleRemoveEvaluator(idx)}>
                        {pt('ies.config_eco.remove')}
                      </Button>
                    </div>
                    {evSpec ? (
                      <div style={{ marginTop: 'var(--ies-space-2)' }}>
                        <SlotSchemaFields
                          idPrefix={`calc-eval-${idx}`}
                          schema={evSpec.parameters_schema}
                          values={evRow.parameters ?? {}}
                          onChangeField={(spec, val) => handleUpdateEvaluatorParam(idx, spec, val)}
                        />
                      </div>
                    ) : null}
                  </div>
                )
              })}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
