/**
 * 导出 feature 前后端契约(与后端 HTTP JSON 字段一一对应, snake_case)。
 *
 * 端点(与后端 iesplan/api/exports.py 一致):
 *   POST /api/projects/{pid}/exports/excel    生成 Excel 报告 → {token, file_name, expires_at_seconds, size_bytes}
 *   POST /api/projects/{pid}/exports/package  导出完整项目包 → {token, file_name, expires_at, manifest, ...}
 *   GET  /api/projects/{pid}/exports/{kind}/download?token=... 下载二进制 Blob
 */

/** Excel 导出请求体(POST .../exports/excel)。 */
export interface ExportExcelRequestDto {
  evidence_package_id: number
  assessment_id: number
  lang: 'zh' | 'en'
}

/** Excel 导出响应 DTO(与 wrapper_keys.export_excel_endpoint 一致)。 */
export interface ExportExcelEnvelopeDto {
  token: string
  file_name: string
  size_bytes: number
  expires_at_seconds: number
}

/** 项目包导出响应 DTO(与 wrapper_keys.export_package_endpoint 一致)。 */
export interface ExportPackageEnvelopeDto {
  token: string
  file_name: string
  expires_at: string
  manifest: unknown
  media_type: string
  object_id: number
  oid: string
  size_bytes: number
}
