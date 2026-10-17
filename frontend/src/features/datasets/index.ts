/**
 * 数据集 feature 公开入口(页面组合真正需要的请求、类型与纯映射)。
 *
 * 绑定/解绑经 features/projects 唯一公开草稿传输发送, 本家族只准备
 * dataset.bind/unbind 业务 payload。
 */

export {
  bindVersion,
  downloadTemplate,
  listDatasets,
  listVersions,
  unbindVersion,
  uploadDataset,
  versionFieldNames,
} from './api'
export type { DatasetUploadDetailedInput } from './api'
export { datasetVersionFromServer, qualityFromServer } from './mappers'
export type {
  DatasetDetailResponseDto,
  DatasetFieldDto,
  DatasetListEntryDto,
  DatasetUploadResponseDto,
  DatasetsListResponseDto,
  DatasetVersionDto,
  QualityReportDto,
} from './contracts'
