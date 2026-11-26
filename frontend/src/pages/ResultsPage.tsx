/**
 * 结果分析(ResultsPage)。
 *
 * 采用「顶层摘要 + 五分区 Tab + 一级图表可视化 / 二级分页明细页」架构：
 * - 总览(overview): 求解状态、四维结论、核心财务评价与趋势预览、规划解预览、建设决策、候选方案；
 * - 财务与现金流(financial): 评价现金流组合图、运营年度分量图、建设付款与净现金项、基准比较，支持下钻二级明细表；
 * - 过程与税务(tax): 需量账单、增值税、所得税与折旧、命名成本/金额、具名收付与资产图表，全部支持下钻二级筛选分页表；
 * - 工程与曲线(engineering): 计算步曲线(支持图例开关与窗口翻页)、步序列原始数据表及工程/环境/可靠性指标；
 * - 评估与导出(assessment): 评估历史(支持分页)、重新评估、Excel / 项目包导出。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'

import { ComboChart } from '../components/charts'
import {
  Alert,
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  FormField,
  Pagination,
  Select,
  Spinner,
  Table,
  Tabs,
  TaskOutcomeBadge,
  TaskStatusBadge,
  TBody,
  TD,
  TH,
  THead,
  TR,
} from '../components/ui'
import type { BadgeVariant, IconName, TabItem } from '../components/ui'
import { listDatasets, listVersions as listDatasetVersions } from '../features/datasets'
import { exportPackage as createPackageExport } from '../features/exports'
import { applyScheme, listVersions } from '../features/project-versions'
import { getProject } from '../features/projects'
import {
  assess,
  buildCashflowChartData,
  buildResultSearchParams,
  DETAIL_PARENT_TAB,
  EngineeringTabView,
  FinancialAbsenceAlert,
  FinancialTabView,
  formatEvaluatorDetail,
  getResult,
  listAssessments,
  paginateItems,
  parseResultNavigation,
  readHourly,
  ResultDetailView,
  retryEvaluator,
  SealedFinancialCore,
  select,
  TaxDetailsTabView,
} from '../features/results'
import { newSubmitNonce, stableHash } from '../features/tasks'
import type { HourlySeriesMeta, ResultDetail, ResultTab } from '../features/results'
import { exportTaskReport, isNoAssessmentError } from '../application/exportTaskReport'
import { translateError, useI18n } from '../i18n'
import type { Locale } from '../i18n'
import { formatDateTime, formatNumber, formatPercent } from '../lib/format'
import { downloadBlob } from '../shared/http'
import type {
  ApiError,
  AssessmentDimension,
  AssessmentGrade,
  Currency,
  EvaluationReportBlock,
  EvaluatorResultEntry,
  EvaluatorRunStatus,
  EvidenceCandidate,
  EvidencePackageSummary,
  ExcelExportInput,
  FinancialAbsence,
  FinancialEvaluationBlock,
  MetricValue,
  PlanningSolution,
  ResultAssessment,
  SnapshotRefs,
  SolverSummary,
  TaskOutcome,
} from '../types'
import { useEffectiveTaskContext, useWorkbench } from './workbench'

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

const EVAL_STATUS_META: Record<EvaluatorRunStatus, { variant: BadgeVariant; icon: IconName }> = {
  succeeded: { variant: 'success', icon: 'check' },
  failed: { variant: 'danger', icon: 'cross' },
  timed_out: { variant: 'danger', icon: 'cross' },
  cancelled: { variant: 'warning', icon: 'warning' },
  skipped: { variant: 'neutral', icon: 'info' },
}

const TAB_LABEL_KEYS: Record<ResultTab, string> = {
  overview: 'ies.result.tab_overview',
  financial: 'ies.result.tab_financial',
  tax: 'ies.result.tab_tax',
  engineering: 'ies.result.tab_engineering',
  assessment: 'ies.result.tab_assessment',
}

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

export default function ResultsPage() {
  const { t, locale } = useI18n()
  const [searchParams, setSearchParams] = useSearchParams()
  const { projectId, project, refreshDetailed, selectTaskWithDraft, draftLoadingTaskId } = useWorkbench()
  const currency: Currency = project?.currency ?? 'CNY'

  // 一级 Tab 与二级明细页 URL 状态（唯一导航真相源）
  const nav = useMemo(() => parseResultNavigation(searchParams), [searchParams])
  const activeTab = nav.tab
  const activeDetail = nav.detail

  const handleTabChange = useCallback(
    (nextTab: ResultTab) => {
      setSearchParams((prev) => buildResultSearchParams(prev, { tab: nextTab, detail: null }))
    },
    [setSearchParams],
  )

  const handleOpenDetail = useCallback(
    (detail: ResultDetail) => {
      setSearchParams((prev) =>
        buildResultSearchParams(prev, { tab: DETAIL_PARENT_TAB[detail], detail }),
      )
    },
    [setSearchParams],
  )

  const handleBackFromDetail = useCallback(() => {
    const parentTab = activeDetail ? DETAIL_PARENT_TAB[activeDetail] : activeTab
    setSearchParams((prev) => buildResultSearchParams(prev, { tab: parentTab, detail: null }))
  }, [activeDetail, activeTab, setSearchParams])

  // URL ?package= 直达参数(仅首次读取)
  const [initialPkgParam] = useState<number | null>(() => {
    const v = Number(new URLSearchParams(window.location.search).get('package'))
    return Number.isFinite(v) && v > 0 ? v : null
  })
  const [linkTaskId, setLinkTaskId] = useState<number | null>(() => (initialPkgParam !== null ? -1 : null))
  const [linkDone, setLinkDone] = useState(false)

  const taskCtx = useEffectiveTaskContext(initialPkgParam !== null && !linkDone ? linkTaskId : undefined)
  const { loading, loadError } = { loading: taskCtx.loading, loadError: taskCtx.error }

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
  const pickedTaskId = taskCtx.picked ? taskCtx.picked.taskId : 0
  const explicitNoResult = !taskCtx.loading && taskCtx.explicitBlocked

  // 选中证据包的结果数据
  const [metrics, setMetrics] = useState<Record<string, MetricValue> | null>(null)
  const [solverSummary, setSolverSummary] = useState<SolverSummary | null>(null)
  const [finEval, setFinEval] = useState<FinancialEvaluationBlock | null>(null)
  const [finAbsence, setFinAbsence] = useState<FinancialAbsence>(null)
  const [evaluatorResults, setEvaluatorResults] = useState<EvaluatorResultEntry[]>([])
  const [evaluationReports, setEvaluationReports] = useState<EvaluationReportBlock[]>([])
  const [retryingEvaluator, setRetryingEvaluator] = useState<string | null>(null)
  const [evalRetryOk, setEvalRetryOk] = useState<string | null>(null)
  const [evalRetryError, setEvalRetryError] = useState<string | null>(null)
  const [noEvidence, setNoEvidence] = useState(false)
  const [assessments, setAssessments] = useState<ResultAssessment[]>([])
  const [assessPage, setAssessPage] = useState(1)
  const [assessPageSize, setAssessPageSize] = useState(10)
  const [hourly, setHourly] = useState<{
    flows: Record<string, number[]>
    series: HourlySeriesMeta[]
    start: number
    end: number
    totalRows: number
  } | null>(null)
  const [hourlyJump, setHourlyJump] = useState('0')
  const [hourlyAbsent, setHourlyAbsent] = useState(false)
  const [hourlyError, setHourlyError] = useState<string | null>(null)
  const [hourlyLoading, setHourlyLoading] = useState(false)
  const [resultError, setResultError] = useState<string | null>(null)

  const resultErrorTaskRef = useRef<number | null>(null)
  const clearResultErrorFor = useCallback((incomingTaskId: number | null): void => {
    if (resultErrorTaskRef.current !== null && resultErrorTaskRef.current === incomingTaskId) return
    resultErrorTaskRef.current = null
    setResultError(null)
  }, [])

  const [candList, setCandList] = useState<EvidenceCandidate[]>([])
  const [planSolution, setPlanSolution] = useState<PlanningSolution | null>(null)
  const [snapRefs, setSnapRefs] = useState<SnapshotRefs | null>(null)
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

  // 方案应用
  const [schemeOpen, setSchemeOpen] = useState(false)
  const [schemeBusy, setSchemeBusy] = useState(false)
  const [schemeError, setSchemeError] = useState<string | null>(null)
  const [schemeOk, setSchemeOk] = useState<string | null>(null)
  const [schemeRefreshError, setSchemeRefreshError] = useState<string | null>(null)
  const [schemeRevision, setSchemeRevision] = useState<number | null>(null)
  const [schemeRevisionError, setSchemeRevisionError] = useState<string | null>(null)
  const [schemeRevLoading, setSchemeRevLoading] = useState(false)
  const schemeReadSeq = useRef(0)
  const schemeFlight = useRef(0)
  const selectedPkgRef = useRef<number | null>(null)
  selectedPkgRef.current = selectedPkgId
  const hourlySeq = useRef(0)
  const hourlyAbort = useRef<AbortController | null>(null)

  // 重新评估
  const [assessOpen, setAssessOpen] = useState(false)
  const [assessBusy, setAssessBusy] = useState(false)
  const [assessError, setAssessError] = useState<string | null>(null)
  const [assessOk, setAssessOk] = useState(false)

  // 导出
  const [exportLang, setExportLang] = useState<Locale>(locale)
  const [exportBusy, setExportBusy] = useState<'excel' | 'package' | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const [exportOk, setExportOk] = useState(false)
  const [exportNeedsAssess, setExportNeedsAssess] = useState(false)

  // ?package= 直达初始化
  const linkAttemptRef = useRef<number | null>(null)
  useEffect(() => {
    if (initialPkgParam === null || linkDone || taskCtx.loading || taskCtx.error) return
    if (linkAttemptRef.current !== null) return
    const hit = taskCtx.history.find((tk) => tk.latest_package_id === initialPkgParam) ?? null
    if (hit === null) {
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
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          next.delete('package')
          return next
        },
        { replace: true },
      )
    })
  }, [initialPkgParam, linkDone, taskCtx.loading, taskCtx.error, taskCtx.history, selectTaskWithDraft, setSearchParams])

  // 加载选中证据包的结果、评估与逐时曲线
  useEffect(() => {
    hourlyAbort.current?.abort()
    setAssessPage(1)
    if (!selectedPkgId) {
      hourlySeq.current += 1
      setMetrics(null)
      setFinEval(null)
      setFinAbsence(null)
      setEvaluatorResults([])
      setEvaluationReports([])
      setRetryingEvaluator(null)
      setEvalRetryOk(null)
      setEvalRetryError(null)
      setNoEvidence(false)
      setAssessments([])
      setHourly(null)
      setHourlyAbsent(false)
      setHourlyError(null)
      setHourlyLoading(false)
      clearResultErrorFor(null)
      setCandList([])
      setPlanSolution(null)
      setSolverSummary(null)
      setSnapRefs(null)
      setSnapSources(null)
      return
    }
    setMetrics(null)
    setFinEval(null)
    setFinAbsence(null)
    setEvaluatorResults([])
    setEvaluationReports([])
    setRetryingEvaluator(null)
    setEvalRetryOk(null)
    setEvalRetryError(null)
    setNoEvidence(false)
    setAssessments([])
    setHourly(null)
    setHourlyAbsent(false)
    setHourlyError(null)
    setHourlyLoading(false)
    clearResultErrorFor(pickedTaskId === 0 ? null : pickedTaskId)
    setCandList([])
    setPlanSolution(null)
    setSolverSummary(null)
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
    const controller = new AbortController()
    hourlyAbort.current = controller
    const load = async () => {
      const taskId = pickedTaskId
      try {
        const [res, ass] = await Promise.all([
          getResult(projectId, taskId, controller.signal),
          listAssessments(projectId, taskId, controller.signal),
        ])
        if (cancelled) return
        setMetrics(res.metrics)
        setFinEval(res.financial)
        setFinAbsence(res.financialAbsence)
        setEvaluatorResults(res.evaluatorResults)
        setEvaluationReports(res.evaluationReports)
        setNoEvidence(res.evidence_status === 'no_evidence')
        setAssessments([...ass].sort((a, b) => b.created_at.localeCompare(a.created_at)))
        setCandList(res.candidates)
        setPlanSolution(res.planningSolution)
        setSolverSummary(res.solverSummary)
        setSnapRefs(res.snapshotRefs)
        clearResultErrorFor(taskId)

        const mySeq = hourlySeq.current + 1
        hourlySeq.current = mySeq
        setHourlyLoading(true)
        try {
          const hr = await readHourly(projectId, taskId, { start: 0, limit: 1000 }, controller.signal)
          if (cancelled || hourlySeq.current !== mySeq) return
          if (hr.absent) {
            setHourly(null)
            setHourlyAbsent(true)
          } else {
            setHourly(hr)
            setHourlyJump(String(hr.start))
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
          setEvaluatorResults([])
          setEvaluationReports([])
          setCandList([])
          setPlanSolution(null)
          setSolverSummary(null)
          setSnapRefs(null)
          setSelectedCandidateId(null)
          setApplyOpen(false)
          setSchemeOpen(false)
          setSchemeError(null)
          setSchemeOk(null)
          setSchemeRefreshError(null)
          setSchemeRevLoading(false)
          schemeReadSeq.current += 1
          resultErrorTaskRef.current = taskId
          setResultError(translateError(err as ApiError))
        }
      }
    }
    void load()
    return () => {
      cancelled = true
      controller.abort()
      hourlyAbort.current?.abort()
      hourlySeq.current += 1
    }
  }, [selectedPkgId])

  const retryHourly = useCallback(
    async (start = hourly?.start ?? 0) => {
      hourlyAbort.current?.abort()
      const controller = new AbortController()
      hourlyAbort.current = controller
      const taskId = pickedTaskId
      const mySeq = hourlySeq.current + 1
      hourlySeq.current = mySeq
      setHourlyError(null)
      setHourlyLoading(true)
      try {
        const hr = await readHourly(projectId, taskId, { start, limit: 1000 }, controller.signal)
        if (hourlySeq.current !== mySeq) return
        if (hr.absent) {
          setHourly(null)
          setHourlyAbsent(true)
        } else {
          setHourly(hr)
          setHourlyJump(String(hr.start))
          setHourlyAbsent(false)
        }
      } catch (err) {
        if (hourlySeq.current !== mySeq) return
        setHourlyError(translateError(err as ApiError))
      } finally {
        if (hourlySeq.current === mySeq) setHourlyLoading(false)
      }
    },
    [pickedTaskId, projectId, selectedPkgId, hourly?.start],
  )

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

  const selectedEntry = taskCtx.details.find((d) => d.id === (taskCtx.picked?.taskId ?? -1)) ?? null
  const outcome = selectedEntry?.business_outcome ?? null
  const snapshot = selectedEntry?.calc_snapshot ?? null
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

  const candidates = useMemo(
    () => [...candList].sort((a, b) => a.index - b.index),
    [candList],
  )

  const selectedCandidate = useMemo(() => {
    if (!selectedCandidateId) return null
    const idx = candidates.findIndex((c) => c.index === Number(selectedCandidateId))
    return idx >= 0 ? candidates[idx] : null
  }, [selectedCandidateId, candidates])

  const applyCandidate = useCallback(async () => {
    if (!selectedCandidate) return
    setApplyError(null)
    setApplyOk(false)
    setApplying(true)
    try {
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

  const confirmScheme = useCallback(async () => {
    if (!planSolution || schemeBusy || schemeRevLoading) return
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

  const handleRetryEvaluator = useCallback(
    async (evaluatorRef: string) => {
      if (!pickedTaskId || retryingEvaluator !== null) return
      setEvalRetryOk(null)
      setEvalRetryError(null)
      setRetryingEvaluator(evaluatorRef)
      try {
        const key = `eval-retry-${pickedTaskId}-${stableHash(evaluatorRef)}-${newSubmitNonce()}`
        const res = await retryEvaluator(projectId, pickedTaskId, {
          evaluator: evaluatorRef,
          idempotency_key: key,
        })
        setEvalRetryOk(res.hint ?? t('ies.result.evaluator_retry_ok', { evaluator: evaluatorRef }))
        // 独立子 analysis 重试任务不重开原任务；保留 pickedTaskId 与当前结果页成功提示
        if (res.task.id === pickedTaskId) taskCtx.retry()
      } catch (err) {
        setEvalRetryError(translateError(err as ApiError))
      } finally {
        setRetryingEvaluator(null)
      }
    },
    [pickedTaskId, projectId, retryingEvaluator, t, taskCtx],
  )

  const submitAssessment = useCallback(async () => {
    if (!selectedPkgId) return
    setAssessError(null)
    setAssessOk(false)
    setAssessBusy(true)
    try {
      const created = await assess(projectId, pickedTaskId, 'full')
      setAssessments((prev) => [created, ...prev].sort((a, b) => b.created_at.localeCompare(a.created_at)))
      setAssessOk(true)
      setAssessOpen(false)
    } catch (err) {
      setAssessError(translateError(err as ApiError))
    } finally {
      setAssessBusy(false)
    }
  }, [selectedPkgId, projectId, pickedTaskId])

  const exportExcel = useCallback(async () => {
    if (!selectedPkgId) return
    setExportError(null)
    setExportOk(false)
    setExportNeedsAssess(false)
    setExportBusy('excel')
    try {
      const input: ExcelExportInput = {
        project_id: projectId,
        task_id: pickedTaskId,
        evidence_package_id: selectedPkgId,
        include_hourly: true,
        include_diagnostics: true,
        lang: exportLang,
      }
      const { blob, filename } = await exportTaskReport(input)
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
      const { blob, filename } = await createPackageExport({ project_id: projectId, include_versions: true })
      downloadBlob(blob, filename)
      setExportOk(true)
    } catch (err) {
      setExportError(translateError(err as ApiError))
    } finally {
      setExportBusy(null)
    }
  }, [projectId])

  const isOwner = project?.role === 'owner'

  const tabItems: TabItem<ResultTab>[] = useMemo(
    () => [
      { id: 'overview', label: t(TAB_LABEL_KEYS.overview), alert: failedDims.length > 0 },
      { id: 'financial', label: t(TAB_LABEL_KEYS.financial) },
      { id: 'tax', label: t(TAB_LABEL_KEYS.tax) },
      { id: 'engineering', label: t(TAB_LABEL_KEYS.engineering) },
      {
        id: 'assessment',
        label: t(TAB_LABEL_KEYS.assessment),
        badge: assessments.length > 0 ? assessments.length : undefined,
      },
    ],
    [t, failedDims.length, assessments.length],
  )

  const overviewCfChart = useMemo(() => (finEval ? buildCashflowChartData(finEval) : null), [finEval])
  const cfLabelMap: Record<string, string> = useMemo(
    () => ({
      net: t('ies.result.cf_net'),
      discounted: t('ies.result.cf_discounted'),
      cumulative: t('ies.result.cf_cumulative'),
      cumulative_discounted: t('ies.result.cf_cumulative_discounted'),
    }),
    [t],
  )

  const assessSlice = useMemo(
    () => paginateItems(assessments, assessPage, assessPageSize),
    [assessments, assessPage, assessPageSize],
  )

  if (loading) {
    return (
      <div className="ies-page-placeholder" role="status">
        <Spinner size="lg" />
      </div>
    )
  }

  const loadErrorBlock = loadError ? (
    <div className="ies-section">
      <Alert variant="error">{loadError}</Alert>
      <div style={{ marginTop: 'var(--ies-space-2)' }}>
        <Button onClick={() => taskCtx.retry()}>{t('ies.common.retry')}</Button>
      </div>
    </div>
  ) : null

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

      {/* 持久化结果摘要头 */}
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
          <dl className="ies-kv ies-kv--compact">
            <dt>{t('ies.result.project_version')}</dt>
            <dd>{snapSources ? snapSources.projectVersion : '—'}</dd>
            <dt>{t('ies.result.data_versions')}</dt>
            <dd>{snapSources && snapSources.datasets.length > 0 ? snapSources.datasets.join('、') : '—'}</dd>
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
              <div className="ies-form-row">
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
        </Card>
      </div>

      {resultError ? (
        <div className="ies-section">
          <Alert variant="error">{resultError}</Alert>
        </div>
      ) : null}

      {noEvidence || explicitNoResult ? (
        <div className="ies-section">
          <Alert variant="warning">{t('ies.result.no_evidence')}</Alert>
        </div>
      ) : null}

      {applyOk ? (
        <div className="ies-section">
          <Alert variant="success" closable onClose={() => setApplyOk(false)}>
            {t('ies.result.select_ok')}
          </Alert>
        </div>
      ) : null}

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

      {/* 二级明细页 vs 一级 Tab 视图 */}
      {activeDetail !== null ? (
        <ResultDetailView
          detail={activeDetail}
          parentTab={DETAIL_PARENT_TAB[activeDetail]}
          parentTabLabel={t(TAB_LABEL_KEYS[DETAIL_PARENT_TAB[activeDetail]])}
          fin={finEval}
          hourly={hourly}
          hourlyLoading={hourlyLoading}
          hourlyJump={hourlyJump}
          onHourlyJumpChange={setHourlyJump}
          onRetryHourly={(start) => void retryHourly(start)}
          onBack={handleBackFromDetail}
        />
      ) : (
        <>
          <Tabs
            items={tabItems}
            activeId={activeTab}
            onChange={handleTabChange}
            ariaLabel={t('ies.result.tabs_label')}
            idPrefix="results"
          />

          <div
            role="tabpanel"
            id={`results-panel-${activeTab}`}
            aria-labelledby={`results-tab-${activeTab}`}
          >
            {activeTab === 'overview' ? (
              <>
                {solverSummary ? (
                  <div className="ies-section">
                    <Card title={t('ies.result.solver_summary')}>
                      <Alert variant={solverSummary.math_status === 'solved' ? 'success' : 'warning'}>
                        {t(`ies.result.math_${solverSummary.math_status}`)}
                      </Alert>
                      <dl className="ies-kv" style={{ marginTop: 'var(--ies-space-3)' }}>
                        <dt>{t('ies.result.solver_termination')}</dt>
                        <dd>{t(`ies.result.termination_${solverSummary.termination_reason}`)}</dd>
                        <dt>{t('ies.result.solver_objective')}</dt>
                        <dd>
                          {solverSummary.objective_value === null ? '—' : formatNumber(solverSummary.objective_value)}{' '}
                          {solverSummary.objective_unit}
                        </dd>
                        <dt>{t('ies.result.solver_bound')}</dt>
                        <dd>
                          {solverSummary.best_bound === null ? '—' : formatNumber(solverSummary.best_bound)}{' '}
                          {solverSummary.objective_unit}
                        </dd>
                        <dt>{t('ies.result.solver_gap')}</dt>
                        <dd>{solverSummary.relative_gap === null ? '—' : formatPercent(solverSummary.relative_gap)}</dd>
                      </dl>
                    </Card>
                  </div>
                ) : null}

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

                {finEval ? (
                  <Card
                    title={t('ies.result.fin_sealed_title')}
                    actions={
                      <div className="ies-form-row">
                        <Button variant="secondary" size="sm" onClick={() => handleTabChange('financial')}>
                          {t('ies.result.open_financial_tab')}
                        </Button>
                        <Button variant="secondary" size="sm" onClick={() => handleTabChange('tax')}>
                          {t('ies.result.open_tax_tab')}
                        </Button>
                      </div>
                    }
                    className="ies-section"
                  >
                    <SealedFinancialCore fin={finEval} />
                    {overviewCfChart && overviewCfChart.categories.length > 0 ? (
                      <div style={{ marginTop: 'var(--ies-space-4)' }}>
                        <h3 className="ies-section-subtitle">
                          {t('ies.result.fin_overview_chart_title')}
                        </h3>
                        <ComboChart
                          categories={overviewCfChart.categories}
                          bars={overviewCfChart.bars.map((b) => ({ ...b, label: cfLabelMap[b.key] ?? b.label }))}
                          lines={overviewCfChart.lines.map((l) => ({ ...l, label: cfLabelMap[l.key] ?? l.label }))}
                          ariaLabel={t('ies.result.fin_overview_chart_title')}
                          yLabel={finEval.currency}
                          unit={finEval.currency}
                          height={220}
                          hoverHint={t('ies.result.chart_hover_hint')}
                        />
                      </div>
                    ) : null}
                  </Card>
                ) : finAbsence ? (
                  <FinancialAbsenceAlert finAbsence={finAbsence} outcome={outcome} />
                ) : null}

                {evaluatorResults.length > 0 ? (
                  <Card title={t('ies.result.evaluators_title')} className="ies-section">
                    {evalRetryOk ? (
                      <div style={{ marginBottom: 'var(--ies-space-3)' }}>
                        <Alert variant="success" closable onClose={() => setEvalRetryOk(null)}>
                          {evalRetryOk}
                        </Alert>
                      </div>
                    ) : null}
                    {evalRetryError ? (
                      <div style={{ marginBottom: 'var(--ies-space-3)' }}>
                        <Alert variant="error" closable onClose={() => setEvalRetryError(null)}>
                          {evalRetryError}
                        </Alert>
                      </div>
                    ) : null}
                    <ul className="ies-diag-list">
                      {evaluatorResults.map((ev) => {
                        const meta = EVAL_STATUS_META[ev.status]
                        const canRetry =
                          ev.status === 'failed' || ev.status === 'timed_out' || ev.status === 'cancelled'
                        return (
                          <li key={ev.evaluator} className="ies-diag">
                            <div className="ies-diag__head">
                              <strong>{ev.evaluator}</strong>
                              <Badge
                                variant={meta.variant}
                                icon={meta.icon}
                                label={t(`ies.result.evaluator_status_${ev.status}`)}
                              />
                              {ev.contract ? (
                                <span className="ies-text-note">
                                  {t('ies.result.evaluator_contract')}: {ev.contract}
                                </span>
                              ) : null}
                              {ev.finished_at ? (
                                <time className="ies-diag__time">
                                  {t('ies.result.evaluator_finished_at')}: {formatDateTime(ev.finished_at)}
                                </time>
                              ) : null}
                            </div>
                            {ev.error ? (
                              <div style={{ marginTop: 'var(--ies-space-2)' }}>
                                <Alert variant={canRetry ? 'error' : 'warning'}>
                                  <div>
                                    <strong>
                                      {typeof ev.error.code === 'string'
                                        ? ev.error.code
                                        : JSON.stringify(ev.error.code ?? '')}
                                    </strong>
                                    :{' '}
                                    {typeof ev.error.message === 'string'
                                      ? ev.error.message
                                      : JSON.stringify(ev.error.message ?? '')}
                                  </div>
                                  {(() => {
                                    const detailText = formatEvaluatorDetail(ev.error.detail)
                                    return detailText ? (
                                      <div className="ies-text-note" style={{ marginTop: 'var(--ies-space-1)' }}>
                                        {detailText}
                                      </div>
                                    ) : null
                                  })()}
                                </Alert>
                              </div>
                            ) : null}
                            {canRetry ? (
                              <div style={{ marginTop: 'var(--ies-space-2)' }}>
                                <Button
                                  size="sm"
                                  variant="secondary"
                                  loading={retryingEvaluator === ev.evaluator}
                                  disabled={retryingEvaluator !== null}
                                  onClick={() => void handleRetryEvaluator(ev.evaluator)}
                                >
                                  {t('ies.result.evaluator_retry_btn')}
                                </Button>
                              </div>
                            ) : null}
                          </li>
                        )
                      })}
                    </ul>
                  </Card>
                ) : null}

                {evaluationReports.length > 0 ? (
                  <Card title={t('ies.result.eval_reports_title')} className="ies-section">
                    <div className="ies-stack">
                      {evaluationReports
                        .filter(
                          (rep): rep is EvaluationReportBlock =>
                            rep !== null && typeof rep === 'object' && !Array.isArray(rep),
                        )
                        .map((rep, repIdx) => {
                          const repMetrics = (Array.isArray(rep.metrics) ? rep.metrics : []).filter(
                            (m): m is Record<string, unknown> =>
                              m !== null && typeof m === 'object' && !Array.isArray(m),
                          )
                          const repCharts = Array.isArray(rep.charts) ? rep.charts : []
                          const repTables = Array.isArray(rep.tables) ? rep.tables : []
                          return (
                            <div key={`${String(rep.evaluator ?? repIdx)}-${String(rep.title ?? repIdx)}`}>
                              <h3 className="ies-section-subtitle">
                                {rep.title || rep.evaluator}{' '}
                                <span className="ies-text-note">
                                  ({rep.evaluator} · {rep.schema}@{rep.schema_version})
                                </span>
                              </h3>
                              {repMetrics.length > 0 ? (
                                <dl className="ies-kv">
                                  {repMetrics.map((m, idx) => {
                                    const rawLabel = m.label ?? m.id ?? idx
                                    const labelText =
                                      typeof rawLabel === 'object' && rawLabel !== null
                                        ? JSON.stringify(rawLabel)
                                        : String(rawLabel)
                                    return (
                                      <span key={`${idx}-${String(m.id ?? '')}`} style={{ display: 'contents' }}>
                                        <dt>{labelText}</dt>
                                        <dd>
                                          {m.value === null || m.value === undefined
                                            ? '—'
                                            : typeof m.value === 'object'
                                              ? JSON.stringify(m.value)
                                              : String(m.value)}
                                          {m.unit ? ` ${String(m.unit)}` : ''}
                                        </dd>
                                      </span>
                                    )
                                  })}
                                </dl>
                              ) : null}
                              {repCharts.length > 0 || repTables.length > 0 ? (
                                <p className="ies-text-note" style={{ marginTop: 'var(--ies-space-1)' }}>
                                  {t('ies.result.eval_report_charts_tables', {
                                    charts: repCharts.length,
                                    tables: repTables.length,
                                  })}
                                </p>
                              ) : null}
                            </div>
                          )
                        })}
                    </div>
                  </Card>
                ) : null}

                {!resultError && finEval && Array.isArray(finEval.construction_units) ? (
                  <Card title={t('ies.result.fin_construction_units')} className="ies-section">
                    {finEval.construction_units.length === 0 ? (
                      <p className="ies-text-secondary">{t('ies.result.fin_construction_none')}</p>
                    ) : (
                      <div className="ies-table-wrap">
                        <table className="ies-metric-table" aria-label={t('ies.result.fin_construction_units')}>
                          <thead>
                            <tr>
                              <th>{t('ies.result.fin_construction_unit')}</th>
                              <th>{t('ies.result.fin_construction_instance')}</th>
                              <th>{t('ies.result.fin_construction_mode')}</th>
                              <th>{t('ies.result.fin_construction_built')}</th>
                              <th>{t('ies.result.fin_construction_scales')}</th>
                            </tr>
                          </thead>
                          <tbody>
                            {finEval.construction_units
                              .filter(
                                (unit): unit is NonNullable<typeof unit> =>
                                  unit !== null && typeof unit === 'object' && !Array.isArray(unit),
                              )
                              .map((unit, unitIdx) => (
                              <tr key={unit.unit_id ?? unitIdx}>
                                <td>{unit.unit_id ?? '—'}</td>
                                <td>{unit.instance_id ?? '—'}</td>
                                <td>
                                  {unit.mode === 'mandatory' || unit.mode === 'optional'
                                    ? t(`ies.result.fin_construction_${unit.mode}`)
                                    : (unit.mode ?? '—')}
                                </td>
                                <td>{t(unit.built ? 'ies.result.fin_construction_yes' : 'ies.result.fin_construction_no')}</td>
                                <td>
                                  {(Array.isArray(unit.scales) ? unit.scales : [])
                                    .filter(
                                      (scale): scale is NonNullable<typeof scale> =>
                                        scale !== null && typeof scale === 'object' && !Array.isArray(scale),
                                    )
                                    .map((scale, scaleIdx) => (
                                    <div key={scale.planning_variable_id ?? scaleIdx}>
                                      {scale.planning_variable_id ?? '—'}: {scale.value ?? '—'} {scale.unit ?? ''}
                                    </div>
                                  ))}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </Card>
                ) : null}

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
                        <p className="ies-text-note">
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
              </>
            ) : null}

            {activeTab === 'financial' ? (
              <FinancialTabView
                fin={finEval}
                finAbsence={finAbsence}
                outcome={outcome}
                metrics={metrics}
                currency={currency}
                onOpenDetail={handleOpenDetail}
              />
            ) : null}

            {activeTab === 'tax' ? (
              <TaxDetailsTabView
                fin={finEval}
                finAbsence={finAbsence}
                outcome={outcome}
                onOpenDetail={handleOpenDetail}
              />
            ) : null}

            {activeTab === 'engineering' ? (
              <EngineeringTabView
                hourly={hourly}
                hourlyAbsent={hourlyAbsent}
                hourlyError={hourlyError}
                hourlyLoading={hourlyLoading}
                hourlyJump={hourlyJump}
                onHourlyJumpChange={setHourlyJump}
                onRetryHourly={(start) => void retryHourly(start)}
                onOpenDetail={handleOpenDetail}
                metrics={metrics}
                currency={currency}
              />
            ) : null}

            {activeTab === 'assessment' ? (
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
                  <p className="ies-text-note" style={{ marginBottom: 'var(--ies-space-3)' }}>
                    {t('ies.result.assessment_history_note')}
                  </p>
                  {assessments.length === 0 ? (
                    <EmptyState icon="info" title={t('ies.result.no_assessments')} />
                  ) : (
                    <>
                      <ul className="ies-assess-list">
                        {assessSlice.items.map((a) => (
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
                      <Pagination
                        page={assessSlice.page}
                        pageSize={assessSlice.pageSize}
                        total={assessSlice.total}
                        onPageChange={setAssessPage}
                        onPageSizeChange={(next) => {
                          setAssessPageSize(next)
                          setAssessPage(1)
                        }}
                        pageSizeOptions={[10, 20, 50]}
                      />
                    </>
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
                      <span className="ies-text-note">
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
            ) : null}
          </div>
        </>
      )}

      {/* 选择候选确认 */}
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

      {/* 应用方案确认 */}
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
            <p className="ies-text-note">
              {schemeRevLoading
                ? t('ies.result.apply_scheme_revision_loading')
                : t('ies.result.apply_scheme_revision', { revision: schemeRevision ?? '—' })}
            </p>
            {schemeRevisionError ? (
              <div style={{ marginTop: 'var(--ies-space-2)' }}>
                <Alert variant="error">{schemeRevisionError}</Alert>
              </div>
            ) : null}
            <p className="ies-text-note">
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

      {/* 重新运行系统评估对话框 */}
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
