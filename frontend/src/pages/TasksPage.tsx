/**
 * 任务中心(TasksPage):任务列表 / 提交新任务 / 任务详情。
 *
 * - 任务列表:类型、状态徽章、业务结局徽章(与技术状态正交展示)、进度条、
 *   创建时间、发起人。
 * - 提交新任务:方案评价(calc)/ 规划优化(optimization)/ 不确定性分析(uncertainty),
 *   展示配置摘要;相同配置任务存在时给出重复提交确认。
 * - 任务详情:技术状态与业务结局、进度、诊断列表(定位 + 修复建议)、取消/重试。
 * - 轮询:5 秒自动刷新(页面隐藏时暂停);浏览器关闭后重开可从 localStorage
 *   恢复上次查看的任务。
 * - 不确定性分析:固定方案可靠性 / 重规划敏感性两种模式互斥(明确提示不可合并),
 *   可配置样本数、随机种子与分布参数。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import { translate, translateDiagnostic, translateError, useI18n } from '../i18n'
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  Dialog,
  EmptyState,
  FormField,
  Icon,
  Input,
  Select,
  SeverityBadge,
  Spinner,
  Table,
  TBody,
  TD,
  TH,
  THead,
  TR,
  TaskOutcomeBadge,
  TaskStatusBadge,
} from '../components/ui'
import { formatDateTime, formatPercent, formatRelativeTime } from '../lib/format'
import type {
  ApiError,
  CalcConfig,
  Dataset,
  Diagnostic,
  Task,
  TaskCreateInput,
  TaskDetail,
  TaskStatus,
  TaskType,
  User,
} from '../types'
import { useWorkbench } from './workbench'

const POLL_MS = 5000

/** 可提交的任务类型(方案评价 / 规划优化 / 不确定性分析)。 */
const SUBMITTABLE_TYPES: TaskType[] = ['calc', 'optimization', 'uncertainty']

/** 进行中的状态(不视为终结)。 */
const ACTIVE_STATUSES: TaskStatus[] = ['queued', 'running', 'cancelling']

type UncertaintyMode = 'fixed_reliability' | 'replan_sensitivity'
type DistType = 'normal' | 'triangular' | 'uniform'

interface CreateForm {
  type: TaskType
  configId: number | null
  datasetIds: number[]
  uMode: UncertaintyMode
  nSamples: number
  seed: string
  distType: DistType
  noisePct: number
}

const DEFAULT_FORM: CreateForm = {
  type: 'calc',
  configId: null,
  datasetIds: [],
  uMode: 'fixed_reliability',
  nSamples: 200,
  seed: '',
  distType: 'normal',
  noisePct: 5,
}

/** 确定性哈希(FNV 变体),用于生成客户端幂等键。 */
function stableHash(input: string): string {
  let h1 = 0x811c9dc5
  let h2 = 0x01000193
  for (let i = 0; i < input.length; i++) {
    const c = input.charCodeAt(i)
    h1 = Math.imul(h1 ^ c, 0x01000193) >>> 0
    h2 = Math.imul(h2 ^ c, 0x85ebca6b) >>> 0
  }
  return (h1 ^ h2).toString(36)
}

/** 序列化任务附加参数(键排序,保证相同输入产生相同签名)。 */
function serializeParams(params: Record<string, unknown> | undefined): string {
  if (!params) return ''
  const sorted: Record<string, unknown> = {}
  for (const key of Object.keys(params).sort()) {
    sorted[key] = params[key]
  }
  return JSON.stringify(sorted)
}

function progressOf(task: Task): number {
  const p = task.summary?.percent
  if (p !== null && p !== undefined && Number.isFinite(p)) return p
  return task.status === 'completed' ? 100 : 0
}

