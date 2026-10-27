/**
 * 任务 feature 表单状态与提交装配(纯函数; 原 TasksPage 内联实现逐字迁移)。
 *
 * CreateForm 是草稿 task_working_inputs 的可编辑镜像; workingOfForm /
 * formFromWorking 只做形态转换(未完成编辑态原文保留, 缺席取显式空态),
 * 不做领域校验(提交边界与后端为权威); buildParams/parseEvaluation/
 * logicalSigOf/buildInput 组装提交输入(评价确认顶层字段、幂等键装配口径
 * 与旧行为一致)。全部函数为纯函数: 不访问网络、缓存或 React 状态。
 * 脏标记/防抖/切换序列等时序协调仍归页面, 不在此层。
 */

import type {
  CalcConfig,
  EvaluationConstructionPayment,
  EvaluationMaintenanceExpense,
  TaskCreateInput,
  TaskType,
} from '../../types'

/** 可提交的任务类型(方案评价 / 规划优化 / 不确定性分析 / 批量扫描)。 */
export const SUBMITTABLE_TYPES: TaskType[] = ['calc', 'optimization', 'uncertainty', 'analysis']

export type UncertaintyMode = 'fixed_reliability' | 'replan_sensitivity'
export type DistType = 'normal' | 'triangular' | 'uniform'

/** 批量分析扫描规格行(param_path → 扫描值列表; 后端 03 §8.2 task_params.sweeps)。 */
export interface SweepRow {
  /** 参数路径(如 calc_config.params.discount_rate)。 */
  path: string
  /** 逗号分隔的扫描值(如 0.06,0.08,0.10)。 */
  values: string
  /** 可选单位(如 -、%、CNY/kWh)。 */
  unit: string
}

/**
 * 提交对话框工作表单(草稿 task_working_inputs 的可编辑镜像; 数据集版本
 * 清单唯一权威是草稿 dataset_bindings, 不在此选择——旧 datasetIds 勾选
 * 后端从未消费, 已删除)。
 */
export interface CreateForm {
  type: TaskType
  constructionPeriod: string
  operationYears: string
  discountRate: string
  constructionPayments: string
  maintenanceExpenses: string
  uMode: UncertaintyMode
  nSamples: number
  seed: string
  distType: DistType
  noisePct: number
  sweeps: SweepRow[]
}

export const DEFAULT_FORM: CreateForm = {
  type: 'calc',
  constructionPeriod: '',
  operationYears: '',
  discountRate: '',
  constructionPayments: '',
  maintenanceExpenses: '',
  uMode: 'fixed_reliability',
  nSamples: 200,
  seed: '',
  distType: 'normal',
  noisePct: 5,
  sweeps: [{ path: '', values: '', unit: '' }],
}

/**
 * 表单 → 草稿工作输入(键为 snake_case 业务名, 值是用户所填原文; 不含数据集选择)。
 *
 * 后端只做容器级存放、不解析: 未完成的编辑态(空串/非法 JSON)如实存放,
 * 提交时由边界严格解析, 不在此预判。task_params 与冻结快照同形
 * ({params: 内层}, 见提交装配): 不确定性/批量分析写内层, 其他类型省略
 * (与冻结缺席一致); seed 存原文(提交时再转数值), sweeps 行存原文
 * (读取时文本直用、数组 join)。
 */
export function workingOfForm(f: CreateForm): Record<string, unknown> {
  const working: Record<string, unknown> = {
    task_type: f.type,
    construction_period: f.constructionPeriod,
    operation_years: f.operationYears,
    discount_rate: f.discountRate,
    construction_payments: f.constructionPayments,
    maintenance_expenses: f.maintenanceExpenses,
  }
  if (f.type === 'uncertainty') {
    const inner: Record<string, unknown> = {
      mode: f.uMode,
      n_samples: f.nSamples,
      distribution: { type: f.distType, noise_pct: f.noisePct },
    }
    if (f.seed.trim() !== '') inner.seed = f.seed.trim()
    working.task_params = { params: inner }
  } else if (f.type === 'analysis') {
    working.task_params = {
      params: {
        sweeps: f.sweeps.map((s) => ({ param_path: s.path, values: s.values, unit: s.unit })),
      },
    }
  }
  return working
}

