/**
 * 计算配置页(设计输入 §9.2 参数/变量/目标/约束、§10.1 硬约束、§10.2 财务评价)。
 *
 * - 经济参数:评价周期 / 折现率 / 最低可接受 IRR(与折现率独立的硬约束)/
 *   税率 / 折旧年限 / 币种显示(取自项目);
 * - 变量配置:每类新建设备容量变量(类型/初值/上下界,缺省取设备注册表参数),
 *   存量设备容量固定只读展示;
 * - 目标:默认税后项目投资 IRR 最大化,可选 NPV / 资本金 IRR;碳排放目标(上限约束);
 * - 约束:预定义约束开关列表 + 高级模式受限语法表达式输入;
 *   最低 IRR 硬约束显著显示(不可被目标权重抵消);
 * - 算法选择:auto / 手动(能力列表:支持的变量类型);不兼容算法禁止保存并说明原因;
 * - 保存:本地校验诊断(定位到配置项) + 后端校验;保存成功提示版本号。
 *
 * 挂载方式:路由 /projects/:id/config 由 useParams 取项目 id;
 * 也可由工作台以 <ConfigPage projectId={id} /> 方式嵌入。
 *
 * 持久化约定(params 键,与后端 calc_configs.params JSONB 双向往返):
 *   evaluation_period_years / discount_rate(小数) / tax_rate(小数) /
 *   depreciation_years / variable_initial(名称->初值) /
 *   variable_device_type(名称->设备类型) / carbon_cap_tco2 / algorithm_mode
 */

import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'

import { api } from '../api/client'
import { DiagnosticsList } from '../components/DiagnosticsList'
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  EmptyState,
  FormField,
  Icon,
  IconButton,
  Input,
  Select,
  Spinner,
  Table,
  TBody,
  TD,
  TH,
  THead,
  TR,
} from '../components/ui'
import { errorMessage, useI18n } from '../i18n'
import { pt } from '../i18n/pageMessages'
import { formatNumber } from '../lib/format'
import { useWorkbench } from './workbench'
import { ApiError } from '../types'
import type {
  AlgorithmId,
  BackendVariableType,
  CalcConfig,
  CalcConfigInput,
  CalcConstraint,
  ConfigVariable,
  Device,
  DeviceTypeSpec,
  Diagnostic,
  GraphModel,
  Port,
  Project,
  Severity,
  VariableType,
} from '../types'

export interface ConfigPageProps {
  projectId?: number
}

// ---------------------------------------------------------------------------
// 常量与类型
// ---------------------------------------------------------------------------

interface VariableRow {
  key: string
  name: string
  type: VariableType
  initial: string
  lower: string
  upper: string
  deviceType: string | null
}

interface ExistingDeviceRow {
  name: string
  deviceType: string
  capacity: number | null
}

interface CustomExpr {
  key: string
  name: string
  expression: string
}

interface EcoForm {
  evaluationPeriod: string
  discountRate: string
  taxRate: string
  depreciationYears: string
  minIrr: string
}

/**
 * 预定义约束开关(名称即后端 PREDEFINED_CONSTRAINT_KINDS 的 kind,见
 * backend services/config.py: load_satisfaction / capacity_limits / co2_cap /
 * energy_cost_cap)。co2_cap 由下方"碳排放目标"段单独配置;
 * energy_cost_cap 无前端金额输入, 暂不展示。expression 仅作说明文案。
 */
const PREDEFINED_CONSTRAINTS = [
  {
    name: 'load_satisfaction',
    expression: '负荷必须完全满足(默认不允许削减)',
    commentKey: 'ies.config.con_load_satisfaction',
  },
  {
    name: 'capacity_limits',
    expression: '设备出力 ≤ 容量上限',
    commentKey: 'ies.config.con_capacity_limits',
  },
] as const

const OBJECTIVE_OPTIONS = [
  { value: 'irr_after_tax', key: 'ies.config.objective_irr_max' },
  { value: 'npv_after_tax', key: 'ies.config.objective_npv_max' },
] as const

type ObjectiveValue = (typeof OBJECTIVE_OPTIONS)[number]['value']

/** 前端变量类型 ↔ 后端 VARIABLE_TYPES(continuous/integer/enum/boolean): binary 映射为 boolean。 */
const TYPE_TO_BACKEND: Record<VariableType, BackendVariableType> = {
  continuous: 'continuous',
  binary: 'boolean',
  integer: 'integer',
}

const TYPE_FROM_BACKEND: Record<string, VariableType> = {
  continuous: 'continuous',
  boolean: 'binary',
  integer: 'integer',
}

/** 算法能力表(支持的变量类型;custom 由后端校验)。 */
const ALGORITHM_CAPABILITIES: Record<AlgorithmId, { types: readonly VariableType[] }> = {
  milp: { types: ['continuous', 'binary', 'integer'] },
  lp: { types: ['continuous'] },
  heuristic: { types: ['continuous', 'binary', 'integer'] },
  ga: { types: ['continuous', 'binary', 'integer'] },
  exhaustive: { types: ['continuous', 'binary', 'integer'] },
  custom: { types: ['continuous', 'binary', 'integer'] },
}

const ALGORITHM_IDS: readonly AlgorithmId[] = ['milp', 'lp', 'heuristic', 'ga', 'exhaustive', 'custom']

/** params JSONB 键(与后端 calc_configs.params 双向往返,见文件头注释)。 */
const PARAM_KEYS = {
  evaluationPeriod: 'evaluation_period_years',
  discountRate: 'discount_rate',
  taxRate: 'tax_rate',
  depreciationYears: 'depreciation_years',
  variableInitial: 'variable_initial',
  variableDeviceType: 'variable_device_type',
  carbonCap: 'carbon_cap_tco2',
  algorithmMode: 'algorithm_mode',
} as const

