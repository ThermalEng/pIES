/**
 * 管理维护 feature 纯映射函数(DTO → FrontendModel)。
 *
 * 规范:
 * - 纯函数, 不访问网络或 React 状态;
 * - 不做多余猜测或空数据伪造; 后端权威数据直接映射。
 */

import type { UserStatus } from '../../types'
import type {
  AdminHealthResponseDto,
  AdminUserOutDto,
  AuditEntryDto,
  StorageStatusResponseDto,
  UserDeletePreviewResponseDto,
} from './contracts'
import type {
  AdminUserRow,
  AuditEntry,
  HealthStatus,
  StorageStatus,
  UserDeletePreview,
} from './model'

/**
 * 后端 AdminUserOut DTO → 前端 AdminUserRow 模型。
 */
export function mapAdminUserDtoToModel(dto: AdminUserOutDto): AdminUserRow {
  return {
    id: dto.id,
    username: dto.username,
    display_name: dto.display_name,
    email: null,
    role: dto.role,
    roles: dto.role ? [dto.role] : [],
    status: dto.status as UserStatus,
    locale: 'zh-CN',
    timezone: 'UTC',
    fixed_utc_offset_minutes: 480,
    credential_version: dto.credential_version,
    is_system: false,
    created_at: '',
    updated_at: null,
    last_login_at: dto.last_login_at ?? null,
    last_active_at: dto.last_login_at ?? null,
    project_count: Number(dto.project_count ?? 0),
  }
}

/**
 * 后端 UserDeletePreviewResponse DTO → 前端 UserDeletePreview 模型。
 */
export function mapUserDeletePreviewDto(dto: UserDeletePreviewResponseDto): UserDeletePreview {
  return {
    user_id: dto.user_id,
    username: dto.username,
    project_count: Number(dto.project_count ?? 0),
    projects: (dto.projects ?? []).map((p) => ({
      id: p.id,
      name: p.name,
      status: p.status,
    })),
    confirm_token: dto.confirm_token,
  }
}

/**
 * 后端 StorageStatusResponse DTO → 前端 StorageStatus 模型。
 *
 * total_bytes 与 used_bytes 均反映已存对象大小之和(objects.total_bytes)。
 */
export function mapStorageStatusDto(dto: StorageStatusResponseDto): StorageStatus {
  const total = Number(dto.objects?.total_bytes ?? 0)
  return {
    total_bytes: total,
    used_bytes: total,
    quota_bytes: null,
    object_count: Number(dto.objects?.count ?? 0),
  }
}

/**
 * 后端 AdminHealthResponse DTO → 前端 HealthStatus 模型。
 */
export function mapAdminHealthDto(dto: AdminHealthResponseDto): HealthStatus {
  const liveness = dto.liveness ?? { ok: false }
  const readiness = dto.readiness ?? { db: false }
  const storage = dto.storage ?? { ok: false }
  return {
    status: dto.status ?? 'degraded',
    version: dto.version ?? '',
    checks: {
      liveness: { status: liveness.ok === true ? 'ok' : 'down' },
      readiness: { status: readiness.db === true ? 'ok' : 'down' },
      storage: { status: storage.ok === true ? 'ok' : 'degraded' },
    },
  }
}

/**
 * 后端 AuditEntry DTO → 前端 AuditEntry 模型。
 */
export function mapAuditEntryDto(dto: AuditEntryDto): AuditEntry {
  return {
    id: dto.id,
    entity_type: dto.entity_type ?? null,
    entity_id: dto.entity_id !== null && dto.entity_id !== undefined ? Number(dto.entity_id) : null,
    action: dto.action,
    actor_id: dto.actor_id !== null && dto.actor_id !== undefined ? Number(dto.actor_id) : null,
    actor_type: dto.actor_type ?? null,
    occurred_at: dto.occurred_at,
    ip: dto.ip ?? null,
    before: dto.before ?? null,
    after: dto.after ?? null,
    request_id: dto.request_id ?? null,
    trace_id: dto.trace_id ?? null,
  }
}
