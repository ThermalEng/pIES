/**
 * pIES 前端领域类型定义。
 *
 * 与开发者指南中的领域模型及接口契约保持一致:
 * - 实体主键一律为 BIGINT,前端用 number。
 * - 时间字段一律 ISO8601 字符串(UTC),展示时经 lib/format 按用户时区格式化。
 * - 后端只输出 message_key + params,文案由前端 i18n 渲染(契约 P3)。
 */

// ---------------------------------------------------------------------------
// 通用基础
// ---------------------------------------------------------------------------

/** 实体主键(后端 BIGINT)。 */
export type EntityId = number

/** ISO8601 时间戳字符串。 */
export type ISO8601 = string

/** 分页结果(游标分页,与任务列表 API 语义一致)。 */
export interface PageResult<T> {
  items: T[]
  next_cursor: string | null
  limit: number
}

// ---------------------------------------------------------------------------
// 诊断与错误(与后端 core/diagnostics + core/errors 对齐)
// ---------------------------------------------------------------------------

/** 诊断严重度(04 §5.2):阻断 / 错误 / 警告 / 信息。 */
export type Severity = 'blocking' | 'error' | 'warning' | 'info'

/** 诊断定位信息。 */
export interface DiagnosticLocation {
  object_type: string
  object_id: string | null
  field: string | null
  row: number | number[] | null
}

/** 诊断对象(04 §5.4,后端统一产出,前端按 locale + message_key + params 渲染)。 */
export interface Diagnostic {
  code: string
  message_key: string
  params: Record<string, unknown>
  severity: Severity
  blocking: boolean
  location: DiagnosticLocation | null
  fix_hint_key: string | null
  ref_ids: string[]
  occurred_at: ISO8601
  source: string | null
  trace_id: string | null
  project_id: EntityId | null
  task_id: EntityId | null
  suppressed: boolean | null
}

/** 后端错误信封体 {"error": {...}}。 */
export interface ApiErrorBody {
  code: string
  message_key: string
  severity: Severity
  blocking: boolean
  params: Record<string, unknown>
  location: DiagnosticLocation | null
  fix_hint_key: string | null
  ref_ids: string[]
}

/** 前端统一抛出的 API 错误。 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly severity: Severity
  readonly blocking: boolean
  readonly message_key: string
  readonly params: Record<string, unknown>
  readonly location: DiagnosticLocation | null
  readonly fix_hint_key: string | null
  readonly ref_ids: string[]

  constructor(status: number, body: ApiErrorBody | null, fallbackKey = 'ies.error.unknown') {
    super(body?.message_key ?? fallbackKey)
    this.name = 'ApiError'
    this.status = status
    this.code = body?.code ?? 'API-UNKNOWN'
    this.severity = body?.severity ?? 'error'
    this.blocking = body?.blocking ?? true
    this.message_key = body?.message_key ?? fallbackKey
    this.params = body?.params ?? {}
    this.location = body?.location ?? null
    this.fix_hint_key = body?.fix_hint_key ?? null
    this.ref_ids = body?.ref_ids ?? []
  }
}

// ---------------------------------------------------------------------------
// 账号与认证(U01 身份写入单元)
// ---------------------------------------------------------------------------

export type UserStatus = 'active' | 'disabled' | 'locked'

export interface User {
  id: EntityId
  username: string
  display_name: string
  email: string | null
  status: UserStatus
  locale: string
  timezone: string
  fixed_utc_offset_minutes: number
  credential_version: number
  is_system: boolean
  created_at: ISO8601
  updated_at: ISO8601 | null
  last_login_at: ISO8601 | null
}

export interface LoginRequest {
  username: string
  password: string
  remember?: boolean
}

export interface RegisterRequest {
  username: string
  display_name: string
  email?: string
  password: string
}

/** 登录页公开设置(无需认证; 不包含任何内部细节)。 */
export interface PublicAuthSettings {
  registration_enabled: boolean
  sso_enabled: boolean
  sso_provider_name: string
}

// ---------------------------------------------------------------------------
// 项目(U03 项目写入单元)
// ---------------------------------------------------------------------------

export type ProjectStatus = 'active' | 'archived' | 'deleted'

export type Currency = 'CNY' | 'USD'

