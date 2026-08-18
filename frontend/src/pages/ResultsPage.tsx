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
 * - 工程:能源平衡表(电/热/冷)、设备容量、逐时曲线(SVG 折线)、峰值、购售电、需量。
 * - Pareto:候选点 SVG 散点(目标轴),用户选择候选 -> 参数差异预览 -> 确认应用
 *   (提示将创建新项目版本,原版本保持不变)。
 * - 评估历史(不可变列表)与重新评估按钮。
 * - 导出:Excel 按钮(选择语言 zh/en)、完整项目包按钮(仅所有者可见)。
 *
 * 指标数据为数据驱动:按指标 id 前缀分组展示;后端指标 id 未固定前,
 * 分组按常用前缀(fin./env./eng./rel./energy 等)与常见关键词匹配,缺失时优雅降级。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, downloadBlob } from '../api/client'
import { translate, translateDiagnostic, translateError, useI18n } from '../i18n'
import type { Locale } from '../i18n'
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
  Spinner,
  TaskOutcomeBadge,
  TaskStatusBadge,
  Textarea,
} from '../components/ui'
import type { BadgeVariant, IconName } from '../components/ui'
import { LineChart, ScatterChart } from '../components/charts'
import type { LineSeries, ScatterPoint } from '../components/charts'
import { formatCo2, formatDateTime, formatMoney, formatNumber, formatPercent } from '../lib/format'
import type {
  ApiError,
  AssessmentDimension,
  AssessmentGrade,
  Currency,
  Diagnostic,
  ExcelExportInput,
  EvidencePackageSummary,
  MetricValue,
  ResultAssessment,
  TaskDetail,
  TaskOutcome,
} from '../types'
import { useWorkbench } from './workbench'

// ---------------------------------------------------------------------------
// 类型与常量
// ---------------------------------------------------------------------------

type DimStatus = 'passed' | 'restricted' | 'failed' | 'na' | 'insufficient'

interface Candidate {
  indexId: number
  label: string
  x: number | null
  y: number | null
  metrics: MetricValue[]
}

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

/** 从诊断中推断 IRR 计算状态(唯一/无解/多解/退化/域外/数值失败)。 */
type IrrStatusKey = 'unique' | 'no_solution' | 'multiple' | 'degenerate' | 'out_of_domain' | 'numeric_failure'

function inferIrrStatusKey(diagnostics: Diagnostic[]): IrrStatusKey | null {
  for (const d of diagnostics ?? []) {
    const k = d.message_key.toLowerCase()
    if (!/irr|financial/.test(k)) continue
    if (/no.?solution|infeasible/.test(k)) return 'no_solution'
    if (/multi|multiple/.test(k)) return 'multiple'
    if (/degenerate|degen/.test(k)) return 'degenerate'
    if (/domain|out.?of|range/.test(k)) return 'out_of_domain'
    if (/numeric|nan|fail|error/.test(k)) return 'numeric_failure'
    return 'unique'
  }
  return null
}

/** 从指标中提取 Pareto 候选点(id 形如 pareto|candidate.<i>.x/.y/.label/.index_id)。 */
function extractCandidates(metrics: Record<string, MetricValue> | null): Candidate[] {
  if (!metrics) return []
  const map = new Map<string, { x?: number; y?: number; labelKey?: string; indexId?: number; metrics: MetricValue[] }>()
  for (const [id, m] of Object.entries(metrics)) {
    const m2 = /^(?:pareto|candidate|opt\.candidate|opt\.pareto)(?:\.|\.\d+\.)?(.+)$/i.exec(id)
    if (!m2) continue
    const rest = m2[1]
    const dot = rest.indexOf('.')
    const key = dot === -1 ? rest : rest.slice(0, dot)
    const field = dot === -1 ? '' : rest.slice(dot + 1)
    let entry = map.get(key)
    if (!entry) {
      entry = { metrics: [] }
      map.set(key, entry)
    }
    if (/^x$/i.test(field)) entry.x = m.value ?? undefined
    else if (/^y$/i.test(field)) entry.y = m.value ?? undefined
    else if (/label|name/i.test(field)) entry.labelKey = m.label_key
    else if (/index.?id|id$/i.test(field)) entry.indexId = m.value ?? undefined
    else entry.metrics.push(m)
  }
  const out: Candidate[] = []
  for (const [key, entry] of map) {
    out.push({
      indexId: entry.indexId ?? (Number(key) || 0),
      label: entry.labelKey ? translate(entry.labelKey) : `#${key}`,
      x: entry.x ?? null,
      y: entry.y ?? null,
      metrics: entry.metrics,
    })
  }
  return out.sort((a, b) => a.indexId - b.indexId)
}

