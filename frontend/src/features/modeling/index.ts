/**
 * 建模 feature 公开入口(页面组合真正需要的请求、类型与纯映射)。
 *
 * 草稿模型写经 features/projects 唯一公开草稿传输发送, 本家族只准备
 * 业务 payload; 模板目录/候选校验保存在 api.ts(已走 shared/http)。
 */

export {
  getModel,
  getTemplate,
  listDeviceTypes,
  listProjectModels,
  listTemplates,
  removeConnection,
  removeDevice,
  saveCandidate,
  sendCommands,
  upsertConnection,
  upsertDevice,
  validateCandidate,
  validateModel,
} from './api'
export {
  connectionName,
  connFromDraft,
  deviceFromDraft,
  deviceTypeFromServer,
  deviceTypeListFromServer,
  modelSectionFromView,
  buildYamlSkeleton,
  isValidDeviceId,
} from './mappers'
export type {
  CandidateSaveRequestDto,
  CandidateSaveResultDto,
  CandidateValidateResponseDto,
  CatalogResponseDto,
  DeviceParameterSpecDto,
  DeviceTypeDto,
  DeviceTypeListResponseDto,
  DraftConnectionDto,
  DraftDeviceDto,
  DraftModelSectionDto,
  ModelDiagnosticDto,
  ProjectModelDto,
  ProjectModelListResponseDto,
  TemplateDetailDto,
  TemplateRevisionDetailDto,
  TemplateRevisionDto,
  TemplateSummaryDto,
} from './contracts'
export type {
  CandidateModel,
  ModelDiagnostic,
  ProjectModelSummary,
  SavedModelInfo,
  TemplateDetail,
  TemplateRevision,
  TemplateSummary,
} from './model'
export { CandidateSaveError } from './model'