export type ProjectRole = 'owner' | 'viewer'

export interface Project {
  id: EntityId
  name: string
  description: string | null
  status: ProjectStatus
  owner_id: EntityId
  currency: Currency
  /** 项目固定 UTC 偏移(分钟),所有时序数据按此偏移解释。 */
  fixed_utc_offset_minutes: number
  schema_version: number
  /** 管理员访问授权(所有者控制): true = 管理员可查看细节并转移所有权。 */
  admin_access: boolean
  current_draft_id: EntityId | null
  current_version_id: EntityId | null
  created_at: ISO8601
  updated_at: ISO8601 | null
  created_by: EntityId
  /** 草稿摘要(GET /projects/{id} 响应含 draft;缺省缺省时无)。 */
  draft?: ProjectDraft
  /** 当前登录用户在本项目的角色(列表接口冗余返回)。 */
  role?: ProjectRole
}

export interface ProjectCreateInput {
  name: string
  description?: string
  currency: Currency
  fixed_utc_offset_minutes?: number
}

export interface ProjectMember {
  user_id: EntityId
  username: string
  display_name: string
  role: ProjectRole
  granted_at: ISO8601
}

export interface ProjectDraft {
  id: EntityId
  project_id: EntityId
  revision: number
  content_hash: string
  is_current: boolean
  updated_by: EntityId
  updated_at: ISO8601
  created_at: ISO8601
  /** 草稿内容摘要:数据集绑定清单(U03 dataset.bind 语义命令写入)。 */
  dataset_bindings?: Array<{
    dataset_version_id: number
    dataset_id?: number | null
    role?: string | null
    note?: string | null
  }>
}

export interface ProjectVersion {
  id: EntityId
  project_id: EntityId
  version_no: number
  name: string
  description: string | null
  created_by: EntityId
  created_at: ISO8601
  parent_version_id: EntityId | null
  source_draft_id: EntityId | null
  source_draft_revision: number | null
  reason: string
  fixed_utc_offset_minutes: number
  currency: Currency
  schema_version: number
  content_hash: string
}

/** 项目列表筛选参数。 */
export interface ProjectListParams {
  status?: ProjectStatus
  search?: string
  cursor?: string
  limit?: number
}

// ---------------------------------------------------------------------------
// 系统模型(U04 模型写入单元)
// ---------------------------------------------------------------------------

export interface SystemGraph {
  id: EntityId
  project_id: EntityId
  draft_id: EntityId | null
  project_version_id: EntityId | null
  name: string
  graph_hash: string
  created_by: EntityId
  created_at: ISO8601
}

/** 设备模型精度(04 §7: P1 简化线性 / P2 标准 / P3 详细非线性)。 */
export type Fidelity = 'low' | 'medium' | 'high'

/** 设备属性:存量(existing)或新增(new),规划选型的核心区分。 */
export type DeviceKind = 'existing' | 'new'

export interface Device {
  id: EntityId
  graph_id: EntityId
  device_type: string
  kind: DeviceKind
  name: string
  description: string | null
  /** 参数(容量、效率曲线、投资/运维成本等),键与模式版本由 schema_version 约定。 */
  params: Record<string, unknown>
  model_fidelity: Fidelity
  status: 'active' | 'retired'
  created_at: ISO8601
  updated_at: ISO8601 | null
}

export interface DeviceInput {
  device_type: string
  kind: DeviceKind
  name: string
  description?: string
  params?: Record<string, unknown>
  model_fidelity?: Fidelity
}

export type PortType = 'electric' | 'thermal' | 'cooling' | 'fuel' | 'water' | 'data'

export type PortDirection = 'in' | 'out' | 'bidirectional'

export interface Port {
  id: EntityId
  device_id: EntityId
  port_type: PortType
  direction: PortDirection
  name: string
  /** 容量(单位由设备类型约定:kW/MW/GJ/h 等)。 */
  capacity: number | null
  params: Record<string, unknown>
}

export type ConnType =
  | 'electric_line'
  | 'thermal_pipe'
  | 'cooling_pipe'
  | 'fuel_pipe'
  | 'data_link'

export interface Connection {
  id: EntityId
  graph_id: EntityId
  from_port_id: EntityId
  to_port_id: EntityId
  conn_type: ConnType
  capacity: number | null
  loss_rate: number
  params: Record<string, unknown>
}

