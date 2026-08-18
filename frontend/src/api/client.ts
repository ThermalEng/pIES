/**
 * API 客户端:fetch 封装 + 类型化方法集。
 *
 * 约定(与后端 API 阶段对齐):
 * - 所有请求走相对路径 /api/*,由 Vite 代理转发到后端(见 vite.config.ts)。
 * - 自动携带凭证(credentials: 'include',会话 Cookie)。
 * - 统一错误处理:解析后端错误信封 {"error": {code, message_key, severity,
 *   blocking, params, location, fix_hint_key, ref_ids}} 为 ApiError;
 *   非信封错误映射为通用键(ies.error.*)。
 * - 401(会话失效):清空会话标记,触发 onUnauthorized 回调(默认跳 /login)。
 * - 成功响应兼容两种形态:裸 JSON 或 {"ok": true, "data": ...} 信封。
 *
 * URL 规范(后端实现时应按此挂载路由):
 *   认证   /api/auth/*
 *   项目   /api/projects[/{id}[/...]]
 *   模型   /api/projects/{id}/graph | /api/projects/{id}/devices | /api/devices/{id}
 *           /api/projects/{id}/connections | /api/connections/{id}
 *   数据集 /api/datasets[/{id}[/versions|/sample]]
 *   配置   /api/projects/{id}/config | /api/config/*
 *   校验   /api/validation/*
 *   任务   /api/tasks[/{id}[/cancel|/retry]]
 *   结果   /api/results/* | /api/evidence/*
 *   导出   /api/exports/*
 *   管理   /api/admin/*
 */

import type {
  AdminUserListParams,
  AdminUserRow,
  ApiErrorBody,
  AssessmentInput,
  AuditEntry,
  AuditListParams,
  CalcConfig,
  CalcConfigInput,
  Connection,
  ConnectionInput,
  Currency,
  Dataset,
  DatasetSample,
  DatasetUploadInput,
  DatasetVersion,
  Device,
  DeviceInput,
  DeviceTypeSpec,
  Diagnostic,
  ExcelExportInput,
  GraphModel,
  HealthStatus,
  LoginRequest,
  LoginResponse,
  MetricValue,
  PageResult,
  PackageExportInput,
  Project,
  ProjectCreateInput,
  ProjectDraft,
  ProjectListParams,
  ProjectMember,
  ProjectVersion,
  RegisterRequest,
  Report,
  ResultAssessment,
  ResultDiff,
  ResultSelection,
  Task,
  TaskCreateInput,
  TaskDetail,
  TaskListParams,
  TaskStatus,
  TaskType,
  User,
  ValidationResult,
} from '../types'
import { ApiError } from '../types'

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

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
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
      notifyUnauthorized()
      // 保留后端诊断信封(登录失败/凭证错误等场景的 message_key),无信封时回退会话失效
      let envelope: ApiErrorBody | null = null
      try {
        envelope = parseErrorEnvelope(await parseJson(res))
      } catch {
        envelope = null
      }
      throw new ApiError(401, envelope, envelope ? undefined : 'ies.diag.auth.session_invalid')
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
      notifyUnauthorized()
      let envelope: ApiErrorBody | null = null
      try {
        envelope = parseErrorEnvelope(await parseJson(res))
      } catch {
        envelope = null
      }
      throw new ApiError(401, envelope, envelope ? undefined : 'ies.diag.auth.session_invalid')
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

function fileNameFromDisposition(disposition: string | null, fallback: string): string {
  if (!disposition) return fallback
  const match = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(disposition)
  const name = match?.[1] ?? match?.[2]
  return name ? decodeURIComponent(name) : fallback
}

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
// 类型化方法集
// ---------------------------------------------------------------------------

export interface AuthApi {
  login(input: LoginRequest): Promise<LoginResponse>
  logout(): Promise<void>
  changePassword(input: { old_password: string; new_password: string }): Promise<void>
  confirmTakeover(input: { token: string }): Promise<Project>
  register(input: RegisterRequest): Promise<User>
  /** 获取当前登录用户(页面刷新后恢复用户信息)。 */
  me(): Promise<User>
}

export interface ProjectsApi {
  list(params?: ProjectListParams): Promise<PageResult<Project>>
  create(input: ProjectCreateInput): Promise<Project>
  get(id: number): Promise<Project>
  updateDraft(id: number, input: Record<string, unknown>): Promise<ProjectDraft>
  versions(id: number): Promise<ProjectVersion[]>
  archive(id: number): Promise<Project>
  unarchive(id: number): Promise<Project>
  delete(id: number): Promise<void>
  duplicate(id: number): Promise<Project>
  transfer(id: number, input: { target_username: string }): Promise<void>
  viewers(id: number): Promise<ProjectMember[]>
}