/** 进度条(运行中且无百分比时显示不确定动画)。 */
function ProgressBar({ percent, running }: { percent: number; running: boolean }) {
  const { t } = useI18n()
  const clamped = Math.max(0, Math.min(100, percent))
  return (
    <div
      className="ies-progress"
      role="progressbar"
      aria-label={t('ies.task.percent')}
      aria-valuenow={Math.round(clamped)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div className="ies-progress__track">
        {running && clamped <= 0 ? (
          <div className="ies-progress__bar ies-progress__bar--indeterminate" />
        ) : (
          <div className="ies-progress__bar" style={{ width: `${Math.max(2, clamped)}%` }} />
        )}
      </div>
      <span className="ies-progress__label">{Math.round(clamped)}%</span>
    </div>
  )
}

/** 单条诊断(消息 + 定位 + 修复建议)。 */
function DiagnosticItem({ diag }: { diag: Diagnostic }) {
  const { t } = useI18n()
  const loc = diag.location
  const rowText = loc?.row
    ? Array.isArray(loc.row)
      ? loc.row.join(', ')
      : String(loc.row)
    : null
  return (
    <li className="ies-diag">
      <div className="ies-diag__head">
        <SeverityBadge severity={diag.severity} />
        <span className="ies-diag__code">{diag.code}</span>
        {diag.occurred_at ? (
          <time className="ies-diag__time">{formatDateTime(diag.occurred_at)}</time>
        ) : null}
      </div>
      <p className="ies-diag__msg">{translateDiagnostic(diag)}</p>
      {loc ? (
        <p className="ies-diag__loc">
          {t('ies.task.diagnostic_location')}: {loc.object_type}
          {loc.object_id ? ` #${loc.object_id}` : ''}
          {loc.field ? ` · ${loc.field}` : ''}
          {rowText ? ` · ${t('ies.data.row_count')} ${rowText}` : ''}
        </p>
      ) : null}
      {diag.fix_hint_key ? (
        <p className="ies-diag__fix">
          <Icon name="info" size={12} />
          {t('ies.task.fix_hint')}: {translate(diag.fix_hint_key, diag.params)}
        </p>
      ) : null}
    </li>
  )
}

export default function TasksPage() {
  const { t } = useI18n()
  const { projectId } = useWorkbench()

  // -------------------------------------------------------------------------
  // 数据状态
  // -------------------------------------------------------------------------
  const [tasks, setTasks] = useState<Task[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [me, setMe] = useState<User | null>(null)
  const [config, setConfig] = useState<CalcConfig | null>(null)
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [typeFilter, setTypeFilter] = useState<'all' | TaskType>('all')

  // 选中任务(详情面板),localStorage 持久化以支持重开恢复
  const [selectedId, setSelectedId] = useState<number | null>(() => {
    try {
      const raw = window.localStorage.getItem(`iesplan.tasks.selected.${projectId}`)
      const v = raw ? Number(raw) : NaN
      return Number.isFinite(v) && v > 0 ? v : null
    } catch {
      return null
    }
  })
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)

  // 提交表单
  const [createOpen, setCreateOpen] = useState(false)
  const [form, setForm] = useState<CreateForm>(DEFAULT_FORM)
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [duplicate, setDuplicate] = useState<Task | null>(null)
  const [cancelTarget, setCancelTarget] = useState<Task | null>(null)
  const [retryTarget, setRetryTarget] = useState<Task | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)

  // -------------------------------------------------------------------------
  // 基础数据加载
  // -------------------------------------------------------------------------
  useEffect(() => {
    api.auth
      .me()
      .then(setMe)
      .catch(() => setMe(null))
  }, [])

  useEffect(() => {
    api.config
      .get(projectId)
      .then(setConfig)
      .catch(() => setConfig(null))
  }, [projectId])

  useEffect(() => {
    api.datasets
      .list({ project_id: projectId, limit: 100 })
      .then((page) => setDatasets(page.items.filter((d) => d.status === 'published')))
      .catch(() => setDatasets([]))
  }, [projectId])

  // -------------------------------------------------------------------------
  // 任务列表轮询(5s;页面隐藏时暂停)
  // -------------------------------------------------------------------------
  const loadList = useCallback(async () => {
    try {
      const page = await api.tasks.list({ project_id: projectId, limit: 100 })
      setTasks(page.items)
      setLoadError(null)
    } catch (err) {
      setLoadError(translateError(err as ApiError))
    }
  }, [projectId])

  useEffect(() => {
    void loadList()
    const timer = setInterval(() => {
      if (!document.hidden) void loadList()
    }, POLL_MS)
    const onVisible = () => {
      if (!document.hidden) void loadList()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [loadList])

  // 选中任务持久化(浏览器关闭后重开可恢复视图)
  useEffect(() => {
    try {
      const key = `iesplan.tasks.selected.${projectId}`
      if (selectedId) window.localStorage.setItem(key, String(selectedId))
      else window.localStorage.removeItem(key)
    } catch {
      // 隐私模式忽略
    }
  }, [selectedId, projectId])

  // -------------------------------------------------------------------------
  // 详情轮询(5s)
  // -------------------------------------------------------------------------
  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      setDetailError(null)
      return
    }
    let cancelled = false
    const load = async () => {
      setDetailLoading(true)
      try {
        const d = await api.tasks.get(projectId, selectedId)
        if (!cancelled) {
          setDetail(d)
          setDetailError(null)
        }
      } catch (err) {
        if (!cancelled) setDetailError(translateError(err as ApiError))
      } finally {
        if (!cancelled) setDetailLoading(false)
      }
    }
    void load()
    const timer = setInterval(() => {
      if (!document.hidden) void load()
    }, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [selectedId])

  // -------------------------------------------------------------------------
  // 提交新任务
  // -------------------------------------------------------------------------
  const buildParams = useCallback(
    (f: CreateForm): Record<string, unknown> | undefined => {
      if (f.type !== 'uncertainty') return undefined
      const params: Record<string, unknown> = {
        mode: f.uMode,
        n_samples: f.nSamples,
        distribution: { type: f.distType, noise_pct: f.noisePct },
      }
      const seed = Number(f.seed.trim())
      if (Number.isFinite(seed)) params.seed = seed
      return params
    },
    [],
  )

  const buildInput = useCallback(
    (f: CreateForm): TaskCreateInput => {
      const params = buildParams(f)
      const idemKey = `sub-${f.type}-${f.configId ?? 'none'}-${stableHash(serializeParams(params))}`
      return {
        project_id: projectId,
        type: f.type,
        ...(f.configId !== null ? { config_id: f.configId } : {}),
        ...(f.datasetIds.length > 0 ? { dataset_ids: f.datasetIds } : {}),
        idempotency_key: idemKey,
        ...(params ? { params } : {}),
      }
    },
    [projectId, buildParams],
  )

  const submit = useCallback(
    async (force: boolean) => {
      const input = buildInput(form)
      if (!force && !submitting) {
        // 重复提交提示:同类型 + 同配置(幂等键相同)的非终结任务
        const dup =
          tasks?.find(
            (tk) => tk.idempotency_key === input.idempotency_key && ACTIVE_STATUSES.includes(tk.status),
          ) ?? null
        if (dup) {
          setDuplicate(dup)
          return
        }
      }
      setFormError(null)
      setSubmitting(true)
      try {
        const created = await api.tasks.create(input)
        setCreateOpen(false)
        setSelectedId(created.id)
        setFlash(t('ies.task.create_ok'))
        await loadList()
      } catch (err) {
        setFormError(translateError(err as ApiError))
      } finally {
        setSubmitting(false)
      }
    },
    [buildInput, form, tasks, loadList, submitting, t],
  )

  const confirmDuplicate = useCallback(() => {
    setDuplicate(null)
    void submit(true)
  }, [submit])

  // -------------------------------------------------------------------------
  // 取消 / 重试
  // -------------------------------------------------------------------------
  const doCancel = useCallback(async () => {
    if (!cancelTarget) return
    setActionError(null)
    try {
      await api.tasks.cancel(projectId, cancelTarget.id)
      setCancelTarget(null)
      setFlash(t('ies.task.cancel_ok', { task_id: cancelTarget.id }))
      await loadList()
    } catch (err) {
      setActionError(translateError(err as ApiError))
      setCancelTarget(null)
    }
  }, [cancelTarget, loadList, t])

  const doRetry = useCallback(async () => {
    if (!retryTarget) return
    setActionError(null)
    try {
      await api.tasks.retry(projectId, retryTarget.id)
      setRetryTarget(null)
      setFlash(t('ies.task.retry_ok'))
      await loadList()
    } catch (err) {
      setActionError(translateError(err as ApiError))
      setRetryTarget(null)
    }
  }, [retryTarget, loadList, t])

  // -------------------------------------------------------------------------
  // 派生数据
  // -------------------------------------------------------------------------
  const visibleTasks = useMemo(() => {
    if (!tasks) return null
    return typeFilter === 'all' ? tasks : tasks.filter((tk) => tk.type === typeFilter)
  }, [tasks, typeFilter])

  const cancellable = detail && (detail.status === 'queued' || detail.status === 'running' || detail.status === 'cancelling')
  const retryable = detail && (detail.status === 'failed' || detail.status === 'timed_out' || detail.status === 'cancelled')

  const publishedDatasets = datasets

  // -------------------------------------------------------------------------
  // 渲染
  // -------------------------------------------------------------------------
  return (
    <div className="ies-task-layout">
      {/* 列表 */}
      <section className="ies-section">
        <Card
          title={t('ies.task.task_list')}
          actions={
            <Button icon="plus" onClick={() => setCreateOpen(true)}>
              {t('ies.task.submit')}
            </Button>
          }
          flush
        >
          <div className="ies-flex" style={{ padding: 'var(--ies-space-3) var(--ies-space-4)' }}>
            <Select
              aria-label={t('ies.common.filter')}
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value as 'all' | TaskType)}
              style={{ maxWidth: 220 }}
            >
              <option value="all">{t('ies.task.filter_all')}</option>
              {SUBMITTABLE_TYPES.map((ty) => (
                <option key={ty} value={ty}>
                  {t(`ies.task.type_${ty}`)}
                </option>
              ))}
            </Select>
          </div>
          {loadError ? (
            <div style={{ padding: 'var(--ies-space-4)' }}>
              <Alert variant="error">{loadError}</Alert>
            </div>
          ) : null}
          {visibleTasks === null ? (
            <div style={{ padding: 'var(--ies-space-8)' }}>
              <Spinner size="lg" />
            </div>
          ) : visibleTasks.length === 0 ? (
            <EmptyState
              icon="clock"
              title={t('ies.task.no_tasks')}
              description={t('ies.task.no_tasks_hint')}
            />
          ) : (
            <Table>
              <THead>
                <TR>
                  <TH>{t('ies.common.id')}</TH>
                  <TH>{t('ies.common.type')}</TH>
                  <TH>{t('ies.common.status')}</TH>
                  <TH>{t('ies.task.percent')}</TH>
                  <TH>{t('ies.common.created_at')}</TH>
                  <TH>{t('ies.task.requester')}</TH>
                </TR>
              </THead>
              <TBody>
                {visibleTasks.map((tk) => (
                  <TR
                    key={tk.id}
                    clickable
                    className={selectedId === tk.id ? 'ies-table__row--active' : undefined}
                    onClick={() => setSelectedId(tk.id)}
                  >
                    <TD>#{tk.id}</TD>
                    <TD>{t(`ies.task.type_${tk.type}`)}</TD>
                    <TD>
                      <div className="ies-flex" style={{ flexWrap: 'wrap', gap: 'var(--ies-space-2)' }}>
                        <TaskStatusBadge status={tk.status} />
                        <TaskOutcomeBadge outcome={tk.business_outcome} />
                      </div>
                    </TD>
                    <TD>
                      <ProgressBar
                        percent={progressOf(tk)}
                        running={tk.status === 'running' || tk.status === 'queued'}
                      />
                    </TD>
                    <TD title={formatDateTime(tk.requested_at)}>
                      {formatRelativeTime(tk.requested_at)}
                    </TD>
                    <TD>{tk.requested_by === me?.id ? t('ies.task.me') : `#${tk.requested_by}`}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          )}
        </Card>
      </section>

      {/* 详情 */}
      <section className="ies-task-detail">
        <Card
          title={t('ies.task.detail')}
          actions={detailLoading ? <Spinner size="sm" /> : null}
        >
          {!selectedId ? (
            <EmptyState icon="info" title={t('ies.task.select_task_hint')} />
          ) : detailError ? (
            <Alert variant="error" title={t('ies.common.load_failed', { reason: '' })}>
              {detailError}
            </Alert>
          ) : !detail ? (
            <div style={{ padding: 'var(--ies-space-8)' }}>
              <Spinner size="lg" />
            </div>
          ) : (
            <>
              <div className="ies-flex" style={{ flexWrap: 'wrap', marginBottom: 'var(--ies-space-3)' }}>
                <TaskStatusBadge status={detail.status} />
                <TaskOutcomeBadge outcome={detail.business_outcome} />
                <span className="ies-badge ies-badge--neutral ies-badge--shape-circle">
                  <span className="ies-badge__label">#{detail.id}</span>
                </span>
              </div>

              <dl className="ies-kv">
                <dt>{t('ies.common.type')}</dt>
                <dd>{t(`ies.task.type_${detail.type}`)}</dd>
                <dt>{t('ies.task.requester')}</dt>
                <dd>{detail.requested_by === me?.id ? t('ies.task.me') : `#${detail.requested_by}`}</dd>
                <dt>{t('ies.common.created_at')}</dt>
                <dd>{formatDateTime(detail.requested_at)}</dd>
                <dt>{t('ies.task.attempt')}</dt>
                <dd>
                  {detail.attempt_count}/{detail.max_attempts}
                </dd>
                {detail.progress?.stage ? (
                  <>
                    <dt>{t('ies.task.stage')}</dt>
                    <dd>{detail.progress.stage}</dd>
                  </>
                ) : null}
                {detail.calc_snapshot ? (
                  <>
                    <dt>{t('ies.task.snapshot')}</dt>
                    <dd>
                      #{detail.calc_snapshot.id}
                      {detail.calc_snapshot.random_seed !== null &&
                      detail.calc_snapshot.random_seed !== undefined
                        ? ` · seed ${detail.calc_snapshot.random_seed}`
                        : ''}
                    </dd>
                  </>
                ) : null}
              </dl>

              <div style={{ margin: 'var(--ies-space-3) 0' }}>
                <ProgressBar
                  percent={progressOf(detail)}
                  running={detail.status === 'running' || detail.status === 'queued'}
                />
              </div>

              {detail.outcome_note ? (
                <Alert variant="info" title={t('ies.task.outcome_note')}>
                  {detail.outcome_note}
                </Alert>
              ) : null}

              {detail.attempts.length > 1 ? (
                <details style={{ margin: 'var(--ies-space-3) 0' }}>
                  <summary className="ies-summary">{t('ies.task.attempts')}</summary>
                  <Table>
                    <THead>
                      <TR>
                        <TH>{t('ies.task.attempt')}</TH>
                        <TH>{t('ies.common.status')}</TH>
                        <TH>{t('ies.common.updated_at')}</TH>
                      </TR>
                    </THead>
                    <TBody>
                      {detail.attempts.map((a) => (
                        <TR key={a.attempt_no}>
                          <TD>{a.attempt_no}</TD>
                          <TD>
                            <Badge variant="neutral" label={a.status} />
                          </TD>
                          <TD>{formatDateTime(a.finished_at ?? a.started_at)}</TD>
                        </TR>
                      ))}
                    </TBody>
                  </Table>
                </details>
              ) : null}

              <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, margin: 'var(--ies-space-4) 0 var(--ies-space-2)' }}>
                {t('ies.task.diagnostics')}
              </h3>
              {detail.diagnostics.length > 0 ? (
                <ul className="ies-diag-list">
                  {detail.diagnostics.map((d, i) => (
                    <DiagnosticItem key={`${d.code}-${i}`} diag={d} />
                  ))}
                </ul>
              ) : (
                <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
                  {t('ies.task.no_diagnostics')}
                </p>
              )}

              {detail.evidence.length > 0 ? (
                <div className="ies-flex" style={{ marginTop: 'var(--ies-space-3)' }}>
                  <Link
                    className="ies-btn ies-btn--secondary ies-btn--md"
                    to={`/projects/${projectId}/results?package=${detail.evidence[0].package_id}`}
                  >
                    {t('ies.task.view_results')}
                  </Link>
                </div>
              ) : null}

              {actionError ? (
                <div style={{ marginTop: 'var(--ies-space-3)' }}>
                  <Alert variant="error">{actionError}</Alert>
                </div>
              ) : null}

              <div className="ies-flex" style={{ marginTop: 'var(--ies-space-4)' }}>
                {cancellable ? (
                  <Button variant="danger" onClick={() => setCancelTarget(detail)}>
                    <Icon name="stop" size={14} />
                    {t('ies.task.cancel')}
                  </Button>
                ) : null}
                {retryable ? (
                  <Button variant="secondary" onClick={() => setRetryTarget(detail)}>
                    {t('ies.task.retry')}
                  </Button>
                ) : null}
              </div>
            </>
          )}
        </Card>
      </section>

      {/* 提交任务对话框 */}
      <Dialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title={t('ies.task.submit')}
        size="lg"
        footer={
          <>
            <Button variant="secondary" onClick={() => setCreateOpen(false)}>
              {t('ies.common.cancel')}
            </Button>
            <Button
              loading={submitting}
              disabled={form.configId === null}
              onClick={() => void submit(false)}
            >
              {t('ies.task.submit')}
            </Button>
          </>
        }
      >
        <div className="ies-section">
          <Alert variant="info">{t('ies.task.submit_hint')}</Alert>
        </div>

        <FormField label={t('ies.common.type')} htmlFor="task-type">
          <Select
            id="task-type"
            value={form.type}
            onChange={(e) => setForm({ ...form, type: e.target.value as TaskType })}
          >
            {SUBMITTABLE_TYPES.map((ty) => (
              <option key={ty} value={ty}>
                {t(`ies.task.type_${ty}`)} — {t(`ies.task.type_${ty}_desc`)}
              </option>
            ))}
          </Select>
        </FormField>

        {config ? (
          <Card title={t('ies.task.config_summary')} className="ies-section">
            <dl className="ies-kv">
              <dt>{t('ies.config.name')}</dt>
              <dd>{config.name}</dd>
              <dt>{t('ies.config.algorithm')}</dt>
              <dd>{t(`ies.config.algorithm_${config.algorithm}`)}</dd>
              <dt>{t('ies.config.objectives')}</dt>
              <dd>{config.objectives.length}</dd>
              <dt>{t('ies.config.min_irr')}</dt>
              <dd>{config.min_irr === null ? '—' : formatPercent(config.min_irr)}</dd>
              <dt>{t('ies.common.status')}</dt>
              <dd>
                <Badge
                  variant={config.status === 'frozen' ? 'success' : 'warning'}
                  icon={config.status === 'frozen' ? 'check' : 'clock'}
                  label={t(`ies.config.status_${config.status}`)}
                />
              </dd>
            </dl>
            {config.status !== 'frozen' ? (
              <div style={{ marginTop: 'var(--ies-space-2)' }}>
                <Alert variant="warning">{t('ies.task.config_not_frozen')}</Alert>
              </div>
            ) : null}
          </Card>
        ) : (
          <Alert variant="error" title={t('ies.common.load_failed', { reason: '' })}>
            {t('ies.task.submit_hint')}
          </Alert>
        )}

        {publishedDatasets.length > 0 ? (
          <fieldset className="ies-fieldset">
            <legend>{t('ies.task.datasets')}</legend>
            {publishedDatasets.map((ds) => (
              <Checkbox
                key={ds.id}
                label={ds.name}
                checked={form.datasetIds.includes(ds.id)}
                onChange={() =>
                  setForm({
                    ...form,
                    datasetIds: form.datasetIds.includes(ds.id)
                      ? form.datasetIds.filter((id) => id !== ds.id)
                      : [...form.datasetIds, ds.id],
                  })
                }
              />
            ))}
          </fieldset>
        ) : (
          <p className="ies-form-message" style={{ marginBottom: 'var(--ies-space-4)' }}>
            {t('ies.task.no_published_datasets')}
          </p>
        )}

        {form.type === 'uncertainty' ? (
          <div className="ies-section">
            <Alert variant="warning">{t('ies.task.mode_exclusive')}</Alert>
            <div className="ies-radio-cards">
              {(
                [
                  ['fixed_reliability', 'ies.task.mode_fixed_reliability', 'ies.task.mode_fixed_reliability_desc'],
                  ['replan_sensitivity', 'ies.task.mode_replan_sensitivity', 'ies.task.mode_replan_sensitivity_desc'],
                ] as const
              ).map(([mode, titleKey, descKey]) => (
                <label
                  key={mode}
                  className={`ies-radio-card ${
                    form.uMode === mode ? 'ies-radio-card--checked' : ''
                  }`}
                >
                  <input
                    type="radio"
                    name="u-mode"
                    value={mode}
                    checked={form.uMode === mode}
                    onChange={() => setForm({ ...form, uMode: mode })}
                  />
                  <span className="ies-radio-card__title">{t(titleKey)}</span>
                  <span className="ies-radio-card__desc">{t(descKey)}</span>
                </label>
              ))}
            </div>
            <div className="ies-grid ies-grid--cols-2">
              <FormField label={t('ies.task.n_samples')} htmlFor="u-samples">
                <Input
                  id="u-samples"
                  type="number"
                  min={1}
                  max={10000}
                  value={form.nSamples}
                  onChange={(e) =>
                    setForm({ ...form, nSamples: Math.max(1, Number(e.target.value) || 1) })
                  }
                />
              </FormField>
              <FormField label={t('ies.task.seed')} htmlFor="u-seed">
                <Input
                  id="u-seed"
                  type="number"
                  placeholder={t('ies.common.optional')}
                  value={form.seed}
                  onChange={(e) => setForm({ ...form, seed: e.target.value })}
                />
              </FormField>
              <FormField label={t('ies.task.distribution')} htmlFor="u-dist">
                <Select
                  id="u-dist"
                  value={form.distType}
                  onChange={(e) => setForm({ ...form, distType: e.target.value as DistType })}
                >
                  <option value="normal">{t('ies.task.distribution_normal')}</option>
                  <option value="triangular">{t('ies.task.distribution_triangular')}</option>
                  <option value="uniform">{t('ies.task.distribution_uniform')}</option>
                </Select>
              </FormField>
              <FormField label={t('ies.task.noise_pct')} htmlFor="u-noise">
                <Input
                  id="u-noise"
                  type="number"
                  min={0}
                  max={100}
                  value={form.noisePct}
                  onChange={(e) =>
                    setForm({ ...form, noisePct: Math.max(0, Number(e.target.value) || 0) })
                  }
                />
              </FormField>
            </div>
          </div>
        ) : null}

        {formError ? (
          <Alert variant="error" title={t('ies.task.create_failed_short', { reason: '' })}>
            {formError}
          </Alert>
        ) : null}
      </Dialog>

      {/* 重复提交确认 */}
      <Dialog
        open={duplicate !== null}
        onClose={() => setDuplicate(null)}
        title={t('ies.task.duplicate_warning')}
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setDuplicate(null)}>
              {t('ies.common.cancel')}
            </Button>
            <Button onClick={confirmDuplicate}>{t('ies.task.still_submit')}</Button>
          </>
        }
      >
        {duplicate ? (
          <Alert variant="warning">
            {t('ies.task.duplicate_desc', {
              id: duplicate.id,
              status: t(`ies.task.status_${duplicate.status}`),
            })}
          </Alert>
        ) : null}
      </Dialog>

      {/* 取消确认 */}
      <Dialog
        open={cancelTarget !== null}
        onClose={() => setCancelTarget(null)}
        title={t('ies.task.cancel')}
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setCancelTarget(null)}>
              {t('ies.common.cancel')}
            </Button>
            <Button variant="danger" onClick={() => void doCancel()}>
              {t('ies.common.confirm')}
            </Button>
          </>
        }
      >
        {cancelTarget ? (
          <p style={{ fontSize: 'var(--ies-fs-sm)' }}>
            {t('ies.task.cancel_confirm', { task_id: cancelTarget.id })}
          </p>
        ) : null}
      </Dialog>

      {/* 重试确认 */}
      <Dialog
        open={retryTarget !== null}
        onClose={() => setRetryTarget(null)}
        title={t('ies.task.retry')}
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setRetryTarget(null)}>
              {t('ies.common.cancel')}
            </Button>
            <Button onClick={() => void doRetry()}>{t('ies.common.confirm')}</Button>
          </>
        }
      >
        {retryTarget ? (
          <p style={{ fontSize: 'var(--ies-fs-sm)' }}>
            {t('ies.task.retry_confirm', { id: retryTarget.id })}
          </p>
        ) : null}
      </Dialog>

      {/* 操作结果提示 */}
      {flash ? (
        <div
          role="status"
          style={{
            position: 'fixed',
            right: 'var(--ies-space-5)',
            bottom: 'var(--ies-space-5)',
            zIndex: 90,
          }}
        >
          <Alert variant="success" closable onClose={() => setFlash(null)}>
            {flash}
          </Alert>
        </div>
      ) : null}
    </div>
  )
}
