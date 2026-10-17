/**
 * 数据集 feature 前后端契约(与后端 api/datasets._version_dict/_dataset_dict 同形, snake_case)。
 *
 * 端点(与后端 /api 路由一致):
 *   GET  /api/projects/{pid}/datasets                    数据集列表 {datasets: [{dataset, latest_version}]}
 *   GET  /api/projects/{pid}/datasets/{id}               数据集详情 {dataset, versions: [{...version, files}]}
 *   POST /api/projects/{pid}/datasets/{id}/versions      上传新版本 {dataset_version, diagnostics}
 *
 * latest_version 为 null 表示该数据集尚无版本(合法缺席, 非错误);
 * 详情 versions 条目附带 files 文件摘要, 读取映射不消费。
 *
 * 依赖方向: features/datasets/contracts → ../../types(共享领域类型);
 * mappers 只依赖 contracts/types, 不导入 HTTP 层。
 */

import type { Dataset, DatasetFieldType, Diagnostic, Timeline } from '../../types'

// ---------------------------------------------------------------------------
// 版本字段条目(与后端三处字段构造对应)
// ---------------------------------------------------------------------------

/** 版本字段条目: type 取后端实际发布值(见 DatasetFieldType), 单位与中英文说明。 */
export interface DatasetFieldDto {
  type: DatasetFieldType
  unit: string | null
  description_zh?: string | null
  description_en?: string | null
}

// ---------------------------------------------------------------------------
// 质量报告(消费子集:  rates 实算输入 + 价格对象引用; v2 展示语义未定, 不扩展)
// ---------------------------------------------------------------------------

/** 质量报告消费子集(与 dataset/tables.build_quality_report 的 checks 同形)。 */
export interface QualityReportDto {
  checks?: {
    row_count?: { expected?: number | null } | null
    missing_values?: { total?: number | null } | null
    ranges?: { total?: number | null } | null
  } | null
  price_object_id?: string | null
}

// ---------------------------------------------------------------------------
// 版本 / 信封
// ---------------------------------------------------------------------------

/** 数据集版本 DTO(与后端 _version_dict 字段一一对应)。 */
export interface DatasetVersionDto {
  id: number
  dataset_id: number
  version_no: number
  timeline: Timeline
  /** 可空列: 缺席保留 null, 领域映射为空字符串(见 mappers)。 */
  resolution: string | null
  fixed_utc_offset_minutes: number
  fields: Record<string, DatasetFieldDto>
  units: Record<string, string>
  quality_report: QualityReportDto | null
  provenance: Record<string, unknown> | null
  license: string | null
  created_by: number
  created_at: string
  created_reason: string | null
  /** 详情版本附带的文件摘要, 读取映射不消费。 */
  files?: unknown
}

/** 列表条目: latest_version=null 即无版本数据集, 合法。 */
export interface DatasetListEntryDto {
  dataset: Dataset
  latest_version: DatasetVersionDto | null
}

/** 列表响应: 精确 {datasets} 信封, 直接消费具名键。 */
export interface DatasetsListResponseDto {
  datasets: DatasetListEntryDto[]
}

/** 详情响应: 精确 {dataset, versions} 信封, 直接消费具名键。 */
export interface DatasetDetailResponseDto {
  dataset: Dataset
  versions: DatasetVersionDto[]
}

/** 创建元数据响应: 精确 {dataset} 信封。 */
export interface DatasetCreateResponseDto {
  dataset: Dataset
}

/** 上传响应: 精确 {dataset_version, diagnostics} 信封。 */
export interface DatasetUploadResponseDto {
  dataset_version: DatasetVersionDto
  diagnostics?: Diagnostic[] | null
}