// ---------------------------------------------------------------------------
// 页面
// ---------------------------------------------------------------------------

export default function ResultsPage() {
  const { t, locale } = useI18n()
  const { projectId, project, currentVersion } = useWorkbench()
  const currency: Currency = project?.currency ?? 'CNY'

  // URL ?package= 直达参数(仅首次读取,避免轮询重载)
  const [initialPkgParam] = useState<number | null>(() => {
    const v = Number(new URLSearchParams(window.location.search).get('package'))
    return Number.isFinite(v) && v > 0 ? v : null
  })

  // 证据包收集(来自已完成任务的详情)
  const [entries, setEntries] = useState<Array<{ pkg: EvidencePackageSummary; task: TaskDetail }>>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [selectedPkgId, setSelectedPkgId] = useState<number | null>(null)

  // 选中证据包的结果数据
  const [metrics, setMetrics] = useState<Record<string, MetricValue> | null>(null)
  const [resultDiag, setResultDiag] = useState<Diagnostic[]>([])
  const [assessments, setAssessments] = useState<ResultAssessment[]>([])
  const [hourly, setHourly] = useState<{ resolution: string; n: number; flows: Record<string, number[]> } | null>(null)
  const [resultError, setResultError] = useState<string | null>(null)

  // 数据版本(摘要展示)
  const [dataVersions, setDataVersions] = useState<Array<{ name: string; version_no: number }>>([])

  // Pareto 选择与应用
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null)
  const [applyOpen, setApplyOpen] = useState(false)
  const [applying, setApplying] = useState(false)
  const [applyError, setApplyError] = useState<string | null>(null)
  const [applyOk, setApplyOk] = useState(false)

  // 重新评估表单
  const [assessOpen, setAssessOpen] = useState(false)
  const [assessGrades, setAssessGrades] = useState<Record<AssessmentDimension, AssessmentGrade>>({
    physical: 'pass',
    optimality: 'pass',
    financial: 'pass',
    reliability: 'pass',
  })
  const [assessScore, setAssessScore] = useState('')
  const [assessComment, setAssessComment] = useState('')
  const [assessBusy, setAssessBusy] = useState(false)
  const [assessError, setAssessError] = useState<string | null>(null)
  const [assessOk, setAssessOk] = useState(false)

  // 导出
  const [exportLang, setExportLang] = useState<Locale>(locale)
  const [exportBusy, setExportBusy] = useState<'excel' | 'package' | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const [exportOk, setExportOk] = useState(false)

  // -------------------------------------------------------------------------
  // 证据包收集(完成任务 -> 详情 -> evidence)
  // -------------------------------------------------------------------------
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setLoading(true)
      setLoadError(null)
      try {
        const page = await api.tasks.list({ project_id: projectId, limit: 100 })
        const completed = page.items.filter(
          (tk) =>
            tk.status === 'completed' &&
            (tk.type === 'calc' || tk.type === 'optimization' || tk.type === 'uncertainty'),
        )
        const details = await Promise.all(
          completed.slice(0, 10).map((tk) => api.tasks.get(tk.id).catch(() => null)),
        )
        const collected: Array<{ pkg: EvidencePackageSummary; task: TaskDetail }> = []
        for (const d of details) {
          if (!d) continue
          for (const p of d.evidence ?? []) collected.push({ pkg: p, task: d })
        }
        collected.sort((a, b) => a.pkg.package_id - b.pkg.package_id)
        if (cancelled) return
        setEntries(collected)
        const chosen =
          collected.find((c) => c.pkg.package_id === initialPkgParam) ??
          (collected.length > 0 ? collected[collected.length - 1] : null)
        setSelectedPkgId(chosen ? chosen.pkg.package_id : null)
      } catch (err) {
        if (!cancelled) setLoadError(translateError(err as ApiError))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [projectId, initialPkgParam])

  // -------------------------------------------------------------------------
  // 选中证据包的结果/评估/逐时数据
  // -------------------------------------------------------------------------
  useEffect(() => {
    if (!selectedPkgId) {
      setMetrics(null)
      setResultDiag([])
      setAssessments([])
      setHourly(null)
      setResultError(null)
      return
    }
    let cancelled = false
    const load = async () => {
      try {
        const [res, ass, hr] = await Promise.all([
          api.results.result(selectedPkgId).catch(() => null),
          api.results.assessments(selectedPkgId).catch(() => [] as ResultAssessment[]),
          api.results.hourly(selectedPkgId).catch(() => null),
        ])
        if (cancelled) return
        setMetrics(res?.metrics ?? null)
        setResultDiag(res?.diagnostics ?? [])
        setAssessments([...(ass ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at)))
        setHourly(hr)
        setResultError(null)
      } catch (err) {
        if (!cancelled) {
          setMetrics(null)
          setResultError(translateError(err as ApiError))
        }
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [selectedPkgId])

  // 数据版本(数据集 + 最新版本号)
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const page = await api.datasets.list({ project_id: projectId, limit: 50 })
        const rows = await Promise.all(
          page.items.map(async (ds) => {
            try {
              const vs = await api.datasets.versions(ds.id)
              return { name: ds.name, version_no: vs.length > 0 ? vs[vs.length - 1].version_no : 0 }
            } catch {
              return null
            }
          }),
        )
        if (!cancelled) setDataVersions(rows.filter((r): r is { name: string; version_no: number } => r !== null))
      } catch {
        // 数据版本为非必需信息,失败静默
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [projectId])

  // -------------------------------------------------------------------------
  // 派生数据
  // -------------------------------------------------------------------------
  const selectedEntry = entries.find((e) => e.pkg.package_id === selectedPkgId) ?? null
  const outcome = selectedEntry?.task.business_outcome ?? null
  const snapshot = selectedEntry?.task.calc_snapshot ?? null

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

  const irrKey = useMemo(() => inferIrrStatusKey(resultDiag), [resultDiag])

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

  useEffect(() => {
    setCandidates(extractCandidates(metrics))
  }, [metrics])

  const selectedCandidate = useMemo(() => {
    if (!selectedCandidateId) return null
    const idx = candidates.findIndex((c) => c.indexId === Number(selectedCandidateId))
    return idx >= 0 ? candidates[idx] : null
  }, [selectedCandidateId, candidates])

  // 逐时曲线序列
  const hourlySeries = useMemo(() => {
    if (!hourly) return { power: [] as LineSeries[], soc: [] as LineSeries[] }
    const f = hourly.flows
    const power: LineSeries[] = []
    const buy = f['p_grid_buy'] ?? f['grid_buy']
    const sell = f['p_grid_sell'] ?? f['grid_sell']
    const load = f['e_load'] ?? f['electric_load']
    const pv = f['p_pv'] ?? f['pv']
    if (buy?.length) power.push({ key: 'buy', label: t('ies.result.series_grid_buy'), color: '#0e5cad', values: buy })
    if (load?.length) power.push({ key: 'load', label: t('ies.result.series_load'), color: '#b3261e', values: load })
    if (pv?.length) power.push({ key: 'pv', label: t('ies.result.series_pv'), color: '#8a5a00', values: pv })
    if (sell?.length) power.push({ key: 'sell', label: t('ies.result.series_grid_sell'), color: '#1e7d32', values: sell })
    const soc = f['soc']
    return {
      power,
      soc: soc?.length ? [{ key: 'soc', label: t('ies.result.series_soc'), color: '#0e5cad', values: soc }] : [],
    }
  }, [hourly, t])

  const scatterPoints: ScatterPoint[] = useMemo(
    () =>
      candidates
        .filter((c) => c.x !== null && c.y !== null)
        .map((c) => ({ id: String(c.indexId), x: c.x as number, y: c.y as number, label: c.label })),
    [candidates],
  )

  // -------------------------------------------------------------------------
  // 动作
  // -------------------------------------------------------------------------
  const applyCandidate = useCallback(async () => {
    if (!selectedCandidate) return
    setApplyError(null)
    setApplyOk(false)
    setApplying(true)
    try {
      await api.results.select(projectId, {
        result_index_id: selectedCandidate.indexId,
        reason: 'user-selected-pareto-candidate',
      })
      setApplyOk(true)
      setApplyOpen(false)
    } catch (err) {
      setApplyError(translateError(err as ApiError))
    } finally {
      setApplying(false)
    }
  }, [projectId, selectedCandidate])

  const submitAssessment = useCallback(async () => {
    if (!selectedPkgId) return
    setAssessError(null)
    setAssessOk(false)
    setAssessBusy(true)
    try {
      const score = Number(assessScore)
      const created = await api.results.assess(selectedPkgId, {
        dimensions: { ...assessGrades },
        ...(Number.isFinite(score) && assessScore.trim() !== '' ? { overall_score: score } : {}),
        ...(assessComment.trim() !== '' ? { comment: assessComment.trim() } : {}),
      })
      setAssessments((prev) => [created, ...prev].sort((a, b) => b.created_at.localeCompare(a.created_at)))
      setAssessOk(true)
      setAssessOpen(false)
      setAssessComment('')
      setAssessScore('')
    } catch (err) {
      setAssessError(translateError(err as ApiError))
    } finally {
      setAssessBusy(false)
    }
  }, [selectedPkgId, assessGrades, assessScore, assessComment])

  const exportExcel = useCallback(async () => {
    if (!selectedPkgId) return
    setExportError(null)
    setExportOk(false)
    setExportBusy('excel')
    try {
      // lang 为前端约定附加参数(后端应按此生成对应语言模板,缺省 zh)
      const input = {
        project_id: projectId,
        evidence_package_id: selectedPkgId,
        include_hourly: true,
        include_diagnostics: true,
        lang: exportLang,
      } as ExcelExportInput
      const report = await api.exports.excel(input)
      const { blob, filename } = await api.exports.download(report.id)
      downloadBlob(blob, filename)
      setExportOk(true)
    } catch (err) {
      setExportError(translateError(err as ApiError))
    } finally {
      setExportBusy(null)
    }
  }, [projectId, selectedPkgId, exportLang])

  const exportPackage = useCallback(async () => {
    setExportError(null)
    setExportOk(false)
    setExportBusy('package')
    try {
      const report = await api.exports.package({ project_id: projectId, include_versions: true })
      const { blob, filename } = await api.exports.download(report.id)
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

  if (entries.length === 0) {
    return (
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
    )
  }

  const evidenceBadgeVariant: BadgeVariant =
    selectedEntry?.pkg.status === 'complete'
      ? 'success'
      : selectedEntry?.pkg.status === 'partial'
        ? 'warning'
        : 'danger'

  return (
    <div>
      {loadError ? (
        <div className="ies-section">
          <Alert variant="error">{loadError}</Alert>
        </div>
      ) : null}

      {/* 结果摘要 */}
      <div className="ies-section">
        <Card
          title={t('ies.result.summary')}
          actions={
            entries.length > 1 ? (
              <Select
                aria-label={t('ies.result.evidence_status')}
                value={selectedPkgId ?? ''}
                onChange={(e) => setSelectedPkgId(Number(e.target.value))}
                style={{ maxWidth: 260 }}
              >
                {entries.map((e) => (
                  <option key={e.pkg.package_id} value={e.pkg.package_id}>
                    #{e.pkg.package_id} · {e.task.id} · {e.pkg.status}
                  </option>
                ))}
              </Select>
            ) : undefined
          }
        >
          {selectedEntry ? (
            <dl className="ies-kv">
              <dt>{t('ies.result.project_version')}</dt>
              <dd>{currentVersion ? `v${currentVersion.version_no}` : '—'}</dd>
              <dt>{t('ies.result.data_versions')}</dt>
              <dd>
                {dataVersions.length > 0
                  ? dataVersions.map((d) => `${d.name} v${d.version_no}`).join('、')
                  : '—'}
              </dd>
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
                  <TaskStatusBadge status={selectedEntry.task.status} />
                  <Badge
                    variant={evidenceBadgeVariant}
                    icon={evidenceBadgeVariant === 'success' ? 'check' : 'warning'}
                    label={t(`ies.result.evidence_status_${selectedEntry.pkg.status}`)}
                  />
                </div>
              </dd>
              <dt>{t('ies.result.business_outcome')}</dt>
              <dd>
                <TaskOutcomeBadge outcome={outcome} />
              </dd>
              <dt>{t('ies.common.id')}</dt>
              <dd>
                任务 #{selectedEntry.task.id} · 证据包 #{selectedEntry.pkg.package_id}
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

          {/* 逐时曲线 */}
          {hourlySeries.power.length > 0 || hourlySeries.soc.length > 0 ? (
            <Card title={t('ies.result.hourly_chart')} className="ies-section">
              <p style={{ fontSize: 'var(--ies-fs-xs)', color: 'var(--ies-color-text-secondary)', marginBottom: 'var(--ies-space-2)' }}>
                {t('ies.result.hourly_note', { n: 168 })}
              </p>
              {hourlySeries.power.length > 0 ? (
                <div className="ies-section">
                  <LineChart
                    series={hourlySeries.power}
                    ariaLabel={t('ies.result.hourly_chart')}
                    yLabel={t('ies.unit.kw')}
                  />
                </div>
              ) : null}
              {hourlySeries.soc.length > 0 ? (
                <LineChart
                  series={hourlySeries.soc}
                  ariaLabel={t('ies.result.series_soc')}
                  yLabel={t('ies.result.series_soc')}
                />
              ) : null}
            </Card>
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
        </>
      ) : null}

      {/* Pareto 候选方案 */}
      <Card
        title={t('ies.result.pareto_title')}
        actions={
          candidates.length > 0 && selectedCandidate ? (
            <Button icon="check" onClick={() => setApplyOpen(true)}>
              {t('ies.result.apply_candidate')}
            </Button>
          ) : undefined
        }
        className="ies-section"
      >
        {candidates.length === 0 ? (
          <EmptyState
            icon="question"
            title={t('ies.result.pareto_empty')}
            description={t('ies.result.select_candidate')}
          />
        ) : (
          <div className="ies-grid ies-grid--cols-2">
            <div className="ies-section">
              {scatterPoints.length >= 2 ? (
                <ScatterChart
                  points={scatterPoints}
                  ariaLabel={t('ies.result.pareto_title')}
                  xLabel={t('ies.result.pareto_axis')}
                  yLabel={t('ies.result.pareto_axis')}
                  selectedId={selectedCandidateId}
                  onSelect={(id) => setSelectedCandidateId(id)}
                />
              ) : (
                <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
                  {t('ies.result.param_diff_empty')}
                </p>
              )}
              <ul className="ies-cand-list" style={{ marginTop: 'var(--ies-space-3)' }}>
                {candidates.map((c) => (
                  <li
                    key={c.indexId}
                    className={`ies-cand-row ${
                      selectedCandidateId === String(c.indexId) ? 'ies-cand-row--selected' : ''
                    }`}
                  >
                    <button
                      type="button"
                      className="ies-btn ies-btn--ghost ies-btn--sm"
                      style={{ padding: 0, border: 0, background: 'transparent', minHeight: 0 }}
                      onClick={() => setSelectedCandidateId(String(c.indexId))}
                      aria-pressed={selectedCandidateId === String(c.indexId)}
                    >
                      {c.label}
                    </button>
                    <span className="ies-cand-row__vals">
                      {c.x !== null ? `x ${formatNumber(c.x, { digits: 2 })}` : ''}
                      {c.y !== null ? `y ${formatNumber(c.y, { digits: 2 })}` : ''}
                      {c.x === null && c.y === null ? `#${c.indexId}` : ''}
                    </span>
                  </li>
                ))}
              </ul>
            </div>

            {/* 参数差异预览 */}
            <div>
              <h3 style={{ fontSize: 'var(--ies-fs-sm)', fontWeight: 700, marginBottom: 'var(--ies-space-2)' }}>
                {t('ies.result.param_diff')}
              </h3>
              {selectedCandidate ? (
                selectedCandidate.metrics.length > 0 ? (
                  <MetricTable metrics={selectedCandidate.metrics} currency={currency} />
                ) : (
                  <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
                    {t('ies.result.param_diff_empty')}
                  </p>
                )
              ) : (
                <EmptyState icon="info" title={t('ies.result.select_candidate')} />
              )}
            </div>
          </div>
        )}
      </Card>

      {/* 应用成功提示 */}
      {applyOk ? (
        <div className="ies-section">
          <Alert variant="success" closable onClose={() => setApplyOk(false)}>
            {t('ies.result.apply_ok')}
          </Alert>
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
                  if (latestAssessment) {
                    setAssessGrades({
                      physical: latestAssessment.dimension_physical,
                      optimality: latestAssessment.dimension_optimality,
                      financial: latestAssessment.dimension_financial,
                      reliability: latestAssessment.dimension_reliability,
                    })
                  }
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
                        shape="circle"
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
          {exportOk ? (
            <div style={{ marginTop: 'var(--ies-space-3)' }}>
              <Alert variant="success" closable onClose={() => setExportOk(false)}>
                {t('ies.result.export_ok')}
              </Alert>
            </div>
          ) : null}
          {resultDiag.length > 0 ? (
            <details style={{ marginTop: 'var(--ies-space-4)' }}>
              <summary className="ies-summary">{t('ies.result.assessment')}</summary>
              <ul className="ies-diag-list" style={{ marginTop: 'var(--ies-space-2)' }}>
                {resultDiag.map((d, i) => (
                  <DiagnosticLine key={`${d.code}-${i}`} diag={d} />
                ))}
              </ul>
            </details>
          ) : null}
        </Card>
      </div>

      {/* 应用候选确认(创建新版本提示) */}
      <Dialog
        open={applyOpen}
        onClose={() => setApplyOpen(false)}
        title={t('ies.result.apply_candidate')}
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
        <Alert variant="warning" title={t('ies.result.apply_confirm')}>
          {selectedCandidate ? `#${selectedCandidate.indexId} · ${selectedCandidate.label}` : ''}
        </Alert>
        {applyError ? (
          <div style={{ marginTop: 'var(--ies-space-3)' }}>
            <Alert variant="error">{applyError}</Alert>
          </div>
        ) : null}
      </Dialog>

      {/* 重新评估对话框 */}
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
              {t('ies.common.save')}
            </Button>
          </>
        }
      >
        <div className="ies-grid ies-grid--cols-2">
          {DIMENSIONS.map((d) => (
            <FormField key={d.key} label={t(d.labelKey)} htmlFor={`assess-${d.key}`}>
              <Select
                id={`assess-${d.key}`}
                value={assessGrades[d.key]}
                onChange={(e) =>
                  setAssessGrades({ ...assessGrades, [d.key]: e.target.value as AssessmentGrade })
                }
              >
                <option value="pass">{t('ies.result.grade_pass')}</option>
                <option value="fail">{t('ies.result.grade_fail')}</option>
                <option value="unknown">{t('ies.result.grade_unknown')}</option>
              </Select>
            </FormField>
          ))}
        </div>
        <FormField label={t('ies.result.overall_score')} htmlFor="assess-score">
          <Input
            id="assess-score"
            type="number"
            min={0}
            max={100}
            placeholder="0-100"
            value={assessScore}
            onChange={(e) => setAssessScore(e.target.value)}
          />
        </FormField>
        <FormField label={t('ies.common.description')} htmlFor="assess-comment">
          <Textarea
            id="assess-comment"
            value={assessComment}
            onChange={(e) => setAssessComment(e.target.value)}
          />
        </FormField>
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
        <dt>{labels.irrStatus}</dt>
        <dd>
          {irrKey ? (
            <Badge variant={irrKey === 'unique' ? 'success' : 'warning'} icon={irrKey === 'unique' ? 'check' : 'warning'} label={t(`ies.result.irr_${irrKey}`)} />
          ) : (
            '—'
          )}
        </dd>
      </dl>
      {others.length > 0 ? (
        <div style={{ marginTop: 'var(--ies-space-3)' }}>
          <MetricTable metrics={others} currency={currency} />
        </div>
      ) : null}
    </>
  )
}

/** 结果诊断单行(消息 + 修复建议)。 */
function DiagnosticLine({ diag }: { diag: Diagnostic }) {
  const { t } = useI18n()
  return (
    <li className="ies-diag">
      <p className="ies-diag__msg">{translateDiagnostic(diag)}</p>
      {diag.fix_hint_key ? (
        <p className="ies-diag__fix">
          <Icon name="info" size={12} />
          {t('ies.task.fix_hint')}: {translate(diag.fix_hint_key, diag.params)}
        </p>
      ) : null}
    </li>
  )
}
