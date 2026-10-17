/**
 * 计算配置 feature 纯 mapper: 后端配置信封 → 前端 CalcConfig。
 *
 * 纯函数, 不访问网络、缓存或 React 状态, 不导入 HTTP 层。
 * 可信内部交接直接消费具名键: 后端在用户输入入口已唯一校验全文, 此处
 * 不补齐缺项(不伪造 ''/[]/null/0, 尤其 time_limit_seconds/options),
 * 不重复业务校验、不设信封守卫; 缺席即自然抛错可见。缺项原样透出,
 * 由编辑页显式呈现为空态。
 */

import type { CalcConfig } from '../../types'
import type { ConfigEnvelopeDto, DeploymentDefaultsDto } from './contracts'

/** 后端配置信封 {calculation_config, revision} → 前端 CalcConfig。 */
export function configFromServer(body: ConfigEnvelopeDto, projectId: number): CalcConfig {
  return {
    project_id: projectId,
    calculation_config: body.calculation_config,
    revision: body.revision,
  }
}

/** 部署公开初值 → DeploymentDefaultsDto。 */
export function deploymentDefaultsFromServer(body: DeploymentDefaultsDto): DeploymentDefaultsDto {
  return {
    calculation_defaults: {
      time_limit_seconds: body.calculation_defaults.time_limit_seconds,
    },
  }
}
