/**
 * 项目包导入 feature 请求层(路径拼接 + client 调用; 信封严格解析在 api/client)。
 *
 * 本层不组织跨端点工作流(上传后展示提案、确认后刷新列表的编排在 hook/page 完成)。
 * 不在浏览器解压、hash 或重跑包校验; 确认阶段不恢复重复验证。
 */

import { api } from '../../api/client'
import type { ImportConfirmResult, ImportProposal } from '../../types'

/** 上传项目包创建导入提案(单文件; multipart 由 client 层组装)。 */
export async function proposeImport(file: File): Promise<ImportProposal> {
  return api.projects.proposeImport(file)
}

/** 确认导入提案(同一提案可重试; 只有成功返回才表示已导入)。 */
export async function confirmImport(proposalId: number): Promise<ImportConfirmResult> {
  return api.projects.confirmImport(proposalId)
}
