/**
 * 导出 feature 前端模型。
 */

import type { ExcelExportInput, PackageExportInput } from '../../types'

export type { ExcelExportInput, PackageExportInput }

/** 临时下载会话登记(仅用于维持 token/filename 句柄, 非业务数据缓存)。 */
export interface DownloadSession {
  projectId: number
  kind: 'excel' | 'package'
  token: string
  filename: string
}
