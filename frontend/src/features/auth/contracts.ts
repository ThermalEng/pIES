/**
 * 认证 feature 前后端契约(与后端 HTTP JSON 字段一一对应, snake_case)。
 *
 * 端点(与后端 iesplan/api/auth.py 一致):
 *   POST /api/auth/login            登录 → AuthResponse
 *   POST /api/auth/logout           登出 → {ok: true}
 *   POST /api/auth/change-password  修改密码 → {ok: true, message_key: string}
 *   POST /api/auth/refresh          会话续期 → {ok: true, expires_at: string}
 *   GET  /api/auth/me               当前用户 → UserOut
 *   POST /api/auth/confirm-takeover 确认接管 → AuthResponse
 *   POST /api/auth/register         自助注册 → UserOut
 *   GET  /api/auth/public-settings  公开设置(无需认证) → PublicSettings
 */

/** 后端 UserOut DTO(普通用户响应语义, 不含任何敏感/虚构字段)。 */
export interface UserOutDto {
  id: number
  username: string
  display_name: string
  role: string
  status: string
  force_password_change: boolean
  credential_version: number
  last_login_at: string | null
}

/** 登录请求体(POST /api/auth/login)。 */
export interface LoginRequestDto {
  username: string
  password: string
  device?: string
}

/** 登录/接管响应 DTO(AuthResponse)。 */
export interface AuthResponseDto {
  token: string
  token_type: string
  user: UserOutDto
  needs_takeover_confirm: boolean
}

/** 登出响应 DTO(POST /api/auth/logout)。 */
export interface LogoutResponseDto {
  ok: boolean
}

/** 修改密码请求体(POST /api/auth/change-password)。 */
export interface ChangePasswordRequestDto {
  old_password: string
  new_password: string
}

/** 修改密码响应 DTO。 */
export interface ChangePasswordResponseDto {
  ok: boolean
  message_key: string
}

/** 确认接管响应 DTO(AuthResponse)。 */
export type ConfirmTakeoverResponseDto = AuthResponseDto

/** 自助注册请求体(POST /api/auth/register)。 */
export interface RegisterRequestDto {
  username: string
  password: string
  display_name?: string | null
  email?: string | null
}

/** 公开设置响应 DTO(GET /api/auth/public-settings)。 */
export interface PublicSettingsDto {
  registration_enabled: boolean
  sso_enabled: boolean
  sso_provider_name: string
}
