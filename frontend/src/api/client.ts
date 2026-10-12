/**
 * API 客户端:fetch 封装 + 类型化方法集。
 *
 * 本文件同时承担「前后端契约适配层」职责:后端(backend/iesplan/api/*)路由
 * 以 /api/projects/{project_id}/... 为统一前缀,列表/单资源返回裸信封
 * (如 {projects: [...]}、{project: {...}}),前端方法签名以业务语义为准,
 * 由本层完成路径拼接与请求/响应形状映射。适配规则:
 * - 列表信封 → PageResult{items, next_cursor, limit}(asItems 辅助);
 * - 单资源包裹 → 解包(oneOf / projectFromServer 等辅助);
 * - 字段名与后端 snake_case 保持一致,前端类型字段与之对齐;
 * - 后端不具备的能力(人工评估打分、数据行预览等)按可用端点降级适配,见方法注释。
 *
 * 其余约定(错误信封 / 401 处理 / 会话标记)与原实现一致:
 * - 所有请求走相对路径 /api/*,由 Vite 代理转发到后端(见 vite.config.ts)。
 * - 自动携带凭证(credentials: 'include',会话 Cookie)。
 * - 统一错误处理:解析后端错误信封 {"error": {code, message_key, severity,
 *   blocking, params, location, fix_hint_key, ref_ids}} 为 ApiError;
 * - 成功响应兼容两种形态:裸 JSON 或 {"ok": true, "data": ...} 信封。
 */

import type {
  AdminUserListParams,
  AdminUserRow,
  ApiErrorBody,
  AssessmentGrade,
  AuditEntry,
  AuditListParams,
  CalcConfig,
  CalcConfigInput,
  CalculationConfigBody,
  Connection,
  ConnectionInput,
  Currency,
  Dataset,
  DatasetField,
  DatasetSample,
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
  EnergyCarrier,
  EvidenceCandidate,
  ExcelExportInput,
  FinancialAbsence,
  FinancialEvaluationBlock,
  DraftModelSection,
  HealthStatus,
  LoginRequest,
  MetricValue,
  ImportConfirmResult,
  ImportProposal,
  PackageExportInput,
  PageResult,
  PlanningSolution,
  Project,
  SnapshotRefs,
  ProjectCreateInput,
  ProjectListParams,
  ProjectRole,
  ProjectVersion,
  ProjectVersionCreateInput,
  VersionRestoreResult,
  PublicAuthSettings,
  RegisterRequest,
  Report,
  ResultAssessment,
  ResultSelection,
  Severity,
  Task,
  TaskAttempt,
  TaskBatch,
  TaskCreateInput,
  TaskDetail,
  TaskLease,
  TaskListParams,
  TaskOutcome,
  TaskStatus,
  TaskType,
  Timeline,
  User,
  UserDeletePreview,
  ValidationResult,
} from '../types'
import { ApiError } from '../types'
import { collectHourlyValues } from './hourlyPages'

const API_BASE = '/api'

/** 会话标记(登录成功写入;HttpOnly Cookie 无法被 JS 读取,仅作前端路由守卫的 best-effort)。 */
const SESSION_KEY = 'iesplan.session'

/**
 * 查询参数(undefined/null/空串自动剔除)。
 * 用 object 而非 Record<string, ...>,使接口类型(如 ProjectListParams)可直接传入。
 */
type Query = object

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  /** 拼接到路径后的查询参数。 */
  query?: Query
  /** JSON 请求体。 */
  body?: unknown
  /** multipart 表单(上传场景)。 */
  formData?: FormData
  headers?: Record<string, string>
  /** 超时(毫秒);缺省 60s,0 表示不超时。 */
  timeoutMs?: number
  signal?: AbortSignal
}

// ---------------------------------------------------------------------------
// 会话管理
// ---------------------------------------------------------------------------

function setSession(): void {
  try {
    window.localStorage.setItem(SESSION_KEY, '1')
  } catch {
    // 隐私模式忽略
  }
}

function clearSession(): void {
  try {
    window.localStorage.removeItem(SESSION_KEY)
  } catch {
    // 忽略
  }
}

/** 前端路由守卫用:是否存在会话标记(权威校验仍由后端完成)。 */
export function hasSession(): boolean {
  try {
    return window.localStorage.getItem(SESSION_KEY) === '1'
  } catch {
    return false
  }
}

/** 会话失效回调(由 App 注册,默认跳 /login)。 */
let unauthorizedHandler: (() => void) | null = null

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler
}

function notifyUnauthorized(): void {
  clearSession()
  if (unauthorizedHandler) {
    unauthorizedHandler()
  } else {
    const current = window.location.pathname
    if (current !== '/login') {
      window.location.assign('/login')
    }
  }
}

// ---------------------------------------------------------------------------
// 底层请求
// ---------------------------------------------------------------------------

function buildUrl(path: string, query?: Query): string {
  const base = API_BASE + path
  if (!query) return base
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null && value !== '') {
      params.set(key, String(value))
    }
  }
  const qs = params.toString()
  return qs ? `${base}?${qs}` : base
}

/** 请求超时控制(外部 signal 与内部超时合并)。 */
function createAbort(external?: AbortSignal, timeoutMs?: number): {
  signal: AbortSignal
  cleanup: () => void
} {
  const controller = new AbortController()
  const onExternalAbort = () => controller.abort()
  if (external) {
    external.addEventListener('abort', onExternalAbort, { once: true })
  }
  let timer: ReturnType<typeof setTimeout> | undefined
  if (timeoutMs && timeoutMs > 0) {
    timer = setTimeout(() => controller.abort(new DOMException('Timeout', 'TimeoutError')), timeoutMs)
  }
  return {
    signal: controller.signal,
    cleanup: () => {
      if (timer !== undefined) clearTimeout(timer)
      if (external) {
        external.removeEventListener('abort', onExternalAbort)
      }
    },
  }
}