export interface ConnectionInput {
  from_port_id: EntityId
  to_port_id: EntityId
  conn_type: ConnType
  capacity?: number
  loss_rate?: number
  params?: Record<string, unknown>
}

/** 系统图完整模型(节点/设备/端口/连接)。 */
export interface GraphModel {
  graph: SystemGraph
  devices: Device[]
  ports: Port[]
  connections: Connection[]
}

// ---------------------------------------------------------------------------
// 设备注册表(04 §2/§3)
// ---------------------------------------------------------------------------

export type EnergyCarrier = 'electric' | 'heat' | 'cool' | 'gas' | 'solar'

export interface ParameterSpec {
  unit: string | null
  min: number | null
  max: number | null
  /** 默认值:枚举参数为字符串字面量(如 heat_pump.mode='both'),数值参数为 number,
   *  结构化参数为对象(如 grid_connection.import_tariff={peak,flat,valley})。 */
  default: number | string | Record<string, number> | null
  is_optimizable: boolean
  /** 存量默认值(与 default 同型)。 */
  existing_default: number | string | Record<string, number> | null
  help_key: string
  /** 可选枚举值列表(后端 /api/registry/device-types 透出,前端按此渲染下拉/单选)。 */
  enum?: Array<string | number | boolean> | null
}

/** 服务器端口声明(RR-P1-04: 来自设备 YAML 公开 descriptor, 前端画布按此渲染句柄)。 */
export interface DevicePortSpec {
  name: string
  port_type: PortType
  direction: 'in' | 'out' | 'bidirectional'
  energy_carrier: EnergyCarrier
  capacity_ref: string | null
}

export interface DeviceTypeSpec {
  type_id: string
  version: string
  name_zh: string
  name_en: string
  energy_carriers: EnergyCarrier[]
  is_load: boolean
  capabilities: string[]
  model_method: 'mechanism' | 'data_repeat' | 'data_predict'
  stateful: boolean
  ports: DevicePortSpec[]
  parameters: Record<string, ParameterSpec>
}

// ---------------------------------------------------------------------------
// 数据集(U05 数据集写入单元)
// ---------------------------------------------------------------------------

export type DatasetStatus = 'draft' | 'published' | 'deprecated'

export type Timeline = 'hourly' | 'quarter_hourly' | 'daily' | 'monthly' | 'yearly' | 'custom'

export interface Dataset {
  id: EntityId
  /** NULL 表示共享/全局数据集。 */
  project_id: EntityId | null
  name: string
  description: string | null
  status: DatasetStatus
  default_license: string | null
  created_by: EntityId
  created_at: ISO8601
  updated_at: ISO8601 | null
}

export interface DatasetField {
  name: string
  type: 'float' | 'int' | 'text' | 'datetime'
  unit: string | null
  description?: string
}

export interface QualityReport {
  missing_rate: number | null
  outlier_rate: number | null
  interpolation_notes: string[] | null
}

export interface DatasetVersion {
  id: EntityId
  dataset_id: EntityId
  version_no: number
  timeline: Timeline
  resolution: string
  fixed_utc_offset_minutes: number
  /** 字段定义(名称 -> 字段)。 */
  fields: Record<string, DatasetField>
  /** 字段单位表(与 fields 一一对应)。 */
  units: Record<string, string>
  quality_report: QualityReport | null
  provenance: Record<string, unknown> | null
  license: string | null
  content_hash: string
  created_by: EntityId
  created_at: ISO8601
  created_reason: string | null
}

export interface DatasetFile {
  id: EntityId
  dataset_version_id: EntityId
  file_kind: 'data' | 'header' | 'manifest' | 'metadata'
  format: 'parquet' | 'csv' | 'json'
  row_count: number
  size_bytes: number
  created_at: ISO8601
}

/** 数据预览(样本行,表头 + 行数据)。 */
export interface DatasetSample {
  headers: string[]
  rows: unknown[][]
  total_rows: number
}

export interface DatasetUploadInput {
  project_id?: EntityId
  name: string
  description?: string
  timeline?: Timeline
  file: File
}

