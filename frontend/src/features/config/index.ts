/**
 * 计算配置 feature 公开入口(页面组合真正需要的请求与纯映射)。
 */

export { getCalcConfig, getDeploymentDefaults, saveCalcConfig, getPlanningConfig, savePlanningConfig } from './api'
export { configFromServer, deploymentDefaultsFromServer } from './mappers'
export type { ConfigEnvelopeDto, DeploymentDefaultsDto, PlanningConfigBody, PlanningConfigEnvelopeDto, PlanningConstraintDto, PlanningVariableDto } from './contracts'
