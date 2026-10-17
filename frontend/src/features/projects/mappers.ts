/**
 * 项目 feature 纯 mapper: 后端项目 DTO → 前端领域模型。
 *
 * 纯函数, 不访问网络、缓存或 React 状态, 不导入 HTTP 层与聚合器。
 * 可信响应直接消费具名键; 列表/视图的既有缺席语义与旧聚合器实现一致
 * (搬迁, 不重设计); 畸形体自然抛错可见。
 * PUT draft 返回修订号走严格消费(见 api, 后端恒返, 不回显期望值)。
 */

import type {
  Project,
  ProjectCreateInput,
  ProjectDraft,
  ProjectListParams,
} from '../../types'
import type {
  DraftViewDto,
  ProjectCreateRequestDto,
  ProjectDto,
  ProjectListItemDto,
  ProjectListResponseDto,
  ProjectMutationResponseDto,
  ProjectViewResponseDto,
} from './contracts'
import { roleOf } from './contracts'

/** 列表信封 {projects} → 项目模型(行内 my_role 提升为 role)。 */
export function projectListFromServer(body: ProjectListResponseDto, params?: ProjectListParams): Project[] {
  void params
  return body.projects.map((p) => projectFromDto(p, p.my_role))
}

/** 项目主体 DTO → 前端 Project(草稿缺席时无 draft 键, 不猜测)。 */
export function projectFromDto(p: ProjectDto, myRole: unknown): Project {
  return {
    id: p.id,
    name: p.name,
    description: p.description,
    status: p.status,
    owner_id: p.owner_id,
    currency: p.currency,
    schema_version: p.schema_version,
    current_draft_id: p.current_draft_id,
    current_version_id: p.current_version_id,
    created_at: p.created_at,
    updated_at: p.updated_at,
    created_by: p.created_by,
    role: roleOf(myRole),
  }
}

/** 项目视图 {project, draft, my_role} → 前端 Project(含草稿摘要; 与旧聚合器同形)。 */
export function projectFromView(body: ProjectViewResponseDto): Project {
  const out = projectFromDto(body.project, body.my_role)
  const draft = body.draft
  if (draft !== null && typeof draft === 'object') {
    out.draft = draftFromView(draft)
  }
  return out
}

/** 视图草稿 → 前端 ProjectDraft(绑定清单与工作输入缺席语义与旧实现一致)。 */
export function draftFromView(draft: DraftViewDto): ProjectDraft {
  const bindings = Array.isArray(draft.dataset_bindings) ? draft.dataset_bindings : undefined
  // 任务提交工作输入只读草稿内容文档的 task_working_inputs 单键(纯对象才
  // 携带; 缺席/形态不符为 undefined, 对话框取默认空态, 不猜测)。
  const content = draft.content
  const workingRaw =
    content !== null && typeof content === 'object' && !Array.isArray(content)
      ? (content as Record<string, unknown>).task_working_inputs
      : null
  const working =
    workingRaw !== null && typeof workingRaw === 'object' && !Array.isArray(workingRaw)
      ? (workingRaw as Record<string, unknown>)
      : undefined
  return {
    id: draft.id,
    project_id: draft.project_id,
    revision: draft.revision,
    content_object_id: draft.content_object_id,
    is_current: draft.is_current !== false,
    updated_by: draft.updated_by,
    updated_at: draft.updated_at,
    created_at: draft.created_at,
    dataset_bindings: bindings,
    task_working_inputs: working,
  }
}

/** 创建输入 → 后端请求体(与后端 ProjectCreateRequest 同形)。 */
export function createRequestFromInput(input: ProjectCreateInput): ProjectCreateRequestDto {
  return {
    name: input.name,
    description: input.description ?? null,
    currency: input.currency,
    baseline_resolution: input.baseline_resolution,
    baseline_start_year: input.baseline_start_year,
    baseline_scenario_mode: input.baseline_scenario_mode,
  }
}

/** 创建/归档响应 {project, my_role} → 前端 Project。 */
export function projectFromMutation(res: ProjectMutationResponseDto): Project {
  return projectFromDto(res.project, res.my_role)
}

/** 兼容旧聚合器 projectFromServer 入口: 接受 {project, draft?, my_role?} 包裹体。 */
export function projectFromServer(body: {
  project: ProjectDto
  draft?: DraftViewDto | null
  my_role?: unknown
}): Project {
  return projectFromView({
    project: body.project,
    draft: body.draft ?? null,
    my_role: body.my_role,
  })
}

/** 列表项 my_role 缺席时的宽容提升(旧 asItems 行内语义的归属实现)。 */
export function projectFromListItem(p: ProjectListItemDto): Project {
  return projectFromDto(p, p.my_role)
}
