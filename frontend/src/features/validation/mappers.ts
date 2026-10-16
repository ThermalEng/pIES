/**
 * 校验 feature 纯 mapper: 后端校验报告/确认 → 前端 ValidationResult。
 *
 * 纯函数, 不访问网络、缓存或 React 状态, 不导入 HTTP 层。
 * 报告诊断条目与前端 Diagnostic 同形(后端 core/diagnostics.Diagnostic
 * 序列化), 直接消费数组, 不做 {items}/{key}/裸数组并集猜测;
 * blocks_submit 缺席即视为不阻断(valid 口径与旧行为一致)。
 */

import type { ValidationResult } from '../../types'
import type { BaselineConfirmResponseDto, ValidationRunResponseDto } from './contracts'

/** 完整预检响应 {report, stored} → 前端 ValidationResult。 */
export function validationResultFromServer(body: ValidationRunResponseDto): ValidationResult {
  const report = body.report
  return {
    valid: report.blocks_submit !== true,
    diagnostics: report.diagnostics,
  }
}

/** 基准确认响应 → {confirmed, diagnostics}(确认人/时间暂无消费者, 不伪装诊断)。 */
export function baselineConfirmFromServer(body: BaselineConfirmResponseDto): {
  confirmed: boolean
  diagnostics: ValidationResult['diagnostics']
} {
  return { confirmed: body.confirmed === true, diagnostics: [] }
}
