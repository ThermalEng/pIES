/** 通用 HTTP、会话及浏览器下载；不依赖业务 feature。 */
import { ApiError } from '../types'
import type { ApiErrorBody } from '../types'

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

export function setSession(): void {
  try {
    window.localStorage.setItem(SESSION_KEY, '1')
  } catch {
    // 隐私模式忽略
  }
}

export function clearSession(): void {
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

/** 由非 2xx 响应构造 ApiError(标准 {error} 信封经解析透传, 其余走状态码通用文案)。 */
function toApiError(status: number, body: unknown): ApiError {
  const envelope = parseErrorEnvelope(body)
  if (envelope) return new ApiError(status, envelope)
  // 无信封:按状态码映射通用文案键
  let fallbackKey = 'ies.error.unknown'
  if (status === 0 || status >= 500) fallbackKey = 'ies.error.internal'
  else if (status === 404) fallbackKey = 'ies.error.route_not_found'
  return new ApiError(status, null, fallbackKey)
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
 * 底层请求: 统一错误信封解析 / 401 会话处理, 成功正文原样交给调用方 DTO。
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
    return payload as T
  } finally {
    cleanup()
  }
}

/** 二进制下载(Blob 返回,带 Content-Disposition 文件名解析)。 */
export async function requestBlob(path: string, opts: RequestOptions = {}): Promise<Blob> {
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
