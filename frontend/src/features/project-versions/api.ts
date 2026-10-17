/**
 * 项目版本 feature 请求层(传输层实现, 经 shared/http 直调)。
 *
 * 成功正文按 contracts 具名键直接消费后交 mappers 映射; 畸形体自然抛错
 * 可见, 不做形状并集猜测。
 * 本层不组织跨端点工作流(创建后刷新项目/列表的编排在 hooks 完成)。
 * 应用方案的结果页编排(选择/确认/刷新)留在页面/应用层, 此处只做
 * 单次引用发送与 {version, draft} 映射。
 */

import type { ApplySchemeInput, ApplySchemeResult, ProjectVersionCreateInput, VersionRestoreResult } from '../../types'
import { request } from '../../shared/http'
import type { VersionCreateRequestDto, VersionLineageResponseDto, VersionListResponseDto, VersionResponseDto } from './contracts'
import type { VersionModel } from './model'
import {
  applySchemeRequestFromInput,
  lineageFromServer,
  versionFromServer,
  versionListFromServer,
} from './mappers'

/** 版本列表(新版本在前, 后端已倒序; 前端不重排, 保持服务端顺序)。 */
export async function listVersions(projectId: number): Promise<VersionModel[]> {
  const body = await request<VersionListResponseDto>(`/projects/${projectId}/versions`)
  return versionListFromServer(body)
}

/** 版本详情(仅版本元数据)。 */
export async function getVersion(projectId: number, versionId: number): Promise<VersionModel> {
  const body = await request<VersionResponseDto>(`/projects/${projectId}/versions/${versionId}`)
  return versionFromServer(body)
}

/** 从当前草稿创建版本(原因恒为 manual_save)。 */
export async function createVersion(
  projectId: number,
  dto: VersionCreateRequestDto,
): Promise<VersionModel> {
  const input: ProjectVersionCreateInput = { name: dto.name, reason: dto.reason }
  if (dto.description !== null) input.description = dto.description
  const payload: Record<string, unknown> = { name: input.name }
  if (input.description !== undefined) payload.description = input.description
  if (input.reason !== undefined) payload.reason = input.reason
  const body = await request<VersionResponseDto>(`/projects/${projectId}/versions`, {
    method: 'POST',
    body: payload,
  })
  return versionFromServer(body)
}

/**
 * 恢复历史版本(沿后端自动命名, 不发送 name/description)。
 * 后端创建 reason=restore、parent=来源的新版本与新草稿, 不倒写历史。
 */
export async function restoreVersion(
  projectId: number,
  versionId: number,
): Promise<VersionRestoreResult> {
  const body = await request<VersionLineageResponseDto>(
    `/projects/${projectId}/versions/${versionId}/restore`,
    { method: 'POST', body: {} },
  )
  const { version, draftId, draftRevision } = lineageFromServer(body)
  return { version, draftId, draftRevision }
}

/**
 * 应用方案(任务引用 + 解标识 + 确认时草稿修订; 参数与基线由服务端权威解析)。
 * 修订不符服务端 409, 不创建版本。唯一 {version, draft} 契约。
 */
export async function applyScheme(projectId: number, input: ApplySchemeInput): Promise<ApplySchemeResult> {
  const body = await request<VersionLineageResponseDto>(`/projects/${projectId}/apply-result`, {
    method: 'POST',
    body: applySchemeRequestFromInput(input),
  })
  const { version, draftId, draftRevision } = lineageFromServer(body)
  return { version, draftId, draftRevision }
}
