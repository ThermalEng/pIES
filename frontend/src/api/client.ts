/**
 * 现有业务 API 的组合入口；各业务族逐步迁入 feature。
 * 通用传输、会话和下载辅助由 shared/http 拥有。
 * 结果/导出直接组合 feature 公开入口和 application 用例；
 * 尚未迁移的业务适配辅助不能作为新 feature 的依赖。
 */

import type {
  AdminUserListParams,
  AdminUserRow,
  ApiErrorBody,
  AuditEntry,
  AuditListParams,
  Connection,
  ConnectionInput,
  Currency,
  Dataset,
  DatasetUploadInput,
  DatasetVersion,
  Device,
  ApplySchemeInput,
  ApplySchemeResult,
  DeviceInput,
  DeviceKind,
  DevicePortSpec,
  DeviceTypeSpec,
  Diagnostic,
  ExcelExportInput,
  DraftModelSection,
  HealthStatus,
  LoginRequest,
  ImportConfirmResult,
  ImportProposal,
  PackageExportInput,
  PageResult,
  Project,
  ProjectCreateInput,
  ProjectListParams,
  ProjectRole,
  ProjectVersion,
  ProjectVersionCreateInput,
  VersionRestoreResult,
  PublicAuthSettings,
  RegisterRequest,
  Task,
  TaskDetail,
  TaskDraftLoadResult,
  TaskListParams,
  TaskStatus,
  TaskType,
  User,
  UserDeletePreview,
  ValidationResult,
} from '../types'
import { ApiError } from '../types'
import type {
  DatasetDetailResponseDto,
  DatasetsListResponseDto,
  DatasetUploadResponseDto,
} from '../features/datasets/contracts'
import { datasetVersionFromServer } from '../features/datasets/mappers'
import {
  getResult,
  listAssessments,
  assess as assessResult,
  select as selectResult,
  readHourly,
} from '../features/results'
import {
  exportPackage,
  downloadExport,
} from '../features/exports'
import { getTaskDetail, listTasks, loadTaskDraft } from '../features/tasks'
import { exportTaskReport } from '../application/exportTaskReport'

import { request, requestBlob, setSession, clearSession } from '../shared/http'

// ---------------------------------------------------------------------------
// 契约适配辅助(后端形状 → 前端类型)
// ---------------------------------------------------------------------------

/** 对象化(非对象输入返回空对象, 避免解构崩溃)。 */
function asRecord(body: unknown): Record<string, unknown> {
  return body !== null && typeof body === 'object' && !Array.isArray(body)
    ? (body as Record<string, unknown>)
    : {}
}

/** 删除账号预告响应解析(0.2.0 B1): {user_id, username, project_count, projects, confirm_token}。 */
function parseUserDeletePreview(body: unknown): UserDeletePreview {
  const rec = asRecord(body)
  const projects = Array.isArray(rec.projects)
    ? (rec.projects as unknown[])
        .filter((p) => p !== null && typeof p === 'object')
        .map((p) => {
          const pr = p as Record<string, unknown>
          return {
            id: Number(pr.id ?? 0),
            name: String(pr.name ?? ''),
            status: String(pr.status ?? ''),
          }
        })
    : []
  return {
    user_id: Number(rec.user_id ?? 0),
    username: String(rec.username ?? ''),
    project_count: Number(rec.project_count ?? projects.length),
    projects,
    confirm_token: String(rec.confirm_token ?? ''),
  }
}

/** 列表信封适配:兼容 {items, next_cursor} 与 {key: [...]} 与裸数组(PageResult 统一)。 */
function asItems<T>(body: unknown, key: string, limit?: number): PageResult<T> {
  if (Array.isArray(body)) return { items: body as T[], next_cursor: null, limit: limit ?? (body as T[]).length }
  const rec = asRecord(body)
  let items: T[] | null = null
  if (Array.isArray(rec.items)) items = rec.items as T[]
  else if (Array.isArray(rec[key])) items = rec[key] as T[]
  if (items === null) items = []
  const rawNext = rec.next_cursor
  const next = rawNext === null || rawNext === undefined ? null : String(rawNext)
  const lmt = typeof rec.limit === 'number' ? (rec.limit as number) : items.length
  return { items, next_cursor: next, limit: limit ?? lmt }
}

/** 列表字段适配:{items} / {key} / 裸数组 → T[]。 */
function asList<T>(body: unknown, key: string): T[] {
  if (Array.isArray(body)) return body as T[]
  const rec = asRecord(body)
  if (Array.isArray(rec.items)) return rec.items as T[]
  if (Array.isArray(rec[key])) return rec[key] as T[]
  return []
}

/** 单资源包裹适配:{key: {...}} → 内部对象(无包裹时原样返回)。 */
function oneOf<T>(body: unknown, key: string): T {
  const rec = asRecord(body)
  if (key in rec) return rec[key] as T
  return body as T
}

/** 后端 UserOut(id/username/display_name/role/status/force_password_change/...)
 *  → 前端 User(补齐前端类型要求的缺省字段)。 */
function normalizeUser(u: Record<string, unknown>): User {
  return {
    id: Number(u.id ?? 0),
    username: String(u.username ?? ''),
    display_name: String(u.display_name ?? ''),
    email: u.email === null || u.email === undefined ? null : String(u.email),
    role: String(u.role ?? ''),
    status: (u.status as User['status']) ?? 'active',
    locale: String(u.locale ?? 'zh-CN'),
    timezone: String(u.timezone ?? 'UTC'),
    fixed_utc_offset_minutes: Number(u.fixed_utc_offset_minutes ?? 480),
    credential_version: Number(u.credential_version ?? 0),
    is_system: Boolean(u.is_system ?? false),
    created_at: String(u.created_at ?? ''),
    updated_at: u.updated_at === null || u.updated_at === undefined ? null : String(u.updated_at),
    last_login_at: u.last_login_at === null || u.last_login_at === undefined ? null : String(u.last_login_at),
  }
}

/**
 * 后端项目包裹 {project, draft, versions, my_role} → 前端 Project。
 * 草稿摘要只透出修订号与数据集绑定清单(绑定状态以草稿为权威, 数据页真实徽章来源);
 * create/archive 等端点只返回 {project, my_role}, 不虚构 draft。
 */
