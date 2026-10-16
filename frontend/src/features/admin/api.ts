/**
 * 管理维护 feature API 客户端(直接消费唯一后端 DTO 契约)。
 *
 * 规范:
 * - 仅导入 shared/http.ts 通用请求, 禁止反向依赖 api/client.ts;
 * - 严格对齐后端契约, 移除旧 asList/asItems/asRecord 形状猜测;
 * - 正确区分真实字段与展示模型。
 */

import { request } from '../../shared/http'
import type { PageResult } from '../../types'
import type {
  ActionOkResponseDto,
  AdminHealthResponseDto,
  AuditListResponseDto,
  ResetPasswordRequestDto,
  ResetPasswordResponseDto,
  SecuritySettingsResponseDto,
  SecuritySettingsUpdateRequestDto,
  StorageStatusResponseDto,
  UserDeleteConfirmRequestDto,
  UserDeletePreviewResponseDto,
  UserDeleteResponseDto,
  UsersListResponseDto,
} from './contracts'
import {
  mapAdminHealthDto,
  mapAdminUserDtoToModel,
  mapAuditEntryDto,
  mapStorageStatusDto,
  mapUserDeletePreviewDto,
} from './mappers'
import type {
  AdminUserListParams,
  AdminUserRow,
  AuditEntry,
  AuditListParams,
  HealthStatus,
  StorageStatus,
  UserDeletePreview,
} from './model'

/**
 * 获取管理员用户列表。
 */
export async function listUsers(params?: AdminUserListParams): Promise<PageResult<AdminUserRow>> {
  const res = await request<UsersListResponseDto>('/auth/users')
  let list = res.users
  if (params?.status) {
    list = list.filter((u) => u.status === params.status)
  }
  return {
    items: list.map(mapAdminUserDtoToModel),
    next_cursor: null,
    limit: list.length,
  }
}

/**
 * 管理员重置目标用户密码(签发临时密码并强制改密)。
 */
export async function resetPassword(userId: number, newPassword: string): Promise<void> {
  const body: ResetPasswordRequestDto = { new_password: newPassword }
  await request<ResetPasswordResponseDto>(`/auth/users/${userId}/reset-password`, {
    method: 'POST',
    body,
  })
}

/**
 * 停用用户(管理员操作)。
 */
export async function deactivateUser(userId: number): Promise<void> {
  await request<ActionOkResponseDto>(`/auth/users/${userId}/deactivate`, {
    method: 'POST',
  })
}

/**
 * 重新启用用户(管理员操作)。
 */
export async function reactivateUser(userId: number): Promise<void> {
  await request<ActionOkResponseDto>(`/auth/users/${userId}/reactivate`, {
    method: 'POST',
  })
}

/**
 * 请求删除账号影响预告(包含受影响项目清单与确认令牌)。
 */
export async function previewUserDelete(userId: number): Promise<UserDeletePreview> {
  const res = await request<UserDeletePreviewResponseDto>(
    `/auth/users/${userId}/delete-preview`,
    {
      method: 'POST',
    },
  )
  return mapUserDeletePreviewDto(res)
}

/**
 * 确认删除用户及其拥有的项目。
 */
export async function deleteUser(
  userId: number,
  confirmToken: string,
): Promise<{ deleted_projects: number }> {
  const body: UserDeleteConfirmRequestDto = {
    confirm: true,
    confirm_token: confirmToken,
  }
  const res = await request<UserDeleteResponseDto>(`/auth/users/${userId}`, {
    method: 'DELETE',
    body,
  })
  return { deleted_projects: res.deleted_projects }
}

/**
 * 读取当前安全设置(管理员)。
 */
export async function getSecuritySettings(): Promise<{ registration_enabled: boolean }> {
  const res = await request<SecuritySettingsResponseDto>('/auth/settings')
  return { registration_enabled: res.registration_enabled }
}

/**
 * 更新安全设置中的自助注册开关(管理员)。
 */
export async function setRegistrationEnabled(
  enabled: boolean,
): Promise<{ registration_enabled: boolean }> {
  const body: SecuritySettingsUpdateRequestDto = { registration_enabled: enabled }
  const res = await request<SecuritySettingsResponseDto>('/auth/settings', {
    method: 'PUT',
    body,
  })
  return { registration_enabled: res.registration_enabled }
}

/**
 * 获取存储用量与状态视图(管理员)。
 */
export async function getStorageStatus(): Promise<StorageStatus> {
  const res = await request<StorageStatusResponseDto>('/admin/storage')
  return mapStorageStatusDto(res)
}

/**
 * 获取运维健康完整视图(管理员)。
 */
export async function getAdminHealth(): Promise<HealthStatus> {
  const res = await request<AdminHealthResponseDto>('/admin/health')
  return mapAdminHealthDto(res)
}

/**
 * 查询审计日志(管理员过滤与游标翻页)。
 */
export async function queryAudit(params?: AuditListParams): Promise<PageResult<AuditEntry>> {
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

  const res = await request<AuditListResponseDto>('/admin/audit', { query })
  return {
    items: res.items.map(mapAuditEntryDto),
    next_cursor: res.next_cursor !== null && res.next_cursor !== undefined ? String(res.next_cursor) : null,
    limit: params?.limit ?? res.items.length,
  }
}