// ---------------------------------------------------------------------------
// 计算配置(U06 配置写入单元)
// ---------------------------------------------------------------------------

export type AlgorithmId = 'milp' | 'lp' | 'heuristic' | 'ga' | 'exhaustive' | 'custom'

/** 前端表单变量类型(UI 概念;binary 在提交时映射为后端 boolean)。 */
export type VariableType = 'continuous' | 'binary' | 'integer'

/** 后端变量类型(services/config.py VARIABLE_TYPES)。 */
export type BackendVariableType = 'continuous' | 'integer' | 'boolean' | 'enum'

export interface ConfigVariable {
  name: string
  type: BackendVariableType
  /** 初始值(continuous/integer 必填;boolean 需 0/1)。 */
  initial: number | null
  /** 下界(与后端 min 对应)。 */
  min: number | null
  /** 上界(与后端 max 对应)。 */
  max: number | null
}

export interface Objective {
  /** 目标指标 id(与后端 OBJECTIVE_METRICS 一致: irr_after_tax / npv_after_tax 等)。 */
  metric: string
  /** 多目标加权系数。 */
  weight: number
  direction: 'min' | 'max'
}

export interface CalcConstraint {
  /** 约束类型(后端 U06 格式): predefined 预定义种类 / expression 受限表达式。 */
  type: 'predefined' | 'expression'
  /** 约束载荷: predefined 用 kind(co2_cap 含 max_tons); expression 用 name/expression。 */
  payload: {
    /** 预定义约束种类(load_satisfaction / capacity_limits / co2_cap / energy_cost_cap)。 */
    kind?: string
    /** co2_cap 的年碳排放上限(tCO₂/年)。 */
    max_tons?: number
    /** energy_cost_cap 的年购能费用上限。 */
    max_amount?: number
    /** 表达式约束名称(展示用)。 */
    name?: string
    /** 表达式约束声明(受限语法, 见 04 §4)。 */
    expression?: string
  }
}

export interface CalcConfig {
  id: EntityId
  project_id: EntityId
  name: string
  description: string | null
  /** 参数当前值(电价、燃料价、贴现率等)。 */
  params: Record<string, unknown>
  variables: ConfigVariable[]
  objectives: Objective[]
  constraints: CalcConstraint[]
  min_irr: number | null
  algorithm: AlgorithmId
  solver: string | null
  /** 容差(最优性间隙 MIPGap、可行性容差等)。 */
  tolerances: Record<string, number>
  random_seed: number | null
  status: 'draft' | 'frozen'
  version: number
  updated_by: EntityId
  created_at: ISO8601
  updated_at: ISO8601 | null
}

export type CalcConfigInput = Omit<
  CalcConfig,
  'id' | 'project_id' | 'status' | 'version' | 'updated_by' | 'created_at' | 'updated_at'
>

export interface AlgorithmSpec {
  name: string
  label_zh: string
  label_en: string
  description_key: string
  default_precision: Fidelity
  supports: string[]
}

// ---------------------------------------------------------------------------
// 任务(U07 任务写入单元 / 03 任务调度规格)
// ---------------------------------------------------------------------------

export type TaskType =
  | 'calc'
  | 'optimization'
  | 'uncertainty'
  | 'analysis'
  | 'import'
  | 'export'
  | 'report'
  | 'dataset_build'

export type TaskStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'cancelling'
  | 'cancelled'
  | 'timed_out'
  | 'failed'

/** 业务结局(与执行状态正交)。 */
export type TaskOutcome =
  | 'normal_completion'
  | 'no_recommendation'
  | 'no_feasible_multi_objective'
  | 'partial_batch'
  | 'restricted_results'
  | 'insufficient_evidence'

export interface TaskSummary {
  attempt_no: number
  percent: number | null
  stage: string | null
  queue_position: number | null
}

export interface Task {
  id: EntityId
  project_id: EntityId
  type: TaskType
  status: TaskStatus
  business_outcome: TaskOutcome | null
  idempotency_key: string
  calc_snapshot_id: EntityId | null
  requested_by: EntityId
  requested_at: ISO8601
  priority: number
  deadline: ISO8601 | null
  superseded_by_task_id: EntityId | null
  attempt_count: number
  max_attempts: number
  created_at: ISO8601
  updated_at: ISO8601 | null
  /** 是否存在可用证据包(RR-P1-05: 结果可用性是任务元数据, 不靠探测 404 猜测)。 */
  result_available: boolean
  /** 列表接口返回的进度摘要(主来源仍是详情/轮询接口)。 */
  summary?: TaskSummary
}

