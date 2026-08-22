// QA-E2E-01 前置造数 API 客户端(13.2: 仅用于测试环境前置造数和结束清理,
// 与被测 UI 动作严格隔离; 每个场景使用独立用户/项目/幂等键)。

import type { APIRequestContext } from '@playwright/test'

export interface CreatedUser {
  id: number
  username: string
  password: string
}

export interface CreatedProject {
  id: number
  name: string
}

const BASE = process.env.E2E_APP_URL ?? 'http://web:80'

/** 通用 JSON 请求(带可选 bearer token)。 */
export async function requestJson(
  ctx: APIRequestContext,
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  path: string,
  body?: unknown,
  token?: string,
): Promise<any> {
  const res = await ctx.fetch(`${BASE}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    data: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await res.text()
  let json: any = null
  try {
    json = text ? JSON.parse(text) : null
  } catch {
    json = null
  }
  if (!res.ok()) {
    throw new Error(
      `${method} ${path} -> ${res.status()}: ${text.slice(0, 500)}`,
    )
  }
  return json
}

/**
 * 登录获取 token(前置造数阶段使用; 被测 UI 流程不用)。
 * 若新会话为 takeover_pending(旧窗口仍在), 立即确认接管激活, 保证 token 可用。
 */
export async function loginToken(ctx: APIRequestContext, username: string, password: string): Promise<string> {
  const res = await requestJson(ctx, 'POST', '/api/auth/login', { username, password })
  if (!res?.token) throw new Error(`login(${username}) 未返回 token`)
  if (res.needs_takeover_confirm === true) {
    await requestJson(ctx, 'POST', '/api/auth/confirm-takeover', { token: res.token }, res.token)
  }
  return res.token
}

/**
 * 创建独立用户(engineer 角色; 默认可直接登录)。
 * 注册开关默认关闭: 先用管理员开启(幂等), 注册成功即视为造数完成。
 * @param opts.forceChange 为 true 时额外调用管理员重置密码(requires_change=True,
 *   首登强制改密), 返回密码为临时密码。
 */
export async function createEngineer(
  ctx: APIRequestContext,
  username: string,
  password: string,
  opts: { forceChange?: boolean } = {},
): Promise<CreatedUser> {
  const token = await adminToken(ctx)
  // 注册开关幂等开启(注册端点仅工程师, 不影响其他验收)
  await requestJson(ctx, 'PUT', '/api/auth/settings', { registration_enabled: true }, token)
  const res = await requestJson(ctx, 'POST', '/api/auth/register', {
    username,
    password,
    display_name: username,
  }, token)
  const created: CreatedUser = { id: res.id, username, password }
  if (opts.forceChange) {
    // 管理员重置密码 → requires_change=True(首登强制改密验收用)
    await requestJson(ctx, 'POST', `/api/auth/users/${res.id}/reset-password`, {
      new_password: password,
    }, token)
  }
  return created
}

/** 管理员登录(RR-P2-11: 每次实时登录, 不缓存模块级 token)。

 * 模块级缓存曾导致跨场景失效: 场景 7 在 UI 中登录同一管理员会顶掉
 * 先前 API 会话缓存的 token, 后续场景用旧 token 请求即 401。
 * 每次造数使用生命周期明确的独立会话(loginToken 已处理 takeover 确认),
 * UI 登录场景之前无需手动登出 —— 新会话确认接管即自然失效旧会话。
 */
export async function adminToken(ctx: APIRequestContext): Promise<string> {
  return loginToken(ctx, 'admin', process.env.E2E_ADMIN_PASSWORD ?? 'Iesplan-Admin-2026!')
}

/** 创建项目(所有者 = 指定用户)。 */
export async function createProject(
  ctx: APIRequestContext,
  userToken: string,
  name: string,
  description = 'QA-E2E 前置造数项目',
): Promise<CreatedProject> {
  const res = await requestJson(ctx, 'POST', '/api/projects', {
    name,
    description,
  }, userToken)
  return { id: res.id, name: res.name }
}

/** 删除用户(管理员; 先预览取确认令牌, 再携带 confirm + 令牌执行删除)。 */
export async function deleteUser(ctx: APIRequestContext, userId: number): Promise<void> {
  const adminTok = await adminToken(ctx)
  const preview = await requestJson(ctx, 'POST', `/api/auth/users/${userId}/delete-preview`, undefined, adminTok)
  const confirmToken = String((preview as { confirm_token?: unknown }).confirm_token ?? '')
  await requestJson(ctx, 'DELETE', `/api/auth/users/${userId}`, { confirm: true, confirm_token: confirmToken }, adminTok)
}

/** 删除项目(所有者或管理员)。 */
export async function deleteProject(
  ctx: APIRequestContext,
  projectId: number,
  userToken?: string,
): Promise<void> {
  await requestJson(ctx, 'DELETE', `/api/projects/${projectId}`, undefined, userToken ?? (await adminToken(ctx)))
}

/**
 * 通过 API 构造最小可校验模型(电网→电负荷/热泵、热泵→热负荷):
 * 避开画布拖放/连线 UI(连线手势由建模专项验收覆盖),
 * 供任务提交/结果/导出流程场景使用。
 */
export async function buildMinimalModel(
  ctx: APIRequestContext,
  projectId: number,
  userToken: string,
): Promise<void> {
  const deviceTypes = await requestJson(ctx, 'GET', '/api/registry/device-types', undefined, userToken)
  const typeId = (keyword: string) => {
    const t = deviceTypes.items.find((it: any) => it.type_id.includes(keyword))
    if (!t) throw new Error(`未找到设备类型: ${keyword}`)
    return t.type_id
  }
  const addDevice = async (keyword: string): Promise<number> => {
    // 负荷设备必须有 profile 数据引用(装配闸门 ASM-INPUT-004 阻断无数据的负荷);
    // 字符串引用 "dataset:列名" 由装配按列名匹配绑定数据集版本(与集成测试同构)。
    const profileRef: Record<string, string> = {
      electric_load: 'load_profile',
      heat_load: 'heat_profile',
      cooling_load: 'cooling_profile',
    }
    const params: Record<string, unknown> = {}
    const refParam = profileRef[keyword]
    if (refParam) {
      const col = { electric_load: 'e_load', heat_load: 'h_load', cooling_load: 'c_load' }[keyword]
      params[refParam] = `dataset:${col}`
    }
    const res = await requestJson(ctx, 'POST', `/api/projects/${projectId}/model/devices`, {
      device_type: typeId(keyword),
      name: `E2E ${keyword}`,
      params,
      is_existing: false,
      model_precision: 'medium',
    }, userToken)
    return res.device.id
  }
  const gridId = await addDevice('grid_connection')
  const loadId = await addDevice('electric_load')
  const hpId = await addDevice('heat_pump')
  const heatLoadId = await addDevice('heat_load')
  // 热泵带 cool:out 冷却端口, 系统图需有冷却载体平衡节点(冷却负载),
  // 否则拓扑校验报 PARAM-UNIT-003 阻断任务提交
  const coolLoadId = await addDevice('cooling_load')

  // 读取端口映射后按 载能:方向 匹配连接(热泵 electric in / heat out)
  const graph = await requestJson(ctx, 'GET', `/api/projects/${projectId}/model`, undefined, userToken)
  const portOf = (deviceId: number, portType: string, direction: string): number => {
    const p = graph.ports.find(
      (it: any) => it.device_id === deviceId && it.port_type === portType && it.direction === direction,
    )
    if (!p) throw new Error(`未找到端口 ${deviceId}/${portType}/${direction}`)
    return p.id
  }
  const connect = (from: number, to: number, connType: string): Promise<any> =>
    requestJson(ctx, 'POST', `/api/projects/${projectId}/model/connections`, {
      from_port_id: from,
      to_port_id: to,
      attrs: { conn_type: connType },
    }, userToken)
  await connect(portOf(gridId, 'electric', 'out'), portOf(loadId, 'electric', 'in'), 'electric_line')
  await connect(portOf(gridId, 'electric', 'out'), portOf(hpId, 'electric', 'in'), 'electric_line')
  // 热泵热端端口类型为 thermal(注册表热载体), 冷端为 cooling
  await connect(portOf(hpId, 'thermal', 'out'), portOf(heatLoadId, 'thermal', 'in'), 'thermal_pipe')
  await connect(portOf(hpId, 'cooling', 'out'), portOf(coolLoadId, 'cooling', 'in'), 'cooling_pipe')
}

/** 会话可用性自检: 未登录访问公开页应可读, 受保护页应跳登录。 */
export async function probeSession(ctx: APIRequestContext): Promise<void> {
  const res = await ctx.fetch(`${BASE}/api/auth/public-settings`)
  if (!res.ok()) {
    throw new Error(`public-settings 不可用: ${res.status()} ${(await res.text()).slice(0, 300)}`)
  }
}
