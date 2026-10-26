/**
 * 计算配置页(0.12-A1 起 CalculationConfig revision 编辑器)。
 *
 * - 读取:GET /config → 最新 revision 全文填表; 404 即空白表单(时间限制取
 *   GET /deployment/defaults 公开初值并同一次原子填入, 其余字段留空不预填;
 *   初值加载失败即明确报错);
 * - 保存:PUT {calculation_config, expected_revision}(自有链乐观锁, 首次为 null);
 *   成功提示 revision; 409 保留表单仅提示冲突, 由用户主动重载; 422 展示错误信息;
 * - predictions/options/convergence/outputs 为 JSON 文本域, 本地解析失败即
 *   阻断保存; 空 convergence/seed 按契约以 null 提交。
 *
 * 挂载方式:路由 /projects/:id/config 由 useParams 取项目 id;
 * 也可由工作台以 <ConfigPage projectId={id} /> 方式嵌入。
 */

import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'

import { getCalcConfig, getDeploymentDefaults, saveCalcConfig } from '../features/config'
import { Alert, Button, Card, EmptyState, FormField, Input, Select, Spinner, Textarea } from '../components/ui'
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
  precision: '' | 'low' | 'medium' | 'high'
  timeLimit: string
  seed: string
  outputsText: string
  predictionsText: string
  convergenceText: string
  optionsText: string
} {
  return {
    mode: '',
    generator: '',
    solver: '',
    precision: '',
    timeLimit: '',
    seed: '',
    outputsText: '',
    predictionsText: '',
    convergenceText: '',
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
  const [precision, setPrecision] = useState<'' | 'low' | 'medium' | 'high'>('')
  const [timeLimit, setTimeLimit] = useState('')
  const [seed, setSeed] = useState('')
  const [outputsText, setOutputsText] = useState('')
  const [predictionsText, setPredictionsText] = useState('')
  const [convergenceText, setConvergenceText] = useState('')
  const [optionsText, setOptionsText] = useState('')
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'success' | 'error'; text: string } | null>(null)
  // 409 冲突后为 true: 表单保留用户编辑, 仅提供主动重载入口
  const [conflict, setConflict] = useState(false)
  const [reloading, setReloading] = useState(false)

  function applyBody(body: CalculationConfigBody, rev: number | null) {
    setMode(body.mode ?? '')
    setGenerator(body.generator ?? '')
    setSolver(body.solver ?? '')
    setPrecision(body.precision === 'low' || body.precision === 'medium' || body.precision === 'high' ? body.precision : '')
    setTimeLimit(Number.isFinite(body.time_limit_seconds) ? String(body.time_limit_seconds) : '')
    setSeed(typeof body.seed === 'number' ? String(body.seed) : '')
    setOutputsText(pretty(body.outputs))
    setPredictionsText(pretty(body.predictions))
    setConvergenceText(pretty(body.convergence))
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
            setPrecision(e.precision)
            setTimeLimit(String(d.calculation_defaults.time_limit_seconds))
            setSeed(e.seed)
            setOutputsText(e.outputsText)
            setPredictionsText(e.predictionsText)
            setConvergenceText(e.convergenceText)
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
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid])

  /** 组装提交体: 返回 body 或字段错误表(空 convergence/seed 按契约以 null 提交)。 */
  function buildBody(): { body: CalculationConfigBody | null; errors: Record<string, string> } {
    const errors: Record<string, string> = {}
    if (!mode.trim()) errors.mode = t('ies.config.required')
    if (!generator.trim()) errors.generator = t('ies.config.required')
    if (!solver.trim()) errors.solver = t('ies.config.required')
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
    if (predictions.error) errors.predictions = predictions.error
    const convergence = parseJsonField(convergenceText, null)
    if (convergence.error) errors.convergence = convergence.error
    const options = parseJsonField(optionsText, {})
    if (options.error) errors.options = options.error
    if (Object.keys(errors).length > 0) return { body: null, errors }
    return {
      body: {
        schema: 'ies.calculation-config',
        schema_version: '2.0.0',
        mode: mode.trim(),
        predictions: predictions.value as CalculationConfigBody['predictions'],
        generator: generator.trim(),
        solver: solver.trim(),
        precision: precision as 'low' | 'medium' | 'high',
        convergence: convergence.value as CalculationConfigBody['convergence'],
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
        const field = err.location.field
        if (['generator', 'mode', 'precision', 'solver', 'outputs', 'options'].includes(field)) {
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
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--ies-space-4)' }}>
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

      <Card title={t('ies.config.title')}>
        <FormField label={t('ies.config.mode')} htmlFor="calc-mode" required error={fieldErrors.mode}>
          <Input id="calc-mode" value={mode} onChange={(e) => setMode(e.target.value)} placeholder="planning" invalid={!!fieldErrors.mode} />
        </FormField>
        <FormField label={t('ies.config.generator')} htmlFor="calc-generator" required error={fieldErrors.generator} hint={t('ies.config.hint_refs')}>
          <Input
            id="calc-generator"
            value={generator}
            onChange={(e) => setGenerator(e.target.value)}
            placeholder="ies.generator.highs_milp@5.0.0"
            invalid={!!fieldErrors.generator}
          />
        </FormField>
        <FormField label={t('ies.config.solver')} htmlFor="calc-solver" required error={fieldErrors.solver} hint={t('ies.config.hint_refs')}>
          <Input
            id="calc-solver"
            value={solver}
            onChange={(e) => setSolver(e.target.value)}
            placeholder="ies.solver.highs@1.7.2"
            invalid={!!fieldErrors.solver}
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
        <FormField label={t('ies.config.convergence')} htmlFor="calc-convergence" error={fieldErrors.convergence}>
          <Textarea
            id="calc-convergence"
            value={convergenceText}
            onChange={(e) => setConvergenceText(e.target.value)}
            rows={3}
            invalid={!!fieldErrors.convergence}
          />
        </FormField>
        <FormField label={t('ies.config.options')} htmlFor="calc-options" error={fieldErrors.options}>
          <Textarea id="calc-options" value={optionsText} onChange={(e) => setOptionsText(e.target.value)} rows={4} invalid={!!fieldErrors.options} />
        </FormField>
      </Card>
    </div>
  )
}