let SEQ = 0

// ---------------------------------------------------------------------------
// 纯工具
// ---------------------------------------------------------------------------

function numOrNull(s: string): number | null {
  const trimmed = s.trim()
  const v = Number(trimmed)
  return trimmed !== '' && Number.isFinite(v) ? v : null
}

/** 百分比输入(0-100) -> 小数(0-1),与后端财务参数口径一致。 */
function pctToDecimal(pct: string): number | null {
  const v = numOrNull(pct)
  return v === null ? null : v / 100
}

function numText(v: unknown): string {
  return typeof v === 'number' && Number.isFinite(v) ? String(v) : ''
}

/** 小数(0-1) -> 百分比文本(0-100)。 */
function percentText(v: unknown): string {
  return typeof v === 'number' && Number.isFinite(v) ? String(Number((v * 100).toFixed(3))) : ''
}

function num(params: Record<string, unknown> | undefined, key: string): number | null {
  const v = params?.[key]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function objOf(params: Record<string, unknown> | undefined, key: string): Record<string, unknown> {
  const v = params?.[key]
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

/** 设备容量:优先 params 容量键,其次端口容量最大值。 */
function deviceCapacity(device: Device, ports: Port[]): number | null {
  const p = device.params ?? {}
  for (const k of ['capacity', 'rated_capacity', 'installed_capacity', 'power']) {
    const v = p[k]
    if (typeof v === 'number' && Number.isFinite(v)) return v
  }
  const portCaps = ports
    .filter((pt) => pt.device_id === device.id)
    .map((pt) => pt.capacity)
    .filter((c): c is number => c !== null)
  return portCaps.length > 0 ? Math.max(...portCaps) : null
}

/**
 * 设备类型 id(如 ies.device.grid_connection)→ 合法优化变量名短标识。
 * 后端 _IDENT_RE 要求 ^[A-Za-z_][A-Za-z0-9_]*$(services/config.py),
 * 类型 id 含点号,直接拼接会得到非法标识符导致保存 422。
 */
function variableNameForType(typeId: string): string {
  const last = typeId.split('.').filter(Boolean).pop() ?? typeId
  let base = last.replace(/[^A-Za-z0-9_]/g, '_')
  if (!/^[A-Za-z_]/.test(base)) base = `_${base}`
  const name = `${base}_capacity`
  return name.replace(/[^A-Za-z0-9_]/g, '_')
}

/** 设备注册表容量参数(缺省 0 至无上界;容量默认值恒为数值)。 */
function specCapacity(spec: DeviceTypeSpec | undefined): { min: number | null; max: number | null; def: number | null } {
  if (!spec) return { min: 0, max: null, def: null }
  const cap =
    spec.parameters['capacity'] ?? spec.parameters['rated_capacity'] ?? spec.parameters['installed_capacity']
  if (!cap) return { min: 0, max: null, def: null }
  return { min: cap.min, max: cap.max, def: typeof cap.default === 'number' ? cap.default : null }
}

/** 前端本地诊断(定位到配置项 field,渲染于校验诊断列表)。 */
function makeDiag(
  field: string | null,
  code: string,
  messageKey: string,
  severity: Severity,
  params?: Record<string, unknown>,
): Diagnostic {
  return {
    code,
    message_key: messageKey,
    params: params ?? {},
    severity,
    blocking: severity === 'blocking' || severity === 'error',
    location: { object_type: 'config', object_id: null, field, row: null },
    fix_hint_key: null,
    ref_ids: [],
    occurred_at: new Date().toISOString(),
    source: 'frontend',
    trace_id: null,
    project_id: null,
    task_id: null,
    suppressed: false,
  }
}

/** 算法兼容性检查:LP 不支持离散变量,其余按能力表;auto/custom 交由后端。 */
function algorithmIssue(
  algo: AlgorithmId | null,
  rows: VariableRow[],
): { key: string; params: Record<string, unknown> } | null {
  if (!algo || algo === 'custom') return null
  const discrete = rows.filter((r) => r.type !== 'continuous').length
  if (algo === 'lp' && discrete > 0) {
    const reason = pt('ies.config.alg_incompat_discrete', { algo: 'LP', count: discrete })
    return { key: 'ies.config.err.alg_incompat', params: { algo: 'LP', reason } }
  }
  return null
}

// ---------------------------------------------------------------------------
// 页面主体
// ---------------------------------------------------------------------------

export function ConfigPage({ projectId }: ConfigPageProps) {
  const { id } = useParams()
  const pid = projectId ?? (id !== undefined && Number.isFinite(Number(id)) ? Number(id) : undefined)
  const { t } = useI18n()
  // 工作台内已由 WorkbenchProvider 拉取项目;独立挂载时本地兜底
  const wbProject = useWorkbench().project
  const [fallbackProject, setFallbackProject] = useState<Project | null>(null)
  const project = wbProject ?? fallbackProject

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [frozen, setFrozen] = useState(false)
  const [config, setConfig] = useState<CalcConfig | null>(null)
  const [algorithms, setAlgorithms] = useState<Array<{ name: string; label: string; description_key: string }>>([])
  const [graph, setGraph] = useState<GraphModel | null>(null)
  const [specs, setSpecs] = useState<DeviceTypeSpec[]>([])

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [eco, setEco] = useState<EcoForm>({
    evaluationPeriod: '20',
    discountRate: '8',
    taxRate: '25',
    depreciationYears: '10',
    minIrr: '8',
  })
  const [variables, setVariables] = useState<VariableRow[]>([])
  const [existingDevices, setExistingDevices] = useState<ExistingDeviceRow[]>([])
  const [objective, setObjective] = useState<ObjectiveValue>('irr_after_tax')
  const [carbonEnabled, setCarbonEnabled] = useState(false)
  const [carbonCap, setCarbonCap] = useState('')
  const [toggles, setToggles] = useState<Record<string, boolean>>({})
  const [advancedMode, setAdvancedMode] = useState(false)
  const [customExprs, setCustomExprs] = useState<CustomExpr[]>([])
  const [newExpr, setNewExpr] = useState<{ name: string; expression: string }>({ name: '', expression: '' })
  const [algMode, setAlgMode] = useState<'auto' | 'manual'>('auto')
  const [algorithm, setAlgorithm] = useState<AlgorithmId>('milp')
  const [diagnostics, setDiagnostics] = useState<Diagnostic[]>([])
  const [notice, setNotice] = useState<{ kind: 'success' | 'error'; text: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [validating, setValidating] = useState(false)

  /** 将后端配置(或默认配置)灌入表单。 */
  function applyConfig(
    cfg: CalcConfig | CalcConfigInput,
    algos: Array<{ name: string; label: string; description_key: string }>,
    proj: Project | null,
    g: GraphModel | null,
    sp: DeviceTypeSpec[],
  ) {
    setConfig('status' in cfg ? (cfg as CalcConfig) : null)
    setFrozen('status' in cfg && cfg.status === 'frozen')
    if (proj) setFallbackProject(proj)
    setAlgorithms(algos)
    setGraph(g)
    setSpecs(sp)
    setName(cfg.name)
    setDescription(cfg.description ?? '')

    const params = cfg.params ?? {}
    setEco({
      evaluationPeriod: numText(num(params, PARAM_KEYS.evaluationPeriod)),
      discountRate: percentText(num(params, PARAM_KEYS.discountRate)),
      taxRate: percentText(num(params, PARAM_KEYS.taxRate)),
      depreciationYears: numText(num(params, PARAM_KEYS.depreciationYears)),
      minIrr: cfg.min_irr !== null && cfg.min_irr !== undefined ? percentText(cfg.min_irr) : '',
    })

    const typeMap = objOf(params, PARAM_KEYS.variableDeviceType)
    const rows: VariableRow[] = (cfg.variables ?? []).map((v, idx) => ({
      key: `L${idx}`,
      name: v.name,
      type: TYPE_FROM_BACKEND[v.type] ?? 'continuous',
      initial: numText(v.initial),
      lower: numText(v.min),
      upper: numText(v.max),
      deviceType: typeof typeMap[v.name] === 'string' ? (typeMap[v.name] as string) : null,
    }))
    // 无已存变量时:按图中"新增"设备类型生成默认容量变量(上下界取注册表;
    // 变量名去点号满足后端标识符约束, 初值缺省回退到注册表 default/min, 后端要求必有初值)
    if (rows.length === 0 && g) {
      const newTypes = [...new Set(g.devices.filter((d) => d.kind === 'new').map((d) => d.device_type))]
      for (const dt of newTypes) {
        const cap = specCapacity(sp.find((s) => s.type_id === dt))
        rows.push({
          key: `L${rows.length}`,
          name: variableNameForType(dt),
          type: 'continuous',
          initial: numText(cap.def ?? cap.min ?? 0),
          lower: numText(cap.min),
          upper: numText(cap.max),
          deviceType: dt,
        })
      }
    }
    setVariables(rows)
    setExistingDevices(
      (g?.devices ?? [])
        .filter((d) => d.kind === 'existing')
        .map((d) => ({ name: d.name, deviceType: d.device_type, capacity: deviceCapacity(d, g?.ports ?? []) })),
    )

    const objectives = cfg.objectives ?? []
    const first = objectives[0]
    setObjective(
      first && OBJECTIVE_OPTIONS.some((o) => o.value === first.metric) ? (first.metric as ObjectiveValue) : 'irr_after_tax',
    )

    // 约束: 后端格式 {type:'predefined'|'expression', payload} →
    // 预定义种类(load_satisfaction/capacity_limits)映射为开关; expression 进高级表达式列表。
    const constraints = cfg.constraints ?? []
    const knownKinds: Set<string> = new Set(PREDEFINED_CONSTRAINTS.map((p) => p.name))
    const next: Record<string, boolean> = {}
    for (const p of PREDEFINED_CONSTRAINTS) next[p.name] = false
    let seq = 0
    const custom: CustomExpr[] = []
    for (const raw of constraints) {
      const c = raw as { type?: unknown; payload?: Record<string, unknown>; name?: unknown; expression?: unknown }
      if (c.type === 'predefined') {
        const kind = typeof c.payload?.kind === 'string' ? c.payload.kind : ''
        if (knownKinds.has(kind)) next[kind] = true
      } else if (c.type === 'expression') {
        const expr = typeof c.payload?.expression === 'string' ? c.payload.expression : ''
        if (expr.trim()) {
          const name =
            typeof c.payload?.name === 'string' && c.payload.name.trim() ? c.payload.name.trim() : `expr${seq + 1}`
          custom.push({ key: `E${seq++}`, name, expression: expr })
        }
      } else if (typeof c.name === 'string' && typeof c.expression === 'string') {
        // 旧格式 {name, expression} 防御: 非预定义且非碳约束时按表达式处理
        if (!knownKinds.has(c.name) && c.name !== 'co2_cap') {
          custom.push({ key: `E${seq++}`, name: c.name, expression: c.expression })
        }
      }
    }
    setToggles(next)
    setCustomExprs(custom)

    let carbonValue = num(params, PARAM_KEYS.carbonCap)
    const carbonFromConstraint = constraints.some((raw) => {
      const c = raw as { type?: unknown; payload?: Record<string, unknown>; name?: unknown }
      const kind = c.type === 'predefined' ? c.payload?.kind : undefined
      if (kind === 'co2_cap' || c.name === 'co2_cap') {
        if (typeof c.payload?.max_tons === 'number' && Number.isFinite(c.payload.max_tons)) carbonValue = c.payload.max_tons
        return true
      }
      return false
    })
    setCarbonEnabled(carbonValue !== null || carbonFromConstraint)
    setCarbonCap(carbonValue !== null ? numText(carbonValue) : '')

    setAlgMode(params[PARAM_KEYS.algorithmMode] === 'manual' ? 'manual' : 'auto')
    setAlgorithm(ALGORITHM_IDS.includes(cfg.algorithm) ? cfg.algorithm : 'milp')
    setDiagnostics([])
    setNotice(null)
  }

  useEffect(() => {
    if (pid === undefined) {
      setLoading(false)
      return
    }
    // 显式收窄为 number(确保闭包内类型安全)
    const projectIdNum: number = pid
    let cancelled = false
    async function load() {
      setLoading(true)
      setError(null)
      try {
        // 全部 5 个请求彼此独立, 一次并行(原先分 3 段串行, 页面前置耗时是各段之和)
        const [cfgRes, algos, proj, g, sp] = await Promise.all([
          api.config.get(projectIdNum).catch(() => api.config.default(projectIdNum)),
          api.config.algorithms().catch(() => ({ items: [] as Array<{ name: string; label: string; description_key: string }> })),
          wbProject ? Promise.resolve(null) : api.projects.get(projectIdNum).catch(() => null),
          api.model.getGraph(projectIdNum).catch(() => null),
          api.model.deviceTypes().catch(() => [] as DeviceTypeSpec[]),
        ])
        if (cancelled) return
        applyConfig(cfgRes, algos.items, proj, g, sp)
      } catch (err) {
        if (!cancelled) setError(errorMessage(err))
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

  // -------------------------------------------------------------------------
  // 表单操作
  // -------------------------------------------------------------------------

  function updateVariable(key: string, part: 'name' | 'initial' | 'lower' | 'upper', value: string) {
    setVariables((prev) => prev.map((v) => (v.key === key ? { ...v, [part]: value } : v)))
  }

  function updateVariableType(key: string, type: VariableType) {
    setVariables((prev) => prev.map((v) => (v.key === key ? { ...v, type } : v)))
  }

  function addVariable() {
    setVariables((prev) => [
      ...prev,
      { key: `N${++SEQ}`, name: '', type: 'continuous', initial: '', lower: '', upper: '', deviceType: null },
    ])
  }

  function updateExpr(key: string, part: 'name' | 'expression', value: string) {
    setCustomExprs((prev) => prev.map((c) => (c.key === key ? { ...c, [part]: value } : c)))
  }

  function addExpr() {
    if (!newExpr.name.trim() || !newExpr.expression.trim()) return
    setCustomExprs((prev) => [...prev, { key: `N${++SEQ}`, name: newExpr.name.trim(), expression: newExpr.expression.trim() }])
    setNewExpr({ name: '', expression: '' })
  }

  /** 前端校验:输出定位到配置项的阻断/警告诊断(设计输入 §8.3 同风格)。 */
  function validateForm(): Diagnostic[] {
    const out: Diagnostic[] = []
    if (!name.trim()) out.push(makeDiag('name', 'CFG-NAME-001', 'ies.config.err.name_required', 'blocking'))
    const period = numOrNull(eco.evaluationPeriod)
    if (period !== null && (!Number.isInteger(period) || period < 1)) {
      out.push(makeDiag('evaluation_period_years', 'CFG-PERIOD-001', 'ies.config.err.period_invalid', 'blocking'))
    }
    const rateFields: Array<[string, string, string]> = [
      ['discountRate', 'discount_rate', pt('ies.config.discount_rate')],
      ['taxRate', 'tax_rate', pt('ies.config.tax_rate')],
    ]
    for (const [stateKey, fieldKey, label] of rateFields) {
      const v = numOrNull(eco[stateKey as 'discountRate' | 'taxRate'])
      if (v !== null && (v < 0 || v > 100)) {
        out.push(makeDiag(fieldKey, 'CFG-RATE-001', 'ies.config.err.rate_range', 'blocking', { field: label }))
      }
    }
    const depr = numOrNull(eco.depreciationYears)
    if (depr !== null && (!Number.isInteger(depr) || depr < 1)) {
      out.push(makeDiag('depreciation_years', 'CFG-DEPR-001', 'ies.config.err.depreciation_invalid', 'blocking'))
    }
    const minIrr = numOrNull(eco.minIrr)
    if (minIrr !== null && (minIrr < 0 || minIrr > 100)) {
      out.push(makeDiag('min_irr', 'CFG-MINIRR-001', 'ies.config.err.min_irr_range', 'blocking'))
    }
    const seen = new Set<string>()
    variables.forEach((v, idx) => {
      const field = `variable[${idx}]`
      const varName = v.name.trim()
      if (!varName) {
        out.push(makeDiag(field, 'CFG-VARNAME-001', 'ies.config.err.variable_name_required', 'blocking', { index: idx + 1 }))
        return
      }
      if (seen.has(varName)) {
        out.push(makeDiag(field, 'CFG-VARDUP-001', 'ies.config.err.variable_name_dup', 'blocking', { name: varName }))
        return
      }
      seen.add(varName)
      // 后端 _IDENT_RE 约束:变量名必须是合法标识符,否则保存必 422
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(varName)) {
        out.push(
          makeDiag(field, 'CFG-VARNAME-002', 'ies.config.err.variable_name_invalid', 'blocking', { name: varName }),
        )
        return
      }
      const lower = numOrNull(v.lower)
      const upper = numOrNull(v.upper)
      const initial = numOrNull(v.initial)
      if (initial === null) {
        out.push(
          makeDiag(field, 'CFG-VARINI-002', 'ies.config.err.variable_initial_required', 'blocking', { name: varName }),
        )
      }
      if (lower !== null && upper !== null && lower > upper) {
        out.push(makeDiag(field, 'CFG-VARBND-001', 'ies.config.err.variable_bounds', 'blocking', { name: varName }))
      }
      if (initial !== null && ((lower !== null && initial < lower) || (upper !== null && initial > upper))) {
        out.push(makeDiag(field, 'CFG-VARINI-001', 'ies.config.err.variable_initial_range', 'blocking', { name: varName }))
      }
    })
    if (carbonEnabled) {
      const cap = numOrNull(carbonCap)
      if (cap === null || cap <= 0) {
        out.push(makeDiag('carbon_cap', 'CFG-CARBON-001', 'ies.config.err.carbon_cap_invalid', 'blocking'))
      }
    }
    const issue = algMode === 'manual' ? algorithmIssue(algorithm, variables) : null
    if (issue) out.push(makeDiag('algorithm', 'CFG-ALG-001', issue.key, 'blocking', issue.params))
    for (const c of customExprs) {
      const name = String(c.name ?? '').trim()
      const expr = String(c.expression ?? '').trim()
      if (!name || !expr) {
        out.push(makeDiag(`constraint[${c.key}]`, 'CFG-EXPR-001', 'ies.config.err.expression_required', 'blocking'))
      }
    }
    const exprNames = new Set<string>()
    for (const c of customExprs) {
      const name = String(c.name ?? '').trim()
      if (!name) continue
      if (exprNames.has(name)) {
        out.push(makeDiag(`constraint[${c.key}]`, 'CFG-EXPRDUP-001', 'ies.config.err.expression_name_dup', 'blocking', {
          name,
        }))
      }
      exprNames.add(name)
    }
    return out
  }

  /** 表单 -> CalcConfigInput。 */
  function buildInput(): CalcConfigInput {
    const current = config
    const activeRows = variables.filter((v) => v.name.trim())
    // 变量 → 后端格式: 键名 min/max/initial(后端 _validate_variables 契约);
    // 类型映射 binary → boolean(后端 VARIABLE_TYPES)。
    const varRows: ConfigVariable[] = activeRows.map((v) => ({
      name: v.name.trim(),
      type: TYPE_TO_BACKEND[v.type],
      initial: numOrNull(v.initial),
      min: numOrNull(v.lower),
      max: numOrNull(v.upper),
    }))
    const initialMap: Record<string, number | null> = {}
    const typeMap: Record<string, string> = {}
    for (const v of activeRows) {
      initialMap[v.name.trim()] = numOrNull(v.initial)
      if (v.deviceType) typeMap[v.name.trim()] = v.deviceType
    }
    // 约束 → 后端格式 {type, payload}(services/config.py 契约):
    // 预定义种类直接映射 kind; 自定义表达式带 name/expression。
    const constraints: CalcConstraint[] = PREDEFINED_CONSTRAINTS.filter((p) => toggles[p.name]).map((p) => ({
      type: 'predefined',
      payload: { kind: p.name },
    }))
    for (const c of customExprs) {
      if (c.name.trim() && c.expression.trim()) {
        constraints.push({ type: 'expression', payload: { name: c.name.trim(), expression: c.expression.trim() } })
      }
    }
    const carbonValue = numOrNull(carbonCap)
    if (carbonEnabled && carbonValue !== null && carbonValue > 0) {
      constraints.push({ type: 'predefined', payload: { kind: 'co2_cap', max_tons: carbonValue } })
    }
    const minIrr = numOrNull(eco.minIrr)
    const params: Record<string, unknown> = {
      ...(current?.params ?? {}),
      [PARAM_KEYS.evaluationPeriod]: numOrNull(eco.evaluationPeriod),
      [PARAM_KEYS.discountRate]: pctToDecimal(eco.discountRate),
      [PARAM_KEYS.taxRate]: pctToDecimal(eco.taxRate),
      [PARAM_KEYS.depreciationYears]: numOrNull(eco.depreciationYears),
      [PARAM_KEYS.variableInitial]: initialMap,
      [PARAM_KEYS.variableDeviceType]: typeMap,
      [PARAM_KEYS.carbonCap]: carbonEnabled ? carbonValue : null,
      [PARAM_KEYS.algorithmMode]: algMode,
    }
    return {
      name: name.trim(),
      description: description.trim() || null,
      params,
      variables: varRows,
      objectives: [{ metric: objective, weight: 1, direction: 'max' }],
      constraints,
      min_irr: minIrr !== null ? Number((minIrr / 100).toFixed(6)) : null,
      algorithm: algMode === 'manual' ? algorithm : (current?.algorithm ?? 'milp'),
      solver: current?.solver ?? null,
      tolerances: current?.tolerances ?? {},
      random_seed: current?.random_seed ?? null,
    }
  }

  // -------------------------------------------------------------------------
  // 保存 / 校验 / 恢复默认
  // -------------------------------------------------------------------------

  async function handleSave() {
    if (pid === undefined) return
    const local = validateForm()
    setDiagnostics(local)
    if (local.some((d) => d.blocking)) return
    setSaving(true)
    setNotice(null)
    try {
      const saved = await api.config.save(pid, buildInput())
      setConfig(saved)
      setFrozen(saved.status === 'frozen')
      setNotice({ kind: 'success', text: pt('ies.config.saved_ok', { version: saved.version }) })
    } catch (err) {
      // 后端 422 返回 {diagnostics: [...]} 信封(client.ts 已透传到 params),
      // 展示完整诊断明细而非笼统的"未知错误"
      if (err instanceof ApiError && Array.isArray(err.params.diagnostics)) {
        setDiagnostics(err.params.diagnostics as Diagnostic[])
        setNotice({ kind: 'error', text: pt('ies.error.data_validation_failed') })
      } else {
        setNotice({ kind: 'error', text: errorMessage(err) })
      }
    } finally {
      setSaving(false)
    }
  }

  async function handleValidate() {
    if (pid === undefined) return
    setValidating(true)
    try {
      const res = await api.config.validate(pid)
      setDiagnostics(res.diagnostics)
      setNotice(res.valid ? { kind: 'success', text: t('ies.config.validate_ok') } : null)
    } catch (err) {
      setNotice({ kind: 'error', text: errorMessage(err) })
    } finally {
      setValidating(false)
    }
  }

  async function handleReset() {
    if (pid === undefined) return
    setSaving(true)
    try {
      const def = await api.config.default(pid)
      applyConfig(def, algorithms, project, graph, specs)
      setNotice({ kind: 'success', text: t('ies.config.default') })
    } catch (err) {
      setNotice({ kind: 'error', text: errorMessage(err) })
    } finally {
      setSaving(false)
    }
  }

  // -------------------------------------------------------------------------
  // 渲染
  // -------------------------------------------------------------------------

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

  const issue = algMode === 'manual' ? algorithmIssue(algorithm, variables) : null
  const selectedAlgoDesc = algorithms.find((a) => a.name === algorithm)?.description_key
  const hardIrr = numOrNull(eco.minIrr)

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--ies-space-4)' }}>
      <div className="ies-page-header">
        <div>
          <h1 className="ies-page-title">{t('ies.config.title')}</h1>
          <p className="ies-page-subtitle">
            {project ? `${project.name} · ${project.currency} · ${pt('ies.config.currency')}` : ''}
          </p>
        </div>
        <div className="ies-flex">
          <Button variant="ghost" size="md" onClick={() => void handleValidate()} loading={validating} disabled={saving}>
            {t('ies.config.validate')}
          </Button>
          <Button variant="ghost" size="md" onClick={() => void handleReset()} disabled={saving || validating}>
            {t('ies.config.default')}
          </Button>
          <Button variant="primary" size="md" onClick={() => void handleSave()} loading={saving} disabled={frozen || validating}>
            {t('ies.common.save')}
          </Button>
        </div>
      </div>

      {notice ? (
        <Alert variant={notice.kind} closable onClose={() => setNotice(null)}>
          {notice.text}
        </Alert>
      ) : null}
      {frozen ? <Alert variant="warning" title={t('ies.config.frozen')} /> : null}

      {/* 经济参数 */}
      <Card title={pt('ies.config.economic_params')}>
        <div className="ies-meta-grid">
          <FormField label={pt('ies.config.evaluation_period')} htmlFor="cfg-period">
            <Input
              id="cfg-period"
              type="number"
              min={1}
              step={1}
              value={eco.evaluationPeriod}
              disabled={frozen}
              onChange={(e) => setEco({ ...eco, evaluationPeriod: e.target.value })}
            />
          </FormField>
          <FormField label={pt('ies.config.discount_rate')} htmlFor="cfg-discount">
            <Input
              id="cfg-discount"
              type="number"
              min={0}
              max={100}
              step={0.1}
              value={eco.discountRate}
              disabled={frozen}
              onChange={(e) => setEco({ ...eco, discountRate: e.target.value })}
            />
          </FormField>
          <FormField label={pt('ies.config.tax_rate')} htmlFor="cfg-tax">
            <Input
              id="cfg-tax"
              type="number"
              min={0}
              max={100}
              step={0.1}
              value={eco.taxRate}
              disabled={frozen}
              onChange={(e) => setEco({ ...eco, taxRate: e.target.value })}
            />
          </FormField>
          <FormField label={pt('ies.config.depreciation_years')} htmlFor="cfg-depr">
            <Input
              id="cfg-depr"
              type="number"
              min={1}
              step={1}
              value={eco.depreciationYears}
              disabled={frozen}
              onChange={(e) => setEco({ ...eco, depreciationYears: e.target.value })}
            />
          </FormField>
          <FormField label={pt('ies.config.currency')} htmlFor="cfg-cur">
            {project ? (
              <div className="ies-flex">
                <Badge label={project.currency} variant="primary" />
                <span className="ies-form-message">{t(`ies.unit.${project.currency.toLowerCase()}`)}</span>
              </div>
            ) : (
              <span className="ies-form-message">—</span>
            )}
          </FormField>
          <FormField label={t('ies.config.min_irr')} htmlFor="cfg-minirr" hint={pt('ies.config.min_irr_note')}>
            <Input
              id="cfg-minirr"
              type="number"
              min={0}
              max={100}
              step={0.1}
              value={eco.minIrr}
              disabled={frozen}
              placeholder="0-100"
              onChange={(e) => setEco({ ...eco, minIrr: e.target.value })}
            />
          </FormField>
        </div>
      </Card>

      {/* 变量配置 */}
      <Card title={t('ies.config.variables')}>
        <h3 className="ies-config-section-title" style={{ marginTop: 0 }}>
          {pt('ies.config.new_device_variables')}
        </h3>
        <Table>
          <THead>
            <TR>
              <TH>{t('ies.common.name')}</TH>
              <TH>{t('ies.common.type')}</TH>
              <TH>{pt('ies.config.initial_value')}</TH>
              <TH>{pt('ies.config.lower_bound')}</TH>
              <TH>{pt('ies.config.upper_bound')}</TH>
              <TH>{t('ies.common.actions')}</TH>
            </TR>
          </THead>
          <TBody>
            {variables.length === 0 ? (
              <TR>
                <TD colSpan={6}>{t('ies.common.no_data')}</TD>
              </TR>
            ) : (
              variables.map((v, idx) => (
                <TR key={v.key}>
                  <TD>
                    <Input
                      aria-label={`${t('ies.common.name')} ${idx + 1}`}
                      value={v.name}
                      disabled={frozen}
                      onChange={(e) => updateVariable(v.key, 'name', e.target.value)}
                    />
                  </TD>
                  <TD>
                    <Select
                      aria-label={`${t('ies.common.type')} ${idx + 1}`}
                      value={v.type}
                      disabled={frozen}
                      onChange={(e) => updateVariableType(v.key, e.target.value as VariableType)}
                    >
                      <option value="continuous">{t('ies.config.variable_type_continuous')}</option>
                      <option value="binary">{t('ies.config.variable_type_binary')}</option>
                      <option value="integer">{t('ies.config.variable_type_integer')}</option>
                    </Select>
                  </TD>
                  <TD>
                    <Input
                      type="number"
                      aria-label={`${pt('ies.config.initial_value')} ${idx + 1}`}
                      value={v.initial}
                      disabled={frozen}
                      onChange={(e) => updateVariable(v.key, 'initial', e.target.value)}
                    />
                  </TD>
                  <TD>
                    <Input
                      type="number"
                      aria-label={`${pt('ies.config.lower_bound')} ${idx + 1}`}
                      value={v.lower}
                      disabled={frozen}
                      onChange={(e) => updateVariable(v.key, 'lower', e.target.value)}
                    />
                  </TD>
                  <TD>
                    <Input
                      type="number"
                      aria-label={`${pt('ies.config.upper_bound')} ${idx + 1}`}
                      value={v.upper}
                      disabled={frozen}
                      onChange={(e) => updateVariable(v.key, 'upper', e.target.value)}
                    />
                  </TD>
                  <TD>
                    <IconButton
                      aria-label={pt('ies.config.remove_variable')}
                      variant="ghost"
                      disabled={frozen}
                      onClick={() => setVariables((prev) => prev.filter((row) => row.key !== v.key))}
                    >
                      <Icon name="trash" size={14} />
                    </IconButton>
                  </TD>
                </TR>
              ))
            )}
          </TBody>
        </Table>
        <div className="ies-flex ies-flex--between" style={{ marginTop: 'var(--ies-space-3)' }}>
          <Button variant="secondary" size="sm" icon="plus" onClick={addVariable} disabled={frozen}>
            {pt('ies.config.add_variable')}
          </Button>
          <span className="ies-form-message">{pt('ies.config.variable_hint')}</span>
        </div>

        <hr className="ies-divider" />
        <h3 className="ies-config-section-title">{pt('ies.config.existing_devices_fixed')}</h3>
        {existingDevices.length === 0 ? (
          <p className="ies-form-message">{t('ies.common.no_data')}</p>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>{t('ies.common.name')}</TH>
                <TH>{t('ies.common.type')}</TH>
                <TH>{pt('ies.config.capacity')}</TH>
              </TR>
            </THead>
            <TBody>
              {existingDevices.map((d) => (
                <TR key={`${d.name}-${d.deviceType}`}>
                  <TD>{d.name}</TD>
                  <TD>
                    <span className="ies-mono">{d.deviceType}</span>
                  </TD>
                  <TD>
                    <div className="ies-flex">
                      <span>
                        {d.capacity !== null ? formatNumber(d.capacity, { digits: 0 }) : '—'}
                        {d.capacity !== null ? ' kW' : ''}
                      </span>
                      <Badge label={pt('ies.config.fixed')} variant="neutral" size="sm" />
                    </div>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        )}
      </Card>

      {/* 优化目标 */}
      <Card title={t('ies.config.objectives')}>
        <FormField label={pt('ies.config.primary_objective')} htmlFor="cfg-objective">
          <Select
            id="cfg-objective"
            value={objective}
            disabled={frozen}
            onChange={(e) => setObjective(e.target.value as ObjectiveValue)}
          >
            {OBJECTIVE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {pt(o.key)}
              </option>
            ))}
          </Select>
        </FormField>
        <hr className="ies-divider" />
        <h3 className="ies-config-section-title" style={{ marginTop: 0 }}>
          {pt('ies.config.carbon_target')}
        </h3>
        <div className="ies-flex" style={{ alignItems: 'flex-start' }}>
          <Checkbox
            id="cfg-carbon"
            checked={carbonEnabled}
            disabled={frozen}
            onChange={(e) => setCarbonEnabled(e.target.checked)}
            label={pt('ies.config.carbon_cap_enable')}
          />
          <Input
            type="number"
            min={0}
            step={1}
            style={{ maxWidth: 200 }}
            value={carbonCap}
            disabled={frozen || !carbonEnabled}
            aria-label={pt('ies.config.carbon_cap')}
            placeholder="5000"
            onChange={(e) => setCarbonCap(e.target.value)}
          />
          <span className="ies-form-message">{pt('ies.config.carbon_cap')}</span>
        </div>
      </Card>

      {/* 约束条件 */}
      <Card title={t('ies.config.constraints')}>
        <div className="ies-config-hard">
          <div className="ies-config-hard__title">
            <Badge label={pt('ies.config.hard_constraint')} variant="danger" icon="stop" />
            <span>{pt('ies.config.hard_constraint_notice')}</span>
          </div>
          <div className="ies-config-hard__body">
            {hardIrr !== null
              ? pt('ies.config.hard_irr_summary', { value: `${hardIrr}%` })
              : pt('ies.config.hard_irr_unset')}
          </div>
        </div>

        <h3 className="ies-config-section-title">{pt('ies.config.predefined_constraints')}</h3>
        <div>
          {PREDEFINED_CONSTRAINTS.map((p) => (
            <div key={p.name} className="ies-constraint-row">
              <Checkbox
                id={`cfg-con-${p.name}`}
                checked={toggles[p.name] ?? false}
                disabled={frozen}
                onChange={(e) => setToggles({ ...toggles, [p.name]: e.target.checked })}
                label={pt(p.commentKey)}
              />
              <div>
                <span className="ies-mono">{p.expression}</span>
              </div>
            </div>
          ))}
        </div>

        <hr className="ies-divider" />
        <Checkbox
          id="cfg-adv"
          checked={advancedMode}
          disabled={frozen}
          onChange={(e) => setAdvancedMode(e.target.checked)}
          label={pt('ies.config.advanced_mode')}
        />
        {advancedMode ? (
          <div style={{ marginTop: 'var(--ies-space-3)' }}>
            <Alert variant="info" title={pt('ies.config.advanced_mode')}>
              {pt('ies.config.advanced_hint')}
            </Alert>
            <div style={{ marginTop: 'var(--ies-space-3)' }}>
              {customExprs.map((c) => (
                <div key={c.key} className="ies-expr-row">
                  <Input
                    aria-label={pt('ies.config.expression_name')}
                    value={c.name}
                    disabled={frozen}
                    onChange={(e) => updateExpr(c.key, 'name', e.target.value)}
                  />
                  <Input
                    aria-label={pt('ies.config.expression')}
                    value={c.expression}
                    disabled={frozen}
                    placeholder={pt('ies.config.expression_placeholder')}
                    onChange={(e) => updateExpr(c.key, 'expression', e.target.value)}
                  />
                  <IconButton
                    aria-label={pt('ies.config.remove_expression')}
                    variant="ghost"
                    disabled={frozen}
                    onClick={() => setCustomExprs((prev) => prev.filter((row) => row.key !== c.key))}
                  >
                    <Icon name="trash" size={14} />
                  </IconButton>
                </div>
              ))}
              <div className="ies-expr-row">
                <Input
                  aria-label={pt('ies.config.expression_name')}
                  value={newExpr.name}
                  disabled={frozen}
                  placeholder={pt('ies.config.expression_name')}
                  onChange={(e) => setNewExpr({ ...newExpr, name: e.target.value })}
                />
                <Input
                  aria-label={pt('ies.config.expression')}
                  value={newExpr.expression}
                  disabled={frozen}
                  placeholder={pt('ies.config.expression_placeholder')}
                  onChange={(e) => setNewExpr({ ...newExpr, expression: e.target.value })}
                />
                <Button
                  variant="secondary"
                  size="sm"
                  icon="plus"
                  disabled={frozen || !newExpr.name.trim() || !newExpr.expression.trim()}
                  onClick={addExpr}
                >
                  {pt('ies.config.add_expression')}
                </Button>
              </div>
            </div>
          </div>
        ) : null}
      </Card>

      {/* 求解算法 */}
      <Card title={t('ies.config.algorithm')}>
        <FormField label={pt('ies.config.alg_mode')} htmlFor="cfg-algmode">
          <Select id="cfg-algmode" value={algMode} disabled={frozen} onChange={(e) => setAlgMode(e.target.value as 'auto' | 'manual')}>
            <option value="auto">{pt('ies.config.alg_auto')}</option>
            <option value="manual">{pt('ies.config.alg_manual')}</option>
          </Select>
        </FormField>
        {algMode === 'manual' ? (
          <>
            <FormField label={t('ies.config.algorithm')} htmlFor="cfg-algo">
              <Select id="cfg-algo" value={algorithm} disabled={frozen} onChange={(e) => setAlgorithm(e.target.value as AlgorithmId)}>
                {algorithms.length > 0
                  ? algorithms.map((a) => (
                      <option key={a.name} value={a.name}>
                        {a.label}
                      </option>
                    ))
                  : ALGORITHM_IDS.map((a) => (
                      <option key={a} value={a}>
                        {t(`ies.config.algorithm_${a}`)}
                      </option>
                    ))}
              </Select>
            </FormField>
            <div className="ies-flex" style={{ flexWrap: 'wrap' }}>
              <Badge
                label={`${pt('ies.config.alg_capability')}: ${
                  ALGORITHM_CAPABILITIES[algorithm].types.includes('binary') ||
                  ALGORITHM_CAPABILITIES[algorithm].types.includes('integer')
                    ? pt('ies.config.alg_cap_discrete')
                    : pt('ies.config.alg_cap_continuous')
                }`}
                variant="info"
              />
              {algorithm === 'custom' ? <Badge label={pt('ies.config.alg_custom_note')} variant="warning" /> : null}
            </div>
            {selectedAlgoDesc ? <p className="ies-form-message">{pt(selectedAlgoDesc)}</p> : null}
            {issue ? <Alert variant="error" title={pt(issue.key, issue.params)} /> : null}
          </>
        ) : (
          <p className="ies-form-message">{pt('ies.config.alg_auto')}</p>
        )}
      </Card>

      {/* 校验诊断(定位到配置项) */}
      {diagnostics.length > 0 ? (
        <Card title={pt('ies.config.validation_diagnostics')}>
          <DiagnosticsList diagnostics={diagnostics} />
        </Card>
      ) : null}
    </div>
  )
}

export default ConfigPage
