/**
 * 管理维护 feature 前后端契约(与后端 HTTP JSON 字段一一对应, snake_case)。
 *
 * 端点(与后端 iesplan/api/auth.py, admin.py, health.py, objects.py 一致):
 *   GET    /api/auth/users                          用户列表(管理员) → {users: [...]}
 *   POST   /api/auth/users/{id}/reset-password      重置密码(管理员) → {ok: true, message_key: string}
 *   POST   /api/auth/users/{id}/deactivate          停用用户(管理员) → {ok: true}
 *   POST   /api/auth/users/{id}/reactivate          重新启用用户(管理员) → {ok: true}
 *   POST   /api/auth/users/{id}/delete-preview      删除预告(管理员) → UserDeletePreviewResponseDto
 *   DELETE /api/auth/users/{id}                     删除账号(管理员) → {ok: true, deleted_projects: number}
 *   GET    /api/auth/settings                       读取安全设置(管理员) → {registration_enabled: boolean}
 *   PUT    /api/auth/settings                       更新安全设置(管理员) → {registration_enabled: boolean}
 *   GET    /api/admin/storage                       存储状态(管理员) → StorageStatusResponseDto
 *   GET    /api/admin/health                        运维健康(管理员) → AdminHealthResponseDto
 *   GET    /api/admin/audit                         审计查询(管理员) → AuditListResponseDto
 */

import type { UserOutDto } from '../auth/contracts'

/** 管理员用户列表项 DTO(UserOut + project_count)。 */
export interface AdminUserOutDto extends UserOutDto {
  project_count: number
}

/** 用户列表响应 DTO(GET /api/auth/users)。 */
export interface UsersListResponseDto {
  users: AdminUserOutDto[]
}

/** 管理员重置密码请求体(POST /api/auth/users/{id}/reset-password)。 */
export interface ResetPasswordRequestDto {
  new_password: string
}

/** 管理员重置密码响应 DTO。 */
export interface ResetPasswordResponseDto {
  ok: boolean
  message_key: string
}

/** 动作成功响应 DTO(停用/重新启用等)。 */
export interface ActionOkResponseDto {
  ok: boolean
}

/** 用户受影响项目条目。 */
export interface UserDeletePreviewProjectDto {
  id: number
  name: string
  status: string
}

/** 删除账号预告响应 DTO(POST /api/auth/users/{id}/delete-preview)。 */
export interface UserDeletePreviewResponseDto {
  confirm_token: string
  project_count: number
  projects: UserDeletePreviewProjectDto[]
  user_id: number
  username: string
}

/** 删除账号确认请求体(DELETE /api/auth/users/{id})。 */
export interface UserDeleteConfirmRequestDto {
  confirm: boolean
  confirm_token: string
}

/** 删除账号响应 DTO。 */
export interface UserDeleteResponseDto {
  ok: boolean
  deleted_projects: number
}

/** 安全设置响应 DTO(GET /api/auth/settings)。 */
export interface SecuritySettingsResponseDto {
  registration_enabled: boolean
}

/** 安全设置更新请求体(PUT /api/auth/settings)。 */
export interface SecuritySettingsUpdateRequestDto {
  registration_enabled: boolean
}

/** 存储容量 DTO。 */
export interface StorageCapacityDto {
  free_bytes: number
  safe_threshold: number
  ok: boolean
  message?: string
  reason?: string
}

/** 存储对象摘要 DTO。 */
export interface StorageObjectsDto {
  count: number
  total_bytes: number
  by_status?: Record<string, number>
  orphan_count?: number
  pending_deletion_count?: number
}

/** 存储引用摘要 DTO。 */
export interface StorageRefsDto {
  count: number
  referenced_objects?: number
}

/** 存储视图响应 DTO(GET /api/admin/storage)。 */
export interface StorageStatusResponseDto {
  healthy: boolean
  corrupt_count: number
  cleanup_candidates: number
  objects: StorageObjectsDto
  capacity: StorageCapacityDto
  refs: StorageRefsDto
  pending_deletion_count?: number
}

/** 运维健康响应 DTO(GET /api/admin/health)。 */
export interface AdminHealthResponseDto {
  status: 'ok' | 'degraded' | 'down'
  service: string
  version: string
  time: string
  liveness: { ok: boolean; process?: string }
  readiness: { db: boolean }
  metrics?: {
    tasks_by_status?: Record<string, number>
    projects_by_status?: Record<string, number>
    users?: number
  }
  queue?: Record<string, unknown>
  storage?: {
    capacity?: Record<string, unknown>
    corrupt_count?: number
    orphan_count?: number
    object_count?: number
    ok?: boolean
    verify?: Record<string, unknown>
  }
}

/** 审计日志项 DTO。 */
export interface AuditEntryDto {
  id: number
  entity_type: string | null
  entity_id: number | null
  action: string
  actor_id: number | null
  actor_type: string | null
  occurred_at: string
  ip: string | null
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  request_id: string | null
  trace_id: string | null
}

/** 审计列表响应 DTO(GET /api/admin/audit)。 */
export interface AuditListResponseDto {
  items: AuditEntryDto[]
  next_cursor: number | string | null
}