export interface TaskListParams {
  project_id?: EntityId
  type?: TaskType
  status?: TaskStatus
  outcome?: TaskOutcome
  cursor?: string
  limit?: number
}

export interface TaskCreateInput {
  project_id: EntityId
  type: TaskType
  /** 计算类任务(calc/optimization/uncertainty)必填。 */
  config_id?: EntityId
  dataset_ids?: EntityId[]
  priority?: number
  deadline?: ISO8601
  /** 客户端重试幂等键(缺省由后端生成)。 */
  idempotency_key?: string
  /** 任务附加参数(如不确定性采样数)。 */
  params?: Record<string, unknown>
}

export interface TaskAttempt {
  attempt_no: number
  worker_id: string | null
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'stopped'
  stop_reason: string | null
  started_at: ISO8601 | null
  finished_at: ISO8601 | null
}

export interface TaskLease {
  attempt_no: number
  acquired_by: string
  renewed_at: ISO8601
  expires_at: ISO8601
}

export interface TaskProgress {
  task_id: EntityId
  status: TaskStatus
  attempt_no: number
  percent: number | null
  stage: string | null
  detail: Record<string, unknown>
  updated_at: ISO8601
  /** 数据来源(redis/pg),便于前端感知降级。 */
  source: 'redis' | 'pg'
  /** 租约过期重试中。 */
  retry?: boolean
}

export interface TaskBatch {
  parent_task_id: EntityId | null
  child_task_count: number
  children: Array<{ id: EntityId; status: TaskStatus }>
}

export interface CalcSnapshotSummary {
  id: EntityId
  content_hash: string
  random_seed: number | null
}

export interface EvidencePackageSummary {
  package_id: EntityId
  content_hash: string
  status: 'complete' | 'partial' | 'invalid'
}

export interface TaskDetail extends Task {
  attempts: TaskAttempt[]
  current_lease: TaskLease | null
  progress: TaskProgress | null
  batch: TaskBatch | null
  outcome_note: string | null
  diagnostics: Diagnostic[]
  evidence: EvidencePackageSummary[]
  calc_snapshot: CalcSnapshotSummary | null
}

// ---------------------------------------------------------------------------
// 结果(U09 结果写入单元)
// ---------------------------------------------------------------------------

export interface EvidencePackage {
  id: EntityId
  task_id: EntityId
  attempt_id: EntityId | null
  calc_snapshot_id: EntityId
  object_id: EntityId
  content_hash: string
  status: 'complete' | 'partial' | 'invalid'
  created_by: EntityId
  created_at: ISO8601
}

/** 四维有效性评估维度。 */
export type AssessmentDimension = 'physical' | 'optimality' | 'financial' | 'reliability'

export type AssessmentGrade = 'pass' | 'fail' | 'unknown'

export interface ResultAssessment {
  id: EntityId
  evidence_package_id: EntityId
  assessor: 'system' | 'human'
  assessed_by: EntityId | null
  dimension_physical: AssessmentGrade
  dimension_optimality: AssessmentGrade
  dimension_financial: AssessmentGrade
  dimension_reliability: AssessmentGrade
  overall_score: number | null
  comment: string | null
  detail: Record<string, unknown> | null
  created_at: ISO8601
}

export interface AssessmentInput {
  dimensions?: Partial<Record<AssessmentDimension, AssessmentGrade>>
  overall_score?: number
  comment?: string
}

export interface ResultIndex {
  id: EntityId
  project_id: EntityId
  project_version_id: EntityId
  evidence_package_id: EntityId
  assessment_id: EntityId | null
  result_hash: string
  is_latest: boolean
  created_at: ISO8601
}

export interface ResultSelection {
  id: EntityId
  project_id: EntityId
  result_index_id: EntityId
  selected_by: EntityId
  selected_at: ISO8601
  reason: string | null
  is_current: boolean
}

