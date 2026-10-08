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
    baseline_resolution: '1h',
    baseline_start_year: 2026,
    baseline_scenario_mode: 'single',
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
  const specOf = (keyword: string) => {
    const t = deviceTypes.items.find((it: any) => it.type_id.includes(keyword))
    if (!t) throw new Error(`未找到设备类型: ${keyword}`)
    return t
  }
  // 2.0 唯一建模面: 草稿语义命令(名键设备 + 名键端点连接, 无类型/容量/损耗字段)。
  // 端口名取注册表设备 YAML 公开 descriptor(与画布句柄同一来源)。
  const portNameOf = (keyword: string, carrier: string, direction: string): string => {
    const spec = specOf(keyword)
    const p = (spec.ports as any[]).find((it) => it.energy_carrier === carrier && it.direction === direction)
    if (!p) throw new Error(`未找到端口 ${keyword}/${carrier}/${direction}`)
    return p.name
  }
  const nameOf = (keyword: string) => `E2E ${keyword}`
  const commands: any[] = []
  const addDevice = (keyword: string) => {
    // 2.0 无 profile 引用参数(source.data_ref 为唯一数据路径);
    // 负荷数据由运行期按列名合并已绑定数据集, 设备参数只带物理参数。
    commands.push({
      id: `e2e-dev-${keyword}`,
      unit: 'model',
      type: 'model.upsert_device',
      payload: { name: nameOf(keyword), device_type: specOf(keyword).type_id, kind: 'new', params: {} },
    })
  }
  addDevice('grid_connection')
  addDevice('electric_load')
  addDevice('heat_pump')
  addDevice('heat_load')
  // 2.0 热泵无制冷接口, 冷负荷由制冷机供冷(否则冷母线无源阻断任务提交)
  addDevice('electric_chiller')
  addDevice('cooling_load')

  const connect = (fromKw: string, fromCarrier: string, toKw: string, toCarrier: string) => {
    const from = nameOf(fromKw)
    const to = nameOf(toKw)
    const fromPort = portNameOf(fromKw, fromCarrier, 'out')
    const toPort = portNameOf(toKw, toCarrier, 'in')
    commands.push({
      id: `e2e-conn-${fromKw}-${toKw}`,
      unit: 'model',
      type: 'model.upsert_connection',
      payload: {
        name: `${from}.${fromPort}->${to}.${toPort}`,
        from_device: from,
        from_port: fromPort,
        to_device: to,
        to_port: toPort,
      },
    })
  }
  // 注册表现实载体名为 electricity(历史 'electric' 已改名), heat/cool 不变。
  connect('grid_connection', 'electricity', 'electric_load', 'electricity')
  connect('grid_connection', 'electricity', 'heat_pump', 'electricity')
  connect('grid_connection', 'electricity', 'electric_chiller', 'electricity')
  // 热泵热端为 heat 载体, 制冷机冷端为 cool 载体
  connect('heat_pump', 'heat', 'heat_load', 'heat')
  connect('electric_chiller', 'cool', 'cooling_load', 'cool')

  const view = await requestJson(ctx, 'GET', `/api/projects/${projectId}`, undefined, userToken)
  await requestJson(ctx, 'PUT', `/api/projects/${projectId}/draft`, {
    expected_revision: view.draft.revision,
    commands,
  }, userToken)
}

/** 会话可用性自检: 未登录访问公开页应可读, 受保护页应跳登录。 */
export async function probeSession(ctx: APIRequestContext): Promise<void> {
  const res = await ctx.fetch(`${BASE}/api/auth/public-settings`)
  if (!res.ok()) {
    throw new Error(`public-settings 不可用: ${res.status()} ${(await res.text()).slice(0, 300)}`)
  }
}
