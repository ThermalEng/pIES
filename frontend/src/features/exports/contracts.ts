/** Excel 导出请求。 */
export interface ExportExcelRequestDto {
  evidence_package_id: number
  assessment_id: number
  lang: 'zh' | 'en'
}

/** 生成成功后可通过当前项目下载的对象。 */
export interface ExportArtifactDto {
  object_id: number
  file_name: string
}
