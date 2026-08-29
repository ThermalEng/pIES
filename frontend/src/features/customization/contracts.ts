/**
 * 自定义 feature(用户模型模板管理)前后端契约。
 *
 * 端点(与后端 /api 路由一致, prefix /api/model-templates):
 *   POST   /api/model-templates                      创建模板草稿
 *   GET    /api/model-templates                      当前用户模板列表 {templates: [...]}
 *   GET    /api/model-templates/{template_id}        模板详情 {template, document, diagnostics}
 *   PUT    /api/model-templates/{template_id}        保存草稿(expected_revision 乐观锁)
 *   POST   /api/model-templates/{template_id}/validate   模板完整校验 {valid, diagnostics}
 *   POST   /api/model-templates/{template_id}/publish    发布不可变 revision
 *   POST   /api/model-templates/{template_id}/disable    停用
 *   POST   /api/model-templates/{template_id}/enable     重新启用
 *   DELETE /api/model-templates/{template_id}        删除未发布草稿
 *   GET    /api/model-templates/{template_id}/revisions/{revision}  精确 revision 详情
 *
 * 依赖方向: features/customization → ../../types / ../../api/client。
 */

/** 模板生命周期状态。 */
export type TemplateStatusDto = 'draft' | 'published' | 'disabled'

/** 模板列表项(与后端 _template_to_dict 对应)。 */
export interface TemplateDto {
  id: string
  template_id: string
  status: TemplateStatusDto
  description: string | null
  draft_revision: number
  draft_sha256: string | null
  draft_has_inputs: boolean | null
  published_revision: number
  published_at: string | null
  created_at: string | null
  updated_at: string | null
}

/** 精确发布 revision。 */
export interface TemplateRevisionDto {
  id: string
  revision: number
  schema_version: string
  content_sha256: string
  inputs_sha256: string | null
  input_count: number
  yaml_object_id: string
  receipt_object_id: string
  summary_object_id: string
  published_by: string
  published_at: string | null
}

/** 模板详情({template, document, diagnostics})。 */
export interface TemplateDetailDto {
  template: TemplateDto
  document: Record<string, unknown> | null
  diagnostics: ModelDiagnosticDto[]
}

/** 创建/保存草稿请求。 */
export interface TemplateDraftRequestDto {
  model_yaml: string
  description?: string | null
  /** 保存草稿时必填(乐观锁)。 */
  expected_revision?: number
}

/** 发布请求(可重试写操作携带幂等键)。 */
export interface TemplatePublishRequestDto {
  expected_revision: number
  idempotency_key?: string | null
}

/** 校验请求(直接提交候选 YAML)。 */
export interface TemplateValidateRequestDto {
  model_yaml: string
}

/** 后端诊断条目(与 backend/core/diagnostics 对应)。 */
export interface ModelDiagnosticDto {
  code: string
  message_key: string
  severity: 'blocking' | 'error' | 'warning' | 'info'
  blocking: boolean
  params: Record<string, unknown>
  location: { object_type: string; field?: string | null; line?: number | null; column?: number | null } | null
  fix_hint_key: string | null
  ref_ids: string[]
}