function parseErrorEnvelope(body: unknown): ApiErrorBody | null {
  if (body && typeof body === 'object') {
    const error = (body as { error?: unknown }).error
    if (error && typeof error === 'object') {
      const e = error as Partial<ApiErrorBody>
      if (typeof e.message_key === 'string') {
        return {
          code: e.code ?? 'API-UNKNOWN',
          message_key: e.message_key,
          severity: e.severity ?? 'error',
          blocking: e.blocking ?? true,
          params: e.params ?? {},
          location: e.location ?? null,
          fix_hint_key: e.fix_hint_key ?? null,
          ref_ids: e.ref_ids ?? [],
        }
      }
    }
  }
  return null
}

/** 由非 2xx 响应构造 ApiError。 */
function toApiError(status: number, body: unknown): ApiError {
  const envelope = parseErrorEnvelope(body)
  if (envelope) return new ApiError(status, envelope)
  // 0.3.0 C3: 全部错误路径已迁移标准 {error} 信封(宪法 §8.3), 诊断明细在
  // params.diagnostics(parseErrorEnvelope 已透传)。此裸 {diagnostics} fallback 仅作
  // 防御性兜底(未来回归/非标准错误体), 成功响应的 diagnostics 兄弟字段
  // (validation/run 200、dataset 上传 201)不经由此处, 由 unwrapBody 直接返回。
  const raw = body !== null && typeof body === 'object' ? (body as Record<string, unknown>) : {}
  if (Array.isArray(raw.diagnostics)) {
    return new ApiError(status, {
      code: 'VALIDATION-FAILED',
      message_key: 'ies.error.data_validation_failed',
      severity: 'error',
      blocking: true,
      params: { diagnostics: raw.diagnostics },
      location: null,
      fix_hint_key: null,
      ref_ids: [],
    })
  }
  // 无信封:按状态码映射通用文案键
  let fallbackKey = 'ies.error.unknown'
  if (status === 0 || status >= 500) fallbackKey = 'ies.error.internal'
  else if (status === 404) fallbackKey = 'ies.error.route_not_found'
  return new ApiError(status, null, fallbackKey)
}

/** 成功响应解包:支持裸 JSON 或 {"ok": true, "data": ...} 信封。 */
function unwrapBody<T>(body: unknown): T {
  if (body && typeof body === 'object' && !Array.isArray(body)) {
    const record = body as Record<string, unknown>
    if (record.ok === true && 'data' in record) {
      return record.data as T
    }
  }
  return body as T
}

/** 响应体 JSON 解析(空响应返回 null)。 */
async function parseJson(res: Response): Promise<unknown> {
  const text = await res.text()
  if (!text) return null
  try {
    return JSON.parse(text) as unknown
  } catch {
    throw new ApiError(res.status, null, 'ies.error.bad_json')
  }
}

/**
 * 401 响应统一处理(区分「会话失效」与「其他 401」):
 * - 后端认证失败均带诊断信封(message_key);仅当明确表示会话无效
 *   (ies.diag.auth.session_invalid)或响应不可识别时, 才清除本地会话并跳登录页。
 * - takeover_pending 属于"旧窗口被降级/新会话待确认"中间态(H-01),
 *   不应立即清空会话/跳登录页(避免循环登录);由调用方在确认接管后恢复。
 * - 登录请求自身的 401 不触发跳转(页面内展示错误)。
 */
function handleUnauthorized(path: string, envelope: ApiErrorBody | null): ApiError {
  const isLoginPath = path.startsWith('/auth/login')
  const sessionInvalid = envelope?.message_key === 'ies.diag.auth.session_invalid'
  const takeoverPending =
    sessionInvalid && (envelope?.params as { reason?: string } | undefined)?.reason === 'takeover_pending'
  if (!isLoginPath && !takeoverPending && (sessionInvalid || !envelope)) {
    notifyUnauthorized()
  }
  return new ApiError(401, envelope, envelope ? undefined : 'ies.diag.auth.session_invalid')
}

/**
 * 底层请求: 统一错误信封解析 / 401 会话处理 / {ok, data} 信封解包。
 * 供 feature 层 api.ts(如 features/modeling/api.ts)复用, 避免重复实现 HTTP 层。
 * 页面与组件不得直接调用, 必须经 feature api。
 */
export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { signal, cleanup } = createAbort(opts.signal, opts.timeoutMs ?? 60_000)
  try {
    const headers: Record<string, string> = { ...opts.headers }
    let body: BodyInit | undefined
    if (opts.formData) {
      body = opts.formData // 由浏览器生成 Content-Type(multipart boundary)
    } else if (opts.body !== undefined) {
      headers['Content-Type'] = 'application/json'
      body = JSON.stringify(opts.body)
    }

    let res: Response
    try {
      res = await fetch(buildUrl(path, opts.query), {
        method: opts.method ?? 'GET',
        credentials: 'include',
        headers,
        body,
        signal,
      })
    } catch (err) {
      if (signal.aborted && (err as Error).name === 'AbortError') {
        throw new ApiError(0, null, 'ies.error.timeout')
      }
      throw new ApiError(0, null, 'ies.error.network')
    }

    if (res.status === 401) {
      let envelope: ApiErrorBody | null = null
      try {
        envelope = parseErrorEnvelope(await parseJson(res))
      } catch {
        envelope = null
      }
      throw handleUnauthorized(path, envelope)
    }

    const payload = await parseJson(res)
    if (!res.ok) {
      throw toApiError(res.status, payload)
    }
    return unwrapBody<T>(payload)
  } finally {
    cleanup()
  }
}

