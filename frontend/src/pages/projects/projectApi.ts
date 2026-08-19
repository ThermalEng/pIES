/**
 * 项目列表页专用的项目 API 扩展(client.ts 暂未提供的接口)。
 *
 * 与 client.ts 同约定:
 * - 相对路径 /api/*,自动携带凭证(credentials: 'include')。
 * - 后端错误信封解析为 ApiError;成功响应兼容裸 JSON 与 {ok, data} 信封。
 * - 401(会话失效):清除会话标记并跳转 /login。
 *
 * 后端契约(backend/iesplan/api/projects.py):
 *   PUT /api/projects/{id}/viewers   添加/移除查看者(body: {user_id, action: add|remove})
 *                                    返回 {members: [...]}
 * 注意:后端按用户 id 而非用户名操作查看者,本模块经管理员用户清单解析用户名 → id。
 */

import { ApiError } from '../../types'
import type { ApiErrorBody, ProjectMember } from '../../types'
import { api } from '../../api/client'

/** 后端错误信封解析(与 client.ts 的 parseErrorEnvelope 一致)。 */
function parseEnvelope(body: unknown): ApiErrorBody | null {
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

/** 成功响应解包:支持裸 JSON 或 {"ok": true, "data": ...} 信封。 */
function unwrap<T>(body: unknown): T {
  if (body && typeof body === 'object' && !Array.isArray(body)) {
    const record = body as Record<string, unknown>
    if (record.ok === true && 'data' in record) {
      return record.data as T
    }
  }
  return body as T
}

async function request<T>(path: string, method: 'POST' | 'PUT' | 'DELETE', body?: unknown): Promise<T> {
  let res: Response
  try {
    res = await fetch(`/api${path}`, {
      method,
      credentials: 'include',
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new ApiError(0, null, 'ies.error.network')
  }

  if (res.status === 401) {
    try {
      window.localStorage.removeItem('iesplan.session')
    } catch {
      // 隐私模式忽略
    }
    const current = window.location.pathname
    if (current !== '/login') {
      window.location.assign('/login')
    }
    throw new ApiError(401, null, 'ies.diag.auth.session_invalid')
  }

  const text = await res.text()
  let payload: unknown = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      throw new ApiError(res.status, null, 'ies.error.bad_json')
    }
  }

  if (!res.ok) {
    const envelope = parseEnvelope(payload)
    if (envelope) throw new ApiError(res.status, envelope)
    let fallbackKey = 'ies.error.unknown'
    if (res.status >= 500) fallbackKey = 'ies.error.internal'
    else if (res.status === 404) fallbackKey = 'ies.error.route_not_found'
    throw new ApiError(res.status, null, fallbackKey)
  }
  return unwrap<T>(payload)
}

/** 后端查看者接口返回 {members: [...]}: 解包成员清单。 */
function membersOf(body: unknown): ProjectMember[] {
  const rec = body && typeof body === 'object' && !Array.isArray(body) ? (body as Record<string, unknown>) : {}
  return Array.isArray(rec.members) ? (rec.members as ProjectMember[]) : []
}

/** 解析用户名 → 用户 id(后端查看者/转移接口均按 id 操作)。 */
async function resolveUserId(username: string): Promise<number> {
  const page = await api.admin.users({ limit: 1000 })
  const user = page.items.find((u) => u.username === username)
  if (!user) throw new ApiError(400, null, 'ies.project.viewer_not_found')
  return user.id
}

/** 添加查看者(仅所有者)。 */
export async function addProjectViewer(projectId: number, username: string): Promise<ProjectMember> {
  const userId = await resolveUserId(username)
  const body = await request<unknown>(`/projects/${projectId}/viewers`, 'PUT', {
    user_id: userId,
    action: 'add',
  })
  const members = membersOf(body)
  return members[members.length - 1] ?? ({} as ProjectMember)
}

/** 移除查看者(仅所有者)。 */
export async function removeProjectViewer(projectId: number, userId: number): Promise<void> {
  await request<unknown>(`/projects/${projectId}/viewers`, 'PUT', {
    user_id: userId,
    action: 'remove',
  })
}
