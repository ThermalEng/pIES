/**
 * 计算配置 feature 纯 mapper: 后端配置信封 → 前端 CalcConfig。
 *
 * 纯函数, 不访问网络、缓存或 React 状态, 不导入 HTTP 层。
 * 后端在用户输入入口已唯一校验全文, 此处不补齐缺项(不伪造 ''/[]/null/0,
 * 尤其 time_limit_seconds/options), 也不重复第二次业务校验; 信封形态不符
 * 直接抛错可见。缺项原样透出, 由编辑页显式呈现为空态。
 */

import type { CalcConfig } from '../../types'
import type { ConfigEnvelopeDto } from './contracts'

/** 后端配置信封 {calculation_config, revision} → 前端 CalcConfig。 */
export function configFromServer(body: ConfigEnvelopeDto, projectId: number): CalcConfig {
  if (typeof body.revision !== 'number' || !body.calculation_config || typeof body.calculation_config !== 'object') {
    throw new Error('invalid calculation config envelope')
  }
  return {
    project_id: projectId,
    calculation_config: body.calculation_config,
    revision: body.revision,
  }
}