/** 二进制下载(Blob 返回,带 Content-Disposition 文件名解析)。 */
async function requestBlob(path: string, opts: RequestOptions = {}): Promise<Blob> {
  const { signal, cleanup } = createAbort(opts.signal, opts.timeoutMs ?? 120_000)
  try {
    const res = await fetch(buildUrl(path, opts.query), {
      method: opts.method ?? 'GET',
      credentials: 'include',
      signal,
    })
    if (res.status === 401) {
      let envelope: ApiErrorBody | null = null
      try {
        envelope = parseErrorEnvelope(await parseJson(res))
      } catch {
        envelope = null
      }
      throw handleUnauthorized(path, envelope)
    }
    if (!res.ok) {
      const payload = await parseJson(res)
      throw toApiError(res.status, payload)
    }
    return await res.blob()
  } catch (err) {
    if (err instanceof ApiError) throw err
    throw new ApiError(0, null, 'ies.error.network')
  } finally {
    cleanup()
  }
}

// ---------------------------------------------------------------------------
// 下载辅助
// ---------------------------------------------------------------------------

/** 触发浏览器下载 Blob。 */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

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

/**
 * 证据候选列表映射(仅取选择所需的 index, 其余不消费)。
 * 后端→前端可信交换, 直接映射不重复校核; 无列表即空(单解证据)。
 */
function evidenceCandidatesFromServer(v: unknown): EvidenceCandidate[] {
  if (v === null || v === undefined) return []
  return (v as Array<{ index: number }>).map((item) => ({ index: item.index }))
}

/**
 * 规划解读取映射(单解身份见后端 SINGLE_SOLUTION_ID)。
 * null 即无可用解(metrics-only/无解), 不伪造候选; 可信交换直接映射。
 */
function planningSolutionFromServer(v: unknown): PlanningSolution | null {
  if (v === null || v === undefined) return null
  const r = v as PlanningSolution
  const refs = r.refs
  return {
    solution_id: r.solution_id,
    single_solution: r.single_solution,
    params: r.params.map((p) => ({
      variable: p.variable,
      device_ref: p.device_ref,
      parameter: p.parameter,
      value: p.value,
      unit: p.unit,
    })),
    refs: {
      task_id: refs.task_id,
      calc_snapshot_id: refs.calc_snapshot_id,
      project_version_id: refs.project_version_id,
      evidence_package_id: refs.evidence_package_id,
      dataset_version_ids: refs.dataset_version_ids,
    },
  }
}

/**
 * 原快照资源引用映射(无快照为 null; 可信交换直接映射, 不读当前数据)。
 */
function snapshotRefsFromServer(v: unknown): SnapshotRefs | null {
  if (v === null || v === undefined) return null
  const r = v as SnapshotRefs
  return {
    snapshot_id: r.snapshot_id,
    project_version_id: r.project_version_id,
    dataset_version_ids: r.dataset_version_ids,
  }
}

/** 后端任务摘要(task_summary) → 前端 Task。 */
function taskFromSummary(s: Record<string, unknown>, projectId: number): Task {
  return {
    id: Number(s.id),
    project_id: Number(s.project_id ?? projectId),
    type: s.type as TaskType,
    status: s.status as TaskStatus,
    business_outcome: (s.business_outcome as TaskOutcome) ?? null,
    idempotency_key: String(s.idempotency_key ?? ''),
    calc_snapshot_id: s.calc_snapshot_id === null || s.calc_snapshot_id === undefined ? null : Number(s.calc_snapshot_id),
    requested_by: Number(s.requested_by ?? 0),
    requested_at: String(s.requested_at ?? ''),
    priority: Number(s.priority ?? 0),
    deadline: s.deadline === null || s.deadline === undefined ? null : String(s.deadline),
    superseded_by_task_id:
      s.superseded_by_task_id === null || s.superseded_by_task_id === undefined ? null : Number(s.superseded_by_task_id),
    attempt_count: Number(s.attempt_count ?? 0),
    max_attempts: Number(s.max_attempts ?? 1),
    created_at: String(s.created_at ?? ''),
    updated_at: s.updated_at === null || s.updated_at === undefined ? null : String(s.updated_at),
    result_available: s.result_available === true,
    summary: (s.summary as Task['summary']) ?? undefined,
  }
}

/** 后端任务诊断(level/code/message/context) → 前端 Diagnostic(文案键 ies.diag.raw)。 */
function taskDiagFromServer(d: Record<string, unknown>): Diagnostic {
  const level = String(d.level ?? 'error')
  const severity: Severity =
    level === 'blocking' ? 'blocking' : level === 'warning' ? 'warning' : level === 'info' ? 'info' : 'error'
  return {
    code: String(d.code ?? 'TASK-DIAG'),
    message_key: 'ies.diag.raw',
    params: { message: String(d.message ?? '') },
    severity,
    blocking: level === 'blocking' || level === 'error',
    location: null,
    fix_hint_key: null,
    ref_ids: [],
    occurred_at:
      d.created_at === null || d.created_at === undefined ? new Date().toISOString() : String(d.created_at),
    source: 'task',
    trace_id: d.trace_id === null || d.trace_id === undefined ? null : String(d.trace_id),
    project_id: null,
    task_id: null,
    suppressed: false,
  }
}

