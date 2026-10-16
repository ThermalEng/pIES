/**
 * 导出 feature API 客户端。
 *
 * 规范:
 * - 直接消费 ExportExcelEnvelopeDto / ExportPackageEnvelopeDto, 无兼容猜测;
 * - 接受应用层已选择的证据和评估 ID, 不编排其他 feature;
 * - downloadSessions 保留为本地下载会话句柄登记(非业务缓存)。
 */

import { ApiError } from '../../types'
import { request, requestBlob } from '../../shared/http'
import type {
  ExportExcelEnvelopeDto,
  ExportExcelRequestDto,
  ExportPackageEnvelopeDto,
} from './contracts'
import type { DownloadSession, PackageExportInput } from './model'

let downloadSeq = 0
const downloadSessions = new Map<number, DownloadSession>()

/**
 * 触发 Excel 报告导出, 登记下载会话并返回本地会话 id。
 */
export async function exportExcel(projectId: number, body: ExportExcelRequestDto): Promise<number> {
  const res = await request<ExportExcelEnvelopeDto>(`/projects/${projectId}/exports/excel`, {
    method: 'POST',
    body,
  })
  downloadSeq += 1
  const id = downloadSeq
  downloadSessions.set(id, {
    projectId,
    kind: 'excel',
    token: res.token,
    filename: res.file_name,
  })
  return id
}

/**
 * 触发项目包导出, 登记下载会话并返回本地会话 id。
 */
export async function exportPackage(input: PackageExportInput): Promise<number> {
  const projectId = input.project_id
  const res = await request<ExportPackageEnvelopeDto>(`/projects/${projectId}/exports/package`, {
    method: 'POST',
  })
  downloadSeq += 1
  const id = downloadSeq
  downloadSessions.set(id, {
    projectId,
    kind: 'package',
    token: res.token,
    filename: res.file_name,
  })
  return id
}

/**
 * 凭本地下载会话 id 获取二进制 Blob 与文件名。
 */
export async function downloadExport(
  downloadSessionId: number,
): Promise<{ blob: Blob; filename: string }> {
  const session = downloadSessions.get(downloadSessionId)
  if (!session) throw new ApiError(404, null, 'ies.error.route_not_found')
  const blob = await requestBlob(`/projects/${session.projectId}/exports/${session.kind}/download`, {
    query: { token: session.token },
  })
  return { blob, filename: session.filename }
}
