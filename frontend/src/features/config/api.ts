/**
 * 计算配置 feature 请求(传输层实现)。
 *
 * 成功正文按 contracts 具名键直接消费后交 mappers 映射; 畸形体自然抛错
 * 可见。保存请求体 {calculation_config, expected_revision} 与后端
 * CalculationConfigSaveRequest 同形(首次保存 expected_revision 为 null)。
 * 统一部署公开初值是计算配置空白新建的初值来源, 同属本家族。
 */

import type { CalcConfig, CalcConfigInput } from '../../types'
import { request } from '../../shared/http'
import type { ConfigEnvelopeDto, DeploymentDefaultsDto } from './contracts'
import { configFromServer, deploymentDefaultsFromServer } from './mappers'

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

/** 统一部署公开初值(后端 {calculation_defaults: {time_limit_seconds}})。 */
export async function getDeploymentDefaults(): Promise<DeploymentDefaultsDto> {
  const body = await request<DeploymentDefaultsDto>('/deployment/defaults')
  return deploymentDefaultsFromServer(body)
}
