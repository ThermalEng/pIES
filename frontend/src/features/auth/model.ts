/**
 * 认证 feature 前端领域模型。
 */

import type { User, UserStatus } from '../../types'

export type { User, UserStatus }

/** 登录与接管结果(与后端 AuthResponse 字段对齐)。 */
export interface LoginResult {
  token: string
  token_type: string
  user: {
    id: number
    username: string
    display_name: string
    role: string
    status: string
    force_password_change: boolean
    credential_version: number
    last_login_at: string | null
  }
  needs_takeover_confirm: boolean
}

/** 登录页公开设置。 */
export interface PublicAuthSettings {
  registration_enabled: boolean
  sso_enabled: boolean
  sso_provider_name: string
}