function projectFromServer(body: unknown): Project {
  const rec = asRecord(body)
  const p = asRecord(rec.project)
  const out = { ...(p as unknown as Project), role: (rec.my_role as ProjectRole) ?? undefined }
  if (rec.draft !== null && typeof rec.draft === 'object') {
    const draft = asRecord(rec.draft)
    const bindings = Array.isArray(draft.dataset_bindings)
      ? (draft.dataset_bindings as NonNullable<Project['draft']>['dataset_bindings'])
      : undefined
    // 任务提交工作输入只读草稿内容文档的 task_working_inputs 单键(纯对象才
    // 携带; 缺席/形态不符为 undefined, 对话框取默认空态, 不猜测)。
    const content =
      draft.content !== null && typeof draft.content === 'object' && !Array.isArray(draft.content)
        ? (draft.content as Record<string, unknown>)
        : null
    const workingRaw = content?.task_working_inputs
    const working =
      workingRaw !== null && typeof workingRaw === 'object' && !Array.isArray(workingRaw)
        ? (workingRaw as Record<string, unknown>)
        : undefined
    out.draft = {
      id: Number(draft.id ?? 0),
      project_id: Number(draft.project_id ?? 0),
      revision: Number(draft.revision ?? 1),
      content_object_id: Number(draft.content_object_id ?? 0),
      is_current: draft.is_current !== false,
      updated_by: Number(draft.updated_by ?? 0),
      updated_at: String(draft.updated_at ?? ''),
      created_at: String(draft.created_at ?? ''),
      dataset_bindings: bindings,
      task_working_inputs: working,
    }
  }
  return out
}

/**
 * 后端版本 dict(version_to_dict) → 前端 ProjectVersion(严格解析)。
 *
 * 必填缺失或类型不符直接抛错, 不补 0/''/null 伪造版本(尤其 version_no /
 * created_by / created_at / source_draft_revision 不可伪造); 可空字段仅
 * 接受 null/undefined → null。详情端点只返回版本元数据, 此处不读取模型内容。
 */
/** 导入提案解析(与后端 _proposal_to_dict 一一对应; project_id 可缺席为 null)。 */
function importProposalFromServer(v: unknown): ImportProposal {
  if (v === null || typeof v !== 'object' || Array.isArray(v)) {
    throw new Error('invalid import proposal envelope')
  }
  const r = v as Record<string, unknown>
  if (typeof r.id !== 'number' || !Number.isFinite(r.id)) throw new Error('invalid import proposal field: id')
  if (r.project_id !== null && (typeof r.project_id !== 'number' || !Number.isFinite(r.project_id))) {
    throw new Error('invalid import proposal field: project_id')
  }
  if (typeof r.proposer_id !== 'number' || typeof r.status !== 'string') {
    throw new Error('invalid import proposal field')
  }
  const asNullableRecord = (key: string): Record<string, unknown> | null => {
    const val = r[key]
    if (val === null || val === undefined) return null
    if (typeof val !== 'object' || Array.isArray(val)) throw new Error(`invalid import proposal field: ${key}`)
    return val as Record<string, unknown>
  }
  return {
    id: r.id,
    project_id: r.project_id,
    proposer_id: r.proposer_id,
    status: r.status,
    source_object_id: typeof r.source_object_id === 'number' ? r.source_object_id : null,
    review_summary: asNullableRecord('review_summary'),
    review_errors: asNullableRecord('review_errors'),
    created_at: typeof r.created_at === 'string' ? r.created_at : null,
  }
}

function projectVersionFromServer(v: unknown): ProjectVersion {
  if (v === null || typeof v !== 'object' || Array.isArray(v)) {
    throw new Error('invalid project version envelope')
  }
  const r = v as Record<string, unknown>
  const num = (key: string): number => {
    const val = r[key]
    if (typeof val !== 'number' || !Number.isFinite(val)) throw new Error(`invalid project version field: ${key}`)
    return val
  }
  const nullableNum = (key: string): number | null => {
    const val = r[key]
    if (val === null || val === undefined) return null
    if (typeof val !== 'number' || !Number.isFinite(val)) throw new Error(`invalid project version field: ${key}`)
    return val
  }
  const str = (key: string): string => {
    const val = r[key]
    if (typeof val !== 'string') throw new Error(`invalid project version field: ${key}`)
    return val
  }
  const nullableStr = (key: string): string | null => {
    const val = r[key]
    if (val === null || val === undefined) return null
    if (typeof val !== 'string') throw new Error(`invalid project version field: ${key}`)
    return val
  }
  const baseline = r.project_baseline
  if (baseline === null || typeof baseline !== 'object' || Array.isArray(baseline)) {
    throw new Error('invalid project version field: project_baseline')
  }
  const b = baseline as Record<string, unknown>
  if (typeof b.resolution !== 'string' || typeof b.start_year !== 'number' || typeof b.scenario_mode !== 'string') {
    throw new Error('invalid project version field: project_baseline')
  }
  return {
    id: num('id'),
    project_id: num('project_id'),
    version_no: num('version_no'),
    name: str('name'),
    description: nullableStr('description'),
    created_by: num('created_by'),
    created_at: str('created_at'),
    parent_version_id: nullableNum('parent_version_id'),
    source_draft_id: nullableNum('source_draft_id'),
    source_draft_revision: nullableNum('source_draft_revision'),
    reason: str('reason'),
    project_baseline: {
      resolution: b.resolution,
      start_year: b.start_year,
      scenario_mode: b.scenario_mode,
    },
    currency: r.currency as ProjectVersion['currency'],
    schema_version: num('schema_version'),
    content_object_id: num('content_object_id'),
  }
}

/**
 * 审计记录映射(后端 entry_to_dict 可信字段直接消费, 一次映射; 不回填
 * 缺失、不做 Number/String 猜测、不逐字段重复验证)。
 */
function auditEntryFromServer(v: unknown): AuditEntry {
  const r = asRecord(v)
  return {
    id: r.id as number,
    entity_type: r.entity_type as string | null,
    entity_id: r.entity_id as number | null,
    action: r.action as string,
    actor_id: r.actor_id as number | null,
    actor_type: r.actor_type as string | null,
    occurred_at: r.occurred_at as string,
    ip: r.ip as string | null,
    before: r.before as Record<string, unknown> | null,
    after: r.after as Record<string, unknown> | null,
    request_id: r.request_id as string | null,
    trace_id: r.trace_id as string | null,
  }
}

