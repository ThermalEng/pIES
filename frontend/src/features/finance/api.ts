/**
 * 财务配置 feature 请求(传输层实现)。
 *
 * 成功正文按 contracts 具名键直接消费; 畸形体自然抛错可见。
 * 读取类 404 由调用方区分(无引用/无覆盖/未生成是正常状态, 不是失败)。
 */

import { request } from '../../shared/http'
import type {
  EffectiveFinanceEnvelopeDto,
  FinanceOverridesEnvelopeDto,
  FinanceProfileEnvelopeDto,
  FinanceProfileListDto,
  SaveOverridesEnvelopeDto,
  SetProjectProfileDto,
} from './contracts'

/** 已登记 Profile 列表。 */
export async function listFinanceProfiles(): Promise<FinanceProfileListDto> {
  return request<FinanceProfileListDto>('/finance-profiles')
}

/** 登记地区 Profile(全文正式格式, 按 id 复用)。 */
export async function registerFinanceProfile(
  doc: Record<string, unknown>,
): Promise<FinanceProfileEnvelopeDto> {
  return request<FinanceProfileEnvelopeDto>('/finance-profiles', {
    method: 'POST',
    body: { finance_profile: doc },
  })
}

/** 项目当前引用的 Profile(未引用 404)。 */
export async function getProjectFinanceProfile(
  projectId: number,
): Promise<FinanceProfileEnvelopeDto> {
  return request<FinanceProfileEnvelopeDto>(`/projects/${projectId}/finance-profile`)
}

/** 项目引用/切换已登记 Profile(原子生成空覆盖 Effective)。 */
export async function setProjectFinanceProfile(
  projectId: number,
  profileId: string,
): Promise<SetProjectProfileDto> {
  return request<SetProjectProfileDto>(`/projects/${projectId}/finance-profile`, {
    method: 'PUT',
    body: { profile_ref: { id: profileId } },
  })
}

/** 项目当前 Overrides(无覆盖 404, 由调用方呈现“无覆盖”而非失败)。 */
export async function getFinanceOverrides(
  projectId: number,
): Promise<FinanceOverridesEnvelopeDto> {
  return request<FinanceOverridesEnvelopeDto>(`/projects/${projectId}/finance-overrides`)
}

/** 保存 Overrides(全文替换, 自有链乐观锁; 成功返回新 revision)。 */
export async function saveFinanceOverrides(
  projectId: number,
  doc: Record<string, unknown>,
  expectedRevision: number | null,
): Promise<SaveOverridesEnvelopeDto> {
  return request<SaveOverridesEnvelopeDto>(`/projects/${projectId}/finance-overrides`, {
    method: 'PUT',
    body: { finance_overrides: doc, expected_revision: expectedRevision },
  })
}

/** 清空 Overrides(回退到 Profile 裸合并; 成功返回新 revision)。 */
export async function deleteFinanceOverrides(
  projectId: number,
  expectedRevision: number | null,
): Promise<SaveOverridesEnvelopeDto> {
  return request<SaveOverridesEnvelopeDto>(`/projects/${projectId}/finance-overrides`, {
    method: 'DELETE',
    body: { expected_revision: expectedRevision },
  })
}

/** 项目当前 Effective(未生成 404, 由调用方呈现)。 */
export async function getEffectiveFinance(
  projectId: number,
): Promise<EffectiveFinanceEnvelopeDto> {
  return request<EffectiveFinanceEnvelopeDto>(`/projects/${projectId}/effective-finance`)
}
