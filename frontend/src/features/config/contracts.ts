/**
 * 计算配置 feature 前后端契约(与后端 application/configuration/calc_config.py 同形)。
 *
 * 端点(与后端 /api 路由一致):
 *   GET /api/projects/{pid}/config   最新计算配置 {calculation_config, revision}(未保存 404)
 *   PUT /api/projects/{pid}/config   保存 {calculation_config, expected_revision}(全文唯一校验)
 *
 * 配置正文本体为 12 字段 CalculationConfig(见 CalculationConfigBody, 与
 * formats/calculation-config.md 同形); 本文件只声明传输信封。
 *
 * 依赖方向: features/config/contracts → ../../types(共享领域类型);
 * mappers/api 只依赖 contracts/types/shared, 不导入 api/client 聚合器。
 */

import type { CalculationConfigBody } from '../../types'

/** 计算配置信封(读写共用; revision 为自有链乐观锁, 首次保存为 null 仅见于请求)。 */
export interface ConfigEnvelopeDto {
  calculation_config: CalculationConfigBody
  revision: number
}

/** 统一部署公开初值(与后端 /deployment/defaults 同形)。 */
export interface DeploymentDefaultsDto {
  calculation_defaults: { time_limit_seconds: number }
}


/** PlanningConfig public envelope (GET/PUT /api/projects/{pid}/planning-config).
 * Decimal bounds are transmitted as exact strings, never JS floating-point numbers.
 * A saved planning revision is valid only against its effective finance snapshot.
 */
export interface PlanningVariableDto {
  device_ref: string
  parameter: string
  lower_bound: string | null
  upper_bound: string | null
  unit: string | null
}

export interface PlanningConstraintDto {
  type: string
  expression: string
  enabled: boolean
}

export interface PlanningConfigBody {
  objective: { sense: 'minimize' | 'maximize'; expression: string }
  variables: Record<string, PlanningVariableDto>
  constraints: Record<string, PlanningConstraintDto>
}

export interface PlanningConfigEnvelopeDto {
  planning_config: PlanningConfigBody
  revision: number
}
