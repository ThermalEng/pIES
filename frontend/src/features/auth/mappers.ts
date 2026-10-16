/**
 * 认证 feature 纯映射函数(DTO → FrontendModel)。
 *
 * 规范:
 * - 纯函数, 不访问网络、会话存储或 React 状态;
 * - 不做多余猜测或虚构字段; 后端权威数据直接类型化消费。
 */

import type { AuthResponseDto, PublicSettingsDto, UserOutDto } from './contracts'
import type { LoginResult, PublicAuthSettings, User } from './model'

/** 后端 UserOut DTO → 前端 User 模型。 */
export function mapUserDtoToModel(dto: UserOutDto): User {
  return {
    id: dto.id,
    username: dto.username,
    display_name: dto.display_name,
    email: null,
    role: dto.role,
    status: dto.status as User['status'],
    locale: 'zh-CN',
    timezone: 'UTC',
    fixed_utc_offset_minutes: 480,
    credential_version: dto.credential_version,
    is_system: false,
    created_at: '',
    updated_at: null,
    last_login_at: dto.last_login_at ?? null,
  }
}

/** 后端 AuthResponse DTO → 前端 LoginResult 模型。 */
export function mapAuthResponseDtoToModel(dto: AuthResponseDto): LoginResult {
  return {
    token: dto.token,
    token_type: dto.token_type,
    user: {
      id: dto.user.id,
      username: dto.user.username,
      display_name: dto.user.display_name,
      role: dto.user.role,
      status: dto.user.status,
      force_password_change: Boolean(dto.user.force_password_change),
      credential_version: dto.user.credential_version,
      last_login_at: dto.user.last_login_at ?? null,
    },
    needs_takeover_confirm: Boolean(dto.needs_takeover_confirm),
  }
}

/** 后端 PublicSettings DTO → 前端 PublicAuthSettings 模型。 */
export function mapPublicSettingsDtoToModel(dto: PublicSettingsDto): PublicAuthSettings {
  return {
    registration_enabled: Boolean(dto.registration_enabled),
    sso_enabled: Boolean(dto.sso_enabled),
    sso_provider_name: dto.sso_provider_name ?? '',
  }
}
