/**
 * 财务配置 feature 前后端契约(与后端 iesplan/api/config_revisions.py 一致)。
 *
 * 端点:
 *   GET  /api/finance-profiles/              已登记列表 → {items, count}
 *   POST /api/finance-profiles/              登记 → {finance_profile, row}
 *   GET  /api/finance-profiles/{id}          按 id 读取 → {finance_profile, row}
 *   GET  /api/projects/{pid}/finance-profile 当前引用 → {finance_profile, row}
 *   PUT  /api/projects/{pid}/finance-profile 引用/切换 → {finance_profile, overrides_revision, effective_finance_config, revision}
 *   GET  /api/projects/{pid}/finance-overrides 当前覆盖 → {finance_overrides, revision}(无覆盖 404)
 *   PUT  /api/projects/{pid}/finance-overrides 保存覆盖 → 同上形状(失败原子)
 *   DELETE /api/projects/{pid}/finance-overrides 清空覆盖 → 同上形状
 *   GET  /api/projects/{pid}/effective-finance 当前 Effective → {effective_finance_config, revision}(未生成 404)
 *
 * 财务文档均为完整字典形态(正式格式正文), 前端不拆税种字段、不设
 * 税率默认值, 只做全文 JSON 编辑与保存。
 */

/** 注册表行 DTO(后端 profile_row_dict 输出)。 */
export interface FinanceProfileRowDto {
  id: number
  profile_id: string
  region: string
  object_id: number | null
}

/** 已登记列表响应 DTO。 */
export interface FinanceProfileListDto {
  items: FinanceProfileRowDto[]
  count: number
}

/** Profile 全文信封 DTO(登记/读取/引用共用 finance_profile 键)。 */
export interface FinanceProfileEnvelopeDto {
  finance_profile: Record<string, unknown>
  row: Record<string, unknown>
}

/** 项目引用/切换响应 DTO。 */
export interface SetProjectProfileDto {
  finance_profile: Record<string, unknown>
  overrides_revision: number
  effective_finance_config: Record<string, unknown>
  revision: number
}

/** Overrides 全文信封 DTO(读取)。 */
export interface FinanceOverridesEnvelopeDto {
  finance_overrides: Record<string, unknown>
  revision: number
}

/** Overrides 保存/清空响应 DTO。
 *
 * 注意后端在同一对象放了 overrides 与 effective 两个 revision(同名键,
 * JSON 只保留后者): 成功后以 GET /finance-overrides 重新读取覆盖 revision,
 * 不猜测。
 */
export interface SaveOverridesEnvelopeDto {
  finance_overrides: Record<string, unknown>
  revision: number
  effective_finance_config: Record<string, unknown>
}

/** Effective 读取响应 DTO。 */
export interface EffectiveFinanceEnvelopeDto {
  effective_finance_config: Record<string, unknown>
  revision: number
}