/** 任务/配置映射归 features/tasks、features/config 所有, 本文件只作传输层委托。 */
/** 数据集版本/质量纯映射归 features/datasets/mappers 所有, 本文件只作传输层调用。 */

/** 草稿内容设备条目 → 前端 Device(2.0 名键, 无 id/状态/时间字段)。 */
function deviceFromDraft(d: Record<string, unknown>): Device {
  return {
    name: String(d.name ?? ''),
    device_type: String(d.device_type ?? ''),
    kind: (d.kind as DeviceKind) ?? 'new',
    params: asRecord(d.params),
  }
}

/** 草稿内容连接条目 → 前端 Connection(2.0 名键端点, 无类型/容量/损耗)。 */
function connFromDraft(c: Record<string, unknown>): Connection {
  return {
    name: String(c.name ?? ''),
    from_device: String(c.from_device ?? ''),
    from_port: String(c.from_port ?? ''),
    to_device: String(c.to_device ?? ''),
    to_port: String(c.to_port ?? ''),
  }
}

/** 项目视图 draft.content.model → 前端 DraftModelSection(2.0 唯一项目建模事实)。 */
function modelSectionFromView(body: unknown): { revision: number; model: DraftModelSection } {
  const view = asRecord(body)
  const draft = asRecord(view.draft)
  const content = asRecord(draft.content)
  const model = asRecord(content.model)
  return {
    revision: Number(draft.revision ?? 1),
    model: {
      devices: Array.isArray(model.devices)
        ? (model.devices as Record<string, unknown>[]).map(deviceFromDraft)
        : [],
      connections: Array.isArray(model.connections)
        ? (model.connections as Record<string, unknown>[]).map(connFromDraft)
        : [],
    },
  }
}

/** 2.0 连接默认命名(与后端 upsert_connection 缺省名公式一致)。 */
export function connectionName(
  fromDevice: string,
  fromPort: string,
  toDevice: string,
  toPort: string,
): string {
  return `${fromDevice}.${fromPort}->${toDevice}.${toPort}`
}

/** 参数默认值适配:后端枚举参数(如 mode='both')默认值为字符串,数值参数为 number,
 *  结构化参数(如 import_tariff)为 {peak,flat,valley} 对象,原样透传。 */
function paramDefaultValue(v: unknown): number | string | Record<string, number> | null {
  if (typeof v === 'number') return v
  if (typeof v === 'string') return v
  if (typeof v === 'object' && v !== null) {
    const out: Record<string, number> = {}
    for (const [key, val] of Object.entries(v)) {
      if (typeof val === 'number') out[key] = val
    }
    return Object.keys(out).length > 0 ? out : null
  }
  return null
}

/** 后端设备类型注册项 → 前端 DeviceTypeSpec(参数规格字段名适配 + 真实端口透传)。 */
function deviceTypeFromServer(s: Record<string, unknown>): DeviceTypeSpec {
  const params = asRecord(s.parameters)
  const ports = Array.isArray(s.ports) ? (s.ports as DevicePortSpec[]) : []
  // 注册表公开 DTO 用 names {zh-CN,en-US} 字典(无 name_zh/name_en 平铺字段),
  // 载能只存在于 ports[].energy_carrier(无 energy_carriers 顶层数组): 直接按
  // 端口声明顺序去重派生, 与 descriptor 同一身份, 不做形状复核与值过滤。
  const names = asRecord(s.names)
  const carriers = [...new Set(ports.map((p) => p.energy_carrier))]
  return {
    type_id: String(s.type_id ?? ''),
    version: String(s.version ?? ''),
    name_zh: String(names['zh-CN'] ?? ''),
    name_en: String(names['en-US'] ?? ''),
    energy_carriers: carriers,
    is_load: Boolean(s.is_load ?? false),
    capabilities: Array.isArray(s.capabilities) ? (s.capabilities as string[]) : [],
    model_method: (s.model_method as DeviceTypeSpec['model_method']) ?? 'mechanism',
    stateful: Boolean(s.stateful ?? false),
    ports,
    parameters: Object.fromEntries(
      Object.entries(params).map(([name, p]) => {
        const pr = asRecord(p)
        return [
          name,
          {
            unit: pr.unit === null || pr.unit === undefined ? null : String(pr.unit),
            min: typeof pr.min === 'number' ? (pr.min as number) : null,
            max: typeof pr.max === 'number' ? (pr.max as number) : null,
            default: paramDefaultValue(pr.default),
            is_optimizable: Boolean(pr.is_optimizable ?? false),
            // 后端同时返回 existing_default(存量默认值)与 stock_or_addition(存量/新增标签);
            // 取值一律走 existing_default(旧实现误从 stock_or_addition 取值得到 NaN)。
            existing_default: paramDefaultValue(pr.existing_default),
            help_key: String(pr.help_key ?? ''),
            // 透出枚举约束(heat_pump.mode=heating/cooling/both 等),前端按 enum 渲染下拉。
            enum: Array.isArray(pr.enum)
              ? (pr.enum as Array<string | number | boolean>)
              : null,
          },
        ]
      }),
    ),
  }
}

/** 语义命令批次的命令 id 计数器(保证批次内唯一)。 */
let commandSeq = 0

/** 构造后端草稿命令(project.py update_draft: id/project_id/expected_revision/session/unit/type/payload)。 */
function makeCommand(
  projectId: number,
  expectedRevision: number,
  type: string,
  payload: Record<string, unknown>,
  unit = 'model',
): Record<string, unknown> {
  commandSeq += 1
  return {
    id: `fe-${Date.now().toString(36)}-${commandSeq.toString(36)}`,
    project_id: projectId,
    expected_revision: expectedRevision,
    session: 'browser',
    unit,
    type,
    payload,
  }
}

/**
 * 发送单条草稿语义命令(数据集绑定等非模型命令), 返回新修订号。
 * 先取项目视图的当前草稿修订作乐观锁, 再 PUT /projects/{id}/draft。
 */
async function sendDraftCommand(
  projectId: number,
  type: string,
  payload: Record<string, unknown>,
): Promise<{ revision: number }> {
  const view = asRecord(await request<unknown>(`/projects/${projectId}`))
  const expectedRevision = Number(asRecord(view.draft).revision ?? 1)
  const cmd = makeCommand(projectId, expectedRevision, type, payload, type.split('.')[0] ?? 'model')
  const body = asRecord(
    await request<unknown>(`/projects/${projectId}/draft`, {
      method: 'PUT',
      body: { expected_revision: expectedRevision, commands: [cmd] },
    }),
  )
  return { revision: Number(body.revision ?? expectedRevision) }
}

