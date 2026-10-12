/**
 * 管理员审计查看 feature model(只读; 直接消费共享 AuditEntry 真实字段)。
 *
 * 筛选沿用后端精确过滤语义: action/对象类型为精确字符串, 对象标识为整数
 * 且必须与对象类型一起发送(无对象类型时不发送标识, 不得冒充项目过滤)。
 */

import type { AuditEntry } from '../../types'

/** 已应用的查询(与 AuditListParams 对齐的可选子集)。 */
export interface AppliedAuditQuery {
  action?: string
  entity_type?: string
  entity_id?: number
}

/** 列表状态(loading 初次加载; entries null 表示尚未成功加载过)。 */
export interface AuditListState {
  entries: AuditEntry[] | null
  nextCursor: string | null
  loading: boolean
  loadingMore: boolean
  error: string | null
  applied: AppliedAuditQuery | null
}
