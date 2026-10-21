/** 财务 DTO 的薄映射；文档全文原样透传，不拆税种字段。 */
import type { FinanceProfileRowDto } from './contracts'

export interface FinanceProfileOption {
  profileId: string
  region: string
}

export function mapProfileRowsToOptions(
  rows: FinanceProfileRowDto[],
): FinanceProfileOption[] {
  return rows.map((row) => ({ profileId: row.profile_id, region: row.region }))
}
