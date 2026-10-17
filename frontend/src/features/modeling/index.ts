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
  validationFromServer,
} from './mappers'
export type {
  DeviceParameterSpecDto,
  DeviceTypeDto,
  DeviceTypeListResponseDto,
  DraftConnectionDto,
  DraftDeviceDto,
  DraftModelSectionDto,
  ValidationReportDto,
  ValidationRunResponseDto,
} from './contracts'