/** 指标数值(含物理合理区间与精度标记)。 */
export interface MetricValue {
  id: string
  /** 展示文案键(如 ies.metric.energy_balance)。 */
  label_key: string
  value: number | null
  unit: string | null
  precision: Fidelity | null
  bounds?: [number, number]
}

/** 逐时流(字段名与 02 §8 一致;数组形状 (n,),n=8760 或分辨率对应长度)。 */
export interface HourlyFlow {
  p_grid_buy?: Array<number | null>
  p_grid_sell?: Array<number | null>
  p_pv?: Array<number | null>
  p_bat_ch?: Array<number | null>
  p_bat_dis?: Array<number | null>
  soc?: Array<number | null>
  p_hp_heat?: Array<number | null>
  p_hp_cool?: Array<number | null>
  p_boiler?: Array<number | null>
  p_chiller?: Array<number | null>
  e_load?: Array<number | null>
  h_load?: Array<number | null>
  c_load?: Array<number | null>
  [field: string]: Array<number | null> | undefined
}

/** 结果包(证据包 + 指标 + 可选逐时流 + 诊断)。 */
export interface ResultBundle {
  evidence_package: EvidencePackage
  kpi: Record<string, MetricValue>
  hourly?: HourlyFlow
  /** 逐时时间轴元数据。 */
  axis?: { resolution: string; n: number; fixed_utc_offset_minutes: number }
  diagnostics: Diagnostic[]
}

export type ReportType = 'excel' | 'pdf' | 'html'

export type ReportStatus = 'generating' | 'ready' | 'failed'

export interface Report {
  id: EntityId
  project_id: EntityId
  report_type: ReportType
  object_id: EntityId
  content_hash: string
  generated_by_task_id: EntityId | null
  generated_by: EntityId
  generated_at: ISO8601
  status: ReportStatus
}

// ---------------------------------------------------------------------------
// 校验(四维有效性:数据/模型/求解/结果)
// ---------------------------------------------------------------------------

export interface ValidationResult {
  valid: boolean
  diagnostics: Diagnostic[]
  /** 基准方案可运行性确认状态(阻断级问题需显式确认)。 */
  baseline_confirmed?: boolean
}

// ---------------------------------------------------------------------------
// 导出
// ---------------------------------------------------------------------------

export interface ExcelExportInput {
  project_id: EntityId
  report_type?: 'excel'
  /** 结果来源证据包(缺省用最新结果)。 */
  evidence_package_id?: EntityId
  include_hourly?: boolean
  include_diagnostics?: boolean
}

export interface PackageExportInput {
  project_id: EntityId
  include_versions?: boolean
}

// ---------------------------------------------------------------------------
// 管理(admin)
// ---------------------------------------------------------------------------

export interface AdminUserRow extends User {
  roles: string[]
  project_count: number
  last_active_at: ISO8601 | null
}

/** 删除账号预告(0.2.0 B1 误操作防护): 将受影响项目清单 + 签名确认令牌。 */
export interface UserDeletePreview {
  user_id: EntityId
  username: string
  project_count: number
  /** 该账号拥有且未删除的项目(名称/id/状态)。 */
  projects: Array<{ id: EntityId; name: string; status: string }>
  /** 执行 DELETE 必须携带的签名确认令牌(10 分钟窗口, 绑定清单)。 */
  confirm_token: string
}

export interface AdminUserListParams {
  search?: string
  status?: UserStatus
  cursor?: string
  limit?: number
}

export interface StorageStats {
  total_bytes: number
  used_bytes: number
  quota_bytes: number | null
  object_count: number
  by_format: Record<string, number>
}

export interface HealthCheckEntry {
  status: 'ok' | 'degraded' | 'down'
  detail?: string
}

export interface HealthStatus {
  status: 'ok' | 'degraded' | 'down'
  version: string
  checks: Record<string, HealthCheckEntry>
}

export interface AuditEntry {
  id: EntityId
  actor_id: EntityId | null
  action: string
  object_type: string | null
  object_id: string | null
  project_id: EntityId | null
  detail: Record<string, unknown> | null
  occurred_at: ISO8601
  trace_id: string | null
}

export interface AuditListParams {
  actor_id?: EntityId
  action?: string
  project_id?: EntityId
  from?: ISO8601
  to?: ISO8601
  cursor?: string
  limit?: number
}
