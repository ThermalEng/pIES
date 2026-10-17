/**
 * 自定义 feature(用户模型模板管理)API 客户端。
 *
 * 端点清单(prefix /api/model-templates)见 contracts.ts。本层只做路径拼接、
 * 信封解包与类型化响应直读; 不组织跨端点工作流。
 */

import { request } from '../../shared/http'
import { ApiError } from '../../types'
import type {
  ModelDiagnosticDto,
  TemplateDeleteResponseDto,
  TemplateDetailDto,
  TemplateDetailResponseDto,
  TemplateDto,
  TemplateListResponseDto,
  TemplateMutationResponseDto,
  TemplatePublishResponseDto,
  TemplateRevisionDetailResponseDto,
  TemplateRevisionDto,
  TemplateValidateResponseDto,
} from './contracts'

/** 当前用户模板列表(全部状态, 最新在前)。 */
export async function listMyTemplates(): Promise<TemplateDto[]> {
  const res = await request<TemplateListResponseDto>('/model-templates')
  return res.templates
}

/** 模板详情(草稿内容 + 聚合诊断)。 */
export async function getTemplateDetail(templateId: string): Promise<TemplateDetailDto> {
  return request<TemplateDetailResponseDto>(`/model-templates/${encodeURIComponent(templateId)}`)
}

/** 创建模板草稿(模板 ID = YAML 的 device.id)。 */
export async function createTemplate(
  slug: string,
  modelYaml: string,
  description: string | null,
): Promise<TemplateDto> {
  const res = await request<TemplateMutationResponseDto>('/model-templates', {
    method: 'POST',
    body: { slug, model_yaml: modelYaml, description },
  })
  return res.template
}

/** 保存模板草稿(完整替换; expected_revision 乐观锁)。 */
export async function saveTemplateDraft(
  templateId: string,
  modelYaml: string,
  expectedRevision: number,
  description: string | null,
): Promise<TemplateDto> {
  const res = await request<TemplateMutationResponseDto>(
    `/model-templates/${encodeURIComponent(templateId)}`,
    {
      method: 'PUT',
      body: { model_yaml: modelYaml, expected_revision: expectedRevision, description },
    },
  )
  return res.template
}

/** 模板完整校验(不落盘): 返回 {valid, diagnostics}。 */
export async function validateTemplateYaml(
  modelYaml: string,
): Promise<{ valid: boolean; diagnostics: ModelDiagnosticDto[] }> {
  return request<TemplateValidateResponseDto>(
    '/model-templates/validate',
    {
      method: 'POST',
      body: { model_yaml: modelYaml },
    },
  )
}

/** 发布模板草稿为不可变 revision(相同内容幂等; 幂等键重放返回同一结果)。 */
export async function publishTemplate(
  templateId: string,
  expectedRevision: number,
  idempotencyKey: string,
): Promise<{ revision: TemplateRevisionDto; duplicate: boolean }> {
  return request<TemplatePublishResponseDto>(
    `/model-templates/${encodeURIComponent(templateId)}/publish`,
    {
      method: 'POST',
      body: { expected_revision: expectedRevision, idempotency_key: idempotencyKey },
    },
  )
}

/** 停用 / 重新启用模板(只影响后续选择)。 */
export async function setTemplateEnabled(templateId: string, enabled: boolean): Promise<TemplateDto> {
  const res = await request<TemplateMutationResponseDto>(
    `/model-templates/${encodeURIComponent(templateId)}/${enabled ? 'enable' : 'disable'}`,
    { method: 'POST' },
  )
  return res.template
}

/** 删除尚未发布的草稿。 */
export async function deleteTemplate(templateId: string): Promise<void> {
  await request<TemplateDeleteResponseDto>(
    `/model-templates/${encodeURIComponent(templateId)}`,
    { method: 'DELETE' },
  )
}

/** 精确发布 revision 详情(规范 YAML + 回执 + 摘要)。 */
export async function getTemplateRevision(
  templateId: string,
  revision: number,
): Promise<TemplateRevisionDetailResponseDto> {
  return request<TemplateRevisionDetailResponseDto>(
    `/model-templates/${encodeURIComponent(templateId)}/revisions/${revision}`,
  )
}

/** 400 校验失败: 抛携带诊断的错误(输入必须保留)。 */
export class TemplateSaveError extends Error {
  readonly diagnostics: ModelDiagnosticDto[]

  constructor(diagnostics: ModelDiagnosticDto[], message = '模板校验失败') {
    super(message)
    this.name = 'TemplateSaveError'
    this.diagnostics = diagnostics
  }
}

/** 从 ApiError 信封 params.diagnostics 提取诊断列表。 */
export function diagnosticsFromApiError(err: unknown): ModelDiagnosticDto[] {
  if (err instanceof ApiError) {
    const list = err.params?.diagnostics
    if (Array.isArray(list)) {
      return list as ModelDiagnosticDto[]
    }
  }
  return []
}
