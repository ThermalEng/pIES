/**
 * 项目包导入 feature 前后端契约(与后端 HTTP JSON 字段一一对应, snake_case)。
 *
 * 端点(与后端 /api 路由一致):
 *   POST /api/projects/import                  上传项目包 → {proposal: {...}}
 *   POST /api/projects/import/{proposal_id}/confirm 确认导入 → 201 {project, my_role}
 *
 * 确认前 proposal.project_id 为 null(提案阶段不创建普通项目, 不得据此导航
 * 或预建展示); 只有确认成功返回才表示已导入。
 */

import type { ProjectRole } from '../../types'
import type { ProjectDto } from '../projects'

/** 导入提案主体(与后端 _proposal_to_dict 一一对应)。 */
export interface ImportProposalDto {
  id: number
  project_id: number | null
  proposer_id: number
  status: string
  source_object_id: number | null
  review_summary: Record<string, unknown> | null
  review_errors: Record<string, unknown> | null
  created_at: string | null
}

/** 提案响应: 精确 {proposal: {...}}。 */
export interface ImportProposalResponseDto {
  proposal: ImportProposalDto
}

/** 确认响应: 精确 {project, my_role}(project 与项目主体同形, 经 projects 映射)。 */
export interface ImportConfirmResponseDto {
  project: ProjectDto
  my_role: unknown
}

/** 确认后角色(字符串才采用, 其余为 null, 不猜测)。 */
export function confirmedRole(myRole: unknown): string | null {
  return typeof myRole === 'string' ? myRole : null
}

export type { ProjectRole }