/**
 * 工作输入任务级参数 → 表单类型相关字段(未知/非法取默认, 不猜测)。
 *
 * 读冻结同形({params: 内层}; 内层键与提交装配一致)。seed 接受文本(自存)
 * 与数值(冻结), 提交时统一转数值; sweeps 值接受文本(自存)与数组(冻结)。
 */
export function applyWorkingTaskParams(next: CreateForm, type: TaskType, tp: Record<string, unknown>): void {
  const rawInner = tp.params
  const inner =
    typeof rawInner === 'object' && rawInner !== null ? (rawInner as Record<string, unknown>) : null
  if (inner === null) return
  if (type === 'uncertainty') {
    if (inner.mode === 'fixed_reliability' || inner.mode === 'replan_sensitivity') next.uMode = inner.mode
    if (typeof inner.n_samples === 'number' && Number.isInteger(inner.n_samples) && inner.n_samples > 0) {
      next.nSamples = inner.n_samples
    }
    if (typeof inner.seed === 'string') next.seed = inner.seed
    else if (typeof inner.seed === 'number' && Number.isFinite(inner.seed)) next.seed = String(inner.seed)
    const dist = inner.distribution
    if (typeof dist === 'object' && dist !== null) {
      const dd = dist as Record<string, unknown>
      if (dd.type === 'normal' || dd.type === 'triangular' || dd.type === 'uniform') next.distType = dd.type
      if (typeof dd.noise_pct === 'number' && Number.isFinite(dd.noise_pct)) next.noisePct = dd.noise_pct
    }
  }
  if (type === 'analysis' && Array.isArray(inner.sweeps)) {
    const rows: SweepRow[] = []
    for (const s of inner.sweeps) {
      if (typeof s !== 'object' || s === null) continue
      const r = s as Record<string, unknown>
      if (typeof r.param_path !== 'string') continue
      const values =
        typeof r.values === 'string'
          ? r.values
          : Array.isArray(r.values)
            ? (r.values as unknown[]).map((x) => String(x)).join(',')
            : null
      if (values === null) continue
      rows.push({
        path: r.param_path,
        values,
        unit: typeof r.unit === 'string' ? r.unit : '',
      })
    }
    if (rows.length > 0) next.sweeps = rows
  }
}

/**
 * 草稿工作输入 → 表单(整体替换; 未知/缺席取显式空态, 不混入他任务字段)。
 *
 * 后端存的是业务值原样(数字/清单/定点字符串)或用户编辑原文, 显示形态由
 * 此处统一负责: 文本直用, 有限数转文本, 清单转 JSON 文本(付款 ratio 定点
 * 字符串转 JSON number, 与提交边界口径一致); 其余形态记空文本, 由提交
 * 边界明确拒绝, 不猜测。
 */
export function formFromWorking(working: Record<string, unknown>): CreateForm {
  const text = (v: unknown): string => {
    if (typeof v === 'string') return v
    if (typeof v === 'number' && Number.isFinite(v)) return String(v)
    if (Array.isArray(v)) return JSON.stringify(v)
    return ''
  }
  const paymentsText = (v: unknown): string => {
    if (typeof v === 'string') return v
    if (!Array.isArray(v)) return ''
    const rows = v.map((row) => {
      if (typeof row !== 'object' || row === null) return row
      const r = { ...(row as Record<string, unknown>) }
      const ratio = (row as Record<string, unknown>).ratio
      if (typeof ratio === 'string' && ratio.trim() !== '') {
        const n = Number(ratio)
        if (Number.isFinite(n)) r.ratio = n
      }
      return r
    })
    return JSON.stringify(rows)
  }
  const next: CreateForm = {
    ...DEFAULT_FORM,
    sweeps: DEFAULT_FORM.sweeps.map((s) => ({ ...s })),
  }
  if ((SUBMITTABLE_TYPES as string[]).includes(working.task_type as string)) {
    next.type = working.task_type as TaskType
  }
  next.constructionPeriod = text(working.construction_period)
  next.operationYears = text(working.operation_years)
  next.discountRate = text(working.discount_rate)
  next.constructionPayments = paymentsText(working.construction_payments)
  next.maintenanceExpenses = text(working.maintenance_expenses)
  const tp = working.task_params
  if (typeof tp === 'object' && tp !== null) {
    applyWorkingTaskParams(next, next.type, tp as Record<string, unknown>)
  }
  return next
}

