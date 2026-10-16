/**
 * 管理维护 feature 纯映射函数(DTO → FrontendModel)。
 *
 * 规范:
 * - 纯函数, 不访问网络或 React 状态;
 * - 不做多余猜测或空数据伪造; 真实字段直接类型化映射。
 */

import type { UserStatus } from '../auth'
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
    role: dto.role,
    status: dto.status as UserStatus,
    force_password_change: dto.force_password_change,
    credential_version: dto.credential_version,
    last_login_at: dto.last_login_at,
    project_count: dto.project_count,
  }
}

/**
 * 后端 UserDeletePreviewResponse DTO → 前端 UserDeletePreview 模型。
 */
export function mapUserDeletePreviewDto(dto: UserDeletePreviewResponseDto): UserDeletePreview {
  return {
    user_id: dto.user_id,
    username: dto.username,
    project_count: dto.project_count,
    projects: dto.projects.map((p) => ({
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
  const usageBytes = dto.objects.total_bytes
  return {
    total_bytes: usageBytes,
    used_bytes: usageBytes,
    quota_bytes: null,
    object_count: dto.objects.count,
  }
}

/**
 * 后端 AdminHealthResponse DTO → 前端 HealthStatus 模型。
 */
export function mapAdminHealthDto(dto: AdminHealthResponseDto): HealthStatus {
  return {
    status: dto.status,
    version: dto.version,
    checks: {
      liveness: { status: dto.liveness.ok ? 'ok' : 'down' },
      readiness: { status: dto.readiness.db ? 'ok' : 'down' },
      storage: { status: dto.storage.ok ? 'ok' : 'degraded' },
    },
  }
}

/**
 * 后端 AuditEntry DTO → 前端 AuditEntry 模型。
 */
export function mapAuditEntryDto(dto: AuditEntryDto): AuditEntry {
  return {
    id: dto.id,
    entity_type: dto.entity_type,
    entity_id: dto.entity_id,
    action: dto.action,
    actor_id: dto.actor_id,
    actor_type: dto.actor_type,
    occurred_at: dto.occurred_at,
    ip: dto.ip,
    before: dto.before,
    after: dto.after,
    request_id: dto.request_id,
    trace_id: dto.trace_id,
  }
}