// ---------------------------------------------------------------------------
// 类型化方法集
// ---------------------------------------------------------------------------

/** 登录/接管响应(与后端 AuthResponse 一致; user 为 UserOut 精简形态)。 */
export interface LoginResult {
  token: string
  token_type: string
  user: {
    id: number
    username: string
    display_name: string
    role: string
    status: string
    force_password_change: boolean
    credential_version: number
    last_login_at: string | null
  }
  needs_takeover_confirm: boolean
}

export interface AuthApi {
  login(input: LoginRequest): Promise<LoginResult>
  logout(): Promise<void>
  changePassword(input: { old_password: string; new_password: string }): Promise<void>
  /** 确认接管(无请求体; 身份取自 Cookie/Bearer, 服务端忽略 body)。 */
  confirmTakeover(): Promise<LoginResult>
  register(input: RegisterRequest): Promise<User>
  /** 获取当前登录用户(页面刷新后恢复用户信息)。 */
  me(): Promise<User>
  /** 登录页公开设置(无需认证): 注册开关 / SSO 入口。 */
  publicSettings(): Promise<PublicAuthSettings>
}

export interface ProjectsApi {
  list(params?: ProjectListParams): Promise<PageResult<Project>>
  create(input: ProjectCreateInput): Promise<Project>
  get(id: number): Promise<Project>
  /** 版本列表: 严格消费唯一 {versions: [...]} 契约, 形状不符抛错(不猜测、不回退空列表)。 */
  versions(id: number): Promise<ProjectVersion[]>
  /** 从当前草稿创建版本: 请求/响应与后端 VersionCreateRequest/{version} 一一对应。 */
  createVersion(id: number, input: ProjectVersionCreateInput): Promise<ProjectVersion>
  /**
   * 恢复历史版本: 与后端 POST .../versions/{vid}/restore 对应。
   * 请求体沿自动命名(不发送 name/description); 响应唯一 {version, draft}。
   * draft 仅取 id/revision(刷新摘要所需), 其余字段忽略不猜测。
   */
  restoreVersion(projectId: number, versionId: number): Promise<VersionRestoreResult>
  /**
   * 应用方案: 与后端 POST .../apply-result 对应。
   * 只发送任务引用、解标识与确认时草稿修订; 参数与来源基线由服务端权威解析,
   * 不发送补丁。修订不符服务端 409, 不创建版本。
   * 响应唯一 {version, draft}, draft 仅取 id/revision。
   */
  applyScheme(projectId: number, input: ApplySchemeInput): Promise<ApplySchemeResult>
  /** 版本详情: 严格消费唯一 {version: {...}} 契约(只返回版本元数据)。 */
  versionDetail(projectId: number, versionId: number): Promise<ProjectVersion>
  /**
   * 上传项目包创建导入提案: 与后端 POST /projects/import(multipart file) 对应。
   * 响应唯一 {proposal: {...}}; 确认前 proposal.project_id 为 null, 不得据此导航。
   */
  proposeImport(file: File): Promise<ImportProposal>
  /**
   * 确认导入提案: 与后端 POST /projects/import/{proposal_id}/confirm 对应。
   * 响应唯一 {project, my_role}; 只有此时才展示已导入并刷新列表。
   */
  confirmImport(proposalId: number): Promise<ImportConfirmResult>
  archive(id: number): Promise<Project>
  unarchive(id: number): Promise<Project>
  /** 删除项目(0.2.0 B4: 须携带与项目名精确匹配的 name 确认)。 */
  delete(id: number, name: string): Promise<void>
}

export interface ModelApi {
  /** 读项目视图草稿内容 model 节(2.0 名键设备与端点连接, 附带草稿修订号)。 */
  getModel(projectId: number): Promise<{ revision: number; model: DraftModelSection }>
  /** 写单个设备(upsert 语义; 改名经 remove+upsert, 由调用方组批)。
   * 调用方传入已加载的草稿修订号, 成功后以后端返回修订号为准, 409 自行处理。 */
  upsertDevice(projectId: number, input: DeviceInput, expectedRevision: number): Promise<{ revision: number }>
  removeDevice(projectId: number, name: string, expectedRevision: number): Promise<{ revision: number }>
  /** 写单条连接(名键端点; 未显式命名时按默认公式命名并返回)。 */
  upsertConnection(
    projectId: number,
    input: ConnectionInput,
    expectedRevision: number,
  ): Promise<{ revision: number; name: string }>
  removeConnection(projectId: number, name: string, expectedRevision: number): Promise<{ revision: number }>
  /** 批量草稿语义命令(改名等需多命令原子推进的场景), 返回新修订号。 */
  sendCommands(
    projectId: number,
    commands: Array<{ type: string; payload: Record<string, unknown> }>,
    expectedRevision: number,
  ): Promise<{ revision: number }>
  validate(projectId: number): Promise<ValidationResult>
  deviceTypes(): Promise<DeviceTypeSpec[]>
}

/** 详细上传输入:在基础上传之上额外携带分辨率/固定 UTC 偏移/字段定义/许可证(设计输入 §8)。 */
export interface DatasetUploadDetailedInput extends DatasetUploadInput {
  /** 分辨率说明(如 '1h'、'15min'),对齐 dataset_versions.resolution。 */
  resolution?: string
  /** 数据时间戳的固定 UTC 偏移(分钟),对齐 dataset_versions.fixed_utc_offset_minutes。 */
  fixed_utc_offset_minutes?: number
  /** 字段定义(名称 -> 单位/说明)。 */
  fields?: Record<string, { unit?: string | null; description?: string }>
  /** 本版本许可证或授权信息。 */
  license?: string
}

