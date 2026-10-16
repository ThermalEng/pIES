/**
 * 管理维护 feature 前端领域模型。
 */

import type {
  AdminUserListParams,
  AuditListParams,
  HealthStatus,
} from '../../types'
import type { User } from '../auth'

export type {
  AdminUserListParams,
  AuditListParams,
  HealthStatus,
}

/** 管理员用户行模型(与后端 AdminUserOut 一一对应)。 */
export interface AdminUserRow extends User {
  project_count: number
}

/** 删除账号预告模型。 */
export interface UserDeletePreview {
  user_id: number
  username: string
  project_count: number
  projects: Array<{ id: number; name: string; status: string }>
  confirm_token: string
}

/** 审计条目模型(与后端 entry_to_dict 一一对应)。 */
export interface AuditEntry {
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

/**
 * 存储状态前端模型。
 * 注: objects.total_bytes 是对象存储中已存对象的总大小(已使用量),
 * total_bytes 与 used_bytes 作为展示别名保持一致;
 * quota_bytes 在后端未设配额时为 null。
 */
export interface StorageStatus {
  total_bytes: number
  used_bytes: number
  quota_bytes: number | null
  object_count: number
}
