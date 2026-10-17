/**
 * 项目 feature 请求(传输层实现)。
 *
 * 全部请求经 shared/http request 发出; 成功正文按 contracts 具名键直接
 * 消费后交 mappers 映射; 畸形体自然抛错可见, 不做形状并集猜测。
 * 草稿语义命令 PUT 传输是项目业务协议, 由本文件公开(sendDraftCommand /
 * putDraftCommands); modeling/dataset/task 各自准备业务 payload 后调用,
 * 不在各家族复制命令组装。返回修订号直接消费后端实际返回的新修订号,
 * 冲突由 409 表达, 不回显期望值、不猜 revision 1。
 */

import type {
  PageResult,
  Project,
  ProjectCreateInput,
  ProjectListParams,
} from '../../types'
import { request } from '../../shared/http'
import type {
  DraftCommandDto,
  DraftCommandInput,
  DraftUpdateResponseDto,
  ProjectListResponseDto,
  ProjectMutationResponseDto,
  ProjectViewResponseDto,
} from './contracts'
import {
  createRequestFromInput,
  projectFromMutation,
  projectFromView,
  projectListFromServer,
} from './mappers'

/** 项目列表(后端精确 {projects: [...]} 信封, 直接消费具名键)。 */
export async function listProjects(params?: ProjectListParams): Promise<PageResult<Project>> {
  const body = await request<ProjectListResponseDto>('/projects', { query: params })
  const items = projectListFromServer(body, params)
  return { items, next_cursor: null, limit: items.length }
}

/** 创建项目(请求体与后端 ProjectCreateRequest 同形; 响应 {project, my_role})。 */
export async function createProject(input: ProjectCreateInput): Promise<Project> {
  const body = await request<ProjectMutationResponseDto>('/projects', {
    method: 'POST',
    body: createRequestFromInput(input),
  })
  return projectFromMutation(body)
}

/** 项目视图(精确 {project, draft, versions, my_role}; versions 由版本特性消费)。 */
export async function getProject(id: number): Promise<Project> {
  const body = await request<ProjectViewResponseDto>(`/projects/${id}`)
  return projectFromView(body)
}

/** 归档项目(响应 {project, my_role})。 */
export async function archiveProject(id: number): Promise<Project> {
  const body = await request<ProjectMutationResponseDto>(`/projects/${id}/archive`, { method: 'POST' })
  return projectFromMutation(body)
}

/** 撤销归档(响应 {project, my_role})。 */
export async function unarchiveProject(id: number): Promise<Project> {
  const body = await request<ProjectMutationResponseDto>(`/projects/${id}/unarchive`, { method: 'POST' })
  return projectFromMutation(body)
}

/** 删除项目(0.2.0 B4 误操作防护: 须携带精确匹配的项目名)。 */
export async function deleteProject(id: number, name: string): Promise<void> {
  await request<void>(`/projects/${id}`, { method: 'DELETE', body: { confirm: true, name } })
}

// ---------------------------------------------------------------------------
// 草稿语义命令传输(项目业务协议, 各家族共用)
// ---------------------------------------------------------------------------

/** 语义命令批次的命令 id 计数器(保证批次内唯一)。 */
let commandSeq = 0

/** 构造后端草稿命令(id/project_id/expected_revision/session/unit/type/payload)。 */
function makeCommand(
  projectId: number,
  expectedRevision: number,
  type: string,
  payload: Record<string, unknown>,
  unit: string,
): DraftCommandDto {
  commandSeq += 1
  return {
    id: `fe-${Date.now().toString(36)}-${commandSeq.toString(36)}`,
    project_id: projectId,
    expected_revision: expectedRevision,
    session: 'browser',
    unit,
    type,
    payload,
  }
}

/** 命令业务单元(与旧聚合器同口径: type 前缀, 缺省 model)。 */
function commandUnit(type: string): string {
  return type.split('.')[0] ?? 'model'
}

/**
 * 发送草稿语义命令(调用方给定显式期望修订, 写前不 GET;
 * 成功直接消费后端返回的新修订号, 冲突由后端 409 表达)。
 */
export async function putDraftCommands(
  projectId: number,
  commands: DraftCommandInput[],
  expectedRevision: number,
): Promise<{ revision: number }> {
  const body = await request<DraftUpdateResponseDto>(`/projects/${projectId}/draft`, {
    method: 'PUT',
    body: {
      expected_revision: expectedRevision,
      commands: commands.map((c) => makeCommand(projectId, expectedRevision, c.type, c.payload, commandUnit(c.type))),
    },
  })
  return { revision: body.revision }
}

/**
 * 发送单条草稿语义命令(先取项目视图当前草稿修订作乐观锁, 再 PUT)。
 */
export async function sendDraftCommand(
  projectId: number,
  type: string,
  payload: Record<string, unknown>,
): Promise<{ revision: number }> {
  const body = await request<ProjectViewResponseDto>(`/projects/${projectId}`)
  return putDraftCommands(projectId, [{ type, payload }], body.draft.revision)
}