export interface ModelApi {
  getGraph(projectId: number): Promise<GraphModel>
  addDevice(projectId: number, input: DeviceInput): Promise<Device>
  updateDevice(projectId: number, deviceId: number, input: Partial<DeviceInput>): Promise<Device>
  deleteDevice(projectId: number, deviceId: number): Promise<void>
  connect(projectId: number, input: ConnectionInput): Promise<Connection>
  disconnect(projectId: number, connectionId: number): Promise<void>
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
  versions(id: number): Promise<DatasetVersion[]>
  sample(id: number): Promise<DatasetSample>
}

export interface ConfigApi {
  get(projectId: number): Promise<CalcConfig>
  save(projectId: number, input: CalcConfigInput): Promise<CalcConfig>
  validate(projectId: number): Promise<ValidationResult>
  default(): Promise<CalcConfigInput>
  algorithms(): Promise<{ items: Array<{ name: string; label: string; description_key: string }> }>
}

export interface ValidationApi {
  run(projectId: number, opts?: { config_id?: number }): Promise<ValidationResult>
  baselineConfirm(projectId: number): Promise<{ confirmed: boolean; diagnostics: Diagnostic[] }>
}

export interface TasksApi {
  list(params?: TaskListParams): Promise<PageResult<Task>>
  create(input: TaskCreateInput): Promise<Task>
  get(id: number): Promise<TaskDetail>
  cancel(id: number): Promise<Task>
  retry(id: number): Promise<Task>
}

export interface ResultsApi {
  result(evidencePackageId: number): Promise<{
    evidence_package_id: number
    metrics: Record<string, MetricValue>
    diagnostics: Diagnostic[]
  }>
  assessments(evidencePackageId: number): Promise<ResultAssessment[]>
  assess(evidencePackageId: number, input: AssessmentInput): Promise<ResultAssessment>
  select(projectId: number, input: { result_index_id: number; reason?: string }): Promise<ResultSelection>
  diff(a: number, b: number): Promise<ResultDiff>
  hourly(evidencePackageId: number): Promise<{ resolution: string; n: number; flows: Record<string, number[]> }>
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
}