/** 后端任务详情 dict → 前端 TaskDetail。 */
function taskDetailFromServer(d: Record<string, unknown>, projectId: number): TaskDetail {
  const base = taskFromSummary(d, projectId)
  const progress = asRecord(d.progress)
  const attempts: TaskAttempt[] = Array.isArray(d.attempts)
    ? (d.attempts as Record<string, unknown>[]).map((a) => ({
        attempt_no: Number(a.attempt_no ?? 0),
        worker_id: a.worker_id === null || a.worker_id === undefined ? null : String(a.worker_id),
        status: (a.status as TaskAttempt['status']) ?? 'pending',
        stop_reason: a.stop_reason === null || a.stop_reason === undefined ? null : String(a.stop_reason),
        started_at: a.started_at === null || a.started_at === undefined ? null : String(a.started_at),
        finished_at: a.finished_at === null || a.finished_at === undefined ? null : String(a.finished_at),
      }))
    : []
  const batch = d.batch as TaskBatch | null | undefined
  return {
    ...base,
    attempts,
    current_lease: d.current_lease === null || d.current_lease === undefined ? null : (d.current_lease as TaskLease),
    progress:
      d.progress === null || d.progress === undefined
        ? null
        : {
            task_id: base.id,
            status: base.status,
            attempt_no: Number(progress.attempt_no ?? 0),
            percent: typeof progress.percent === 'number' ? (progress.percent as number) : null,
            stage: progress.stage === null || progress.stage === undefined ? null : String(progress.stage),
            detail: asRecord(progress.detail),
            updated_at:
              progress.updated_at === null || progress.updated_at === undefined
                ? ''
                : String(progress.updated_at),
            source: progress.source === 'redis' ? 'redis' : 'pg',
            retry: Boolean(progress.retry ?? false),
          },
    batch: batch ?? null,
    outcome_note: d.outcome_note === null || d.outcome_note === undefined ? null : String(d.outcome_note),
    diagnostics: Array.isArray(d.diagnostics)
      ? (d.diagnostics as Record<string, unknown>[]).map(taskDiagFromServer)
      : [],
    evidence: Array.isArray(d.evidence) ? (d.evidence as TaskDetail['evidence']) : [],
    calc_snapshot: d.calc_snapshot === null || d.calc_snapshot === undefined ? null : (d.calc_snapshot as TaskDetail['calc_snapshot']),
  }
}

/** 后端评估 dict(dimensions 聚合形态) → 前端 ResultAssessment(扁平维度字段)。 */
function assessmentFromServer(a: Record<string, unknown>): ResultAssessment {
  const dims = asRecord(a.dimensions)
  return {
    id: Number(a.id),
    evidence_package_id: Number(a.evidence_package_id ?? 0),
    assessor: a.assessor === 'human' ? 'human' : 'system',
    assessed_by: a.assessed_by === null || a.assessed_by === undefined ? null : Number(a.assessed_by),
    dimension_physical: (dims.physical as AssessmentGrade) ?? 'unknown',
    dimension_optimality: (dims.optimality as AssessmentGrade) ?? 'unknown',
    dimension_financial: (dims.financial as AssessmentGrade) ?? 'unknown',
    dimension_reliability: (dims.reliability as AssessmentGrade) ?? 'unknown',
    overall_score: typeof a.overall_score === 'number' ? (a.overall_score as number) : null,
    comment: a.comment === null || a.comment === undefined ? null : String(a.comment),
    detail: a.detail === null || a.detail === undefined ? null : (a.detail as Record<string, unknown>),
    created_at: String(a.created_at ?? ''),
  }
}

/**
 * 后端配置信封 {calculation_config, revision} → 前端 CalcConfig。
 *
 * 可信响应直接类型映射: 后端在用户输入入口已唯一校验全文, 此处不补齐
 * 缺项(不伪造 ''/[]/null/0, 尤其 time_limit_seconds/options), 也不重复
 * 作第二次业务校验; 仅保留 revision 信封判断。缺项原样透出, 由编辑页
 * 显式呈现为空态。
 */
function configFromServer(body: unknown, projectId: number): CalcConfig {
  const env = asRecord(body)
  if (typeof env.revision !== 'number' || !env.calculation_config || typeof env.calculation_config !== 'object') {
    throw new Error('invalid calculation config envelope')
  }
  return {
    project_id: projectId,
    calculation_config: env.calculation_config as CalculationConfigBody,
    revision: env.revision as number,
  }
}

/** 后端质量报告 dict(checks 聚合形态) → 前端 QualityReport。 */
function qualityFromServer(q: Record<string, unknown>): { missing_rate: number | null; outlier_rate: number | null; interpolation_notes: string[] | null; price_object_id: string | null } {
  const checks = asRecord(q.checks)
  const missing = asRecord(checks.missing_values)
  const ranges = asRecord(checks.ranges)
  const rc = asRecord(checks.row_count)
  const rowCount = Number(rc.expected ?? 0)
  const oid = q.price_object_id
  return {
    missing_rate: rowCount > 0 ? Number(missing.total ?? 0) / rowCount : null,
    outlier_rate: rowCount > 0 ? Number(ranges.total ?? 0) / rowCount : null,
    interpolation_notes: null,
    price_object_id: typeof oid === 'string' && oid ? oid : null,
  }
}

