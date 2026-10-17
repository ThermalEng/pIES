/**
 * 项目版本 feature mapper: 表单 model → 请求 DTO, 响应 DTO → 展示 model(纯函数)。
 *
 * 名称两侧空白在前端收敛一次(后端仍以 PROJ-CMD-001 为权威拒收空名);
 * 描述空串归一为 null(与后端 Optional 语义一致); reason 恒为 manual_save,
 * 不接受调用方传入(防止伪装 restore/apply_result 等后端内部原因)。
 * 版本主体严格解析(必填缺失/类型不符直接抛错, 不补 0/''/null 伪造版本);
 * 可空字段仅接受 null/undefined → null。
 */

import type { ApplySchemeInput, ProjectVersion } from '../../types'
import type {
  ApplySchemeRequestDto,
  ProjectVersionDto,
  VersionCreateRequestDto,
  VersionDraftPointerDto,
  VersionLineageResponseDto,
  VersionListResponseDto,
  VersionResponseDto,
} from './contracts'
import type { VersionCreateForm } from './model'

/** 创建表单校验失败(纯前端必填检查, 后端仍为权威)。 */
export class VersionFormError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'VersionFormError'
  }
}

/**
 * 创建表单 → 请求 DTO。
 * 名称去首尾空白后仍为空则抛 VersionFormError(阻止发送, 保留用户输入)。
 */
export function buildCreateRequest(form: VersionCreateForm): VersionCreateRequestDto {
  const name = form.name.trim()
  if (!name) {
    throw new VersionFormError('version name is required')
  }
  const description = form.description.trim()
  return {
    name,
    description: description ? description : null,
    reason: 'manual_save',
  }
}

/** 后端版本 dict(version_to_dict) → 前端 ProjectVersion。 */
export function projectVersionFromServer(v: ProjectVersionDto): ProjectVersion {
  return {
    id: v.id,
    project_id: v.project_id,
    version_no: v.version_no,
    name: v.name,
    description: v.description,
    created_by: v.created_by,
    created_at: v.created_at,
    parent_version_id: v.parent_version_id,
    source_draft_id: v.source_draft_id,
    source_draft_revision: v.source_draft_revision,
    reason: v.reason,
    project_baseline: {
      resolution: v.project_baseline.resolution,
      start_year: v.project_baseline.start_year,
      scenario_mode: v.project_baseline.scenario_mode,
    },
    currency: v.currency as ProjectVersion['currency'],
    schema_version: v.schema_version,
    content_object_id: v.content_object_id,
  }
}

/** 版本列表信封 {versions} → 展示 model(后端已倒序, 前端不重排)。 */
export function versionListFromServer(body: VersionListResponseDto): ProjectVersion[] {
  return body.versions.map(projectVersionFromServer)
}

/** 单版本信封 {version} → 展示 model。 */
export function versionFromServer(body: VersionResponseDto): ProjectVersion {
  return projectVersionFromServer(body.version)
}

/** 草稿指针严格解析(仅 id/revision)。 */
export function draftPointerFromServer(d: VersionDraftPointerDto): { id: number; revision: number } {
  return { id: d.id, revision: d.revision }
}

/** 恢复/应用方案信封 {version, draft} → 版本 + 草稿指针。 */
export function lineageFromServer(body: VersionLineageResponseDto): {
  version: ProjectVersion
  draftId: number
  draftRevision: number
} {
  const version = projectVersionFromServer(body.version)
  const draft = draftPointerFromServer(body.draft)
  return { version, draftId: draft.id, draftRevision: draft.revision }
}

/** 应用方案输入 → 请求体(只发送引用与确认时修订)。 */
export function applySchemeRequestFromInput(input: ApplySchemeInput): ApplySchemeRequestDto {
  return {
    task_id: input.task_id,
    solution_id: input.solution_id,
    expected_revision: input.expected_revision,
  }
}
