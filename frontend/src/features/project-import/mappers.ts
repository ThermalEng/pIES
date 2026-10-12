/**
 * 项目包导入 feature 纯 mapper(DTO↔model; 不访问网络、缓存或 React 状态)。
 */

import { ApiError } from '../../types'
import type { ImportProposal } from '../../types'
import type { ProposalView } from './model'

function asRecord(value: unknown): Record<string, unknown> | null {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

/** 从提案 review_summary 提取展示快照(缺失即 null, 不伪造、不猜测)。 */
export function proposalSnapshot(proposal: ImportProposal): ProposalView {
  const summary = asRecord(proposal.review_summary)
  const snapshot = summary ? asRecord(summary.project_snapshot) : null
  const staging = summary ? asRecord(summary.staging) : null
  const objects = staging ? staging.objects : null
  return {
    name: snapshot && typeof snapshot.name === 'string' ? snapshot.name : null,
    currency: snapshot && typeof snapshot.currency === 'string' ? snapshot.currency : null,
    stagedCount: Array.isArray(objects) ? objects.length : null,
  }
}

/** 从 ApiError 的标准错误 params 提取 reasons(仅字符串项; 非法形状即空数组)。 */
export function errorReasons(err: unknown): string[] {
  if (!(err instanceof ApiError)) return []
  const reasons = (err.params as Record<string, unknown> | null)?.reasons
  if (!Array.isArray(reasons)) return []
  return reasons.filter((r): r is string => typeof r === 'string' && r.length > 0)
}
