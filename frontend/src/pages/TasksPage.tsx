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

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { listDatasets, listVersions as listDatasetVersions } from '../features/datasets'
import {
  DEFAULT_FORM,
  SUBMITTABLE_TYPES,
  buildInput,
  cancelTask,
  createTask,
  describeFrozenConfig,
  describeFrozenEvaluation,
  describeFrozenFinance,
  formFromWorking,
  getTaskDetail,
  getTaskInputs,
  hasSavedConfig,
  listTasks,
  logicalSigOf,
  newSubmitNonce,
  parseEvaluation,
  retryTask,
  saveWorkingInputs,
  serializeParams,
  workingOfForm,
} from '../features/tasks'
import { getCalcConfig } from '../features/config'
import { runValidation } from '../features/validation'
import type { CreateForm, DistType } from '../features/tasks'
import { getMe } from '../features/auth'
import { translate, translateDiagnostic, translateError, useI18n } from '../i18n'
import {
  Alert,
  Badge,
  Button,
  Card,
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
  Textarea,
} from '../components/ui'
import { formatDateTime, formatRelativeTime } from '../lib/format'
import type {
  ApiError,
  CalcConfig,
  Dataset,
  Diagnostic,
  ProjectVersion,
  Task,
  TaskDetail,
  TaskInputs,
  TaskStatus,
  TaskType,
  User,
} from '../types'
import { useAutosave, useWorkbench } from './workbench'

const POLL_MS = 5000

/** 进行中的状态(不视为终结)。 */
const ACTIVE_STATUSES: TaskStatus[] = ['queued', 'running', 'cancelling']

// 表单状态类型与工作输入/提交装配纯映射归 features/tasks(form.ts) 所有,
// 本页只持有表单实例与时序协调(纯映射见 features/tasks/form.ts 与 mappers.ts)。