export interface DatasetsApi {
  list(params: { project_id: number; status?: string; cursor?: string; limit?: number }): Promise<PageResult<Dataset>>
  template(): Promise<Blob>
  upload(input: DatasetUploadInput): Promise<DatasetVersion>
  /**
   * 详细上传(设计输入 §8.2/§8.3):后端校验后返回数据集版本,
   * 并附带质量报告(version.quality_report)与诊断列表(阻断/警告分级,
   * 经 location 定位到字段/行);阻断性错误未修复时版本不可绑定计算。
   */
  uploadDetailed(input: DatasetUploadDetailedInput): Promise<DatasetVersion & { diagnostics?: Diagnostic[] }>
  versions(projectId: number, id: number): Promise<DatasetVersion[]>
  /** 最新版本字段名(行级预览无能力, 只返元数据, 不带行载体)。 */
  versionFields(projectId: number, id: number): Promise<string[]>
  /**
   * 绑定数据集版本到项目草稿(dataset.bind 语义命令, 写入 draft.content.dataset_bindings)。
   * 绑定是校验 VALID-DATA-001 与任务数据输入的权威来源。
   */
  bind(projectId: number, datasetVersionId: number, datasetId: number): Promise<{ revision: number }>
  /** 解除数据集版本绑定(dataset.unbind 语义命令)。 */
  unbind(projectId: number, datasetVersionId: number): Promise<{ revision: number }>
}

/** 部署公开初值(统一部署配置中允许公开的非秘密默认值)。 */
export interface DeploymentDefaults {
  calculation_defaults: { time_limit_seconds: number }
}

export interface DeploymentApi {
  defaults(): Promise<DeploymentDefaults>
}

export interface ExportsApi {
  /**
   * Excel 结果报告导出: 返回本地下载会话 id(一次下载引用, 非持久化报告)。
   * 项目包是完整项目现场导出, 不是报告, 同样只返回本地下载会话 id。
   */
  excel(input: ExcelExportInput): Promise<number>
  package(input: PackageExportInput): Promise<number>
  /** 按本地下载会话 id 取 Blob 与建议文件名并下载。 */
  download(downloadSessionId: number): Promise<{ blob: Blob; filename: string }>
}

export interface AdminApi {
  users(params?: AdminUserListParams): Promise<PageResult<AdminUserRow>>
  storage(): Promise<{ total_bytes: number; used_bytes: number; quota_bytes: number | null; object_count: number }>
  health(): Promise<HealthStatus>
  audit(params?: AuditListParams): Promise<PageResult<AuditEntry>>
  /** 删除账号预告(管理员): 返回将受影响项目清单 + 确认令牌。 */
  previewUserDelete(userId: number): Promise<UserDeletePreview>
  /** 删除账号(管理员): 需 confirm=true + preview 签发的 confirm_token, 级联删除其项目。 */
  deleteUser(userId: number, confirmToken: string): Promise<{ deleted_projects: number }>
  /** 停用账号(管理员)。 */
  deactivateUser(userId: number): Promise<void>
  /** 重新启用账号(管理员)。 */
  reactivateUser(userId: number): Promise<void>
  /** 重置密码(管理员): 签发临时密码并强制目标用户改密, 其全部会话失效。 */
  resetPassword(userId: number, newPassword: string): Promise<void>
  /** 读取安全设置(管理员)。 */
  getSecuritySettings(): Promise<{ registration_enabled: boolean }>
  /** 更新安全设置: 自助注册开关(管理员)。 */
  setRegistrationEnabled(enabled: boolean): Promise<{ registration_enabled: boolean }>
}