/** 后端数据集版本 dict → 前端 DatasetVersion(fields/quality_report 形状适配)。 */
function datasetVersionFromServer(v: Record<string, unknown>): DatasetVersion {
  const fields = asRecord(v.fields)
  return {
    id: Number(v.id),
    dataset_id: Number(v.dataset_id ?? 0),
    version_no: Number(v.version_no ?? 0),
    timeline: (v.timeline as Timeline) ?? 'hourly',
    resolution: String(v.resolution ?? ''),
    fixed_utc_offset_minutes: Number(v.fixed_utc_offset_minutes ?? 480),
    fields: Object.fromEntries(
      Object.entries(fields).map(([name, f]) => {
        const fr = asRecord(f)
        return [
          name,
          {
            name,
            type: (fr.type as DatasetField['type']) ?? 'float',
            unit: fr.unit === null || fr.unit === undefined ? null : String(fr.unit),
            description: String(fr.description_zh ?? fr.description ?? ''),
          },
        ]
      }),
    ),
    units: asRecord(v.units) as Record<string, string>,
    quality_report:
      v.quality_report === null || v.quality_report === undefined
        ? null
        : qualityFromServer(asRecord(v.quality_report)),
    provenance: v.provenance === null || v.provenance === undefined ? null : (v.provenance as Record<string, unknown>),
    license: v.license === null || v.license === undefined ? null : String(v.license),
    created_by: Number(v.created_by ?? 0),
    created_at: String(v.created_at ?? ''),
    created_reason: v.created_reason === null || v.created_reason === undefined ? null : String(v.created_reason),
  }
}

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
  return {
    type_id: String(s.type_id ?? ''),
    version: String(s.version ?? ''),
    name_zh: String(s.name_zh ?? ''),
    name_en: String(s.name_en ?? ''),
    energy_carriers: Array.isArray(s.energy_carriers) ? (s.energy_carriers as EnergyCarrier[]) : [],
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

// ---------------------------------------------------------------------------
// 跨方法模块级缓存(后端路由以 project/task 为键, 前端方法以业务对象为键)
// ---------------------------------------------------------------------------

/** 证据包 id → 任务 id(由任务详情填充; 结果/导出 API 以任务为路由键)。 */
const pkgTaskCache = new Map<number, number>()

/** 证据包 id → 最新评估 id(由评估历史填充; Excel 导出需要 assessment_id)。 */
const pkgAssessmentCache = new Map<number, number>()

/** 任务 → 结果视图原始响应缓存(hourly 复用 result 的同一次 GET, 避免重复往返)。 */
const resultBodyCache = new Map<string, Record<string, unknown>>()

/** 下载授权会话(id → 下载参数; 后端下载走短期 token, 前端以会话 id 引用)。 */
const downloadSessions = new Map<number, { projectId: number; kind: 'excel' | 'package'; token: string; filename: string }>()
let downloadSeq = 0

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

/** 解析证据包的最新评估 id(Excel 导出需要; 缓存未命中时按需回退:先取最新评估列表,若空则触发一次 full 评估)。 */
async function resolveAssessmentId(projectId: number, evidencePackageId: number): Promise<number> {
  const cached = pkgAssessmentCache.get(evidencePackageId)
  if (cached !== undefined && cached > 0) return cached
  const taskId = pkgTaskCache.get(evidencePackageId)
  if (taskId !== undefined) {
    try {
      const body = await request<unknown>(`/projects/${projectId}/tasks/${taskId}/result/assessments`)
      const items = asList<Record<string, unknown>>(body, 'items').map(assessmentFromServer)
      for (const a of items) pkgAssessmentCache.set(a.evidence_package_id, a.id)
      const latest = items[0]
      if (latest) return latest.id
      // 评估列表为空:主动触发一次 full 评估(后端 run_assessment 创建新记录)
      const assess = await request<unknown>(`/projects/${projectId}/tasks/${taskId}/result/assess`, {
        method: 'POST',
        body: { assessment_type: 'full' },
      })
      const created = assessmentFromServer(asRecord(oneOf<Record<string, unknown>>(assess, 'assessment')))
      pkgAssessmentCache.set(created.evidence_package_id, created.id)
      return created.id
    } catch {
      // 评估接口不可用:走"无 assessment_id"兼容路径(由后端 fallback 处理)
    }
  }
  throw new ApiError(404, null, 'ies.error.no_assessment_available')
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
  confirmTakeover(input: { token: string }): Promise<LoginResult>
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
  list(params: { project_id?: number; status?: string; cursor?: string; limit?: number }): Promise<PageResult<Dataset>>
  template(): Promise<Blob>
  upload(input: DatasetUploadInput): Promise<DatasetVersion>
  /**
   * 详细上传(设计输入 §8.2/§8.3):后端校验后返回数据集版本,
   * 并附带质量报告(version.quality_report)与诊断列表(阻断/警告分级,
   * 经 location 定位到字段/行);阻断性错误未修复时版本不可绑定计算。
   */
  uploadDetailed(input: DatasetUploadDetailedInput): Promise<DatasetVersion & { diagnostics?: Diagnostic[] }>
  versions(projectId: number, id: number): Promise<DatasetVersion[]>
  sample(projectId: number, id: number): Promise<DatasetSample>
  /**
   * 绑定数据集版本到项目草稿(dataset.bind 语义命令, 写入 draft.content.dataset_bindings)。
   * 绑定是校验 VALID-DATA-001 与任务数据输入的权威来源。
   */
  bind(projectId: number, datasetVersionId: number, datasetId: number): Promise<{ revision: number }>
  /** 解除数据集版本绑定(dataset.unbind 语义命令)。 */
  unbind(projectId: number, datasetVersionId: number): Promise<{ revision: number }>
}

export interface ConfigApi {
  get(projectId: number): Promise<CalcConfig>
  save(projectId: number, input: CalcConfigInput): Promise<CalcConfig>
}

/** 部署公开初值(统一部署配置中允许公开的非秘密默认值)。 */
export interface DeploymentDefaults {
  calculation_defaults: { time_limit_seconds: number }
}

export interface DeploymentApi {
  defaults(): Promise<DeploymentDefaults>
}

export interface ValidationApi {
  run(projectId: number): Promise<ValidationResult>
  baselineConfirm(
    projectId: number,
    assumptions: Record<string, unknown>,
  ): Promise<{ confirmed: boolean; diagnostics: Diagnostic[] }>
}

export interface TasksApi {
  list(params?: TaskListParams): Promise<PageResult<Task>>
  create(input: TaskCreateInput): Promise<Task>
  get(projectId: number, taskId: number): Promise<TaskDetail>
  cancel(projectId: number, taskId: number): Promise<Task>
  retry(projectId: number, taskId: number): Promise<Task>
}

export interface ResultsApi {
  result(projectId: number, taskId: number): Promise<{
    evidence_package_id: number
    metrics: Record<string, MetricValue> | null
    diagnostics: Diagnostic[]
    /** 真实候选列表(选择用 index; 空即无候选可供选择)。 */
    candidates: EvidenceCandidate[]
    /** 规划解读取(无可用解为 null, 不伪造候选)。 */
    planningSolution: PlanningSolution | null
    /** 原快照资源引用(历史来源展示唯一依据)。 */
    snapshotRefs: SnapshotRefs | null
  }>
  assessments(projectId: number, taskId: number): Promise<ResultAssessment[]>
  /** 触发系统评估(FE-BE-03: 后端仅接受 assessment_type, 不支持人工评分输入) */
  assess(
    projectId: number,
    taskId: number,
    assessmentType: 'full' | 'physical' | 'optimality' | 'financial' | 'reliability',
  ): Promise<ResultAssessment>
  select(projectId: number, taskId: number, input: { result_index_id: number; reason?: string }): Promise<ResultSelection>
  hourly(projectId: number, taskId: number): Promise<{ flows: Record<string, number[]> }>
}

export interface ExportsApi {
  excel(input: ExcelExportInput): Promise<Report>
  package(input: PackageExportInput): Promise<Report>
  /** 下载报告文件(返回 Blob 与建议文件名)。 */
  download(reportId: number): Promise<{ blob: Blob; filename: string }>
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
    confirmTakeover(input: { token: string }): Promise<LoginResult> {
      return request<LoginResult>('/auth/confirm-takeover', { method: 'POST', body: input }).then((res) => {
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
    list(params?: { project_id?: number; status?: string; cursor?: string; limit?: number }): Promise<PageResult<Dataset>> {
      const projectId = params?.project_id
      if (projectId === undefined || projectId === null) {
        // 后端数据集按项目挂载(/api/projects/{pid}/datasets); 无项目上下文时返回空页
        return Promise.resolve({ items: [], next_cursor: null, limit: 0 })
      }
      return request<unknown>(`/projects/${projectId}/datasets`).then((body) => {
        // 后端返回 {datasets: [{dataset, latest_version}]}: 解包 dataset 为行
        const entries = asList<Record<string, unknown>>(body, 'datasets')
        return {
          items: entries.map((e) => asRecord(e.dataset) as unknown as Dataset),
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
      return request<unknown>(`/projects/${projectId}/datasets/${id}`).then((body) =>
        asList<Record<string, unknown>>(body, 'versions').map(datasetVersionFromServer),
      )
    },
    sample(projectId: number, id: number): Promise<DatasetSample> {
      // 后端无数据行预览端点; 降级: 从最新版本字段名生成表头, 不返回行数据
      return request<unknown>(`/projects/${projectId}/datasets/${id}`).then((body) => {
        const versions = asList<Record<string, unknown>>(body, 'versions')
        const latest = versions[0]
        return { headers: Object.keys(asRecord(latest?.fields ?? {})), rows: [], total_rows: 0 }
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

  config: {
    get(projectId: number): Promise<CalcConfig> {
      // 后端返回 {calculation_config, revision}: 映射为前端配置对象
      return request<unknown>(`/projects/${projectId}/config`).then((body) => configFromServer(body, projectId))
    },
    save(projectId: number, input: CalcConfigInput): Promise<CalcConfig> {
      // 后端 PUT 需要 {calculation_config, expected_revision}(自有链乐观锁):
      // expected_revision 由调用方传入已加载 revision, 首次保存为 null。
      return request<unknown>(`/projects/${projectId}/config`, {
        method: 'PUT',
        body: { calculation_config: input.calculation_config, expected_revision: input.expected_revision },
      }).then((body) => configFromServer(body, projectId))
    },
  } satisfies ConfigApi,

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

  validation: {
    run(projectId: number): Promise<ValidationResult> {
      return request<unknown>(`/projects/${projectId}/validation/run`, { method: 'POST' }).then((body) => {
        // 后端返回 {report, stored}: 报告含 status/blocks_submit/diagnostics
        const report = asRecord(asRecord(body).report)
        return {
          valid: report.blocks_submit !== true,
          diagnostics: asList<Diagnostic>(report, 'diagnostics'),
        }
      })
    },
    baselineConfirm(
      projectId: number,
      assumptions: Record<string, unknown>,
    ): Promise<{ confirmed: boolean; diagnostics: Diagnostic[] }> {
      // 假设内容必须由调用方显式提供(用户确认的评价输入); 本层不编默认值、
      // 不从计算配置派生(0.12-A1 起计算配置不再承载经济参数)。
      return request<unknown>(`/projects/${projectId}/validation/baseline-confirm`, {
        method: 'POST',
        body: { assumptions },
      }).then((body) => ({ confirmed: asRecord(body).confirmed === true, diagnostics: [] }))
    },
  } satisfies ValidationApi,

  tasks: {
    list(params?: TaskListParams): Promise<PageResult<Task>> {
      const projectId = params?.project_id
      if (projectId === undefined || projectId === null) {
        throw new ApiError(400, null, 'ies.error.unknown')
      }
      const query = { ...params } as Record<string, unknown>
      delete query.project_id
      return request<unknown>(`/projects/${projectId}/tasks`, { query }).then((body) => {
        const page = asItems<Record<string, unknown>>(body, 'tasks')
        return {
          items: page.items.map((t) => taskFromSummary(t, projectId)),
          next_cursor: page.next_cursor,
          limit: page.limit,
        }
      })
    },
    create(input: TaskCreateInput): Promise<Task> {
      const config: Record<string, unknown> = {}
      if (input.config_revision !== undefined) config.config_revision = input.config_revision
      if (input.dataset_ids !== undefined) config.dataset_ids = input.dataset_ids
      if (input.priority !== undefined) config.priority = input.priority
      if (input.deadline !== undefined) config.deadline = input.deadline
      if (input.params !== undefined) config.params = input.params
      return request<unknown>(`/projects/${input.project_id}/tasks`, {
        method: 'POST',
        body: {
          task_type: input.type,
          config: Object.keys(config).length > 0 ? config : null,
          // 评价确认是顶层类型化任务输入, 不混入 config(与后端 TaskCreateRequest 同形)。
          ...(input.evaluation !== undefined ? { evaluation: input.evaluation } : {}),
          idempotency_key: input.idempotency_key ?? null,
          parent_task_id: null,
        },
      }).then((res) => taskFromSummary(asRecord(oneOf<Record<string, unknown>>(res, 'task')), input.project_id))
    },
    get(projectId: number, taskId: number): Promise<TaskDetail> {
      return request<unknown>(`/projects/${projectId}/tasks/${taskId}`).then(async (body) => {
        const detail = taskDetailFromServer(asRecord(oneOf<Record<string, unknown>>(body, 'task')), projectId)
        // RR-P1-05: 结果可用性是任务元数据(result_available), 只有明确可用时
        // 才读取证据包; 未完成/无结果不是页面级失败, 不靠探测 /result 的 404 猜测。
        if (detail.result_available) {
          try {
            const result = await request<unknown>(`/projects/${projectId}/tasks/${taskId}/result`)
            const r = asRecord(oneOf<Record<string, unknown>>(result, 'result'))
            const evidence = asRecord(r.evidence)
            const pkgId = Number(evidence.id ?? 0)
            if (pkgId > 0) {
              const status = String(evidence.status ?? 'complete')
              detail.evidence = [
                {
                  package_id: pkgId,
                  object_id: Number(evidence.object_id ?? 0),
                  status: status as TaskDetail['evidence'][number]['status'],
                },
              ]
              // 缓存 证据包 → 任务 反向映射(结果/导出 API 以任务为路由键)
              pkgTaskCache.set(pkgId, taskId)
            }
          } catch {
            // 结果元数据读取失败: 保持空 evidence(详情仍可查, 结果页负责重试)
          }
        }
        return detail
      })
    },
    cancel(projectId: number, taskId: number): Promise<Task> {
      return request<unknown>(`/projects/${projectId}/tasks/${taskId}/cancel`, {
        method: 'POST',
        body: { reason: 'user_cancel' },
      }).then((res) => taskFromSummary(asRecord(oneOf<Record<string, unknown>>(res, 'task')), projectId))
    },
    retry(projectId: number, taskId: number): Promise<Task> {
      return request<unknown>(`/projects/${projectId}/tasks/${taskId}/retry`, { method: 'POST', body: {} }).then(
        (res) => taskFromSummary(asRecord(oneOf<Record<string, unknown>>(res, 'task')), projectId),
      )
    },
  } satisfies TasksApi,

  results: {
    result(projectId: number, taskId: number): Promise<{
      /** available=已提交证据包; no_evidence=任务尚无证据包(未完成), 内容字段为 null。 */
      evidence_status: 'available' | 'no_evidence'
      evidence_package_id: number
      metrics: Record<string, MetricValue> | null
      diagnostics: Diagnostic[]
      /** 已封存财务评价(同一证据内容原样投影, 无块为 null)。 */
      financial: FinancialEvaluationBlock | null
      /** 缺块原因(有块为 null; 后端按可观察事实判定)。 */
      financialAbsence: FinancialAbsence
      /** 真实候选列表(选择用 index)。 */
      candidates: EvidenceCandidate[]
      /** 规划解读取(无可用解为 null)。 */
      planningSolution: PlanningSolution | null
      /** 原快照资源引用。 */
      snapshotRefs: SnapshotRefs | null
    }> {
      return request<unknown>(`/projects/${projectId}/tasks/${taskId}/result`).then((body) => {
        // 后端返回 {result: {evidence_status, evidence, metrics_summary, ...}}
        const r = asRecord(oneOf<Record<string, unknown>>(body, 'result'))
        const evidence = asRecord(r.evidence)
        // 缓存原始响应, hourly 复用同一次 GET(避免选中包时重复请求同一端点)
        resultBodyCache.set(`${projectId}:${taskId}`, r)
        const status = r.evidence_status === 'no_evidence' ? 'no_evidence' : 'available'
        const fin = r.financial_evaluation as FinancialEvaluationBlock | null | undefined
        const absence = r.financial_absence as FinancialAbsence | undefined
        return {
          evidence_status: status,
          evidence_package_id: Number(evidence.id ?? 0),
          metrics:
            status === 'no_evidence'
              ? null
              : ((r.metrics_summary as Record<string, MetricValue>) ?? {}),
          diagnostics: [],
          financial:
            status === 'no_evidence' || fin === null || fin === undefined ? null : fin,
          financialAbsence: fin ? null : (absence ?? null),
          candidates:
            status === 'no_evidence' ? [] : evidenceCandidatesFromServer(r.candidates),
          planningSolution:
            status === 'no_evidence' ? null : planningSolutionFromServer(r.planning_solution),
          snapshotRefs:
            status === 'no_evidence' ? null : snapshotRefsFromServer(r.snapshot_refs),
        }
      })
    },
    assessments(projectId: number, taskId: number): Promise<ResultAssessment[]> {
      return request<unknown>(`/projects/${projectId}/tasks/${taskId}/result/assessments`).then((body) => {
        const items = asList<Record<string, unknown>>(body, 'items').map(assessmentFromServer)
        for (const a of items) pkgAssessmentCache.set(a.evidence_package_id, a.id)
        return items
      })
    },
    assess(projectId: number, taskId: number, assessmentType: 'full' | 'physical' | 'optimality' | 'financial' | 'reliability' = 'full'): Promise<ResultAssessment> {
      // 后端 assess 为「触发系统四维检查」(assessment_type), 不接收人工评分输入
      // (FE-BE-03: 人工评分无法持久化, 前端不再伪装接受 AssessmentInput 后丢弃)
      return request<unknown>(`/projects/${projectId}/tasks/${taskId}/result/assess`, {
        method: 'POST',
        body: { assessment_type: assessmentType },
      }).then((body) => assessmentFromServer(asRecord(oneOf<Record<string, unknown>>(body, 'assessment'))))
    },
    select(projectId: number, taskId: number, input: { result_index_id: number; reason?: string }): Promise<ResultSelection> {
      // 后端按 solution_id(证据候选索引)选择: 前端 result_index_id 即候选索引
      return request<unknown>(`/projects/${projectId}/tasks/${taskId}/result/select`, {
        method: 'POST',
        body: {
          solution_id: input.result_index_id,
          selection_type: 'adopt',
          reason: input.reason ?? null,
        },
      }).then((body) => {
        const s = asRecord(oneOf<Record<string, unknown>>(body, 'selection'))
        return {
          id: Number(s.id),
          project_id: Number(s.project_id ?? projectId),
          result_index_id: Number(s.result_index_id ?? 0),
          selected_by: Number(s.selected_by ?? 0),
          selected_at: String(s.selected_at ?? ''),
          reason: s.reason === null || s.reason === undefined ? null : String(s.reason),
          is_current: s.is_current !== false,
        }
      })
    },
    hourly(projectId: number, taskId: number): Promise<{ flows: Record<string, number[]> }> {
      // 后端 hourly 为单字段分页查询(需 field 参数): 先取结果视图的 hourly_refs
      // 字段清单, 再并行拉取各字段完整序列组装 flows。单字段超过后端
      // MAX_HOURLY_LIMIT 时按公开 next_start 分页读到 null 为止(见 hourlyPages)。
      // 结果视图复用 result() 已拉取的缓存(避免同一次选中重复 GET 同一端点)。
      const cacheKey = `${projectId}:${taskId}`
      const pending = resultBodyCache.get(cacheKey)
        ? Promise.resolve({ result: resultBodyCache.get(cacheKey) })
        : request<unknown>(`/projects/${projectId}/tasks/${taskId}/result`).then((body) => {
            const r = asRecord(oneOf<Record<string, unknown>>(body, 'result'))
            resultBodyCache.set(cacheKey, r)
            return { result: r }
          })
      return pending.then(async (body) => {
        const r = asRecord(oneOf<Record<string, unknown>>(body, 'result'))
        const refs = Array.isArray(r.hourly_refs) ? (r.hourly_refs as Record<string, unknown>[]) : []
        const ref = refs[0]
        // 无证据包/无逐时引用是显式状态(后端 evidence_status/no_hourly_refs),
        // 不再静默返回空 flows 掩盖失败。
        if (!ref) throw new ApiError(404, null, 'ies.error.no_hourly_data')
        const fields = Array.isArray(ref.fields) ? (ref.fields as string[]).slice(0, 16) : []
        const rows = Number(ref.rows ?? 0)
        // 各字段序列完全独立, 并行拉取(原先逐字段串行最多 16 次往返)
        const pages = await Promise.all(
          fields.map(async (field) => {
            try {
              const values = await collectHourlyValues(
                (start, limit) =>
                  request<unknown>(`/projects/${projectId}/tasks/${taskId}/result/hourly`, {
                    query: { field, start, end: rows, limit },
                  }).then((pageBody) => {
                    const page = asRecord(pageBody)
                    return { values: page.values as number[], next_start: page.next_start as number | null }
                  }),
                rows,
              )
              return [field, values] as const
            } catch {
              // 单字段读取失败不影响其余字段
              return null
            }
          }),
        )
        const flows: Record<string, number[]> = {}
        for (const hit of pages) {
          if (hit && Array.isArray(hit[1])) flows[hit[0]] = hit[1] as number[]
        }
        return { flows }
      })
    },
  } satisfies ResultsApi,

  exports: {
    excel(input: ExcelExportInput): Promise<Report> {
      const projectId = input.project_id
      // 后端需要 assessment_id(必填): 取该证据包最新评估(评估历史缓存/拉取)
      return resolveAssessmentId(projectId, input.evidence_package_id ?? 0).then((assessmentId) =>
        request<unknown>(`/projects/${projectId}/exports/excel`, {
          method: 'POST',
          body: {
            evidence_package_id: input.evidence_package_id,
            assessment_id: assessmentId,
            lang: input.lang ?? 'zh',
          },
        }).then((body) => {
          // 后端返回 {token, file_name, ...}: 记录下载会话, 返回 Report 形态(id=会话 id)
          const rec = asRecord(body)
          downloadSeq += 1
          const id = downloadSeq
          downloadSessions.set(id, {
            projectId,
            kind: 'excel',
            token: String(rec.token ?? ''),
            filename: String(rec.file_name ?? 'report.xlsx'),
          })
          return {
            id,
            project_id: projectId,
            report_type: 'excel',
            object_id: input.evidence_package_id ?? 0,
            generated_by_task_id: null,
            generated_by: 0,
            generated_at: '',
            status: 'ready' as const,
          }
        }),
      )
    },
    package(input: PackageExportInput): Promise<Report> {
      const projectId = input.project_id
      return request<unknown>(`/projects/${projectId}/exports/package`, { method: 'POST' }).then((body) => {
        // 后端返回 {token, file_name, ...}: 记录下载会话, 返回 Report 形态(id=会话 id)
        const rec = asRecord(body)
        downloadSeq += 1
        const id = downloadSeq
        downloadSessions.set(id, {
          projectId,
          kind: 'package',
          token: String(rec.token ?? ''),
          filename: String(rec.file_name ?? 'project-package.zip'),
        })
        return {
          id,
          project_id: projectId,
          report_type: 'pdf' as const,
          object_id: 0,
          generated_by_task_id: null,
          generated_by: 0,
          generated_at: '',
          status: 'ready' as const,
        }
      })
    },
    async download(reportId: number): Promise<{ blob: Blob; filename: string }> {
      // 后端下载走短期单对象授权 token: 用会话 id 还原 projectId/kind/token
      const session = downloadSessions.get(reportId)
      if (!session) throw new ApiError(404, null, 'ies.error.route_not_found')
      const blob = await requestBlob(`/projects/${session.projectId}/exports/${session.kind}/download`, {
        query: { token: session.token },
      })
      return { blob, filename: session.filename }
    },
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
  const uploaded = asRecord(
    await request<unknown>(`/projects/${projectId}/datasets/${datasetId}/versions`, {
      method: 'POST',
      formData,
      timeoutMs: 0,
    }),
  )
  const version = datasetVersionFromServer(asRecord(uploaded.dataset_version))
  const diagnostics = Array.isArray(uploaded.diagnostics) ? (uploaded.diagnostics as Diagnostic[]) : []
  return { ...version, diagnostics }
}

export type { ApiErrorBody }

/** 任务类型/状态等枚举导出,供页面组件使用。 */
export type { TaskStatus, TaskType, Currency }
