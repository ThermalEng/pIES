/**
 * 项目版本 feature 请求层(路径拼接 + client 调用; 信封严格解析在 api/client)。
 *
 * 本层不组织跨端点工作流(创建后刷新项目/列表的编排在 hooks 完成)。
 */

import { api } from '../../api/client'
import type { ProjectVersionCreateInput } from '../../types'
import type { VersionCreateRequestDto } from './contracts'
import type { VersionModel } from './model'

/** 版本列表(新版本在前, 后端已倒序; 前端不重排, 保持服务端顺序)。 */
export async function listVersions(projectId: number): Promise<VersionModel[]> {
  return api.projects.versions(projectId)
}

/** 版本详情(仅版本元数据)。 */
export async function getVersion(projectId: number, versionId: number): Promise<VersionModel> {
  return api.projects.versionDetail(projectId, versionId)
}

/** 从当前草稿创建版本(原因恒为 manual_save)。 */
export async function createVersion(
  projectId: number,
  dto: VersionCreateRequestDto,
): Promise<VersionModel> {
  const input: ProjectVersionCreateInput = { name: dto.name, reason: dto.reason }
  if (dto.description !== null) input.description = dto.description
  return api.projects.createVersion(projectId, input)
}
