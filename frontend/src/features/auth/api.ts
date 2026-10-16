/**
 * 认证 feature API 客户端(直接消费唯一后端 DTO 契约)。
 *
 * 依赖规范:
 * - 仅导入 shared/http.ts 通用请求与会话操作, 禁止反向依赖 api/client.ts;
 * - 不做兼容形状猜测, 移除旧 normalizeUser 中的虚构默认值。
 */

import { clearSession, request, setSession } from '../../shared/http'
import type {
  AuthResponseDto,
  ChangePasswordRequestDto,
  ChangePasswordResponseDto,
  LoginRequestDto,
  LogoutResponseDto,
  PublicSettingsDto,
  RegisterRequestDto,
  UserOutDto,
} from './contracts'
import {
  mapAuthResponseDtoToModel,
  mapPublicSettingsDtoToModel,
  mapUserDtoToModel,
} from './mappers'
import type { LoginResult, PublicAuthSettings, User } from './model'

/**
 * 用户登录: 校验密码并签发窗口凭证。
 */
export async function login(input: LoginRequestDto): Promise<LoginResult> {
  const res = await request<AuthResponseDto>('/auth/login', {
    method: 'POST',
    body: input,
  })
  setSession()
  return mapAuthResponseDtoToModel(res)
}

/**
 * 撤销当前窗口会话并清空本地会话标记(仅在成功时清除会话)。
 */
export async function logout(): Promise<void> {
  await request<LogoutResponseDto>('/auth/logout', { method: 'POST' })
  clearSession()
}

/**
 * 修改密码(首登强制改密或用户主动修改)。
 */
export async function changePassword(input: ChangePasswordRequestDto): Promise<void> {
  await request<ChangePasswordResponseDto>('/auth/change-password', {
    method: 'POST',
    body: input,
  })
}

/**
 * 确认接管: 将当前待接管会话转为 active。
 */
export async function confirmTakeover(): Promise<LoginResult> {
  const res = await request<AuthResponseDto>('/auth/confirm-takeover', {
    method: 'POST',
  })
  setSession()
  return mapAuthResponseDtoToModel(res)
}

/**
 * 自助注册(注册开关开启时可用)。
 */
export async function register(input: RegisterRequestDto): Promise<User> {
  const res = await request<UserOutDto>('/auth/register', {
    method: 'POST',
    body: input,
  })
  return mapUserDtoToModel(res)
}

/**
 * 获取当前登录用户信息(页面刷新后恢复登录态)。
 */
export async function getMe(): Promise<User> {
  const res = await request<UserOutDto>('/auth/me')
  return mapUserDtoToModel(res)
}

/**
 * 获取登录页公开设置(无需认证)。
 */
export async function getPublicSettings(): Promise<PublicAuthSettings> {
  const res = await request<PublicSettingsDto>('/auth/public-settings')
  return mapPublicSettingsDtoToModel(res)
}
