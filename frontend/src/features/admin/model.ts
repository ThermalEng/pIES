/**
 * 管理维护 feature 前端领域模型。
 */

import type {
  AdminUserListParams,
  AdminUserRow,
  AuditEntry,
  AuditListParams,
  HealthStatus,
  UserDeletePreview,
} from '../../types'

export type {
  AdminUserListParams,
  AdminUserRow,
  AuditEntry,
  AuditListParams,
  HealthStatus,
  UserDeletePreview,
}

/** 存储状态前端模型(展示存储用量与对象数)。 */
export interface StorageStatus {
  total_bytes: number
  used_bytes: number
  quota_bytes: number | null
  object_count: number
}
