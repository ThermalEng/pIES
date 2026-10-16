/**
 * 校验 feature 前后端契约(与后端 application/validations/cases.py 同形)。
 *
 * 端点(与后端 /api 路由一致):
 *   POST /api/projects/{pid}/validation/run               完整预检 {report, stored}
 *   POST /api/projects/{pid}/validation/baseline-confirm  基准确认 {confirmed, confirmed_by, confirmed_at}
 *   GET  /api/projects/{pid}/validation                   最近报告 {report, stored}(本家族暂无消费者, 不在此封装)
 *
 * 报告诊断条目即 core/diagnostics.Diagnostic.to_dict 形态, 与前端
 * Diagnostic 同形, 直接消费(见 mappers)。财务基准确认入口已随旧形态
 * 经济参数移除而暂停 UI(0.12-A1), 后端接口保留。
 *
 * 依赖方向: features/validation/contracts → ../../types(共享领域类型);
 * mappers/api 只依赖 contracts/types/shared, 不导入 api/client 聚合器。
 */

import type { Diagnostic } from '../../types'

/** 校验报告体(与 ValidationReport.to_dict 同形; 只消费三键)。 */
export interface ValidationReportDto {
  status: string
  blocks_submit: boolean
  diagnostics: Diagnostic[]
}

/** 完整预检响应: 精确 {report, stored} 信封。 */
export interface ValidationRunResponseDto {
  report: ValidationReportDto
  stored: boolean
}

/** 基准确认响应(与 confirm_baseline 返回同形; 确认人/时间暂无消费者, 声明备查)。 */
export interface BaselineConfirmResponseDto {
  confirmed: boolean
  confirmed_by: number | null
  confirmed_at: string | null
}

/** 基准确认请求体(假设内容由调用方显式提供, 不编默认值、不从配置派生)。 */
export interface BaselineConfirmRequestDto {
  assumptions: Record<string, unknown>
}
