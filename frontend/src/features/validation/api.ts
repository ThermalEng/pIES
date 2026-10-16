/**
 * 校验 feature 请求(传输层实现, 供 api/client 聚合器委托)。
 *
 * 成功正文按 contracts 具名键直接消费后交 mappers 映射; 畸形体自然抛错
 * 可见。假设内容必须由调用方显式提供(用户确认的评价输入); 本层不编默认
 * 值、不从计算配置派生(0.12-A1 起计算配置不再承载经济参数)。
 */

import type { Diagnostic, ValidationResult } from '../../types'
import { request } from '../../shared/http'
import type { BaselineConfirmRequestDto, BaselineConfirmResponseDto, ValidationRunResponseDto } from './contracts'
import { baselineConfirmFromServer, validationResultFromServer } from './mappers'

/** 运行完整预检(报告含 status/blocks_submit/diagnostics)。 */
export async function runValidation(projectId: number): Promise<ValidationResult> {
  const body = await request<ValidationRunResponseDto>(`/projects/${projectId}/validation/run`, { method: 'POST' })
  return validationResultFromServer(body)
}

/** 记录财务基准确认(确认人/时间由服务端记, 追加式审计)。 */
export async function confirmBaseline(
  projectId: number,
  assumptions: Record<string, unknown>,
): Promise<{ confirmed: boolean; diagnostics: Diagnostic[] }> {
  const requestBody: BaselineConfirmRequestDto = { assumptions }
  const body = await request<BaselineConfirmResponseDto>(
    `/projects/${projectId}/validation/baseline-confirm`,
    { method: 'POST', body: requestBody },
  )
  return baselineConfirmFromServer(body)
}
