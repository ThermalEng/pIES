/**
 * 计算配置 feature 请求(传输层实现, 供 api/client 聚合器委托)。
 *
 * 成功正文按 contracts 具名键直接消费后交 mappers 映射; 畸形体自然抛错
 * 可见。保存请求体 {calculation_config, expected_revision} 与后端
 * CalculationConfigSaveRequest 同形(首次保存 expected_revision 为 null)。
 */

import type { CalcConfig, CalcConfigInput } from '../../types'
import { request } from '../../shared/http'
import type { ConfigEnvelopeDto } from './contracts'
import { configFromServer } from './mappers'

/** 读取最新计算配置 revision(未保存过 404, 由调用方呈现失败)。 */
export async function getCalcConfig(projectId: number): Promise<CalcConfig> {
  const body = await request<ConfigEnvelopeDto>(`/projects/${projectId}/config`)
  return configFromServer(body, projectId)
}

/** 保存计算配置(全文替换, 自有链乐观锁; 成功返回新 revision)。 */
export async function saveCalcConfig(projectId: number, input: CalcConfigInput): Promise<CalcConfig> {
  const body = await request<ConfigEnvelopeDto>(`/projects/${projectId}/config`, {
    method: 'PUT',
    body: { calculation_config: input.calculation_config, expected_revision: input.expected_revision },
  })
  return configFromServer(body, projectId)
}
