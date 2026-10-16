/**
 * 管理员审计查看 feature 请求层(路径拼接 + client 调用)。
 *
 * 只读 GET /admin/audit, 参数一一透传; 不在浏览器拼接项目过滤, 不改写
 * 游标, 不做聚合或缓存。
 */

import { queryAudit as adminQueryAudit } from '../admin'
import type { AuditEntry } from '../../types'
import type { AppliedAuditQuery } from './model'

/** 审计列表每页条数(固定; 游标分页由后端 next_cursor 驱动)。 */
export const AUDIT_PAGE_SIZE = 10

/** 查询审计(首屏 cursor 为空; 翻页传入上一页 next_cursor)。 */
export async function queryAudit(
  query: AppliedAuditQuery,
  cursor: string | null,
): Promise<{ items: AuditEntry[]; next_cursor: string | null }> {
  const page = await adminQueryAudit({
    ...query,
    limit: AUDIT_PAGE_SIZE,
    ...(cursor === null ? {} : { cursor }),
  })
  return { items: page.items, next_cursor: page.next_cursor }
}
