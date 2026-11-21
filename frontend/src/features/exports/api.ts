/** 导出与已认证的对象下载。 */

import { request, requestBlob } from '../../shared/http'
import type { PackageExportInput } from '../../types'
import type { ExportArtifactDto, ExportExcelRequestDto } from './contracts'

async function exportFile(
  projectId: number,
  kind: 'excel' | 'package',
  body?: ExportExcelRequestDto,
): Promise<{ blob: Blob; filename: string }> {
  const path = `/projects/${projectId}/exports/${kind}`
  const artifact = await request<ExportArtifactDto>(path, { method: 'POST', body })
  const blob = await requestBlob(`${path}/${artifact.object_id}/download`)
  return { blob, filename: artifact.file_name }
}

export function exportExcel(projectId: number, body: ExportExcelRequestDto) {
  return exportFile(projectId, 'excel', body)
}

export function exportPackage(input: PackageExportInput) {
  return exportFile(input.project_id, 'package')
}
