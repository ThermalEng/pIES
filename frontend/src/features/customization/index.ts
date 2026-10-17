/**
 * 自定义 feature(用户模型模板管理)公开入口。
 *
 * 页面经由此桶文件统一导入请求方法与 DTO 契约类型，不跨 feature 导入内部文件。
 */

export {
  createTemplate,
  deleteTemplate,
  diagnosticsFromApiError,
  getTemplateDetail,
  getTemplateRevision,
  listMyTemplates,
  publishTemplate,
  saveTemplateDraft,
  setTemplateEnabled,
  validateTemplateYaml,
  TemplateSaveError,
} from './api'

export type {
  ModelDiagnosticDto,
  TemplateDeleteResponseDto,
  TemplateDetailDto,
  TemplateDetailResponseDto,
  TemplateDraftRequestDto,
  TemplateDto,
  TemplateListResponseDto,
  TemplateMutationResponseDto,
  TemplatePublishRequestDto,
  TemplatePublishResponseDto,
  TemplateRevisionDetailResponseDto,
  TemplateRevisionDto,
  TemplateStatusDto,
  TemplateValidateRequestDto,
  TemplateValidateResponseDto,
} from './contracts'