/**
 * 已保存配置存在性(A1 仅判存在: revision 只是配置版本链序号, 不是可提交
 * 的配置 id, 不得映射、不随任务发送; 任务引用绑定为 0.12-A2 范围)。
 */
export function hasSavedConfig(cfg: CalcConfig | null): boolean {
  return cfg !== null && typeof cfg.revision === 'number' && cfg.revision > 0
}

/** 确定性哈希(FNV 变体),用于生成客户端幂等键。 */
export function stableHash(input: string): string {
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
export function serializeParams(params: Record<string, unknown> | undefined): string {
  if (!params) return ''
  const sorted: Record<string, unknown> = {}
  for (const key of Object.keys(params).sort()) {
    sorted[key] = params[key]
  }
  return JSON.stringify(sorted)
}

/** 新逻辑提交的一次性随机段(幂等键允许字符集内; 时间 + 随机, 碰撞可忽略)。 */
export function newSubmitNonce(): string {
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
}

/** 表单任务附加参数装配(不确定性/批量分析; 其余类型无附加参数)。 */
export function buildParams(f: CreateForm): Record<string, unknown> | undefined {
  if (f.type === 'uncertainty') {
    const params: Record<string, unknown> = {
      mode: f.uMode,
      n_samples: f.nSamples,
      distribution: { type: f.distType, noise_pct: f.noisePct },
    }
    const seed = Number(f.seed.trim())
    if (Number.isFinite(seed)) params.seed = seed
    return params
  }
  if (f.type === 'analysis') {
    // 批量扫描: 构造 task_params.sweeps(03 §8.2), 与后端 executors 格式一致
    const sweeps = f.sweeps
      .filter((s) => s.path.trim() && s.values.trim())
      .map((s) => {
        const sweep: Record<string, unknown> = {
          param_path: s.path.trim(),
          values: s.values
            .split(',')
            .map((v) => v.trim())
            .filter((v) => v !== '')
            .map((v) => Number(v)),
        }
        if (s.unit.trim()) sweep.unit = s.unit.trim()
        return sweep
      })
    if (sweeps.length === 0) return { sweeps: [] }
    return { sweeps }
  }
  return undefined
}

/** 评价确认解析结果(提交边界必填形态)。 */
export interface ParsedEvaluation {
  evaluation_years: number
  construction_period: number
  operation_years: number
  discount_rate: number
  construction_payments: EvaluationConstructionPayment[]
  maintenance_expenses: EvaluationMaintenanceExpense[]
}

/** 评价确认解析(提交边界必填; 空值/非法返回 null 由调用方提示, 后端仍为权威校验)。
 * 空字符串经 Number() 会静默变为 0, 必须先判空, 不得直接转数字。
 * 折现率为宪法 §7.3 比例口径(0..1), 与后端 TASK-REQ-015 同口径。
 * 评价年限不手填: 由建设期 K + 运营年限 N 自动派生 E=K+N 并随提交
 * 明确发送(关系成立由后端核对)。付款与维护为显式 JSON(无默认值;
 * 无维护支出填 []), 形态由后端边界核对。合法的数值写法(如 '+1'、
 * 首尾空格、'0.080')按 Number() 接受, 展示 E 与提交采用同一数值口径。 */
export type EvaluationField = 'constructionPeriod' | 'operationYears' | 'discountRate' | 'constructionPayments' | 'maintenanceExpenses'

const validConstructionPeriod = (value: number) => Number.isInteger(value) && value >= 0
const validOperationYears = (value: number) => Number.isInteger(value) && value >= 1

/** 年限展示只取决于 K/N；其他评价字段仍由提交解析核对。 */
export function deriveEvaluationYears(f: Pick<CreateForm, 'constructionPeriod' | 'operationYears'>): number | null {
  if (!f.constructionPeriod.trim() || !f.operationYears.trim()) return null
  const period = Number(f.constructionPeriod.trim())
  const operation = Number(f.operationYears.trim())
  return validConstructionPeriod(period) && validOperationYears(operation) ? period + operation : null
}

/** 即时输入诊断；付款条目等领域约束仍由后端核对。 */
export function evaluationFieldErrors(f: CreateForm): Partial<Record<EvaluationField, string>> {
  const errors: Partial<Record<EvaluationField, string>> = {}
  const numeric = (key: EvaluationField, valid: (value: number) => boolean, message: string) => {
    const raw = String(f[key]).trim()
    if (!raw) errors[key] = 'ies.task.field_required'
    else if (!valid(Number(raw))) errors[key] = message
  }
  numeric('constructionPeriod', validConstructionPeriod, 'ies.task.construction_period_invalid')
  numeric('operationYears', validOperationYears, 'ies.task.operation_years_invalid')
  numeric('discountRate', (v) => Number.isFinite(v) && v >= 0 && v <= 1, 'ies.task.discount_rate_invalid')
  for (const key of ['constructionPayments', 'maintenanceExpenses'] as const) {
    if (!f[key].trim()) errors[key] = 'ies.task.field_required'
    else {
      try {
        if (!Array.isArray(JSON.parse(f[key]))) errors[key] = 'ies.task.json_array_invalid'
      } catch {
        errors[key] = 'ies.task.json_array_invalid'
      }
    }
  }
  return errors
}

export function parseEvaluation(f: CreateForm): ParsedEvaluation | null {
  if (Object.keys(evaluationFieldErrors(f)).length > 0) return null
  const period = Number(f.constructionPeriod.trim())
  const operation = Number(f.operationYears.trim())
  return {
    evaluation_years: period + operation,
    construction_period: period,
    operation_years: operation,
    discount_rate: Number(f.discountRate.trim()),
    construction_payments: JSON.parse(f.constructionPayments) as EvaluationConstructionPayment[],
    maintenance_expenses: JSON.parse(f.maintenanceExpenses) as EvaluationMaintenanceExpense[],
  }
}

/** 逻辑签名(类型 + 所引 revision + 参数指纹): 仅用于重复提交提示的
 * 同输入判断, 不是幂等身份(幂等身份是完整幂等键, 由后端裁决)。 */
export function logicalSigOf(f: CreateForm, savedRevision: number | null): string {
  const params = buildParams(f)
  // 整个评价签名必须落在后端幂等键格式集 [A-Za-z0-9._:-] 内且有界:
  // 合法表单取已解析规范值(K/N/费率规范文本 + 付款/维护 JSON 指纹),
  // 不同写法但同数值(如 '+1' 与 '1')得同一签名; 非法表单取原始文本
  // 指纹(仍合法有界, 仅用于对话框内提示, 提交仍走输入反馈)。
  // 不另加数值上限, 不放宽后端键格式, 不新增安全哈希。
  const parsed = parseEvaluation(f)
  const evalSig =
    parsed === null
      ? `x${stableHash(`${f.constructionPeriod.trim()}|${f.operationYears.trim()}|${f.discountRate.trim()}|${f.constructionPayments.trim()}|${f.maintenanceExpenses.trim()}`)}`
      : `${parsed.construction_period}_${parsed.operation_years}_${String(parsed.discount_rate)}_${stableHash(JSON.stringify(parsed.construction_payments))}_${stableHash(JSON.stringify(parsed.maintenance_expenses))}`
  return `sub-${f.type}-r${savedRevision ?? 'none'}-${stableHash(serializeParams(params))}-e${evalSig}`
}

/** 表单 → 提交输入(幂等键 = 逻辑签名 + 本次提交一次性随机段)。 */
export function buildInput(
  f: CreateForm,
  projectId: number,
  savedRevision: number | null,
  submitNonce: string,
): TaskCreateInput {
  const params = buildParams(f)
  // 幂等键 = 逻辑签名 + 本次提交一次性随机段: 每次明确发起的新逻辑
  // 提交(对话框打开)换新键, 参数变更/项目版本变化永不误复用历史任务;
  // 同一提交的网络重试复用原键, 由后端按同键返回同一任务。
  const idemKey = `${logicalSigOf(f, savedRevision)}-${submitNonce}`
  const evaluation = parseEvaluation(f)
  return {
    project_id: projectId,
    type: f.type,
    ...(savedRevision !== null ? { config_revision: savedRevision } : {}),
    ...(evaluation ? { evaluation } : {}),
    idempotency_key: idemKey,
    ...(params ? { params } : {}),
  }
}
