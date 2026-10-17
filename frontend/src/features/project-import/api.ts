/**
 * 项目包导入 feature 请求层(传输层实现, 经 shared/http 直调)。
 *
 * 成功正文按 contracts 具名键直接消费后交 mappers 映射; 畸形体自然抛错可见。
 * 本层不组织跨端点工作流(上传后展示提案、确认后刷新列表的编排在 hook/page 完成)。
 * 不在浏览器解压、hash 或重跑包校验; 确认阶段不恢复重复验证。
 */

import type { ImportConfirmResult, ImportProposal } from '../../types'
import { request } from '../../shared/http'
import type { ImportConfirmResponseDto, ImportProposalResponseDto } from './contracts'
import { confirmFromServer, proposalFromServer } from './mappers'

/** 上传项目包创建导入提案(单文件 multipart; 超时不设限由服务端控制)。 */
export async function proposeImport(file: File): Promise<ImportProposal> {
  const formData = new FormData()
  formData.append('file', file)
  const body = await request<ImportProposalResponseDto>('/projects/import', {
    method: 'POST',
    formData,
    timeoutMs: 0,
  })
  return proposalFromServer(body)
}

/** 确认导入提案(同一提案可重试; 只有成功返回才表示已导入)。 */
export async function confirmImport(proposalId: number): Promise<ImportConfirmResult> {
  const body = await request<ImportConfirmResponseDto>(`/projects/import/${proposalId}/confirm`, {
    method: 'POST',
  })
  return confirmFromServer(body)
}
