/**
 * 认证 feature 前端领域模型。
 */

import type { User, UserStatus } from '../../types'

export type { User, UserStatus }

/** 登录与接管结果(与后端 AuthResponse 对齐)。 */
export interface LoginResult {
  token: string
  token_type: string
  user: User
  needs_takeover_confirm: boolean
}

/** 登录页公开设置。 */
export interface PublicAuthSettings {
  registration_enabled: boolean
  sso_enabled: boolean
  sso_provider_name: string
}