export const api = {
  auth: {
    login(input: LoginRequest): Promise<LoginResult> {
      return request<LoginResult>('/auth/login', { method: 'POST', body: input }).then((res) => {
        setSession()
        return res
      })
    },
    logout(): Promise<void> {
      return request<void>('/auth/logout', { method: 'POST' }).then(() => {
        clearSession()
      })
    },
    changePassword(input: { old_password: string; new_password: string }): Promise<void> {
      return request<void>('/auth/change-password', { method: 'POST', body: input })
    },
    confirmTakeover(): Promise<LoginResult> {
      return request<LoginResult>('/auth/confirm-takeover', { method: 'POST' }).then((res) => {
        setSession()
        return res
      })
    },
    register(input: RegisterRequest): Promise<User> {
      return request<unknown>('/auth/register', { method: 'POST', body: input }).then((res) =>
        normalizeUser(asRecord(res)),
      )
    },
    publicSettings(): Promise<PublicAuthSettings> {
      return request<unknown>('/auth/public-settings').then((res) => {
        const body = asRecord(res)
        return {
          registration_enabled: Boolean(body.registration_enabled),
          sso_enabled: Boolean(body.sso_enabled),
          sso_provider_name: String(body.sso_provider_name ?? ''),
        }
      })
    },
    me(): Promise<User> {
      return request<unknown>('/auth/me').then((res) => normalizeUser(asRecord(res)))
    },
  } satisfies AuthApi,

  projects: {
    list(params?: ProjectListParams): Promise<PageResult<Project>> {
      return request<unknown>('/projects', { query: params }).then((body) => {
        // 后端返回 {projects: [...]}; items 行内 my_role 提升为 role
        const raw = asItems<Record<string, unknown>>(body, 'projects')
        return {
          items: raw.items.map((p) => ({
            ...(p as unknown as Project),
            role: (p.my_role as ProjectRole) ?? undefined,
          })),
          next_cursor: null,
          limit: raw.items.length,
        }
      })
    },
    create(input: ProjectCreateInput): Promise<Project> {
      return request<unknown>('/projects', {
        method: 'POST',
        body: {
          name: input.name,
          description: input.description ?? null,
          currency: input.currency,
          baseline_resolution: input.baseline_resolution,
          baseline_start_year: input.baseline_start_year,
          baseline_scenario_mode: input.baseline_scenario_mode,
        },
      }).then((body) => projectFromServer(body))
    },
    get(id: number): Promise<Project> {
      // 后端返回 {project, draft, versions, my_role}: 解包 project + role
      return request<unknown>(`/projects/${id}`).then((body) => projectFromServer(body))
    },
    versions(id: number): Promise<ProjectVersion[]> {
      // 本流程只消费唯一 {versions: [...]} 契约; 其他形状直接抛错, 不回退空列表。
      return request<unknown>(`/projects/${id}/versions`).then((body) => {
        const rec = asRecord(body)
        if (!Array.isArray(rec.versions)) throw new Error('invalid project versions envelope')
        return (rec.versions as unknown[]).map(projectVersionFromServer)
      })
    },
    createVersion(id: number, input: ProjectVersionCreateInput): Promise<ProjectVersion> {
      const payload: Record<string, unknown> = { name: input.name }
      if (input.description !== undefined) payload.description = input.description
      if (input.reason !== undefined) payload.reason = input.reason
      return request<unknown>(`/projects/${id}/versions`, { method: 'POST', body: payload }).then((body) => {
        const rec = asRecord(body)
        return projectVersionFromServer(rec.version)
      })
    },
    versionDetail(projectId: number, versionId: number): Promise<ProjectVersion> {
      return request<unknown>(`/projects/${projectId}/versions/${versionId}`).then((body) => {
        const rec = asRecord(body)
        return projectVersionFromServer(rec.version)
      })
    },
    proposeImport(file: File): Promise<ImportProposal> {
      const formData = new FormData()
      formData.append('file', file)
      return request<unknown>('/projects/import', { method: 'POST', formData, timeoutMs: 0 }).then((body) => {
        const rec = asRecord(body)
        return importProposalFromServer(rec.proposal)
      })
    },
    confirmImport(proposalId: number): Promise<ImportConfirmResult> {
      return request<unknown>(`/projects/import/${proposalId}/confirm`, { method: 'POST' }).then((body) => {
        const rec = asRecord(body)
        const project = projectFromServer({ project: rec.project, my_role: rec.my_role })
        return { project, myRole: typeof rec.my_role === 'string' ? rec.my_role : null }
      })
    },
    restoreVersion(projectId: number, versionId: number): Promise<VersionRestoreResult> {
      // 唯一 {version, draft} 契约; draft 仅取刷新所需的 id/revision。
      return request<unknown>(`/projects/${projectId}/versions/${versionId}/restore`, {
        method: 'POST',
        body: {},
      }).then((body) => {
        const rec = asRecord(body)
        const version = projectVersionFromServer(rec.version)
        const draft = rec.draft
        if (draft === null || typeof draft !== 'object' || Array.isArray(draft)) {
          throw new Error('invalid version restore envelope')
        }
        const d = draft as Record<string, unknown>
        if (typeof d.id !== 'number' || typeof d.revision !== 'number') {
          throw new Error('invalid version restore envelope')
        }
        return { version, draftId: d.id, draftRevision: d.revision }
      })
    },
    applyScheme(projectId: number, input: ApplySchemeInput): Promise<ApplySchemeResult> {
      // 唯一 {version, draft} 契约; 只发送引用与确认时修订, draft 仅取刷新所需的 id/revision。可信交换直接映射。
      return request<unknown>(`/projects/${projectId}/apply-result`, {
        method: 'POST',
        body: { task_id: input.task_id, solution_id: input.solution_id, expected_revision: input.expected_revision },
      }).then((body) => {
        const rec = asRecord(body)
        const d = rec.draft as { id: number; revision: number }
        return { version: projectVersionFromServer(rec.version), draftId: d.id, draftRevision: d.revision }
      })
    },
    archive(id: number): Promise<Project> {
      return request<unknown>(`/projects/${id}/archive`, { method: 'POST' }).then((body) => projectFromServer(body))
    },
    unarchive(id: number): Promise<Project> {
      return request<unknown>(`/projects/${id}/unarchive`, { method: 'POST' }).then((body) => projectFromServer(body))
    },
    delete(id: number, name: string): Promise<void> {
      // 0.2.0 B4 误操作防护: 须携带与待删除项目名精确匹配的 name(替代空布尔 confirm)
      return request<void>(`/projects/${id}`, { method: 'DELETE', body: { confirm: true, name } })
    },
  } satisfies ProjectsApi,

  model: {
    async getModel(projectId: number): Promise<{ revision: number; model: DraftModelSection }> {
      const body = await request<unknown>(`/projects/${projectId}`)
      return modelSectionFromView(body)
    },
    async sendCommands(
      projectId: number,
      commands: Array<{ type: string; payload: Record<string, unknown> }>,
      expectedRevision: number,
    ): Promise<{ revision: number }> {
      // 修订号由调用方(ModelPage 已加载值)传入, 写前不 GET; 成功以后端返回为准。
      const body = asRecord(
        await request<unknown>(`/projects/${projectId}/draft`, {
          method: 'PUT',
          body: {
            expected_revision: expectedRevision,
            commands: commands.map((c) =>
              makeCommand(projectId, expectedRevision, c.type, c.payload, c.type.split('.')[0] ?? 'model'),
            ),
          },
        }),
      )
      return { revision: Number(body.revision ?? expectedRevision) }
    },
    async upsertDevice(
      projectId: number,
      input: DeviceInput,
      expectedRevision: number,
    ): Promise<{ revision: number }> {
      return api.model.sendCommands(
        projectId,
        [
          {
            type: 'model.upsert_device',
            payload: {
              name: input.name,
              device_type: input.device_type,
              kind: input.kind,
              ...(input.params !== undefined ? { params: input.params } : {}),
            },
          },
        ],
        expectedRevision,
      )
    },
    async removeDevice(projectId: number, name: string, expectedRevision: number): Promise<{ revision: number }> {
      return api.model.sendCommands(projectId, [{ type: 'model.remove_device', payload: { name } }], expectedRevision)
    },
    async upsertConnection(
      projectId: number,
      input: ConnectionInput,
      expectedRevision: number,
    ): Promise<{ revision: number; name: string }> {
      const name =
        input.name ?? connectionName(input.from_device, input.from_port, input.to_device, input.to_port)
      const { revision } = await api.model.sendCommands(
        projectId,
        [
          {
            type: 'model.upsert_connection',
            payload: {
              name,
              from_device: input.from_device,
              from_port: input.from_port,
              to_device: input.to_device,
              to_port: input.to_port,
            },
          },
        ],
        expectedRevision,
      )
      return { revision, name }
    },
    async removeConnection(projectId: number, name: string, expectedRevision: number): Promise<{ revision: number }> {
      return api.model.sendCommands(projectId, [{ type: 'model.remove_connection', payload: { name } }], expectedRevision)
    },
    validate(projectId: number): Promise<ValidationResult> {
      return request<unknown>(`/projects/${projectId}/validation/run`, { method: 'POST' }).then((body) => {
        const report = asRecord(oneOf<Record<string, unknown>>(body, 'report'))
        const diags = asList<Diagnostic>(report, 'diagnostics')
        return { valid: report.blocks_submit !== true, diagnostics: diags }
      })
    },
    deviceTypes(): Promise<DeviceTypeSpec[]> {
      return request<unknown>('/registry/device-types').then((body) =>
        asList<Record<string, unknown>>(body, 'items').map(deviceTypeFromServer),
      )
    },
  } satisfies ModelApi,

  datasets: {
    list(params: { project_id: number; status?: string; cursor?: string; limit?: number }): Promise<PageResult<Dataset>> {
      // 数据集按项目挂载(/api/projects/{pid}/datasets); 项目 ID 必填,
      // 直接调用公开接口, 不做无项目空页伪成功, 不复核 ID。
      const projectId = params.project_id
      return request<unknown>(`/projects/${projectId}/datasets`).then((body) => {
        // 后端精确信封 {datasets: [{dataset, latest_version}]}: 直接消费具名键。
        // 畸形体自然抛错可见, 不以空列表伪装成功; 空数组与 latest_version=null
        // 均为合法(分别表示无数据集 / 尚无版本), 原样保留。
        const entries = (body as DatasetsListResponseDto).datasets
        return {
          items: entries.map((e) => e.dataset),
          next_cursor: null,
          limit: entries.length,
        }
      })
    },
    template(): Promise<Blob> {
      return requestBlob('/datasets/template')
    },
    upload(input: DatasetUploadInput): Promise<DatasetVersion> {
      return uploadDataset(input as DatasetUploadDetailedInput)
    },
    uploadDetailed(input: DatasetUploadDetailedInput): Promise<DatasetVersion & { diagnostics?: Diagnostic[] }> {
      return uploadDataset(input)
    },
    versions(projectId: number, id: number): Promise<DatasetVersion[]> {
      // 后端精确信封 {dataset, versions}: 直接消费具名键, 畸形体自然抛错可见。
      return request<unknown>(`/projects/${projectId}/datasets/${id}`).then((body) =>
        (body as DatasetDetailResponseDto).versions.map(datasetVersionFromServer),
      )
    },
    versionFields(projectId: number, id: number): Promise<string[]> {
      // 后端无数据行预览端点: 只返回最新版本真实字段名,
      // 不设行/行数载体, 不得伪造空行冒充真实空数据集。
      return request<unknown>(`/projects/${projectId}/datasets/${id}`).then((body) => {
        const versions = (body as DatasetDetailResponseDto).versions
        const latest = versions[0]
        if (latest === undefined) return []
        return Object.keys(latest.fields)
      })
    },
    bind(projectId: number, datasetVersionId: number, datasetId: number): Promise<{ revision: number }> {
      return sendDraftCommand(projectId, 'dataset.bind', {
        dataset_version_id: datasetVersionId,
        dataset_id: datasetId,
        role: 'annual',
      })
    },
    unbind(projectId: number, datasetVersionId: number): Promise<{ revision: number }> {
      return sendDraftCommand(projectId, 'dataset.unbind', { dataset_version_id: datasetVersionId })
    },
  } satisfies DatasetsApi,

  // 计算配置请求归 features/config 所有(页面直接消费公开入口), 本聚合器不再保留委托。

  deployment: {
    defaults(): Promise<DeploymentDefaults> {
      // 后端返回 {calculation_defaults: {time_limit_seconds}}: 形态断言
      return request<unknown>('/deployment/defaults').then((body) => {
        const root = asRecord(body)
        const calc = asRecord(root.calculation_defaults)
        const timeLimit = calc.time_limit_seconds
        if (typeof timeLimit !== 'number' || !Number.isFinite(timeLimit)) {
          throw new ApiError(500, null, 'ies.error.unknown')
        }
        return { calculation_defaults: { time_limit_seconds: timeLimit } }
      })
    },
  } satisfies DeploymentApi,

  // 校验请求归 features/validation 所有(页面直接消费公开入口), 本聚合器不再保留委托。

  tasks: {
    // 任务请求归 features/tasks 公开入口所有: 页面已直调, 本聚合器仅暂留
    // 工作台共享上下文仍消费的 list/get/loadDraft(随工作台接线另立集成候选
    // 一并移除)与跨家族草稿命令通道 saveWorkingInputs(复用
    // sendDraftCommand/makeCommand, 不向 feature 重复一份命令组装)。
    // 跨家族草稿命令传输可待 Codex 共享边界集成。
    list(params?: TaskListParams): Promise<PageResult<Task>> {
      return listTasks(params)
    },
    get(projectId: number, taskId: number): Promise<TaskDetail> {
      return getTaskDetail(projectId, taskId)
    },
    loadDraft(projectId: number, taskId: number): Promise<TaskDraftLoadResult> {
      return loadTaskDraft(projectId, taskId)
    },
    async saveWorkingInputs(
      projectId: number,
      working: Record<string, unknown>,
      expectedRevision?: number,
    ): Promise<{ revision: number }> {
      if (expectedRevision === undefined) {
        return sendDraftCommand(projectId, 'task.set_working_inputs', { working })
      }
      const cmd = makeCommand(projectId, expectedRevision, 'task.set_working_inputs', { working }, 'task')
      const body = asRecord(
        await request<unknown>(`/projects/${projectId}/draft`, {
          method: 'PUT',
          body: { expected_revision: expectedRevision, commands: [cmd] },
        }),
      )
      return { revision: Number(body.revision ?? expectedRevision) }
    },
  },

  results: {
    result: getResult,
    assessments: listAssessments,
    assess: assessResult,
    select: selectResult,
    hourly: readHourly,
  },

  exports: {
    excel: exportTaskReport,
    package: exportPackage,
    download: downloadExport,
  } satisfies ExportsApi,

  admin: {
    users(params?: AdminUserListParams): Promise<PageResult<AdminUserRow>> {
      // 后端用户列表在 /api/auth/users(管理员), 返回 {users: [UserOut]}
      return request<unknown>('/auth/users').then((body) => {
        let list = asList<Record<string, unknown>>(body, 'users')
        if (params?.status) list = list.filter((u) => String(u.status) === params.status)
        return {
          items: list.map((u) => {
            const base = normalizeUser(u)
            return {
              ...base,
              roles: typeof u.role === 'string' && u.role ? [u.role] : [],
              project_count: Number(u.project_count ?? 0),
              last_active_at: base.last_login_at,
            }
          }),
          next_cursor: null,
          limit: list.length,
        }
      })
    },
    storage(): Promise<{
      total_bytes: number
      used_bytes: number
      quota_bytes: number | null
      object_count: number
    }> {
      return request<unknown>('/admin/storage').then((body) => {
        const rec = asRecord(body)
        const objects = asRecord(rec.objects)
        return {
          total_bytes: Number(objects.total_bytes ?? 0),
          used_bytes: Number(objects.total_bytes ?? 0),
          quota_bytes: null,
          object_count: Number(objects.count ?? 0),
        }
      })
    },
    health(): Promise<HealthStatus> {
      return request<unknown>('/admin/health').then((body) => {
        const rec = asRecord(body)
        const liveness = asRecord(rec.liveness)
        const readiness = asRecord(rec.readiness)
        const storage = asRecord(rec.storage)
        return {
          status: (rec.status as HealthStatus['status']) ?? 'degraded',
          version: String(rec.version ?? ''),
          checks: {
            liveness: { status: liveness.ok === true ? 'ok' : 'down' },
            readiness: { status: readiness.db === true ? 'ok' : 'down' },
            // STO-07: 存储健康 provider 形状 {capacity, ok}; 容量不可测(capacity_unknown)降级
            storage: {
              status: storage.ok === true ? 'ok' : 'degraded',
            },
          },
        }
      })
    },
    audit(params?: AuditListParams): Promise<PageResult<AuditEntry>> {
      // 审计查询: 参数与后端 entry_to_dict 字段一一透传, 不改名、不虚构
      // (无 project_id 语义; before/after/request_id 等一并保留在行内)。
      const query: Record<string, unknown> = {}
      if (params?.entity_type !== undefined) query.entity_type = params.entity_type
      if (params?.entity_id !== undefined) query.entity_id = params.entity_id
      if (params?.action !== undefined) query.action = params.action
      if (params?.actor_id !== undefined) query.actor_id = params.actor_id
      if (params?.actor_type !== undefined) query.actor_type = params.actor_type
      if (params?.since !== undefined) query.since = params.since
      if (params?.until !== undefined) query.until = params.until
      if (params?.cursor !== undefined) query.cursor = params.cursor
      if (params?.limit !== undefined) query.limit = params.limit
      return request<unknown>('/admin/audit', { query }).then((body) => {
        const page = asItems<Record<string, unknown>>(body, 'items')
        return {
          items: page.items.map(auditEntryFromServer),
          next_cursor: page.next_cursor,
          limit: params?.limit ?? page.limit,
        }
      })
    },
    deleteUser(userId: number, confirmToken: string): Promise<{ deleted_projects: number }> {
      return request<unknown>(`/auth/users/${userId}`, {
        method: 'DELETE',
        body: { confirm: true, confirm_token: confirmToken },
      }).then((body) => {
        const rec = asRecord(body)
        return { deleted_projects: Number(rec.deleted_projects ?? 0) }
      })
    },
    previewUserDelete(userId: number): Promise<UserDeletePreview> {
      return request<unknown>(`/auth/users/${userId}/delete-preview`, { method: 'POST' }).then(
        (body) => parseUserDeletePreview(body),
      )
    },
    deactivateUser(userId: number): Promise<void> {
      return request<void>(`/auth/users/${userId}/deactivate`, { method: 'POST' })
    },
    reactivateUser(userId: number): Promise<void> {
      return request<void>(`/auth/users/${userId}/reactivate`, { method: 'POST' })
    },
    resetPassword(userId: number, newPassword: string): Promise<void> {
      return request<void>(`/auth/users/${userId}/reset-password`, {
        method: 'POST',
        body: { new_password: newPassword },
      })
    },
    getSecuritySettings(): Promise<{ registration_enabled: boolean }> {
      return request<unknown>('/auth/settings').then((body) => ({
        registration_enabled: Boolean(asRecord(body).registration_enabled),
      }))
    },
    setRegistrationEnabled(enabled: boolean): Promise<{ registration_enabled: boolean }> {
      return request<unknown>('/auth/settings', {
        method: 'PUT',
        body: { registration_enabled: enabled },
      }).then((body) => ({ registration_enabled: Boolean(asRecord(body).registration_enabled) }))
    },
  } satisfies AdminApi,
}

