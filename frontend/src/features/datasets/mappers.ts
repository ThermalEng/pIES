/**
 * 数据集 feature 纯 mapper: 版本/质量 DTO → 前端领域模型。
 *
 * 全部函数为纯函数: 不访问网络、缓存或 React 状态, 不导入 HTTP 层。
 * 可信响应直接消费: id/dataset_id/version_no/timeline/offset/created_by/created_at
 * 在后端均为非空列, 不补 0/'hourly'/480 等猜测值; 字段条目一律带 type,
 * 说明只取已发布的 description_zh(后端从不发布裸 description 键);
 * resolution 可空列保留空字符串缺席语义, quality/provenance/license/created_reason
 * 可空列保留 null。畸形体不做形状兼容, 自然抛错可见。
 */

import type { DatasetVersion, QualityReport } from '../../types'
import type { DatasetVersionDto, QualityReportDto } from './contracts'

/** 后端质量报告 dict(checks 聚合形态) → 前端 QualityReport。 */
export function qualityFromServer(q: QualityReportDto): QualityReport {
  const checks = q.checks ?? {}
  const missing = checks.missing_values ?? {}
  const ranges = checks.ranges ?? {}
  const rowCount = Number(checks.row_count?.expected ?? 0)
  const oid = q.price_object_id
  return {
    missing_rate: rowCount > 0 ? Number(missing.total ?? 0) / rowCount : null,
    outlier_rate: rowCount > 0 ? Number(ranges.total ?? 0) / rowCount : null,
    interpolation_notes: null,
    price_object_id: typeof oid === 'string' && oid ? oid : null,
  }
}

/** 后端数据集版本 dict → 前端 DatasetVersion(与 api/datasets._version_dict 同形)。 */
export function datasetVersionFromServer(v: DatasetVersionDto): DatasetVersion {
  return {
    id: v.id,
    dataset_id: v.dataset_id,
    version_no: v.version_no,
    timeline: v.timeline,
    resolution: v.resolution ?? '',
    fixed_utc_offset_minutes: v.fixed_utc_offset_minutes,
    fields: Object.fromEntries(
      Object.entries(v.fields).map(([name, f]) => [
        name,
        {
          name,
          type: f.type,
          unit: f.unit,
          description: f.description_zh ?? '',
        },
      ]),
    ),
    units: v.units,
    quality_report: v.quality_report == null ? null : qualityFromServer(v.quality_report),
    provenance: v.provenance,
    license: v.license,
    created_by: v.created_by,
    created_at: v.created_at,
    created_reason: v.created_reason,
  }
}
