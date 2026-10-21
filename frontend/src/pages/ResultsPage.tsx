/**
 * 结果分析(ResultsPage)。
 *
 * - 结果摘要:项目版本 / 数据版本 / 计算配置(快照 + 种子)/ 技术状态 / 业务结局。
 * - 四维结论卡片:物理 / 最优性 / 财务 / 可靠性,各自状态
 *   (passed / restricted / failed / na / insufficient),颜色 + 文字 + 图标;
 *   综合摘要不得掩盖任一维度失败(失败维度单独告警)。
 * - 财务:投资 / 运行成本 / 收益 / 现金流表 / IRR 状态
 *   (唯一、无解、多解、退化、超出定义域、数值失败)。
 * - 环境:运行碳排放 / 排放边界 / 因子版本。
 * - 工程:能源平衡表(电/热/冷)、设备容量、计算步曲线(SVG 折线, 横轴为步序号)、峰值、购售电、需量。
 * - 候选方案:真实候选列表(#index),选择只记录采用,不创建版本;
 *   规划解参数预览(封存解 × 冻结规划绑定);应用方案以后者为权威,
 *   在来源版本工作状态上写回并派生新版本(切换为当前)。
 * - 来源:仅按原快照引用展示项目版本与数据集,不读当前最新。
 * - 评估历史(不可变列表)与重新评估按钮。
 * - 导出:Excel 按钮(选择语言 zh/en)、完整项目包按钮(仅所有者可见)。
 *
 * 指标数据为数据驱动:按指标 id 前缀分组展示;后端指标 id 未固定前,
 * 分组按常用前缀(fin./env./eng./rel./energy 等)与常见关键词匹配,缺失时优雅降级。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { listDatasets, listVersions as listDatasetVersions } from '../features/datasets'
import { applyScheme, listVersions } from '../features/project-versions'
import { getProject } from '../features/projects'
import { downloadBlob } from '../shared/http'
import { exportTaskReport, isNoAssessmentError } from '../application/exportTaskReport'
import { exportPackage as createPackageExport, downloadExport } from '../features/exports'
import { getResult, listAssessments, assess, select, readHourly } from '../features/results'
import type { HourlySeriesMeta } from '../features/results'
import { translate, translateError, useI18n } from '../i18n'
import type { Locale } from '../i18n'
import {
  Alert,
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  FormField,
  Select,
  Spinner,
  Table,
  TaskOutcomeBadge,
  TaskStatusBadge,
  TBody,
  TD,
  TH,
  THead,
  TR,
} from '../components/ui'
import type { BadgeVariant, IconName } from '../components/ui'
import { LineChart } from '../components/charts'
import type { LineSeries } from '../components/charts'
import { formatCo2, formatDateTime, formatMoney, formatNumber, formatPercent } from '../lib/format'
import type {
  ApiError,
  AssessmentDimension,
  AssessmentGrade,
  Currency,
  EvidenceCandidate,
  ExcelExportInput,
  EvidencePackageSummary,
  FinancialAbsence,
  FinancialEvaluationBlock,
  MetricValue,
  PlanningSolution,
  ResultAssessment,
  SnapshotRefs,
  TaskOutcome,
} from '../types'
import { useEffectiveTaskContext, useWorkbench } from './workbench'

// ---------------------------------------------------------------------------
// 类型与常量
// ---------------------------------------------------------------------------

type DimStatus = 'passed' | 'restricted' | 'failed' | 'na' | 'insufficient'

const DIMENSIONS: Array<{ key: AssessmentDimension; labelKey: string }> = [
  { key: 'physical', labelKey: 'ies.result.dimension_physical' },
  { key: 'optimality', labelKey: 'ies.result.dimension_optimality' },
  { key: 'financial', labelKey: 'ies.result.dimension_financial' },
  { key: 'reliability', labelKey: 'ies.result.dimension_reliability' },
]

const DIM_STATUS_META: Record<DimStatus, { variant: BadgeVariant; icon: IconName }> = {
  passed: { variant: 'success', icon: 'check' },
  restricted: { variant: 'warning', icon: 'warning' },
  failed: { variant: 'danger', icon: 'cross' },
  na: { variant: 'neutral', icon: 'question' },
  insufficient: { variant: 'warning', icon: 'info' },
}

const IRR_STATUS_KEY = 'ies.result.irr_status'

/** 四维状态派生:评估等级优先,业务结局兜底(证据不足/受限/部分完成)。 */
function deriveDimStatus(
  grade: AssessmentGrade | undefined,
  outcome: TaskOutcome | null | undefined,
): DimStatus {
  const restrictedOutcomes: TaskOutcome[] = [
    'restricted_results',
    'partial_batch',
    'no_recommendation',
    'no_feasible_multi_objective',
  ]
  if (outcome === 'insufficient_evidence') return 'insufficient'
  if (grade === 'fail') return 'failed'
  if (grade === 'pass') return restrictedOutcomes.includes(outcome ?? 'normal_completion') ? 'restricted' : 'passed'
  return restrictedOutcomes.includes(outcome ?? 'normal_completion') ? 'restricted' : 'na'
}

// ---------------------------------------------------------------------------
// 指标辅助
// ---------------------------------------------------------------------------

function pickMetrics(
  metrics: Record<string, MetricValue> | null,
  patterns: RegExp[],
): MetricValue[] {
  if (!metrics) return []
  return Object.entries(metrics)
    .filter(([id]) => patterns.some((re) => re.test(id)))
    .map(([, m]) => m)
}

function findMetric(metrics: Record<string, MetricValue> | null, patterns: RegExp[]): MetricValue | null {
  if (!metrics) return null
  const hit = Object.entries(metrics).find(([id]) => patterns.some((re) => re.test(id)))
  return hit ? hit[1] : null
}

/** 指标数值格式化(金额/CO2/百分比特殊处理,其余按数值 + 单位)。 */
function formatMetricValue(m: MetricValue, currency: Currency): string {
  if (m.value === null || m.value === undefined || Number.isNaN(m.value)) return '—'
  const unit = (m.unit ?? '').toLowerCase()
  if (unit.includes('cny') || unit.includes('usd') || unit.includes('¥') || unit.includes('$')) {
    return formatMoney(m.value, currency)
  }
  if (unit.includes('co2') || unit.includes('co₂') || unit.includes('tco2')) {
    return formatCo2(m.value)
  }
  if (unit === '%' || unit === 'percent' || unit === 'pct') {
    return formatPercent(m.value / 100)
  }
  return `${formatNumber(m.value, { digits: 2 })}${m.unit ? ` ${m.unit}` : ''}`
}

/** 封存 IRR 状态徽章键(唯一/无解/多解/退化/域外/数值失败)。 */
type IrrStatusKey = 'unique' | 'no_solution' | 'multiple' | 'degenerate' | 'out_of_domain' | 'numeric_failure'

/**
 * 封存 evaluation.irr_status → 徽章键(唯一权威映射, 摘要与封存卡共用)。
 * 后端 'normal' 即唯一解；其余五枚举直通；未知状态不猜测，回 null 即不展示。
 */
function irrStatusToKey(irrStatus: string): IrrStatusKey | null {
  return irrStatus === 'normal'
    ? 'unique'
    : irrStatus === 'no_solution' ||
        irrStatus === 'multiple' ||
        irrStatus === 'degenerate' ||
        irrStatus === 'out_of_domain' ||
        irrStatus === 'numeric_failure'
      ? (irrStatus as IrrStatusKey)
      : null
}

// ---------------------------------------------------------------------------
// 页面
// ---------------------------------------------------------------------------