// ---------------------------------------------------------------------------
// 数据集上传(两步: 创建元数据 → 上传版本)
// ---------------------------------------------------------------------------

/** 数据集上传: 后端拆分为「创建数据集元数据」+「上传版本(multipart)」两步。 */
async function uploadDataset(
  input: DatasetUploadDetailedInput,
): Promise<DatasetVersion & { diagnostics?: Diagnostic[] }> {
  const projectId = input.project_id
  if (projectId === undefined || projectId === null) {
    throw new ApiError(400, null, 'ies.error.unknown')
  }
  // 1) 创建数据集元数据
  const created = asRecord(
    await request<unknown>(`/projects/${projectId}/datasets`, {
      method: 'POST',
      body: {
        name: input.name,
        description: input.description ?? null,
        source_category: 'user_upload',
        license: input.license ?? null,
      },
    }),
  )
  const datasetId = Number(asRecord(created.dataset).id)
  // 2) 上传版本(multipart: file/resolution/utc_offset_minutes/fields/meta)
  const formData = new FormData()
  formData.append('file', input.file)
  formData.append('resolution', input.resolution ?? '1h')
  if (input.fixed_utc_offset_minutes !== undefined && input.fixed_utc_offset_minutes !== null) {
    formData.append('utc_offset_minutes', String(input.fixed_utc_offset_minutes))
  }
  if (input.fields && Object.keys(input.fields).length > 0) {
    formData.append(
      'fields',
      JSON.stringify(
        Object.fromEntries(
          Object.entries(input.fields).map(([k, v]) => [k, { unit: v.unit ?? '' }]),
        ),
      ),
    )
  }
  formData.append(
    'meta',
    JSON.stringify({
      source_category: 'user_upload',
      // 空许可证省略(后端 meta 白名单要求 license 为字符串, 显式 null 会阻断)
      ...(input.license ? { license: input.license } : {}),
      created_reason: 'upload',
    }),
  )
  const uploaded = (await request<unknown>(`/projects/${projectId}/datasets/${datasetId}/versions`, {
    method: 'POST',
    formData,
    timeoutMs: 0,
  })) as DatasetUploadResponseDto
  const version = datasetVersionFromServer(uploaded.dataset_version)
  const diagnostics = uploaded.diagnostics ?? []
  return { ...version, diagnostics }
}

export type { ApiErrorBody }

/** 任务类型/状态等枚举导出,供页面组件使用。 */
export type { TaskStatus, TaskType, Currency }
