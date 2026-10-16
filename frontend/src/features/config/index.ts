/**
 * 计算配置 feature 公开入口(页面组合真正需要的请求与纯映射)。
 */

export { getCalcConfig, saveCalcConfig } from './api'
export { configFromServer } from './mappers'
export type { ConfigEnvelopeDto } from './contracts'