export const api = {
  auth: {
    login(input: LoginRequest): Promise<LoginResponse> {
      return request<LoginResponse>('/auth/login', { method: 'POST', body: input }).then((res) => {
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
    confirmTakeover(input: { token: string }): Promise<Project> {
      return request<Project>('/auth/confirm-takeover', { method: 'POST', body: input })
    },
    register(input: RegisterRequest): Promise<User> {
      return request<User>('/auth/register', { method: 'POST', body: input })
    },
    me(): Promise<User> {
      return request<User>('/auth/me')
    },
  } satisfies AuthApi,

  projects: {
    list(params?: ProjectListParams): Promise<PageResult<Project>> {
      return request<PageResult<Project>>('/projects', { query: params })
    },
    create(input: ProjectCreateInput): Promise<Project> {
      return request<Project>('/projects', { method: 'POST', body: input })
    },
    get(id: number): Promise<Project> {
      return request<Project>(`/projects/${id}`)
    },
    updateDraft(id: number, input: Record<string, unknown>): Promise<ProjectDraft> {
      return request<ProjectDraft>(`/projects/${id}/draft`, { method: 'PUT', body: input })
    },
    versions(id: number): Promise<ProjectVersion[]> {
      return request<ProjectVersion[]>(`/projects/${id}/versions`)
    },
    archive(id: number): Promise<Project> {
      return request<Project>(`/projects/${id}/archive`, { method: 'POST' })
    },
    unarchive(id: number): Promise<Project> {
      return request<Project>(`/projects/${id}/unarchive`, { method: 'POST' })
    },
    delete(id: number): Promise<void> {
      return request<void>(`/projects/${id}`, { method: 'DELETE' })
    },
    duplicate(id: number): Promise<Project> {
      return request<Project>(`/projects/${id}/duplicate`, { method: 'POST' })
    },
    transfer(id: number, input: { target_username: string }): Promise<void> {
      return request<void>(`/projects/${id}/transfer`, { method: 'POST', body: input })
    },
    viewers(id: number): Promise<ProjectMember[]> {
      return request<ProjectMember[]>(`/projects/${id}/viewers`)
    },
  } satisfies ProjectsApi,

  model: {
    getGraph(projectId: number): Promise<GraphModel> {
      return request<GraphModel>(`/projects/${projectId}/graph`)
    },
    addDevice(projectId: number, input: DeviceInput): Promise<Device> {
      return request<Device>(`/projects/${projectId}/devices`, { method: 'POST', body: input })
    },
    updateDevice(projectId: number, deviceId: number, input: Partial<DeviceInput>): Promise<Device> {
      return request<Device>(`/projects/${projectId}/devices/${deviceId}`, {
        method: 'PUT',
        body: input,
      })
    },
    deleteDevice(projectId: number, deviceId: number): Promise<void> {
      return request<void>(`/projects/${projectId}/devices/${deviceId}`, { method: 'DELETE' })
    },
    connect(projectId: number, input: ConnectionInput): Promise<Connection> {
      return request<Connection>(`/projects/${projectId}/connections`, { method: 'POST', body: input })
    },
    disconnect(projectId: number, connectionId: number): Promise<void> {
      return request<void>(`/projects/${projectId}/connections/${connectionId}`, {
        method: 'DELETE',
      })
    },
    validate(projectId: number): Promise<ValidationResult> {
      return request<ValidationResult>(`/projects/${projectId}/graph/validate`, { method: 'POST' })
    },
    deviceTypes(): Promise<DeviceTypeSpec[]> {
      return request<DeviceTypeSpec[]>('/model/device-types')
    },
  } satisfies ModelApi,

  datasets: {
    list(params?: { project_id?: number; status?: string; cursor?: string; limit?: number }): Promise<PageResult<Dataset>> {
      return request<PageResult<Dataset>>('/datasets', { query: params })
    },
    template(): Promise<Blob> {
      return requestBlob('/datasets/template')
    },
    upload(input: DatasetUploadInput): Promise<DatasetVersion> {
      const formData = new FormData()
      formData.append('file', input.file)
      if (input.project_id !== undefined) formData.append('project_id', String(input.project_id))
      formData.append('name', input.name)
      if (input.description) formData.append('description', input.description)
      if (input.timeline) formData.append('timeline', input.timeline)
      return request<DatasetVersion>('/datasets', { method: 'POST', formData, timeoutMs: 0 })
    },
    uploadDetailed(input: DatasetUploadDetailedInput): Promise<DatasetVersion & { diagnostics?: Diagnostic[] }> {
      const formData = new FormData()
      formData.append('file', input.file)
      if (input.project_id !== undefined) formData.append('project_id', String(input.project_id))
      formData.append('name', input.name)
      if (input.description) formData.append('description', input.description)
      if (input.timeline) formData.append('timeline', input.timeline)
      if (input.resolution) formData.append('resolution', input.resolution)
      if (input.fixed_utc_offset_minutes !== undefined) {
        formData.append('fixed_utc_offset_minutes', String(input.fixed_utc_offset_minutes))
      }
      if (input.fields) formData.append('fields', JSON.stringify(input.fields))
      if (input.license) formData.append('license', input.license)
      return request<DatasetVersion & { diagnostics?: Diagnostic[] }>('/datasets', {
        method: 'POST',
        formData,
        timeoutMs: 0,
      })
    },
    versions(id: number): Promise<DatasetVersion[]> {
      return request<DatasetVersion[]>(`/datasets/${id}/versions`)
    },
    sample(id: number): Promise<DatasetSample> {
      return request<DatasetSample>(`/datasets/${id}/sample`)
    },
  } satisfies DatasetsApi,

  config: {
    get(projectId: number): Promise<CalcConfig> {
      return request<CalcConfig>(`/projects/${projectId}/config`)
    },
    save(projectId: number, input: CalcConfigInput): Promise<CalcConfig> {
      return request<CalcConfig>(`/projects/${projectId}/config`, { method: 'PUT', body: input })
    },
    validate(projectId: number): Promise<ValidationResult> {
      return request<ValidationResult>(`/projects/${projectId}/config/validate`, { method: 'POST' })
    },
    default(): Promise<CalcConfigInput> {
      return request<CalcConfigInput>('/config/default')
    },
    algorithms(): Promise<{ items: Array<{ name: string; label: string; description_key: string }> }> {
      return request<{ items: Array<{ name: string; label: string; description_key: string }> }>(
        '/config/algorithms',
      )
    },
  } satisfies ConfigApi,

  validation: {
    run(projectId: number, opts?: { config_id?: number }): Promise<ValidationResult> {
      return request<ValidationResult>('/validation/run', {
        method: 'POST',
        body: { project_id: projectId, ...opts },
      })
    },
    baselineConfirm(projectId: number): Promise<{ confirmed: boolean; diagnostics: Diagnostic[] }> {
      return request<{ confirmed: boolean; diagnostics: Diagnostic[] }>('/validation/baseline-confirm', {
        method: 'POST',
        body: { project_id: projectId },
      })
    },
  } satisfies ValidationApi,

  tasks: {
    list(params?: TaskListParams): Promise<PageResult<Task>> {
      return request<PageResult<Task>>('/tasks', { query: params })
    },
    create(input: TaskCreateInput): Promise<Task> {
      return request<Task>('/tasks', { method: 'POST', body: input })
    },
    get(id: number): Promise<TaskDetail> {
      return request<TaskDetail>(`/tasks/${id}`)
    },
    cancel(id: number): Promise<Task> {
      return request<Task>(`/tasks/${id}/cancel`, { method: 'POST' })
    },
    retry(id: number): Promise<Task> {
      return request<Task>(`/tasks/${id}/retry`, { method: 'POST' })
    },
  } satisfies TasksApi,

  results: {
    result(evidencePackageId: number): Promise<{
      evidence_package_id: number
      metrics: Record<string, MetricValue>
      diagnostics: Diagnostic[]
    }> {
      return request<{
        evidence_package_id: number
        metrics: Record<string, MetricValue>
        diagnostics: Diagnostic[]
      }>(`/evidence/${evidencePackageId}/result`)
    },
    assessments(evidencePackageId: number): Promise<ResultAssessment[]> {
      return request<ResultAssessment[]>(`/evidence/${evidencePackageId}/assessments`)
    },
    assess(evidencePackageId: number, input: AssessmentInput): Promise<ResultAssessment> {
      return request<ResultAssessment>(`/evidence/${evidencePackageId}/assessments`, {
        method: 'POST',
        body: input,
      })
    },
    select(projectId: number, input: { result_index_id: number; reason?: string }): Promise<ResultSelection> {
      return request<ResultSelection>(`/projects/${projectId}/results/select`, {
        method: 'POST',
        body: input,
      })
    },
    diff(a: number, b: number): Promise<ResultDiff> {
      return request<ResultDiff>('/results/diff', { query: { a, b } })
    },
    hourly(evidencePackageId: number): Promise<{
      resolution: string
      n: number
      flows: Record<string, number[]>
    }> {
      return request<{
        resolution: string
        n: number
        flows: Record<string, number[]>
      }>(`/evidence/${evidencePackageId}/hourly`)
    },
  } satisfies ResultsApi,

  exports: {
    excel(input: ExcelExportInput): Promise<Report> {
      return request<Report>('/exports/excel', { method: 'POST', body: input })
    },
    package(input: PackageExportInput): Promise<Report> {
      return request<Report>('/exports/package', { method: 'POST', body: input })
    },
    async download(reportId: number): Promise<{ blob: Blob; filename: string }> {
      const res = await fetch(buildUrl(`/exports/download/${reportId}`), {
        method: 'GET',
        credentials: 'include',
      })
      if (res.status === 401) {
        notifyUnauthorized()
        let envelope: ApiErrorBody | null = null
        try {
          envelope = parseErrorEnvelope(await parseJson(res))
        } catch {
          envelope = null
        }
        throw new ApiError(401, envelope, envelope ? undefined : 'ies.diag.auth.session_invalid')
      }
      if (!res.ok) {
        throw toApiError(res.status, await parseJson(res))
      }
      const blob = await res.blob()
      return { blob, filename: fileNameFromDisposition(res.headers.get('Content-Disposition'), 'report.xlsx') }
    },
  } satisfies ExportsApi,

  admin: {
    users(params?: AdminUserListParams): Promise<PageResult<AdminUserRow>> {
      return request<PageResult<AdminUserRow>>('/admin/users', { query: params })
    },
    storage(): Promise<{
      total_bytes: number
      used_bytes: number
      quota_bytes: number | null
      object_count: number
    }> {
      return request<{
        total_bytes: number
        used_bytes: number
        quota_bytes: number | null
        object_count: number
      }>('/admin/storage')
    },
    health(): Promise<HealthStatus> {
      return request<HealthStatus>('/admin/health')
    },
    audit(params?: AuditListParams): Promise<PageResult<AuditEntry>> {
      return request<PageResult<AuditEntry>>('/admin/audit', { query: params })
    },
  } satisfies AdminApi,
}

export type { ApiErrorBody }

/** 任务类型/状态等枚举导出,供页面组件使用。 */
export type { TaskStatus, TaskType, Currency }
