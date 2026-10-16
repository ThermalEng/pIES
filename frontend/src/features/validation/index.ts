/**
 * 校验 feature 公开入口(页面组合真正需要的请求与纯映射)。
 */

export { confirmBaseline, runValidation } from './api'
export { baselineConfirmFromServer, validationResultFromServer } from './mappers'
export type {
  BaselineConfirmRequestDto,
  BaselineConfirmResponseDto,
  ValidationReportDto,
  ValidationRunResponseDto,
} from './contracts'