export default function ResultsPage() {
  const { t, locale } = useI18n()
  // 全局任务选择(任务/导出页同选择; null = 无显式选择走默认最新完成)
  const { projectId, project, refreshDetailed, selectTaskWithDraft, draftLoadingTaskId } = useWorkbench()
  const currency: Currency = project?.currency ?? 'CNY'

  // URL ?package= 直达参数(仅首次读取; 一次性初始化同一共享任务选择,
  // 完成后用户选择统管草稿/结果/评估/导出, 不再是常驻的第二权威)。
  const [initialPkgParam] = useState<number | null>(() => {
    const v = Number(new URLSearchParams(window.location.search).get('package'))
    return Number.isFinite(v) && v > 0 ? v : null
  })
  // ?package= 映射到的显式任务 id(-1 = 包未知/不可解析, 阻塞不回退)。
  // 初始化完成(linkDone)后改走全局选择; 完成前保持 -1, 不闪现他任务结果。
  const [linkTaskId, setLinkTaskId] = useState<number | null>(() => (initialPkgParam !== null ? -1 : null))
  const [linkDone, setLinkDone] = useState(false)

  // 有效任务上下文(与导出页同一规则、同一来源; 只读列表与详情)
  const taskCtx = useEffectiveTaskContext(initialPkgParam !== null && !linkDone ? linkTaskId : undefined)
  const { loading, loadError } = { loading: taskCtx.loading, loadError: taskCtx.error }
  // 证据包选择器选项(走完全部 completed 历史的各任务最新包, 按包 id 升序)。
  const options = useMemo(() => {
    const collected: Array<{
      packageId: number
      taskId: number
      status: EvidencePackageSummary['status']
      outcome: TaskOutcome | null
    }> = []
    for (const tk of taskCtx.history) {
      if (tk.latest_package_id === null || tk.latest_evidence_status === null) continue
      collected.push({
        packageId: tk.latest_package_id,
        taskId: tk.id as number,
        status: tk.latest_evidence_status as EvidencePackageSummary['status'],
        outcome: tk.business_outcome,
      })
    }
    collected.sort((a, b) => a.packageId - b.packageId)
    return collected
  }, [taskCtx.history])
  const selectedPkgId = taskCtx.picked ? taskCtx.picked.packageId : null
  /** 选中证据包所属任务 id(结果域 API 路由键; 无选中为 0, 后端明确 404)。 */
  const pickedTaskId = taskCtx.picked ? taskCtx.picked.taskId : 0
  /** 显式选择无可用结果(未完成/无证据/未知): 只展示无结果, 永不回退他任务。 */
  const explicitNoResult = !taskCtx.loading && taskCtx.explicitBlocked

  // 选中证据包的结果数据
  const [metrics, setMetrics] = useState<Record<string, MetricValue> | null>(null)
  /** 已封存财务评价(同一证据内容原样投影, 页面只展示不重算)。 */
  const [finEval, setFinEval] = useState<FinancialEvaluationBlock | null>(null)
  /** 缺块原因(有块为 null; 后端按可观察事实判定, 前端不猜测)。 */
  const [finAbsence, setFinAbsence] = useState<FinancialAbsence>(null)
  const [noEvidence, setNoEvidence] = useState(false)
  const [assessments, setAssessments] = useState<ResultAssessment[]>([])
  const [hourly, setHourly] = useState<{ flows: Record<string, number[]>; series: HourlySeriesMeta[] } | null>(null)
  /** 曲线合法缺席(后端显式无逐时引用, 非失败, 不清其他结果)。 */
  const [hourlyAbsent, setHourlyAbsent] = useState(false)
  /** 曲线读取失败(整组失败, 不清财务/指标/摘要, 可重试)。 */
  const [hourlyError, setHourlyError] = useState<string | null>(null)
  const [hourlyLoading, setHourlyLoading] = useState(false)
  const [resultError, setResultError] = useState<string | null>(null)
  // 结果错误归属任务: 草稿载入/结果读取失败钉在所属任务上。结果加载 effect
  // 切换清理时, 属于正选中任务自身的失败予以保留(失败与结果展示可共存,
  // 直达载入失败晚于选中包到达时不被清理吞掉); 切走或新的成功载入即清除。
  const resultErrorTaskRef = useRef<number | null>(null)
  /** 结果域切换/落定清理错误位: 正选中任务自身的失败保留, 其余清除。 */
  const clearResultErrorFor = useCallback((incomingTaskId: number | null): void => {
    if (resultErrorTaskRef.current !== null && resultErrorTaskRef.current === incomingTaskId) return
    resultErrorTaskRef.current = null
    setResultError(null)
  }, [])

  // 真实候选列表/规划解/快照引用(随选中证据包加载, 不按指标键反解)
  const [candList, setCandList] = useState<EvidenceCandidate[]>([])
  const [planSolution, setPlanSolution] = useState<PlanningSolution | null>(null)
  const [snapRefs, setSnapRefs] = useState<SnapshotRefs | null>(null)
  // 快照来源展示(项目版本 + 数据集名称, 按引用 id 匹配; 失败可见)
  const [snapSources, setSnapSources] = useState<{
    projectVersion: string
    datasets: string[]
    error: string | null
  } | null>(null)

  // Pareto 选择与应用
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null)
  const [applyOpen, setApplyOpen] = useState(false)
  const [applying, setApplying] = useState(false)
  const [applyError, setApplyError] = useState<string | null>(null)
  const [applyOk, setApplyOk] = useState(false)

  // 方案应用(规划解预览 → 来源版本基线派生新版本; 服务端权威解析)
  const [schemeOpen, setSchemeOpen] = useState(false)
  const [schemeBusy, setSchemeBusy] = useState(false)
  const [schemeError, setSchemeError] = useState<string | null>(null)
  const [schemeOk, setSchemeOk] = useState<string | null>(null)
  const [schemeRefreshError, setSchemeRefreshError] = useState<string | null>(null)
  // 确认时捕获的当前草稿修订(每次打开确认框经公开查询现读, 发送前不重读)。
  const [schemeRevision, setSchemeRevision] = useState<number | null>(null)
  const [schemeRevisionError, setSchemeRevisionError] = useState<string | null>(null)
  const [schemeRevLoading, setSchemeRevLoading] = useState(false)
  // 确认准备读序号: 关闭/切换后迟到的读响应丢弃, 不碰旧弹框。
  const schemeReadSeq = useRef(0)
  // 写请求飞行令牌: 旧回调不修改新操作状态, 完成只解除所属飞行。
  const schemeFlight = useRef(0)
  // 切换证据包的迟到读响应不发布状态(读取消机制, 不用于写请求)。
  const selectedPkgRef = useRef<number | null>(null)
  selectedPkgRef.current = selectedPkgId
  // 曲线读取序号: 切换证据/重试后迟到的旧读响应丢弃, 不覆盖新选择。
  const hourlySeq = useRef(0)

  // 重新评估对话框(只触发系统四维检查, FE-BE-03)
  const [assessOpen, setAssessOpen] = useState(false)
  const [assessBusy, setAssessBusy] = useState(false)
  const [assessError, setAssessError] = useState<string | null>(null)
  const [assessOk, setAssessOk] = useState(false)

  // 导出
  const [exportLang, setExportLang] = useState<Locale>(locale)
  const [exportBusy, setExportBusy] = useState<'excel' | 'package' | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const [exportOk, setExportOk] = useState(false)
  /** 导出缺评估引导(无既有评估时置 true, 不隐式创建评估)。 */
  const [exportNeedsAssess, setExportNeedsAssess] = useState(false)

  // -------------------------------------------------------------------------
  // ?package= 直达初始化(一次性): 按走完的历史把包映射为任务, 经同一共享
  // 选择边界选中(含草稿载入)。成功/同任务无操作后清 URL 参数并交还用户选择
  // (同任务无操作不重拷快照, 草稿编辑保留; 简单重挂载/重载无参数即不重拷)。
  // 未知包保持阻塞并保留参数(历史走完仍无该包, 不回退默认); 载入失败可见
  // 且保留参数, 不伪造选择, 不自动循环重试(等用户经证据选择器显式重试)。
  // -------------------------------------------------------------------------
  // 直达单次尝试标记: 同一包只主动发起一次载入。结果/错误回调不随忙态或
  // 历史变化取消(忙态变化只决定本次是否发起), 故一次尝试的结局必然可见;
  // 失败后 effect 重跑亦不再重发。
  // ?package= 直达初始化: 本 effect 拥有唯一一次尝试。结果/错误在 busy
  // 状态翻转后仍保留可观察; 仅真实上下文变化/unmount 才取消, 失败后显式重试。
  const linkAttemptRef = useRef<number | null>(null)
  useEffect(() => {
    if (initialPkgParam === null || linkDone || taskCtx.loading || taskCtx.error) return
    if (linkAttemptRef.current !== null) return
    const hit = taskCtx.history.find((tk) => tk.latest_package_id === initialPkgParam) ?? null
    if (hit === null) {
      // 未知包: 保持 -1 阻塞, 不回退默认。
      setLinkTaskId(-1)
      return
    }
    const mappedId = hit.id as number
    setLinkTaskId(mappedId)
    linkAttemptRef.current = mappedId
    void selectTaskWithDraft(mappedId).then((res) => {
      if (res.error !== null) {
        resultErrorTaskRef.current = mappedId
        setResultError(res.error)
        return
      }
      resultErrorTaskRef.current = null
      setLinkDone(true)
      window.history.replaceState(null, '', window.location.pathname)
    })
  }, [initialPkgParam, linkDone, taskCtx.loading, taskCtx.error, taskCtx.history, selectTaskWithDraft])

  // -------------------------------------------------------------------------
  // 选中证据包的结果/评估/逐时数据
  // -------------------------------------------------------------------------
  useEffect(() => {
    if (!selectedPkgId) {
      // 取消选择即作废在途曲线/重试读, 不向空选择发布旧响应。
      hourlySeq.current += 1
      setMetrics(null)
      setFinEval(null)
      setFinAbsence(null)
      setNoEvidence(false)
      setAssessments([])
      setHourly(null)
      setHourlyAbsent(false)
      setHourlyError(null)
      setHourlyLoading(false)
      clearResultErrorFor(null)
      setCandList([])
      setPlanSolution(null)
      setSnapRefs(null)
      setSnapSources(null)
      return
    }
    // 切换即清理旧结果与选择/确认状态: 加载中不提供选择/确认,
    // 迟到响应沿取消机制丢弃, 不覆盖新选中。应用写请求飞行中证据选择被禁用,
    // 此处不解除 schemeBusy(完成只由所属飞行解除)。
    setMetrics(null)
    setFinEval(null)
    setFinAbsence(null)
    setNoEvidence(false)
    setAssessments([])
    setHourly(null)
    setHourlyAbsent(false)
    setHourlyError(null)
    setHourlyLoading(false)
    // 正选中任务自身草稿载入失败的错误予以保留(与结果展示共存), 其余清除。
    clearResultErrorFor(pickedTaskId === 0 ? null : pickedTaskId)
    setCandList([])
    setPlanSolution(null)
    setSnapRefs(null)
    setSnapSources(null)
    setSelectedCandidateId(null)
    setApplyOpen(false)
    setApplyOk(false)
    setApplyError(null)
    setSchemeOpen(false)
    setSchemeError(null)
    setSchemeOk(null)
    setSchemeRefreshError(null)
    setSchemeRevision(null)
    setSchemeRevisionError(null)
    setSchemeRevLoading(false)
    schemeReadSeq.current += 1
    hourlySeq.current += 1
    let cancelled = false
    const load = async () => {
      // 结果域 API 以任务为路由键: 选中证据包所属任务即绑定任务(同源, 不反查)。
      const taskId = pickedTaskId
      try {
        const [res, ass] = await Promise.all([
          // 主结果读取失败必须可见: 不吞错, 由外层错误呈现。
          getResult(projectId, taskId),
          listAssessments(projectId, taskId),
        ])
        if (cancelled) return
        setMetrics(res.metrics)
        setFinEval(res.financial)
        setFinAbsence(res.financialAbsence)
        // evidence_status=no_evidence: 任务存在但尚无证据包(后端显式状态, 非猜测)
        setNoEvidence(res.evidence_status === 'no_evidence')
        setAssessments([...ass].sort((a, b) => b.created_at.localeCompare(a.created_at)))
        // 主结果状态先全部发布(财务/指标/候选/规划解/快照引用), 再读曲线:
        // 曲线慢/挂起不延迟主结果展示。曲线缺席与失败只记曲线状态。
        // 真实候选/规划解/快照引用(后端权威, 不反解指标键)
        setCandList(res.candidates)
        setPlanSolution(res.planningSolution)
        setSnapRefs(res.snapshotRefs)
        // 结果读成功不吞掉正选中任务自身的草稿载入失败(草稿仍未载入)。
        clearResultErrorFor(taskId)
        // 曲线独立读取: 整组失败直接呈现, 不缩水、不补空。
        // 序号 guard 避免旧读覆盖新选择。
        const mySeq = hourlySeq.current + 1
        hourlySeq.current = mySeq
        setHourlyLoading(true)
        try {
          const hr = await readHourly(projectId, taskId)
          if (cancelled || hourlySeq.current !== mySeq) return
          if (hr.absent) {
            setHourly(null)
            setHourlyAbsent(true)
          } else {
            setHourly({ flows: hr.flows, series: hr.series })
            setHourlyAbsent(false)
          }
          setHourlyError(null)
        } catch (err) {
          if (cancelled || hourlySeq.current !== mySeq) return
          setHourly(null)
          setHourlyAbsent(false)
          setHourlyError(translateError(err as ApiError))
        } finally {
          if (!cancelled && hourlySeq.current === mySeq) setHourlyLoading(false)
        }
      } catch (err) {
        if (!cancelled) {
          setMetrics(null)
          setFinEval(null)
          setFinAbsence(null)
          setCandList([])
          setPlanSolution(null)
          setSnapRefs(null)
          setSelectedCandidateId(null)
          setApplyOpen(false)
          setSchemeOpen(false)
          setSchemeError(null)
          setSchemeOk(null)
          setSchemeRefreshError(null)
          setSchemeRevLoading(false)
          schemeReadSeq.current += 1
          // 结果读取失败钉在当前任务上(与草稿载入失败同一保留规则)。
          resultErrorTaskRef.current = taskId
          setResultError(translateError(err as ApiError))
        }
      }
    }
    void load()
    return () => {
      cancelled = true
      // 卸载/切换时作废曲线与重试在途读: 旧响应不再发布(含 retryHourly,
      // 它只认序号、不认 cancelled)。
      hourlySeq.current += 1
    }
  }, [selectedPkgId])

  // 曲线重试: 仅重读所选任务曲线, 不重求解/重评估; 旧读按序号丢弃,
  // 不覆盖切换后的新选择。重试期间不碰财务/指标/摘要状态。
  const retryHourly = useCallback(async () => {
    const taskId = pickedTaskId
    const mySeq = hourlySeq.current + 1
    hourlySeq.current = mySeq
    setHourlyError(null)
    setHourlyLoading(true)
    try {
      const hr = await readHourly(projectId, taskId)
      if (hourlySeq.current !== mySeq) return
      if (hr.absent) {
        setHourly(null)
        setHourlyAbsent(true)
      } else {
        setHourly({ flows: hr.flows, series: hr.series })
        setHourlyAbsent(false)
      }
    } catch (err) {
      if (hourlySeq.current !== mySeq) return
      setHourlyError(translateError(err as ApiError))
    } finally {
      if (hourlySeq.current === mySeq) setHourlyLoading(false)
    }
  }, [pickedTaskId, projectId, selectedPkgId])

  // 快照来源展示: 仅按原快照引用解析名称, 不读当前最新。
  // 名称缺失显示精确引用 id; 查询失败可见, 不静默吞错。
  useEffect(() => {
    if (!snapRefs) {
      setSnapSources(null)
      return
    }
    let cancelled = false
    const load = async () => {
      try {
        const versions = await listVersions(projectId)
        const hit = versions.find((v) => v.id === snapRefs.project_version_id)
        const page = await listDatasets({ project_id: projectId, limit: 50 })
        const matched = new Set<number>()
        const shown: string[] = []
        for (const ds of page.items) {
          const vs = await listDatasetVersions(projectId, ds.id)
          for (const v of vs) {
            if (snapRefs.dataset_version_ids.includes(v.id) && !matched.has(v.id)) {
              matched.add(v.id)
              shown.push(`${ds.name} v${v.version_no}`)
            }
          }
        }
        // 未解析显示精确引用 id, 不造名称。
        for (const id of snapRefs.dataset_version_ids) {
          if (!matched.has(id)) shown.push(`id:${id}`)
        }
        if (cancelled) return
        setSnapSources({
          projectVersion: hit ? `v${hit.version_no}` : `id:${snapRefs.project_version_id}`,
          datasets: shown,
          error: null,
        })
      } catch (err) {
        if (!cancelled) {
          setSnapSources({
            projectVersion: `id:${snapRefs.project_version_id}`,
            datasets: snapRefs.dataset_version_ids.map((id) => `id:${id}`),
            error: err instanceof Error ? err.message : String(err),
          })
        }
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [projectId, snapRefs])

  // -------------------------------------------------------------------------
  // 派生数据
  // -------------------------------------------------------------------------
  // 选中条目展示(绑定任务的精确详情; picked 恒有对应详情, 缺席即无结果分支)。
  const selectedEntry = taskCtx.details.find((d) => d.id === (taskCtx.picked?.taskId ?? -1)) ?? null
  const outcome = selectedEntry?.business_outcome ?? null
  const snapshot = selectedEntry?.calc_snapshot ?? null
  /** 选中证据包状态(绑定任务精确详情的证据行; 摘要徽章用)。 */
  const selectedPkgStatus =
    selectedEntry?.evidence.find((p) => p.package_id === selectedPkgId)?.status ?? null

  const latestAssessment = useMemo(
    () => (assessments.length > 0 ? assessments[0] : null),
    [assessments],
  )

  const dimStatusOf = useCallback(
    (key: AssessmentDimension): DimStatus => {
      const grade = latestAssessment ? latestAssessment[`dimension_${key}`] : undefined
      return deriveDimStatus(grade, outcome)
    },
    [latestAssessment, outcome],
  )

  const failedDims = useMemo(
    () => DIMENSIONS.filter((d) => dimStatusOf(d.key) === 'failed').map((d) => t(d.labelKey)),
    [dimStatusOf, t],
  )

  // 旧摘要徽章不再从诊断猜测: 与封存卡同一封存状态事实、同一映射；
  // 无财务评价块或状态不可显示时为 null, 摘要侧不展示状态行(不补默认)。
  const irrKey = useMemo(
    () => (finEval ? irrStatusToKey(finEval.evaluation.irr_status) : null),
    [finEval],
  )

  // 财务/环境/工程指标分组(数据驱动)
  const finMetrics = useMemo(
    () =>
      pickMetrics(metrics, [/^fin\./, /irr/i, /cashflow/i, /investment/i, /revenue/i, /opex|o&m|running.?cost/i]),
    [metrics],
  )
  const cashflowMetrics = useMemo(() => pickMetrics(metrics, [/cashflow|cf_/i]), [metrics])
  const envMetrics = useMemo(() => pickMetrics(metrics, [/^env\./, /co2/i, /emission/i, /carbon/i, /factor/i]), [metrics])
  const co2Metric = useMemo(() => findMetric(metrics, [/^env\.co2/i, /^co2/i, /co2$/i, /^env\.emission/i]), [metrics])
  const boundaryMetric = useMemo(() => findMetric(metrics, [/boundary/i]), [metrics])
  const factorMetric = useMemo(() => findMetric(metrics, [/factor/i]), [metrics])
  const envOthers = useMemo(
    () => envMetrics.filter((m) => m !== co2Metric && m !== boundaryMetric && m !== factorMetric),
    [envMetrics, co2Metric, boundaryMetric, factorMetric],
  )

  const balanceMetrics = useMemo(() => pickMetrics(metrics, [/balance|^energy\.|^eng\.balance/i]), [metrics])
  const capacityMetrics = useMemo(() => pickMetrics(metrics, [/capacity|^dev\.|^eng\.device/i]), [metrics])
  const peakMetrics = useMemo(() => pickMetrics(metrics, [/peak/i]), [metrics])
  const gridMetrics = useMemo(() => pickMetrics(metrics, [/grid|p_grid|purchase|sell/i]), [metrics])
  const demandMetrics = useMemo(() => pickMetrics(metrics, [/demand/i]), [metrics])
  const relMetrics = useMemo(() => pickMetrics(metrics, [/^rel\.|sample/i]), [metrics])

  // 真实候选(后端候选列表 index 排序; 不按指标键反解)
  const candidates = useMemo(
    () => [...candList].sort((a, b) => a.index - b.index),
    [candList],
  )

  const selectedCandidate = useMemo(() => {
    if (!selectedCandidateId) return null
    const idx = candidates.findIndex((c) => c.index === Number(selectedCandidateId))
    return idx >= 0 ? candidates[idx] : null
  }, [selectedCandidateId, candidates])

  // 计算步序列按单位分组: 标签直接公开 instance/variable, 单位直接使用,
  // 不留 series_id 备用显示与未知单位组掩盖缺件。
  const hourlyGroups = useMemo(() => {
    if (!hourly) return [] as { unit: string; series: LineSeries[] }[]
    const palette = ['#0e5cad', '#b3261e', '#8a5a00', '#1e7d32', '#6a1b9a', '#00838f']
    const groups = new Map<string, { unit: string; series: LineSeries[] }>()
    let index = 0
    for (const meta of hourly.series) {
      const values = hourly.flows[meta.series_id]
      if (!values?.length) continue
      const label = `${meta.instance_id}.${meta.variable_id}`
      let group = groups.get(meta.unit)
      if (!group) {
        group = { unit: meta.unit, series: [] }
        groups.set(meta.unit, group)
      }
      group.series.push({ key: meta.series_id, label, color: palette[index % palette.length], values })
      index += 1
    }
    return [...groups.values()]
  }, [hourly])

  // 展示计数与入图序列同源(非硬编码, 不含未入图字段)。
  const hourlyPointCount = useMemo(() => {
    let max = 0
    for (const group of hourlyGroups) {
      for (const s of group.series) {
        if (s.values.length > max) max = s.values.length
      }
    }
    return max
  }, [hourlyGroups])

  // -------------------------------------------------------------------------
  // 动作
  // -------------------------------------------------------------------------
  // 选择只记录采用(后端 select), 不创建版本、不写回参数。
  const applyCandidate = useCallback(async () => {
    if (!selectedCandidate) return
    setApplyError(null)
    setApplyOk(false)
    setApplying(true)
    try {
      // 结果域 API 以任务为路由键: 选中证据包所属任务即绑定任务(同源)。
      await select(projectId, pickedTaskId, {
        result_index_id: selectedCandidate.index,
        reason: 'user-selected-candidate',
      })
      setApplyOk(true)
      setApplyOpen(false)
    } catch (err) {
      setApplyError(translateError(err as ApiError))
    } finally {
      setApplying(false)
    }
  }, [projectId, selectedCandidate, pickedTaskId])

  // 打开应用方案确认: 每次经公开项目查询现读当前草稿修订(不用 Workbench
  // 缓存), 读成功后才允许确认; 读失败可见报错、不伪造; 关闭/切换后迟到的读
  // 响应按序号丢弃, 不重新打开旧弹框。读取只服务本次确认, 后续 POST 固定发送
  // 该次捕获的修订, 发送前不重读。
  const openSchemeDialog = useCallback(async () => {
    setSchemeError(null)
    setSchemeOk(null)
    setSchemeRefreshError(null)
    setSchemeRevision(null)
    setSchemeRevisionError(null)
    setSchemeRevLoading(true)
    setSchemeOpen(true)
    const seq = schemeReadSeq.current + 1
    schemeReadSeq.current = seq
    try {
      const view = await getProject(projectId)
      if (schemeReadSeq.current !== seq) return
      const rev = view.draft?.revision ?? null
      if (rev === null) {
        setSchemeRevisionError(t('ies.result.apply_scheme_revision_unavailable'))
      } else {
        setSchemeRevision(rev)
      }
    } catch {
      if (schemeReadSeq.current !== seq) return
      setSchemeRevisionError(t('ies.result.apply_scheme_revision_unavailable'))
    } finally {
      if (schemeReadSeq.current === seq) setSchemeRevLoading(false)
    }
  }, [projectId, t])

  // 应用方案(规划解预览 → 来源版本基线派生新版本; 只发送任务引用、解标识与
  // 确认时草稿修订, 参数与基线由服务端权威解析)。明确确认后才请求, 取消不
  // 变更; 处理中禁用证据选择与对话框关闭/取消, 避免重复; 服务端成功后刷新
  // 始终处理, 页面展示过期才按所属关系处理; 成功与刷新失败分开报告, 不重复应用。
  const confirmScheme = useCallback(async () => {
    if (!planSolution || schemeBusy || schemeRevLoading) return
    // 确认时修订缺失: 不伪造、不发送, 可见报错。
    if (schemeRevision === null) {
      setSchemeRevisionError(t('ies.result.apply_scheme_revision_unavailable'))
      return
    }
    const pkgAtConfirm = selectedPkgId
    const flight = schemeFlight.current + 1
    schemeFlight.current = flight
    setSchemeError(null)
    setSchemeOk(null)
    setSchemeRefreshError(null)
    setSchemeBusy(true)
    try {
      // 发送打开确认框时捕获的修订; 发送前不重读最新, 否则绕过冲突检测。
      const applied = await applyScheme(projectId, {
        task_id: pickedTaskId,
        solution_id: planSolution.solution_id,
        expected_revision: schemeRevision,
      })
      const refreshed = await refreshDetailed()
      if (schemeFlight.current !== flight || selectedPkgRef.current !== pkgAtConfirm) return
      const errors = [refreshed.projectError, refreshed.versionsError].filter(
        (m): m is string => m !== null,
      )
      setSchemeOk(`v${applied.version.version_no} ${applied.version.name}`)
      setSchemeRefreshError(errors.length > 0 ? errors.join('\n') : null)
      setSchemeOpen(false)
    } catch (err) {
      if (schemeFlight.current !== flight || selectedPkgRef.current !== pkgAtConfirm) return
      setSchemeError(translateError(err as ApiError))
    } finally {
      if (schemeFlight.current === flight) setSchemeBusy(false)
    }
  }, [projectId, planSolution, schemeBusy, schemeRevLoading, schemeRevision, selectedPkgId, pickedTaskId, refreshDetailed, t])

  const submitAssessment = useCallback(async () => {
    if (!selectedPkgId) return
    setAssessError(null)
    setAssessOk(false)
    setAssessBusy(true)
    try {
      // 后端 assess = 触发系统四维检查(FE-BE-03: 人工评分输入不支持持久化,
      // 不再伪装接受评分/评论后静默丢弃)
      const created = await assess(projectId, pickedTaskId, 'full')
      setAssessments((prev) => [created, ...prev].sort((a, b) => b.created_at.localeCompare(a.created_at)))
      setAssessOk(true)
      setAssessOpen(false)
    } catch (err) {
      setAssessError(translateError(err as ApiError))
    } finally {
      setAssessBusy(false)
    }
  }, [selectedPkgId])

  const exportExcel = useCallback(async () => {
    if (!selectedPkgId) return
    setExportError(null)
    setExportOk(false)
    setExportNeedsAssess(false)
    setExportBusy('excel')
    try {
      // 若无既有评估, excel 直接抛缺评估业务错并由导出区引导用户先完成
      // 系统评估, 不隐式创建评估。评估历史每次真实读取，不用内存缓存。
      const input: ExcelExportInput = {
        project_id: projectId,
        task_id: pickedTaskId,
        evidence_package_id: selectedPkgId,
        include_hourly: true,
        include_diagnostics: true,
        lang: exportLang,
      }
      const downloadSessionId = await exportTaskReport(input)
      const { blob, filename } = await downloadExport(downloadSessionId)
      downloadBlob(blob, filename)
      setExportOk(true)
    } catch (err) {
      if (isNoAssessmentError(err)) {
        setExportNeedsAssess(true)
      } else {
        setExportError(translateError(err as ApiError))
      }
    } finally {
      setExportBusy(null)
    }
  }, [projectId, selectedPkgId, pickedTaskId, exportLang])

  const exportPackage = useCallback(async () => {
    setExportError(null)
    setExportOk(false)
    setExportBusy('package')
    try {
      const downloadSessionId = await createPackageExport({ project_id: projectId, include_versions: true })
      const { blob, filename } = await downloadExport(downloadSessionId)
      downloadBlob(blob, filename)
      setExportOk(true)
    } catch (err) {
      setExportError(translateError(err as ApiError))
    } finally {
      setExportBusy(null)
    }
  }, [projectId])

  const isOwner = project?.role === 'owner'

  // -------------------------------------------------------------------------
  // 渲染
  // -------------------------------------------------------------------------
  if (loading) {
    return (
      <div className="ies-page-placeholder" role="status">
        <Spinner size="lg" />
      </div>
    )
  }

  // 收集失败即使用户尚无条目也必须可见(早返不得吞掉全局错误与重试)。
  const loadErrorBlock = loadError ? (
    <div className="ies-section">
      <Alert variant="error">{loadError}</Alert>
      <div style={{ marginTop: 'var(--ies-space-2)' }}>
        <Button onClick={() => taskCtx.retry()}>{t('ies.common.retry')}</Button>
      </div>
    </div>
  ) : null

  // 无绑定(无显式选择且默认无可用结果, 或显式选择无可用结果): 只展示
  // 无结果/阻塞, 永不回退他任务。
  if (selectedEntry === null) {
    return (
      <div>
        {loadErrorBlock}
        {explicitNoResult && !loadError ? (
          <div className="ies-section">
            <Alert variant="warning">{t('ies.result.no_evidence')}</Alert>
          </div>
        ) : null}
        {loadError ? null : (
          <div className="ies-section">
            <Card title={t('ies.result.summary')}>
              <EmptyState
                icon="info"
                title={t('ies.result.no_result_available')}
                description={t('ies.result.no_result_hint')}
                action={
                  <Link className="ies-btn ies-btn--primary ies-btn--md" to={`/projects/${projectId}/tasks`}>
                    {t('ies.nav.tasks')}
                  </Link>
                }
              />
            </Card>
          </div>
        )}
      </div>
    )
  }

  const evidenceBadgeVariant: BadgeVariant =
    selectedPkgStatus === 'complete' ? 'success' : selectedPkgStatus === 'partial' ? 'warning' : 'danger'

  return (
    <div>
      {loadErrorBlock}

      {/* 结果摘要 */}
      <div className="ies-section">
        <Card
          title={t('ies.result.summary')}
          actions={
            options.length > 1 ? (
              <Select
                aria-label={t('ies.result.evidence_status')}
                value={selectedPkgId ?? ''}
                disabled={schemeBusy || draftLoadingTaskId !== null}
                onChange={(e) => {
                  const pkgId = Number(e.target.value)
                  const hit = options.find((x) => x.packageId === pkgId) ?? null
                  // 选择即全局任务选择(任务 id)并将其冻结输入载入工作草稿,
                  // 与任务页同一边界; 失败可见且选择不变, 不建任务/版本。
                  // 用户选择接管后直达参数不再覆盖(见 linkDone)。
                  // 显式切换先清旧错; 新失败钉在所选任务上(同一直达失败保留规则)。
                  setResultError(null)
                  resultErrorTaskRef.current = null
                  setLinkDone(true)
                  void selectTaskWithDraft(hit ? hit.taskId : null).then((res) => {
                    if (res.error !== null) {
                      if (hit) resultErrorTaskRef.current = hit.taskId
                      setResultError(res.error)
                    }
                  })
                }}
                style={{ maxWidth: 260 }}
              >
                {options.map((o) => (
                  <option key={o.packageId} value={o.packageId}>
                    #{o.packageId} · {o.taskId} · {o.status}
                  </option>
                ))}
              </Select>
            ) : undefined
          }
        >
          {selectedEntry ? (
            <dl className="ies-kv">
              <dt>{t('ies.result.project_version')}</dt>
              <dd>{snapSources ? snapSources.projectVersion : '—'}</dd>
              <dt>{t('ies.result.data_versions')}</dt>
              <dd>
                {snapSources && snapSources.datasets.length > 0
                  ? snapSources.datasets.join('、')
                  : '—'}
              </dd>
              {snapSources?.error ? (
                <>
                  <dt>{t('ies.result.source_error')}</dt>
                  <dd>{snapSources.error}</dd>
                </>
              ) : null}
              <dt>{t('ies.result.calc_config')}</dt>
              <dd>
                {snapshot ? `#${snapshot.id}` : '—'}
                {snapshot?.random_seed !== null && snapshot?.random_seed !== undefined
                  ? ` · seed ${snapshot.random_seed}`
                  : ''}
              </dd>
              <dt>{t('ies.common.status')}</dt>
              <dd>
                <div className="ies-flex" style={{ flexWrap: 'wrap', gap: 'var(--ies-space-2)' }}>
                  <TaskStatusBadge status={selectedEntry.status} />
                  <Badge
                    variant={evidenceBadgeVariant}
                    icon={evidenceBadgeVariant === 'success' ? 'check' : 'warning'}
                    label={t(`ies.result.evidence_status_${selectedPkgStatus ?? 'invalid'}`)}
                  />
                </div>
              </dd>
              <dt>{t('ies.result.business_outcome')}</dt>
              <dd>
                <TaskOutcomeBadge outcome={outcome} />
              </dd>
              <dt>{t('ies.common.id')}</dt>
              <dd>
                任务 #{selectedEntry.id} · 证据包 #{selectedPkgId}
              </dd>
            </dl>
          ) : (
            <Alert variant="warning">{t('ies.result.no_result_available')}</Alert>
          )}
        </Card>
      </div>

      {/* 四维结论卡片 */}
      <div className="ies-section">
        <div className="ies-dims">
          {DIMENSIONS.map((d) => {
            const status = dimStatusOf(d.key)
            const meta = DIM_STATUS_META[status]
            return (
              <div key={d.key} className={`ies-dim ies-dim--${status}`} role="status">
                <div className="ies-dim__head">
                  <span className="ies-dim__title">{t(d.labelKey)}</span>
                  <Badge variant={meta.variant} icon={meta.icon} label={t(`ies.result.dim_status_${status}`)} />
                </div>
                <p className="ies-dim__note">{t('ies.result.dim_summary_note')}</p>
              </div>
            )
          })}
        </div>
        {failedDims.length > 0 ? (
          <div style={{ marginTop: 'var(--ies-space-3)' }}>
            <Alert variant="error" title={t('ies.result.dim_failed_alert', { dims: failedDims.join('、') })} />
          </div>
        ) : null}
      </div>

      {resultError ? (
        <div className="ies-section">
          <Alert variant="error">{resultError}</Alert>
        </div>
      ) : null}

      {/* 后端显式 no_evidence 状态: 任务存在但证据包尚未提交, 不以空数据伪装成功 */}
      {noEvidence || explicitNoResult ? (
        <div className="ies-section">
          <Alert variant="warning">{t('ies.result.no_evidence')}</Alert>
        </div>
      ) : null}

      {/* 已封存财务评价: 只展示封存事实, 不重算; 缺块按后端判定原因说明, 不伪装通过 */}
      {finEval ? (
        <FinancialEvaluationCard fin={finEval} />
      ) : finAbsence ? (
        <div className="ies-section">
          <Alert variant="warning">
            {finAbsence === 'outcome_not_normal'
              ? t('ies.result.fin_absence_outcome_not_normal', { outcome: outcome ?? '—' })
              : finAbsence === 'solution_not_requested'
                ? t('ies.result.fin_absence_solution_not_requested')
                : t('ies.result.fin_absence_missing_unknown')}
          </Alert>
        </div>
      ) : null}


      {metrics ? (
        <>
          {/* 财务 */}
          <div className="ies-grid ies-grid--cols-2">
            <Card title={t('ies.result.financial_card')} className="ies-section">
              <MetricGroup
                metrics={finMetrics}
                currency={currency}
                labels={{
                  irr: t('ies.result.irr'),
                  irrStatus: t(IRR_STATUS_KEY),
                }}
                irrKey={irrKey}
              />
              {cashflowMetrics.length > 0 ? (
                <div style={{ marginTop: 'var(--ies-space-3)' }}>
                  <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, marginBottom: 'var(--ies-space-2)' }}>
                    {t('ies.result.cashflow')}
                  </h3>
                  <MetricTable metrics={cashflowMetrics} currency={currency} />
                </div>
              ) : null}
            </Card>

            {/* 环境 */}
            <Card title={t('ies.result.env_card')} className="ies-section">
              <dl className="ies-kv">
                <dt>{t('ies.result.co2')}</dt>
                <dd>{co2Metric ? formatMetricValue(co2Metric, currency) : '—'}</dd>
                <dt>{t('ies.result.emission_boundary')}</dt>
                <dd>{boundaryMetric ? formatMetricValue(boundaryMetric, currency) : '—'}</dd>
                <dt>{t('ies.result.factor_version')}</dt>
                <dd>{factorMetric ? formatMetricValue(factorMetric, currency) : '—'}</dd>
              </dl>
              {envOthers.length > 0 ? (
                <div style={{ marginTop: 'var(--ies-space-3)' }}>
                  <MetricTable metrics={envOthers} currency={currency} />
                </div>
              ) : null}
            </Card>
          </div>

          {/* 工程 */}
          <Card title={t('ies.result.eng_card')} className="ies-section">
            {balanceMetrics.length > 0 ? (
              <div>
                <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, marginBottom: 'var(--ies-space-2)' }}>
                  {t('ies.result.energy_balance_table')}
                </h3>
                <MetricTable metrics={balanceMetrics} currency={currency} />
              </div>
            ) : null}
            <div className="ies-grid ies-grid--cols-3">
              {capacityMetrics.length > 0 ? (
                <div className="ies-section">
                  <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, marginBottom: 'var(--ies-space-2)' }}>
                    {t('ies.result.device_capacity')}
                  </h3>
                  <MetricTable metrics={capacityMetrics} currency={currency} />
                </div>
              ) : null}
              {peakMetrics.length > 0 ? (
                <div className="ies-section">
                  <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, marginBottom: 'var(--ies-space-2)' }}>
                    {t('ies.result.peak')}
                  </h3>
                  <MetricTable metrics={peakMetrics} currency={currency} />
                </div>
              ) : null}
              {gridMetrics.length > 0 ? (
                <div className="ies-section">
                  <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, marginBottom: 'var(--ies-space-2)' }}>
                    {t('ies.result.grid_purchase_sale')}
                  </h3>
                  <MetricTable metrics={gridMetrics} currency={currency} />
                </div>
              ) : null}
            </div>
            {demandMetrics.length > 0 ? (
              <div>
                <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, marginBottom: 'var(--ies-space-2)' }}>
                  {t('ies.result.demand_charge')}
                </h3>
                <MetricTable metrics={demandMetrics} currency={currency} />
              </div>
            ) : null}
          </Card>
        </>
      ) : null}

      {/* 计算步序列(横轴为步序号, 按单位分组): 成功/合法缺席/读取失败/加载四态。
          按曲线自身状态独立展示, 不依赖指标摘要 — 封存链顶层 metrics_summary
          可合法为 null, 而独立逐时引用与读取已有真实数据; 摘要缺席不遮蔽曲线。
          失败不清财务摘要, 重试仅重读曲线。成功但无可入图数据与合法缺席同展示;
          无选中/无结果/主结果读取失败时曲线各态均为空, 不显示本区块。 */}
      {hourly || hourlyAbsent || hourlyError || hourlyLoading ? (
        hourlyGroups.length > 0 ? (
          <Card title={t('ies.result.hourly_chart')} className="ies-section">
            <p style={{ fontSize: 'var(--ies-fs-xs)', color: 'var(--ies-color-text-secondary)', marginBottom: 'var(--ies-space-2)' }}>
              {t('ies.result.hourly_note', { n: hourlyPointCount })}
            </p>
            {hourlyGroups.map((group) => (
              <div className="ies-section" key={group.unit}>
                <LineChart
                  series={group.series}
                  ariaLabel={`${t('ies.result.hourly_chart')} (${group.unit})`}
                  yLabel={group.unit}
                  xLabel={t('ies.result.hourly_step_axis')}
                />
              </div>
            ))}
          </Card>
        ) : (
          <Card title={t('ies.result.hourly_chart')} className="ies-section">
            {hourlyError ? (
              <>
                <Alert variant="error">{hourlyError}</Alert>
                <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)', marginTop: 'var(--ies-space-2)' }}>
                  {t('ies.result.hourly_failed')}
                </p>
                <div style={{ marginTop: 'var(--ies-space-2)' }}>
                  <Button onClick={() => void retryHourly()} disabled={hourlyLoading}>
                    {t('ies.common.retry')}
                  </Button>
                </div>
              </>
            ) : hourlyAbsent || (hourly && !hourlyLoading) ? (
              <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
                {t('ies.result.hourly_absent')}
              </p>
            ) : (
              <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
                {t('ies.result.hourly_loading')}
              </p>
            )}
          </Card>
        )
      ) : null}

      {/* 可靠性统计 */}
      {relMetrics.length > 0 ? (
        <Card title={t('ies.result.dimension_reliability')} className="ies-section">
          <MetricTable metrics={relMetrics} currency={currency} />
          <p style={{ fontSize: 'var(--ies-fs-xs)', color: 'var(--ies-color-text-secondary)', marginTop: 'var(--ies-space-2)' }}>
            {t('ies.result.rel_note')}
          </p>
        </Card>
      ) : null}

      {/* 候选方案(真实候选列表; 选择只记录采用, 不创建版本)。
          读取失败时不渲染空态, 失败由错误条呈现, 不伪装成暂无候选。 */}
      {!resultError ? (
      <Card
        title={t('ies.result.candidates_title')}
        actions={
          candidates.length > 0 && selectedCandidate ? (
            <Button icon="check" onClick={() => setApplyOpen(true)}>
              {t('ies.result.select_candidate_btn')}
            </Button>
          ) : undefined
        }
        className="ies-section"
      >
        {candidates.length === 0 ? (
          <EmptyState
            icon="question"
            title={t('ies.result.candidates_empty')}
            description={t('ies.result.select_candidate')}
          />
        ) : (
          <ul className="ies-cand-list">
            {candidates.map((c) => (
              <li
                key={c.index}
                className={`ies-cand-row ${
                  selectedCandidateId === String(c.index) ? 'ies-cand-row--selected' : ''
                }`}
              >
                <button
                  type="button"
                  className="ies-btn ies-btn--ghost ies-btn--sm"
                  style={{ padding: 0, border: 0, background: 'transparent', minHeight: 0 }}
                  onClick={() => setSelectedCandidateId(String(c.index))}
                  aria-pressed={selectedCandidateId === String(c.index)}
                >
                  #{c.index}
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>
      ) : null}

      {/* 规划解参数预览(封存解 × 冻结规划绑定; 无解不伪造候选)。
          读取失败时不渲染空态, 不伪装成没有解。 */}
      {!resultError ? (
      <Card
        title={t('ies.result.plan_solution_title')}
        className="ies-section"
        actions={
          planSolution && planSolution.params.length > 0 ? (
            <Button onClick={() => void openSchemeDialog()}>
              {t('ies.result.apply_scheme_btn')}
            </Button>
          ) : undefined
        }
      >
        {planSolution ? (
          <>
            <Table>
              <THead>
                <TR>
                  <TH>{t('ies.result.plan_solution_variable')}</TH>
                  <TH>{t('ies.result.plan_solution_device')}</TH>
                  <TH>{t('ies.result.plan_solution_parameter')}</TH>
                  <TH>{t('ies.result.plan_solution_value')}</TH>
                </TR>
              </THead>
              <TBody>
                {planSolution.params.map((p) => (
                  <TR key={p.variable}>
                    <TD>{p.variable}</TD>
                    <TD>{p.device_ref}</TD>
                    <TD>{p.parameter}</TD>
                    <TD>{`${formatNumber(p.value, { digits: 4 })} ${p.unit}`}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
            <p style={{ fontSize: 'var(--ies-fs-xs)', color: 'var(--ies-color-text-secondary)' }}>
              {t('ies.result.plan_solution_refs', {
                solution: planSolution.solution_id,
                task: planSolution.refs.task_id,
                snapshot: planSolution.refs.calc_snapshot_id,
                version: planSolution.refs.project_version_id,
                evidence: planSolution.refs.evidence_package_id,
              })}
            </p>
          </>
        ) : (
          <EmptyState icon="info" title={t('ies.result.plan_solution_empty')} />
        )}
      </Card>
      ) : null}

      {/* 选择成功提示(仅记录采用, 不创建版本) */}
      {applyOk ? (
        <div className="ies-section">
          <Alert variant="success" closable onClose={() => setApplyOk(false)}>
            {t('ies.result.select_ok')}
          </Alert>
        </div>
      ) : null}

      {/* 应用方案成功与刷新失败分开报告(不重复应用) */}
      {schemeOk ? (
        <div className="ies-section">
          <Alert variant="success" closable onClose={() => setSchemeOk(null)}>
            {t('ies.result.apply_scheme_ok', { version: schemeOk })}
          </Alert>
        </div>
      ) : null}
      {schemeRefreshError ? (
        <div className="ies-section">
          <Alert variant="error">{t('ies.result.apply_scheme_refresh_failed', { reason: schemeRefreshError })}</Alert>
        </div>
      ) : null}

      {/* 评估历史 + 导出 */}
      <div className="ies-grid ies-grid--cols-2">
        <Card
          title={t('ies.result.assessment_history')}
          actions={
            selectedPkgId ? (
              <Button
                variant="secondary"
                onClick={() => {
                  setAssessError(null)
                  setAssessOpen(true)
                }}
              >
                {t('ies.result.reassess')}
              </Button>
            ) : undefined
          }
          className="ies-section"
        >
          <p style={{ fontSize: 'var(--ies-fs-xs)', color: 'var(--ies-color-text-secondary)', marginBottom: 'var(--ies-space-3)' }}>
            {t('ies.result.assessment_history_note')}
          </p>
          {assessments.length === 0 ? (
            <EmptyState icon="info" title={t('ies.result.no_assessments')} />
          ) : (
            <ul className="ies-assess-list">
              {assessments.map((a) => (
                <li key={a.id} className="ies-assess-item">
                  <div className="ies-assess-item__head">
                    <span>
                      {a.assessor === 'system' ? t('ies.result.assessor_system') : t('ies.result.assessor_human')}
                      {a.overall_score !== null && a.overall_score !== undefined
                        ? ` · ${t('ies.result.overall_score')} ${formatNumber(a.overall_score, { digits: 1 })}`
                        : ''}
                    </span>
                    <time>{formatDateTime(a.created_at)}</time>
                  </div>
                  <div className="ies-assess-item__grades">
                    {DIMENSIONS.map((d) => (
                      <Badge
                        key={d.key}
                        variant={
                          a[`dimension_${d.key}`] === 'pass'
                            ? 'success'
                            : a[`dimension_${d.key}`] === 'fail'
                              ? 'danger'
                              : 'neutral'
                        }
                        icon={a[`dimension_${d.key}`] === 'pass' ? 'check' : a[`dimension_${d.key}`] === 'fail' ? 'cross' : 'question'}
                        label={t(d.labelKey)}
                      />
                    ))}
                  </div>
                  {a.comment ? <p className="ies-assess-item__comment">{a.comment}</p> : null}
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title={t('ies.result.export_section')} className="ies-section">
          <div className="ies-flex" style={{ alignItems: 'flex-end', marginBottom: 'var(--ies-space-3)' }}>
            <FormField label={t('ies.result.export_lang')} htmlFor="export-lang">
              <Select
                id="export-lang"
                value={exportLang}
                onChange={(e) => setExportLang(e.target.value as Locale)}
                style={{ maxWidth: 180 }}
              >
                <option value="zh">简体中文</option>
                <option value="en">English</option>
              </Select>
            </FormField>
            <Button
              icon="download"
              loading={exportBusy === 'excel'}
              disabled={!selectedPkgId}
              onClick={() => void exportExcel()}
            >
              {t('ies.result.export_excel_btn')}
            </Button>
          </div>
          <div className="ies-flex">
            <Button
              variant="secondary"
              icon="download"
              loading={exportBusy === 'package'}
              onClick={() => void exportPackage()}
            >
              {t('ies.result.export_package_btn')}
            </Button>
            {!isOwner ? (
              <span style={{ fontSize: 'var(--ies-fs-xs)', color: 'var(--ies-color-text-secondary)' }}>
                {t('ies.result.export_package_owner_only')}
              </span>
            ) : null}
          </div>
          {exportError ? (
            <div style={{ marginTop: 'var(--ies-space-3)' }}>
              <Alert variant="error">{exportError}</Alert>
            </div>
          ) : null}
          {exportNeedsAssess ? (
            <div style={{ marginTop: 'var(--ies-space-3)' }}>
              <Alert variant="warning">{t('ies.result.export_need_assessment')}</Alert>
              <div style={{ marginTop: 'var(--ies-space-2)' }}>
                <Button variant="secondary" onClick={() => setAssessOpen(true)}>
                  {t('ies.result.reassess')}
                </Button>
              </div>
            </div>
          ) : null}
          {exportOk ? (
            <div style={{ marginTop: 'var(--ies-space-3)' }}>
              <Alert variant="success" closable onClose={() => setExportOk(false)}>
                {t('ies.result.export_ok')}
              </Alert>
            </div>
          ) : null}
        </Card>
      </div>

      {/* 选择候选确认(仅记录采用, 不创建版本) */}
      <Dialog
        open={applyOpen}
        onClose={() => setApplyOpen(false)}
        title={t('ies.result.select_candidate_btn')}
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setApplyOpen(false)}>
              {t('ies.common.cancel')}
            </Button>
            <Button loading={applying} onClick={() => void applyCandidate()}>
              {t('ies.common.confirm')}
            </Button>
          </>
        }
      >
        <Alert variant="warning" title={t('ies.result.select_candidate_confirm')}>
          {selectedCandidate ? `#${selectedCandidate.index}` : ''}
        </Alert>
        {applyError ? (
          <div style={{ marginTop: 'var(--ies-space-3)' }}>
            <Alert variant="error">{applyError}</Alert>
          </div>
        ) : null}
      </Dialog>

      {/* 应用方案确认(来源版本基线派生新版本; 服务端权威解析, 取消不请求;
          写请求飞行中禁用关闭/取消, 证据选择同时禁用) */}
      <Dialog
        open={schemeOpen}
        onClose={() => {
          if (!schemeBusy) setSchemeOpen(false)
        }}
        title={t('ies.result.apply_scheme_title')}
        size="md"
        footer={
          <>
            <Button
              variant="secondary"
              disabled={schemeBusy}
              onClick={() => setSchemeOpen(false)}
            >
              {t('ies.common.cancel')}
            </Button>
            <Button
              loading={schemeBusy || schemeRevLoading}
              disabled={schemeRevision === null}
              onClick={() => void confirmScheme()}
            >
              {t('ies.common.confirm')}
            </Button>
          </>
        }
      >
        {planSolution ? (
          <>
            <p className="ies-muted">
              {t('ies.result.apply_scheme_source')}:{' '}
              {snapSources?.projectVersion ?? `id:${planSolution.refs.project_version_id}`}
            </p>
            <p style={{ fontSize: 'var(--ies-fs-xs)', color: 'var(--ies-color-text-secondary)' }}>
              {schemeRevLoading
                ? t('ies.result.apply_scheme_revision_loading')
                : t('ies.result.apply_scheme_revision', { revision: schemeRevision ?? '—' })}
            </p>
            {schemeRevisionError ? (
              <div style={{ marginTop: 'var(--ies-space-2)' }}>
                <Alert variant="error">{schemeRevisionError}</Alert>
              </div>
            ) : null}
            <p style={{ fontSize: 'var(--ies-fs-xs)', color: 'var(--ies-color-text-secondary)' }}>
              {t('ies.result.plan_solution_refs', {
                solution: planSolution.solution_id,
                task: planSolution.refs.task_id,
                snapshot: planSolution.refs.calc_snapshot_id,
                version: planSolution.refs.project_version_id,
                evidence: planSolution.refs.evidence_package_id,
              })}
            </p>
            <p className="ies-muted">{t('ies.result.apply_scheme_params')}:</p>
            <ul className="ies-diag-list" style={{ marginTop: 'var(--ies-space-2)' }}>
              {planSolution.params.map((p) => (
                <li key={p.variable}>
                  {p.variable}: {p.device_ref}.{p.parameter} = {`${formatNumber(p.value, { digits: 4 })} ${p.unit}`}
                </li>
              ))}
            </ul>
            <Alert variant="warning" title={t('ies.result.apply_scheme_impact_title')}>
              {t('ies.result.apply_scheme_impact', {
                version: snapSources?.projectVersion ?? `id:${planSolution.refs.project_version_id}`,
              })}
            </Alert>
          </>
        ) : null}
        {schemeError ? (
          <div style={{ marginTop: 'var(--ies-space-3)' }}>
            <Alert variant="error">{schemeError}</Alert>
          </div>
        ) : null}
      </Dialog>

      {/* 重新运行系统评估对话框(FE-BE-03: 只触发系统四维检查,
          不支持人工评分持久化, 移除无法落库的评分/评论输入) */}
      <Dialog
        open={assessOpen}
        onClose={() => setAssessOpen(false)}
        title={t('ies.result.assess_dialog_title')}
        size="md"
        footer={
          <>
            <Button variant="secondary" onClick={() => setAssessOpen(false)}>
              {t('ies.common.cancel')}
            </Button>
            <Button loading={assessBusy} onClick={() => void submitAssessment()}>
              {t('ies.result.assess_run')}
            </Button>
          </>
        }
      >
        <p className="ies-muted">{t('ies.result.assess_rerun_hint')}</p>
        {assessError ? <Alert variant="error">{assessError}</Alert> : null}
        {assessOk ? (
          <Alert variant="success" closable onClose={() => setAssessOk(false)}>
            {t('ies.result.assessment_created')}
          </Alert>
        ) : null}
      </Dialog>
    </div>
  )
}

// ---------------------------------------------------------------------------
// 子组件
// ---------------------------------------------------------------------------

/** 指标表(名称 / 数值)。 */
function MetricTable({ metrics, currency }: { metrics: MetricValue[]; currency: Currency }) {
  return (
    <table className="ies-metric-table">
      <tbody>
        {metrics.map((m) => (
          <tr key={m.id}>
            <td>{translate(m.label_key)}</td>
            <td>{formatMetricValue(m, currency)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** 财务指标组:IRR 状态 + 关键指标。 */
function MetricGroup({
  metrics,
  currency,
  labels,
  irrKey,
}: {
  metrics: MetricValue[]
  currency: Currency
  labels: { irr: string; irrStatus: string }
  irrKey: IrrStatusKey | null
}) {
  const { t } = useI18n()
  const irrMetric = metrics.find((m) => /irr/i.test(m.id) && !/status/i.test(m.id)) ?? null
  const others = metrics.filter((m) => m !== irrMetric)
  return (
    <>
      <dl className="ies-kv">
        <dt>{labels.irr}</dt>
        <dd>{irrMetric ? formatMetricValue(irrMetric, currency) : '—'}</dd>
        {irrKey ? (
          <>
            <dt>{labels.irrStatus}</dt>
            <dd>
              <Badge variant={irrKey === 'unique' ? 'success' : 'warning'} icon={irrKey === 'unique' ? 'check' : 'warning'} label={t(`ies.result.irr_${irrKey}`)} />
            </dd>
          </>
        ) : null}
      </dl>
      {others.length > 0 ? (
        <div style={{ marginTop: 'var(--ies-space-3)' }}>
          <MetricTable metrics={others} currency={currency} />
        </div>
      ) : null}
    </>
  )
}

/** 已封存财务评价卡: 只展示封存事实, 不重算、不补默认折现率。金额为封存字符串原样。 */
function FinancialEvaluationCard({ fin }: { fin: FinancialEvaluationBlock }) {
  const { t } = useI18n()
  const ccy = fin.currency
  const ev = fin.evaluation
  const rows = ev.rows
  const payments = fin.payments_applied
  const annual = fin.annual_operation
  const irrKey: IrrStatusKey | null = irrStatusToKey(ev.irr_status)
  const payback = (prefix: 'static' | 'dynamic'): { value: number | null; status: string } =>
    prefix === 'static'
      ? { value: ev.static_payback, status: ev.static_status }
      : { value: ev.dynamic_payback, status: ev.dynamic_status }
  const staticPb = payback('static')
  const dynamicPb = payback('dynamic')
  return (
    <Card title={t('ies.result.fin_sealed_title')} className="ies-section">
      <dl className="ies-kv">
        <dt>{t('ies.result.fin_e')}</dt>
        <dd>{fin.evaluation_years}</dd>
        <dt>{t('ies.result.fin_k')}</dt>
        <dd>{fin.construction_period}</dd>
        <dt>{t('ies.result.fin_n')}</dt>
        <dd>{fin.operation_years}</dd>
        <dt>{t('ies.result.fin_construction_total')}</dt>
        <dd>
          {fin.construction_total} {fin.construction_total_unit}
        </dd>
        <dt>{t('ies.result.fin_discount')}</dt>
        <dd>{fin.discount_rate}</dd>
        <dt>{t('ies.result.fin_basis')}</dt>
        <dd>
          {fin.price_basis} / {t('ies.result.fin_base_year')}: {fin.base_year}
        </dd>
      </dl>
      <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
        {t('ies.result.fin_axis_note')}
      </p>
      {payments.length > 0 ? (
        <div style={{ marginTop: 'var(--ies-space-3)' }}>
          <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, marginBottom: 'var(--ies-space-2)' }}>
            {t('ies.result.fin_payments')}
          </h3>
          <table className="ies-metric-table">
            <tbody>
              {payments.map((p, index) => (
                <tr key={`${p.t}-${index}`}>
                  <td>
                    {t('ies.result.fin_t_payment', { year: p.t })}
                  </td>
                  <td>
                    {p.ratio} × C = {p.amount} {ccy}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {annual.length > 0 ? (
        <div style={{ marginTop: 'var(--ies-space-3)' }}>
          <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, marginBottom: 'var(--ies-space-2)' }}>
            {t('ies.result.fin_yearly')}
          </h3>
          <table className="ies-metric-table">
            <tbody>
              {annual.map((a, i) => (
                <tr key={a.financial_year}>
                  <td>{t('ies.result.fin_year_row', { j: i + 1, year: a.financial_year })}</td>
                  <td>
                    {a.energy_net} − {a.variable_om} − {a.fixed_om} − {a.maintenance} = {a.net} {ccy}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {fin.tax_cash ? (
        <TaxCashSections fin={fin} />
      ) : null}
      {fin.annual_net_flows && fin.annual_net_flows.length > 0 ? (
        <div style={{ marginTop: 'var(--ies-space-3)' }}>
          <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, marginBottom: 'var(--ies-space-2)' }}>
            {t('ies.result.fin_tax_flows')}
          </h3>
          <table className="ies-metric-table">
            <tbody>
              {fin.annual_net_flows.map((value, tIndex) => (
                <tr key={tIndex}>
                  <td>CF[t={tIndex}]</td>
                  <td>
                    {value} {ccy}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {rows.length > 0 ? (
        <div style={{ marginTop: 'var(--ies-space-3)' }}>
          <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, marginBottom: 'var(--ies-space-2)' }}>
            {t('ies.result.fin_cf_table')}
          </h3>
          <table className="ies-metric-table">
            <tbody>
              {rows.map((r) => (
                <tr key={r.year}>
                  <td>CF[t={r.year}]</td>
                  <td>
                    {r.net} / {r.discounted} / {r.cumulative} / {r.cumulative_discounted} {ccy}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <div style={{ marginTop: 'var(--ies-space-3)' }}>
        <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, marginBottom: 'var(--ies-space-2)' }}>
          {t('ies.result.fin_metrics')}
        </h3>
        <dl className="ies-kv">
          <dt>NPV</dt>
          <dd>
            {ev.npv} {ev.npv_unit}
          </dd>
          <dt>{t('ies.result.irr')}</dt>
          <dd>{ev.irr ?? '—'}</dd>
          <dt>{t(IRR_STATUS_KEY)}</dt>
          <dd>
            {irrKey ? (
              <Badge
                variant={irrKey === 'unique' ? 'success' : 'warning'}
                icon={irrKey === 'unique' ? 'check' : 'warning'}
                label={t(`ies.result.irr_${irrKey}`)}
              />
            ) : (
              '—'
            )}
          </dd>
          <dt>{t('ies.result.fin_payback_static')}</dt>
          <dd>
            {staticPb.value ?? '—'} {staticPb.value !== null ? t('ies.result.fin_year_unit') : ''}{' '}
            {t(`ies.result.fin_pb_${staticPb.status}`)}
          </dd>
          <dt>{t('ies.result.fin_payback_dynamic')}</dt>
          <dd>
            {dynamicPb.value ?? '—'} {dynamicPb.value !== null ? t('ies.result.fin_year_unit') : ''}{' '}
            {t(`ies.result.fin_pb_${dynamicPb.status}`)}
          </dd>
        </dl>
      </div>
    </Card>
  )
}

/** 同次解税务事实展示(封存原样, 金额裸字符串只展示不重算)。 */
function TaxCashSections({ fin }: { fin: FinancialEvaluationBlock }) {
  const { t } = useI18n()
  const ccy = fin.currency
  const tax = fin.tax_cash
  if (!tax) return null
  const horizonMonths = fin.evaluation_years * 12
  const sectionTitle = { fontSize: 'var(--ies-fs-sm)', fontWeight: 700, marginBottom: 'var(--ies-space-2)' } as const
  const sectionGap = { marginTop: 'var(--ies-space-3)' } as const
  const noneText = t('ies.result.fin_tax_none')
  return (
    <>
      <div style={sectionGap}>
        <h3 style={sectionTitle}>{t('ies.result.fin_tax_vat')}</h3>
        {tax.vat.length > 0 ? (
          <table className="ies-metric-table">
            <tbody>
              {tax.vat.map((row, index) => (
                <tr key={`${row.pool}-${row.month}-${index}`}>
                  <td>
                    {row.pool} / m={row.month}
                  </td>
                  <td>
                    {t('ies.result.fin_tax_paid')}: {row.paid} / {t('ies.result.fin_tax_claimed')}: {row.refund_claimed} /{' '}
                    {t('ies.result.fin_tax_arrived')}: {row.refund_arrived} / {t('ies.result.fin_tax_carry')}: {row.carry} {ccy}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>{noneText}</p>
        )}
      </div>
      <div style={sectionGap}>
        <h3 style={sectionTitle}>{t('ies.result.fin_tax_refunds')}</h3>
        {tax.refunds.length > 0 ? (
          <table className="ies-metric-table">
            <tbody>
              {tax.refunds.map((row, index) => (
                <tr key={`${row.pool}-${row.period}-${index}`}>
                  <td>
                    {row.pool} / {t('ies.result.fin_tax_period', { p: row.period })} →{' '}
                    {t('ies.result.fin_tax_arrival', { p: row.arrival_period })}
                    {row.arrival_period > horizonMonths ? t('ies.result.fin_tax_post_receivable') : ''}
                  </td>
                  <td>
                    {row.amount} {ccy}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>{noneText}</p>
        )}
      </div>
      <div style={sectionGap}>
        <h3 style={sectionTitle}>{t('ies.result.fin_tax_income')}</h3>
        {tax.income.length > 0 ? (
          <table className="ies-metric-table">
            <tbody>
              {tax.income.map((row, index) => (
                <tr key={`${row.rule}-${row.year}-${index}`}>
                  <td>
                    {row.rule} / y={row.year}
                  </td>
                  <td>
                    {t('ies.result.fin_tax_base')}: {row.taxable_base} / {t('ies.result.fin_tax_used')}: {row.loss_used} /{' '}
                    {t('ies.result.fin_tax_generated')}: {row.loss_generated} / {t('ies.result.fin_tax_expired')}:{' '}
                    {row.loss_expired} / {t('ies.result.fin_tax_tax')}: {row.tax} {ccy}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>{noneText}</p>
        )}
      </div>
      <div style={sectionGap}>
        <h3 style={sectionTitle}>{t('ies.result.fin_tax_depreciation')}</h3>
        {tax.depreciation.length > 0 ? (
          <table className="ies-metric-table">
            <tbody>
              {tax.depreciation.map((row, index) => (
                <tr key={`${row.rule}-${row.asset}-${row.year}-${index}`}>
                  <td>
                    {row.rule} / {row.asset} / y={row.year}
                  </td>
                  <td>
                    {row.amount} {ccy}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>{noneText}</p>
        )}
        <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
          {t('ies.result.fin_tax_depreciation_note')}
        </p>
      </div>
      <div style={sectionGap}>
        <h3 style={sectionTitle}>{t('ies.result.fin_tax_balances')}</h3>
        {tax.loss_balances.length > 0 ? (
          <table className="ies-metric-table">
            <tbody>
              {tax.loss_balances.map((row, index) => (
                <tr key={`${row.rule}-${row.vintage_id}-${row.year}-${index}`}>
                  <td>
                    {row.rule} / {row.vintage_id} / y={row.year} / {t('ies.result.fin_tax_last_usable', { y: row.last_usable_year })}
                  </td>
                  <td>
                    {row.amount} {ccy}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>{noneText}</p>
        )}
      </div>
    </>
  )
}
