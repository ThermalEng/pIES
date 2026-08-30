/**
 * 自定义 feature(用户模型模板管理)API 客户端。
 *
 * 端点清单(prefix /api/model-templates)见 contracts.ts。本层只做路径拼接、
 * 信封解包与严格解析; 不组织跨端点工作流。
 */

import { request } from '../../api/client'
import { ApiError } from '../../types'
import type {
  ModelDiagnosticDto,
  TemplateDetailDto,
  TemplateDto,
  TemplateRevisionDto,
} from './contracts'

/** 当前用户模板列表(全部状态, 最新在前)。 */
export async function listMyTemplates(): Promise<TemplateDto[]> {
  const body = await request<unknown>('/model-templates')
  const rec = asRecord(body)
  if (!rec || !Array.isArray(rec.templates)) {
    throw new Error('GET /model-templates 响应缺少 templates')
  }
  return rec.templates as TemplateDto[]
}

/** 模板详情(草稿内容 + 聚合诊断)。 */
export async function getTemplateDetail(templateId: string): Promise<TemplateDetailDto> {
  const body = await request<unknown>(`/model-templates/${encodeURIComponent(templateId)}`)
  const rec = asRecord(body)
  if (!rec || !rec.template) {
    throw new Error('GET /model-templates/{id} 响应缺少 template')
  }
  return {
    template: rec.template as unknown as TemplateDto,
    document: rec.document as Record<string, unknown> | null,
    diagnostics: Array.isArray(rec.diagnostics) ? (rec.diagnostics as ModelDiagnosticDto[]) : [],
  }
}

/** 创建模板草稿(模板 ID = YAML 的 device.id)。 */
export async function createTemplate(slug: string, modelYaml: string, description: string | null): Promise<TemplateDto> {
  const body = await request<unknown>('/model-templates', {
    method: 'POST',
    body: { slug, model_yaml: modelYaml, description },
  })
  const rec = asRecord(body)
  if (!rec || !rec.template) {
    throw new Error('POST /model-templates 响应缺少 template')
  }
  return rec.template as unknown as TemplateDto
}

/** 保存模板草稿(完整替换; expected_revision 乐观锁)。 */
export async function saveTemplateDraft(
  templateId: string,
  modelYaml: string,
  expectedRevision: number,
  description: string | null,
): Promise<TemplateDto> {
  const body = await request<unknown>(`/model-templates/${encodeURIComponent(templateId)}`, {
    method: 'PUT',
    body: { model_yaml: modelYaml, expected_revision: expectedRevision, description },
  })
  const rec = asRecord(body)
  if (!rec || !rec.template) {
    throw new Error('PUT /model-templates/{id} 响应缺少 template')
  }
  return rec.template as unknown as TemplateDto
}

/** 模板完整校验(不落盘): 返回 {valid, diagnostics}。 */
export async function validateTemplateYaml(modelYaml: string): Promise<{ valid: boolean; diagnostics: ModelDiagnosticDto[] }> {
  const body = await request<unknown>(`/model-templates/${encodeURIComponent('validate')}`, {
    method: 'POST',
    body: { model_yaml: modelYaml },
  })
  const rec = asRecord(body)
  if (!rec || typeof rec.valid !== 'boolean') {
    throw new Error('POST /model-templates/validate 响应缺少 valid')
  }
  return {
    valid: rec.valid as boolean,
    diagnostics: Array.isArray(rec.diagnostics) ? (rec.diagnostics as ModelDiagnosticDto[]) : [],
  }
}

/** 发布模板草稿为不可变 revision(相同内容幂等; 幂等键重放返回同一结果)。 */
export async function publishTemplate(
  templateId: string,
  expectedRevision: number,
  idempotencyKey: string,
): Promise<{ revision: TemplateRevisionDto; duplicate: boolean }> {
  const body = await request<unknown>(`/model-templates/${encodeURIComponent(templateId)}/publish`, {
    method: 'POST',
    body: { expected_revision: expectedRevision, idempotency_key: idempotencyKey },
  })
  const rec = asRecord(body)
  if (!rec || !rec.revision) {
    throw new Error('POST /model-templates/{id}/publish 响应缺少 revision')
  }
  return {
    revision: rec.revision as unknown as TemplateRevisionDto,
    duplicate: rec.duplicate === true,
  }
}

/** 停用 / 重新启用模板(只影响后续选择)。 */
export async function setTemplateEnabled(templateId: string, enabled: boolean): Promise<TemplateDto> {
  const body = await request<unknown>(
    `/model-templates/${encodeURIComponent(templateId)}/${enabled ? 'enable' : 'disable'}`,
    { method: 'POST' },
  )
  const rec = asRecord(body)
  if (!rec || !rec.template) {
    throw new Error('POST /model-templates/{id}/{enable|disable} 响应缺少 template')
  }
  return rec.template as unknown as TemplateDto
}

/** 删除尚未发布的草稿。 */
export async function deleteTemplate(templateId: string): Promise<void> {
  await request<unknown>(`/model-templates/${encodeURIComponent(templateId)}`, {
    method: 'DELETE',
  })
}

/** 精确发布 revision 详情(规范 YAML + 回执 + 摘要)。 */
export async function getTemplateRevision(
  templateId: string,
  revision: number,
): Promise<{ revision: TemplateRevisionDto; document: Record<string, unknown>; receipt: Record<string, unknown>; summary: Record<string, unknown> }> {
  const body = await request<unknown>(
    `/model-templates/${encodeURIComponent(templateId)}/revisions/${revision}`,
  )
  const rec = asRecord(body)
  if (!rec || !rec.revision) {
    throw new Error('GET /model-templates/{id}/revisions/{n} 响应缺少 revision')
  }
  return {
    revision: rec.revision as unknown as TemplateRevisionDto,
    document: (rec.document as Record<string, unknown>) ?? {},
    receipt: (rec.receipt as Record<string, unknown>) ?? {},
    summary: (rec.summary as Record<string, unknown>) ?? {},
  }
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
  if (!(err instanceof ApiError)) return []
  const params = asRecord((err as { params?: unknown }).params)
  if (!params || !Array.isArray(params.diagnostics)) return []
  return params.diagnostics as ModelDiagnosticDto[]
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}