/** 冻结输入只读视图(快照投影; 版本/数据集名称按冻结 id 解析, 缺失显精确引用)。 */
function FrozenInputsView({
  inputs,
  versions,
  datasetNames,
  labels,
}: {
  inputs: TaskInputs
  versions: ProjectVersion[]
  datasetNames: string[]
  labels: {
    snapshot: string
    version: string
    datasets: string
    evaluation: string
    config: string
    planning: string
    finance: string
    models: string
  }
}) {
  const snap = inputs.snapshot
  if (!snap) return null
  const versionHit = versions.find((v) => v.id === snap.project_version_id)
  const modelIds = Object.keys(snap.device_models)
  return (
    <dl className="ies-kv">
      <dt>{labels.snapshot}</dt>
      <dd>
        #{snap.id}
        {snap.random_seed !== null ? ` · seed ${snap.random_seed}` : ''}
        {` · schema ${snap.schema_version}`}
      </dd>
      <dt>{labels.version}</dt>
      <dd>{versionHit ? `v${versionHit.version_no}` : `id:${snap.project_version_id}`}</dd>
      <dt>{labels.datasets}</dt>
      <dd>{datasetNames.length > 0 ? datasetNames.join('、') : '—'}</dd>
      <dt>{labels.evaluation}</dt>
      <dd>{describeFrozenEvaluation(snap.evaluation)}</dd>
      <dt>{labels.config}</dt>
      <dd>{describeFrozenConfig(snap.calc_config_snapshot)}</dd>
      <dt>{labels.planning}</dt>
      <dd>{inputs.frozen_refs.planning_revision !== null ? `revision v${inputs.frozen_refs.planning_revision}` : '—'}</dd>
      <dt>{labels.finance}</dt>
      <dd>{describeFrozenFinance(inputs.frozen_refs.finance_profile_id, inputs.frozen_refs.finance_revision)}</dd>
      <dt>{labels.models}</dt>
      <dd>{modelIds.length > 0 ? `${modelIds.length} · ${modelIds.join('、')}` : '—'}</dd>
    </dl>
  )
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
  // 全局任务选择(结果/导出页同选择; 仅记录选择, 打开表单/切换选择不创建任务)
  const {
    projectId,
    project,
    selectedTaskId: selectedId,
    setSelectedTaskId: setSelectedId,
    selectTaskWithDraft,
    draftLoadingTaskId,
    versions,
    refreshDetailed,
    workspaceCoord,
  } = useWorkbench()
  const { setStatus: setAutosaveStatus } = useAutosave()

  // -------------------------------------------------------------------------
  // 数据状态
  // -------------------------------------------------------------------------
  const [tasks, setTasks] = useState<Task[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [me, setMe] = useState<User | null>(null)
  const [config, setConfig] = useState<CalcConfig | null>(null)
  // 数据集目录仅服务冻结输入视图的名称解析(只读); 提交对话框不再做数据集
  // 选择(清单权威是草稿 dataset_bindings), 故无目录就绪/挂起/补解状态。
  const [datasets, setDatasets] = useState<Dataset[]>([])
  const [typeFilter, setTypeFilter] = useState<'all' | TaskType>('all')

  // 选中任务来自全局工作台上下文(持久化由 Provider 持有, 此处不另存)
  const [detail, setDetail] = useState<TaskDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  // 冻结输入视图(所选任务快照的只读投影, 非当前草稿; 失败可见, 不回退草稿)
  const [frozenInputs, setFrozenInputs] = useState<TaskInputs | null>(null)
  const [frozenLoading, setFrozenLoading] = useState(false)
  const [frozenError, setFrozenError] = useState<string | null>(null)
  const [frozenDatasetNames, setFrozenDatasetNames] = useState<string[]>([])

  // 工作输入暂存(表单是草稿 task_working_inputs 的可编辑镜像):
  // 脏标记 + 防抖保存; 保存互斥与切换序列归工作台共享协调(与选择载入
  // 同序列, 同页/跨页有序, 旧值不得在载入后落地)。
  const [form, setForm] = useState<CreateForm>(DEFAULT_FORM)
  const formRef = useRef<CreateForm>(form)
  const dirtyRef = useRef(false)
  const saveTimer = useRef<number | null>(null)
  const saveSeqRef = useRef(0)
  /** 切换待落定(任务 id + 载入返回的新草稿修订; 修订到达前不读旧草稿)。 */
  const pendingSwitchRef = useRef<{ task: number; rev: number } | null>(null)
  /** 上次与草稿一致的工作输入签名(防抖保存成功/水合时更新)。 */
  const lastSyncedRef = useRef<string | null>(null)
  /** 上次已应用到表单的草稿工作输入签名(避免重复水合)。 */
  const lastAppliedRef = useRef<string | null>(null)
  /** 工作输入是否已就绪(挂载水合/切换落定已应用，或用户已编辑)。
   * 未就绪时表单仍是默认空态，提交必须禁用，避免在草稿读到前把空值
   * 当成用户输入冻结；切换载入等待中同样禁用，避免冻结旧任务的值。 */
  const [workingReady, setWorkingReady] = useState(false)

  // 提交表单
  const [createOpen, setCreateOpen] = useState(false)
  /** 本次对话框对应的逻辑提交标识: 打开对话框即换新键; 同一提交的网络
   * 重试/确认复用原键(状态不变), 关闭重开即视为新的逻辑提交。 */
  const [submitNonce, setSubmitNonce] = useState(() => newSubmitNonce())
  const [formError, setFormError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  /** 校验门禁结果:提交前 validation.run 的阻断诊断(空数组 = 通过; null = 未检查/校验不可用)。 */
  const [blockers, setBlockers] = useState<Diagnostic[] | null>(null)
  const [checking, setChecking] = useState(false)
  const [duplicate, setDuplicate] = useState<Task | null>(null)
  const [cancelTarget, setCancelTarget] = useState<Task | null>(null)
  const [retryTarget, setRetryTarget] = useState<Task | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)

  // 派生:是否存在已保存配置(仅存在性, 不消费 revision 数值)
  const hasSavedConfigValue = useMemo(() => hasSavedConfig(config), [config])
  // 派生:当前已保存 revision(提交时如实引用并冻结进快照; 未保存为 null)
  const savedRevisionValue = useMemo(
    () => (config && typeof config.revision === 'number' && config.revision > 0 ? config.revision : null),
    [config],
  )

  // 计算配置摘要直接展示精确引用(无映射、无猜测)。
  const calcSummary = useMemo(() => {
    const body = config?.calculation_config
    if (!body) return null
    return {
      mode: body.mode,
      generator: body.generator,
      solver: body.solver,
      precision: body.precision,
    }
  }, [config])

  // -------------------------------------------------------------------------
  // 基础数据加载
  // -------------------------------------------------------------------------
  useEffect(() => {
    getMe()
      .then(setMe)
      .catch(() => setMe(null))
  }, [])

  useEffect(() => {
    getCalcConfig(projectId)
      .then(setConfig)
      .catch(() => setConfig(null))
  }, [projectId])

  useEffect(() => {
    listDatasets({ project_id: projectId, limit: 100 })
      // 后端数据集创建即 draft 状态(无发布流程); 只要未废弃即列入名称解析
      .then((page) => {
        setDatasets(page.items.filter((d) => d.status !== 'deprecated'))
      })
      .catch(() => {
        setDatasets([])
      })
  }, [projectId])

  // -------------------------------------------------------------------------
  // 任务列表轮询(5s;页面隐藏时暂停)
  // -------------------------------------------------------------------------
  const loadList = useCallback(async () => {
    try {
      const page = await listTasks({ project_id: projectId, limit: 100 })
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
        const d = await getTaskDetail(projectId, selectedId)
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
  // 冻结输入视图(所选任务快照只读投影; 名称按冻结 id 解析, 缺失显精确引用)
  // -------------------------------------------------------------------------
  useEffect(() => {
    if (!selectedId) {
      setFrozenInputs(null)
      setFrozenError(null)
      setFrozenDatasetNames([])
      return
    }
    let cancelled = false
    const load = async () => {
      setFrozenLoading(true)
      setFrozenError(null)
      try {
        const inputs = await getTaskInputs(projectId, selectedId)
        if (cancelled) return
        setFrozenInputs(inputs)
        // 数据集名称按冻结版本 id 匹配现有数据集版本(只读查询, 不建任务);
        // 未匹配显精确引用 id, 不读当前绑定猜测。
        const vids = inputs.snapshot ? inputs.snapshot.dataset_version_ids : []
        const names: string[] = []
        const matched = new Set<number>()
        if (vids.length > 0) {
          for (const ds of datasets) {
            try {
              const vs = await listDatasetVersions(projectId, ds.id)
              if (cancelled) return
              for (const v of vs) {
                if (vids.includes(v.id) && !matched.has(v.id)) {
                  matched.add(v.id)
                  names.push(`${ds.name} v${v.version_no}`)
                }
              }
            } catch {
              // 单个数据集版本查询失败不遮蔽冻结引用(下方按 id 兜底展示)
            }
          }
          for (const vid of vids) {
            // 名称未解析: 精确引用 id(版本 id 全局唯一, 直接展示)
            if (!matched.has(vid)) names.push(`id:${vid}`)
          }
        }
        if (!cancelled) setFrozenDatasetNames(names)
      } catch (err) {
        if (!cancelled) {
          setFrozenInputs(null)
          setFrozenDatasetNames([])
          setFrozenError(translateError(err as ApiError))
        }
      } finally {
        if (!cancelled) setFrozenLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
    // datasets 就绪后重解名称(只读); 输入本身随选择变化重读。
  }, [selectedId, projectId, datasets])

  // -------------------------------------------------------------------------
  // 提交新任务(装配纯函数见 features/tasks/form.ts, 本页只做时序与提交动作)
  // -------------------------------------------------------------------------

  const submit = useCallback(
    async (force: boolean) => {
      // 提交即冻结当时工作区: 先把对话框工作输入落到草稿(失败则中止并可见,
      // 不冻结与草稿不一致的值), 再按表单值提交(与刚落定的草稿一致)。
      // Capture and validate the submitted form before any asynchronous draft
      // persistence or validation request. Otherwise an invalid click can build
      // an input *without* evaluation, then later edits make the second check
      // pass while the stale input is still submitted (TASK-REQ-015).
      const submittedForm = { ...formRef.current }
      if (parseEvaluation(submittedForm) === null) {
        setFormError(t('ies.task.evaluation_invalid'))
        return
      }
      if (saveTimer.current !== null) {
        window.clearTimeout(saveTimer.current)
        saveTimer.current = null
      }
      if (dirtyRef.current) {
        const flushed = await persistWorking(submittedForm)
        if (!flushed) {
          setFormError(t('ies.task.working_save_failed'))
          return
        }
      }
      const input = buildInput(submittedForm, projectId, savedRevisionValue, submitNonce)
      // 校验门禁:存在阻断问题禁止提交(与校验页一致; 否则任务提交后约 1 秒
      // 会被 worker 以数据/配置缺失拒绝, 造成无效提交)
      setChecking(true)
      setBlockers(null)
      try {
        const res = await runValidation(projectId)
        const blocking = res.diagnostics.filter((d) => d.blocking || d.severity === 'blocking')
        setBlockers(blocking)
        if (blocking.length > 0) {
          setFormError(null)
          return
        }
      } catch {
        // 校验接口暂不可用:不拦截提交(后端受理时仍会兜底校验)
      } finally {
        setChecking(false)
      }
      if (!force && !submitting) {
        // 重复提交提示: 逻辑签名相同的非终结任务(同类型 + 同 revision +
        // 同参数指纹, 兼容旧键形态)。这只是 UX 提示, 不得误当幂等身份:
        // 真正的幂等身份是完整幂等键(含一次性随机段), 由后端裁决; 用户
        // 确认后仍以本次新键提交产生新任务。
        const sig = logicalSigOf(submittedForm, savedRevisionValue)
        const dup =
          tasks?.find((tk) => {
            // 幂等键可空: 历史 null 行永不命中签名(按缺席处理, 不伪造空串比较)。
            const key = tk.idempotency_key ?? ''
            return ACTIVE_STATUSES.includes(tk.status) && (key === sig || key.startsWith(`${sig}-`))
          }) ?? null
        if (dup) {
          setDuplicate(dup)
          return
        }
      }
      setFormError(null)
      setSubmitting(true)
      try {
        const created = await createTask(input)
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
    [tasks, loadList, projectId, submitting, t, savedRevisionValue, submitNonce],
  )

  const confirmDuplicate = useCallback(() => {
    setDuplicate(null)
    void submit(true)
  }, [submit])

  // 提交对话框打开 = 一次新的逻辑提交: 先换新随机段(幂等键换新),
  // 再预检一次校验门禁, 让用户提前看到阻断问题(不再等到点提交才发现)
  useEffect(() => {
    if (!createOpen) return
    setSubmitNonce(newSubmitNonce())
    let cancelled = false
    setBlockers(null)
    setChecking(true)
    runValidation(projectId)
      .then((res) => {
        if (cancelled) return
        setBlockers(
          res.valid ? [] : res.diagnostics.filter((d) => d.blocking || d.severity === 'blocking'),
        )
      })
      .catch(() => {
        if (!cancelled) setBlockers(null) // 校验不可用:提交时再兜底
      })
      .finally(() => {
        if (!cancelled) setChecking(false)
      })
    return () => {
      cancelled = true
    }
  }, [createOpen, projectId])

  // -------------------------------------------------------------------------
  // 工作输入暂存与水合: 表单是草稿 task_working_inputs 的可编辑镜像
  // -------------------------------------------------------------------------
  // - 编辑经 updateForm: 本地即时 + 脏标记 + 防抖落草稿(含未完成编辑态);
  // - 挂载/重进/外部修订: 脏为 false 且草稿签名变化时整体替换(不覆盖编辑);
  // - 任务切换: 服务端 load-draft 已整体替换工作输入, 此处待新修订到达后
  //   整体替换(切换序列使挂起保存作废, 不覆盖新选择);
  // - 切换/编辑/保存均不创建任务; 提交前先落定脏输入再冻结。
  /** 当前表单工作输入签名(与已落定值比较, 决定落定后脏态去留)。 */
  const keyOfCurrentForm = useCallback((): string => {
    return serializeParams(workingOfForm(formRef.current))
  }, [])
  /**
   * 按当前表单是否仍等于已同步值同步脏态/指示: 相等即已同步(清脏);
   * 不等即排队/落定期间产生了新编辑, 保留其脏态与待落定。
   */
  const syncDirtyStatus = useCallback(
    (syncedKey: string): void => {
      const stillSynced = keyOfCurrentForm() === syncedKey
      dirtyRef.current = !stillSynced
      setAutosaveStatus(stillSynced ? 'saved' : 'dirty')
    },
    [keyOfCurrentForm, setAutosaveStatus],
  )
  const persistWorking = useCallback(
    async (f: CreateForm, expectedRevision?: number): Promise<boolean> => {
      // 同页保存串行: 等待在途保存落定后再执行, 本次 GET 即读到其推进后
      // 的修订, 不与之自竞 409。锁自身永不拒绝, 无需捕获等待段。
      // 互斥与切换序列归工作台共享: 先记序列再排队, 执行前复核——已被
      // 切换超越的保存(旧任务表单值)在写前丢弃, 不得落地覆盖新载入。
      const seq = workspaceCoord.switchSeq.current
      const prev = workspaceCoord.saveMutex.current
      let release!: () => void
      workspaceCoord.saveMutex.current = new Promise<void>((res) => {
        release = res
      })
      await prev
      try {
        // 写前复核在 try 内: 丢弃路径亦经 finally 释放互斥槽, 不得泄漏
        // 槽位卡住其后排队者与选择载入的等待。
        if (workspaceCoord.switchSeq.current !== seq) return false
        const mySave = saveSeqRef.current + 1
        saveSeqRef.current = mySave
        setAutosaveStatus('saving')
        try {
          const working = workingOfForm(f)
          // 排队期间已有完全相同值落定(防抖/关闭/卸载任一先落地):
          // 去重不写, 脏清零, 视为已同步。值比较而非脏标记——往返编辑
          // 回到已同步值时脏仍为 true, 但服务端已有该值, 不必再写;
          // 值不同必写, 不丢编辑。互斥保证执行时此前保存均已落定,
          // lastSynced 新鲜, 跳过不可能掩盖未落定的值。
          if (serializeParams(working) === lastSyncedRef.current) {
            // 去重不写: 脏态按当前表单是否仍等于实际已同步值决定——传已
            // 知落定的 lastSynced(与所带 working 相等), 不得传当前表单
            // (恒等于自身, 会清掉排队期间新编辑 f2 的脏态)。
            syncDirtyStatus(lastSyncedRef.current)
            return true
          }
          if (expectedRevision === undefined) {
            await saveWorkingInputs(projectId, working)
          } else {
            await saveWorkingInputs(projectId, working, expectedRevision)
          }
          // 切换后落定的旧保存作废, 不碰新选择的状态; 但落定的值确已
          // 写入服务端, lastSynced 按实际落定值更新(后续去重依据)。
          const key = serializeParams(working)
          lastSyncedRef.current = key
          if (workspaceCoord.switchSeq.current !== seq || saveSeqRef.current !== mySave) return false
          lastAppliedRef.current = key
          // 脏态按当前表单是否仍等于本次落定值决定: 落定途中用户又编辑
          // (f2) 时保留其脏态与待落定, 不得用旧值(f1)的落定清掉新编辑。
          syncDirtyStatus(key)
          return true
        } catch {
          if (workspaceCoord.switchSeq.current === seq && saveSeqRef.current === mySave) {
            setAutosaveStatus('error')
          }
          return false
        }
      } finally {
        release()
      }
    },
    [projectId, setAutosaveStatus, workspaceCoord, keyOfCurrentForm, syncDirtyStatus],
  )
  const persistRef = useRef(persistWorking)
  persistRef.current = persistWorking

  const schedulePersist = useCallback(() => {
    if (saveTimer.current !== null) window.clearTimeout(saveTimer.current)
    const seq = workspaceCoord.switchSeq.current
    saveTimer.current = window.setTimeout(() => {
      saveTimer.current = null
      if (workspaceCoord.switchSeq.current !== seq) return
      void persistRef.current(formRef.current)
    }, 700)
  }, [workspaceCoord])

  /** 表单唯一写入口(本地即时 + 脏标记 + 防抖落草稿)。 */
  const updateForm = useCallback(
    (next: CreateForm) => {
      formRef.current = next
      setForm(next)
      dirtyRef.current = true
      setAutosaveStatus('dirty')
      schedulePersist()
    },
    [schedulePersist, setAutosaveStatus],
  )

  /** 脏输入落定(提交/关闭对话框前调用; 无脏即 true)。 */
  const flushWorking = useCallback(async (): Promise<boolean> => {
    if (saveTimer.current !== null) {
      window.clearTimeout(saveTimer.current)
      saveTimer.current = null
    }
    if (!dirtyRef.current) return true
    return persistRef.current(formRef.current)
  }, [])

  const draftWorking = project?.draft?.task_working_inputs ?? null
  const draftRevision = project?.draft?.revision ?? 0
  useEffect(() => {
    const pending = pendingSwitchRef.current
    if (pending !== null) {
      if (selectedId !== pending.task) {
        pendingSwitchRef.current = null
      } else if (draftRevision >= pending.rev) {
        // 切换落定: 整体替换为新选择的工作输入(挂起保存作废, 脏标记清零)。
        pendingSwitchRef.current = null
        if (saveTimer.current !== null) {
          window.clearTimeout(saveTimer.current)
          saveTimer.current = null
        }
        dirtyRef.current = false
        if (draftWorking !== null && typeof draftWorking === 'object' && !Array.isArray(draftWorking)) {
          const working = draftWorking as Record<string, unknown>
          const key = serializeParams(working)
          lastSyncedRef.current = key
          lastAppliedRef.current = key
          const next = formFromWorking(working)
          formRef.current = next
          setForm(next)
        }
        setWorkingReady(true)
      } else {
        // 切换载入等待中: 表单仍是旧任务的值，不可提交。
        setWorkingReady(false)
      }
      return
    }
    // 项目未载入时表单仍是默认空态，不可提交（等下分支水合后再开）。
    if (project === null) {
      setWorkingReady(false)
      return
    }
    // 用户已编辑：表单即用户值（水合会跳过脏表单），可以提交。
    if (dirtyRef.current) {
      setWorkingReady(true)
      return
    }
    const key = serializeParams(
      draftWorking !== null && typeof draftWorking === 'object' && !Array.isArray(draftWorking)
        ? (draftWorking as Record<string, unknown>)
        : {},
    )
    if (key === lastAppliedRef.current) {
      setWorkingReady(true)
      return
    }
    // 挂载/重进/外部修订: 非脏时整体替换(缺席即默认空态, 不沿用他任务值)。
    lastAppliedRef.current = key
    if (draftWorking !== null && typeof draftWorking === 'object' && !Array.isArray(draftWorking)) {
      lastSyncedRef.current = key
      const next = formFromWorking(draftWorking as Record<string, unknown>)
      formRef.current = next
      setForm(next)
    }
    setWorkingReady(true)
  }, [project, selectedId, draftRevision, draftWorking])

  // 挂载即重读项目视图(工作台 Provider 常驻, 子页重进不自动重读; 重读后
  // 上方水合 effect 以最新草稿为准, 不读旧快照覆盖编辑)。
  useEffect(() => {
    void refreshDetailed()
  }, [refreshDetailed])

  // 卸载兜底: 脏输入落定一次, 保住导航前最后一次编辑。执行时重读修订
  // (与互斥串行, 不以调度时旧修订号写); 若卸载前已发生切换, 写前复核
  // 序列即丢弃, 不覆盖新选择。结果页切换即走此路径。
  // 入队时脏即排队, 执行时复核(见 persistWorking): 排队后已有同值落定
  // (关闭落定/防抖先落地)则跳过, 值不同或从未落定必写, 不丢编辑。
  useEffect(
    () => () => {
      if (saveTimer.current !== null) {
        window.clearTimeout(saveTimer.current)
        saveTimer.current = null
      }
      if (dirtyRef.current) {
        void persistRef.current(formRef.current)
      }
    },
    [],
  )

  // -------------------------------------------------------------------------
  // 取消 / 重试
  // -------------------------------------------------------------------------
  const doCancel = useCallback(async () => {
    if (!cancelTarget) return
    setActionError(null)
    try {
      await cancelTask(projectId, cancelTarget.id)
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
      await retryTask(projectId, retryTarget.id)
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
                    onClick={() => {
                      // 显式选择即把该任务冻结输入载入工作草稿(仅用户点击;
                      // 初始化/重挂载不触发)。载入期间(忙态)或同任务重复
                      // 点击一律忽略: 不得为此取消防抖计时器/清脏态, 否则
                      // 待落定的编辑丢失; 共享边界同样会忽略此次调用。
                      // 非忽略时: 选择经工作台同序列作废在途旧保存并等其
                      // 落定后再载入; 新修订到达后表单整体替换(见水合 effect)。
                      if (draftLoadingTaskId !== null || tk.id === selectedId) return
                      setActionError(null)
                      if (saveTimer.current !== null) {
                        window.clearTimeout(saveTimer.current)
                        saveTimer.current = null
                      }
                      dirtyRef.current = false
                      void selectTaskWithDraft(tk.id).then((res) => {
                        if (res.error !== null) {
                          setActionError(res.error)
                        } else if (res.loaded) {
                          pendingSwitchRef.current = {
                            task: tk.id,
                            rev: res.draftRevision ?? 0,
                          }
                          setFlash(t('ies.task.draft_loaded', { task_id: tk.id }))
                        }
                      })
                    }}
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
                <span className="ies-badge ies-badge--neutral ies-badge--shape-square">
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
                      {detail.calc_snapshot.config_revision !== null &&
                      detail.calc_snapshot.config_revision !== undefined
                        ? ` · ${t('ies.task.snapshot_config_revision', { revision: detail.calc_snapshot.config_revision })}`
                        : ''}
                    </dd>
                  </>
                ) : null}
              </dl>

              {/* 冻结输入: 所选任务快照的只读投影(非当前草稿); 失败可见, 不回退草稿 */}
              <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, margin: 'var(--ies-space-4) 0 var(--ies-space-2)' }}>
                {t('ies.task.frozen_inputs')}
              </h3>
              {frozenLoading ? (
                <div style={{ padding: 'var(--ies-space-2)' }}>
                  <Spinner size="sm" />
                </div>
              ) : frozenError ? (
                <Alert variant="error">{frozenError}</Alert>
              ) : !frozenInputs ? null : frozenInputs.snapshot === null ? (
                <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
                  {t('ies.task.frozen_no_snapshot')}
                </p>
              ) : (
                <FrozenInputsView
                  inputs={frozenInputs}
                  versions={versions}
                  datasetNames={frozenDatasetNames}
                  labels={{
                    snapshot: t('ies.task.frozen_snapshot'),
                    version: t('ies.task.frozen_version'),
                    datasets: t('ies.task.frozen_datasets'),
                    evaluation: t('ies.task.frozen_evaluation'),
                    config: t('ies.task.frozen_config'),
                    planning: t('ies.task.frozen_planning'),
                    finance: t('ies.task.frozen_finance'),
                    models: t('ies.task.frozen_models'),
                  }}
                />
              )}

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

      {/* 提交任务对话框(关闭即把脏工作输入后台落草稿, 不建任务;
          对话框立即关闭以便继续导航/选择, 不被网络停顿的保存卡住;
          落定失败经工作台自动保存指示可见, 不当成功静默丢编辑;
          提交按钮仍先落定再冻结, 与关闭路径不同) */}
      <Dialog
        open={createOpen}
        onClose={() => {
          setCreateOpen(false)
          setFormError(null)
          void flushWorking()
        }}
        title={t('ies.task.submit')}
        size="lg"
        footer={
          <>
            <Button
              variant="secondary"
              onClick={() => {
                // 与对话框 onClose 同语义: 立即关闭, 脏输入后台落定。
                setCreateOpen(false)
                setFormError(null)
                void flushWorking()
              }}
            >
              {t('ies.common.cancel')}
            </Button>
            <Button
              loading={submitting || checking}
              disabled={!hasSavedConfigValue || !workingReady}
              onClick={() => {
                void submit(false)
              }}
            >
              {t('ies.task.submit')}
            </Button>
          </>
        }
      >
        <div className="ies-section">
          <Alert variant="info">{t('ies.task.submit_hint')}</Alert>
        </div>
        {savedRevisionValue !== null ? (
          <div className="ies-section">
            <Alert variant="info">{t('ies.task.config_ref_note', { revision: savedRevisionValue })}</Alert>
          </div>
        ) : null}

        {checking ? (
          <div className="ies-section">
            <Spinner size="sm" />
            <span className="ies-form-message" style={{ marginLeft: 'var(--ies-space-2)' }}>
              {t('ies.task.validation_checking')}
            </span>
          </div>
        ) : null}
        {blockers !== null && blockers.length > 0 ? (
          <div className="ies-section">
            <Alert variant="error" title={t('ies.task.validation_blocked', { count: blockers.length })}>
              {t('ies.task.validation_blocked_note')}
            </Alert>
            <ul className="ies-diag-list">
              {blockers.map((d, i) => (
                <DiagnosticItem key={`${d.code}-${i}`} diag={d} />
              ))}
            </ul>
          </div>
        ) : null}

        <FormField label={t('ies.common.type')} htmlFor="task-type">
          <Select
            id="task-type"
            value={form.type}
            onChange={(e) => updateForm({ ...form, type: e.target.value as TaskType })}
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
              <dt>{t('ies.config.mode')}</dt>
              <dd>{calcSummary?.mode || '—'}</dd>
              <dt>{t('ies.config.generator')}</dt>
              <dd>{calcSummary?.generator || '—'}</dd>
              <dt>{t('ies.config.solver')}</dt>
              <dd>{calcSummary?.solver || '—'}</dd>
              <dt>{t('ies.config.precision')}</dt>
              <dd>{calcSummary?.precision || '—'}</dd>
            </dl>
            {savedRevisionValue === null ? (
              <div style={{ marginTop: 'var(--ies-space-2)' }}>
                <Alert variant="error">{t('ies.task.config_required')}</Alert>
              </div>
            ) : null}
          </Card>
        ) : (
          <Alert variant="error" title={t('ies.common.load_failed', { reason: '' })}>
            {t('ies.task.submit_hint')}
          </Alert>
        )}

        <div className="ies-section">
          <Alert variant="info">{t('ies.task.datasets_note')}</Alert>
        </div>

        <fieldset className="ies-fieldset">
          <legend>{t('ies.task.evaluation')}</legend>
          <p className="ies-form-message" style={{ marginBottom: 'var(--ies-space-2)' }}>
            {t('ies.task.evaluation_hint')}
          </p>
          <FormField label={t('ies.task.construction_period')} htmlFor="task-construction-period" required>
            <Input
              id="task-construction-period"
              value={form.constructionPeriod}
              placeholder="1"
              inputMode="numeric"
              onChange={(e) => updateForm({ ...form, constructionPeriod: e.target.value })}
            />
          </FormField>
          <FormField label={t('ies.task.operation_years')} htmlFor="task-operation-years" required>
            <Input
              id="task-operation-years"
              value={form.operationYears}
              placeholder="2"
              inputMode="numeric"
              onChange={(e) => updateForm({ ...form, operationYears: e.target.value })}
            />
          </FormField>
          <p className="ies-form-message" style={{ marginBottom: 'var(--ies-space-2)' }}>
            {t('ies.task.evaluation_years_derived', {
              // 与提交接受口径同源: 展示 E 取同一解析的规范值, 非法表单显 '—'
              // (此前正则比 Number() 严格, '+1' 等合法写法显示与接受分裂)。
              years: (() => {
                const parsed = parseEvaluation(form)
                return parsed === null ? '—' : String(parsed.evaluation_years)
              })(),
            })}
          </p>
          <FormField label={t('ies.task.discount_rate')} htmlFor="task-discount-rate" required>
            <Input
              id="task-discount-rate"
              value={form.discountRate}
              placeholder="0.08"
              inputMode="decimal"
              onChange={(e) => updateForm({ ...form, discountRate: e.target.value })}
            />
          </FormField>
          <FormField label={t('ies.task.construction_payments')} htmlFor="task-construction-payments" required>
            <Textarea
              id="task-construction-payments"
              value={form.constructionPayments}
              placeholder={t('ies.task.construction_payments_placeholder')}
              rows={2}
              onChange={(e) => updateForm({ ...form, constructionPayments: e.target.value })}
            />
          </FormField>
          <FormField label={t('ies.task.maintenance_expenses')} htmlFor="task-maintenance-expenses" required>
            <Textarea
              id="task-maintenance-expenses"
              value={form.maintenanceExpenses}
              placeholder={t('ies.task.maintenance_expenses_placeholder')}
              aria-describedby="task-maintenance-expenses-hint"
              rows={3}
              onChange={(e) => updateForm({ ...form, maintenanceExpenses: e.target.value })}
            />
          </FormField>
          <p id="task-maintenance-expenses-hint" className="ies-form-message" style={{ marginBottom: 'var(--ies-space-2)' }}>
            {t('ies.task.maintenance_expenses_hint')}
          </p>
        </fieldset>

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
                    onChange={() => updateForm({ ...form, uMode: mode })}
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
                    updateForm({ ...form, nSamples: Math.max(1, Number(e.target.value) || 1) })
                  }
                />
              </FormField>
              <FormField label={t('ies.task.seed')} htmlFor="u-seed">
                <Input
                  id="u-seed"
                  type="number"
                  placeholder={t('ies.common.optional')}
                  value={form.seed}
                  onChange={(e) => updateForm({ ...form, seed: e.target.value })}
                />
              </FormField>
              <FormField label={t('ies.task.distribution')} htmlFor="u-dist">
                <Select
                  id="u-dist"
                  value={form.distType}
                  onChange={(e) => updateForm({ ...form, distType: e.target.value as DistType })}
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
                    updateForm({ ...form, noisePct: Math.max(0, Number(e.target.value) || 0) })
                  }
                />
              </FormField>
            </div>
          </div>
        ) : null}

        {form.type === 'analysis' ? (
          <div className="ies-section">
            <Alert variant="warning">{t('ies.task.analysis_hint')}</Alert>
            {form.sweeps.map((sweep, idx) => (
              <div key={idx} className="ies-grid ies-grid--cols-3">
                <FormField label={t('ies.task.sweep_path')} htmlFor={`sweep-path-${idx}`}>
                  <Input
                    id={`sweep-path-${idx}`}
                    type="text"
                    placeholder="calc_config.params.discount_rate"
                    value={sweep.path}
                    onChange={(e) =>
                      updateForm({
                        ...form,
                        sweeps: form.sweeps.map((s, i) =>
                          i === idx ? { ...s, path: e.target.value } : s,
                        ),
                      })
                    }
                  />
                </FormField>
                <FormField label={t('ies.task.sweep_values')} htmlFor={`sweep-values-${idx}`}>
                  <Input
                    id={`sweep-values-${idx}`}
                    type="text"
                    placeholder="0.06,0.08,0.10"
                    value={sweep.values}
                    onChange={(e) =>
                      updateForm({
                        ...form,
                        sweeps: form.sweeps.map((s, i) =>
                          i === idx ? { ...s, values: e.target.value } : s,
                        ),
                      })
                    }
                  />
                </FormField>
                <FormField label={t('ies.task.sweep_unit')} htmlFor={`sweep-unit-${idx}`}>
                  <Input
                    id={`sweep-unit-${idx}`}
                    type="text"
                    placeholder="-"
                    value={sweep.unit}
                    onChange={(e) =>
                      updateForm({
                        ...form,
                        sweeps: form.sweeps.map((s, i) =>
                          i === idx ? { ...s, unit: e.target.value } : s,
                        ),
                      })
                    }
                  />
                </FormField>
              </div>
            ))}
            <div className="ies-section__actions">
              <Button
                variant="ghost"
                size="sm"
                onClick={() =>
                  updateForm({ ...form, sweeps: [...form.sweeps, { path: '', values: '', unit: '' }] })
                }
              >
                {t('ies.task.sweep_add')}
              </Button>
              {form.sweeps.length > 1 ? (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => updateForm({ ...form, sweeps: form.sweeps.slice(0, -1) })}
                >
                  {t('ies.task.sweep_remove')}
                </Button>
              ) : null}
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
