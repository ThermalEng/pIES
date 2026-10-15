/**
 * 全局任务选择解析(纯函数, 无浏览器依赖, 可被 node 定向测试直接导入)。
 *
 * - project_id 仅做分组(路由/查询作用域), 选择比较只看任务与尝试时间。
 * - 无显式选择: 默认取完成顺序最晚的已完成计算(最新尝试 finished_at 最大;
 *   后端收尾尝试必写 finished_at, 与证据包有无无关)。该任务无可用结果时
 *   返回 null, 永不回退到更早的任务。
 * - 有显式选择: 只绑定该任务; 未完成/无结果/未知时返回 null,
 *   永不回退到其他任务。
 * - selectDefaultTaskId 在任务列表项(含批量预取 completed_at)上确定默认
 *   候选, 走完全部历史分页, 不截断窗口。
 */

import type { TaskDetail } from '../types'

/** 已绑定的结果上下文: 任务 id + 其最新证据包 id。 */
export interface SelectedEvidence {
  taskId: number
  packageId: number
}

/** 取某任务详情的最新证据包 id(多个时取最大; 无证据为 null)。 */
function latestPackageOf(detail: TaskDetail): number | null {
  let latest: number | null = null
  for (const p of detail.evidence ?? []) {
    if (latest === null || p.package_id > latest) latest = p.package_id
  }
  return latest
}

/** 任务完成时刻(最新尝试 finished_at; 缺席为空串, 排序最末)。 */
export function completionTimeOf(detail: TaskDetail): string {
  let latest = ''
  for (const a of detail.attempts ?? []) {
    const f = a.finished_at ?? ''
    if (f > latest) latest = f
  }
  return latest
}

/** 完成顺序比较(时刻倒序, 持平按任务 id 倒序, 确定性)。 */
function compareCompletedDesc(a: TaskDetail, b: TaskDetail): number {
  const ta = completionTimeOf(a)
  const tb = completionTimeOf(b)
  if (ta !== tb) return tb.localeCompare(ta)
  return (b.id as number) - (a.id as number)
}

/**
 * 按选择解析结果上下文。
 *
 * @param details 已读取的任务详情(含 evidence/attempts; 调用方负责读取失败可见)。
 * @param explicitTaskId 显式选择的任务 id; null 表示无显式选择走默认。
 * @returns 绑定的任务 + 证据包; 无可用结果时为 null(不回退)。
 *
 * 用户既定规则: 只有已完成任务的证据包才可绑定展示/导出。显式选择未
 * 完成任务时即使详情携带有证据行也返回 null(展示无结果, 不回退他任务)。
 */
export function selectEvidencePackage(
  details: TaskDetail[],
  explicitTaskId: number | null,
): SelectedEvidence | null {
  if (explicitTaskId !== null) {
    const detail = details.find((d) => d.id === explicitTaskId) ?? null
    if (!detail) return null
    if (detail.status !== 'completed') return null
    const packageId = latestPackageOf(detail)
    if (packageId === null) return null
    return { taskId: detail.id, packageId }
  }
  // 默认: 已完成任务按完成时刻倒序(持平按任务 id 倒序, 确定性),
  // 取首位; 其无可用结果即无结果, 不降到更早任务。
  const completed = details
    .filter((d) => d.status === 'completed')
    .sort(compareCompletedDesc)
  const first = completed[0] ?? null
  if (!first) return null
  const packageId = latestPackageOf(first)
  if (packageId === null) return null
  return { taskId: first.id, packageId }
}

/**
 * 列表项默认选择(完成最晚的已完成计算任务 id)。
 *
 * 输入为调用方已按类型过滤的 completed 列表项(需 id + completed_at;
 * 完成时刻缺席排最末, 持平按任务 id 倒序, 确定性)。空输入为 null。
 * 排序口径与详情侧 completionTimeOf 一致(缺席最末、持平 id 倒序)。
 */
export function selectDefaultTaskId(
  items: Array<{ id: number; completed_at: string | null }>,
): number | null {
  let best: { id: number; completed_at: string | null } | null = null
  for (const it of items) {
    if (best === null) {
      best = it
      continue
    }
    const ta = it.completed_at ?? ''
    const tb = best.completed_at ?? ''
    if (ta !== tb ? ta > tb : it.id > best.id) best = it
  }
  return best === null ? null : best.id
}
