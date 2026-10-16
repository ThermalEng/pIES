/**
 * 认证与管理 feature 纯契约与映射测试 (Batch 2)。
 *
 * 覆盖:
 * 1. features/auth mappers 与 api:
 *    - mapUserDtoToModel 消费真实 UserOutDto, 严格断言仅含后端支持字段, 不含虚构默认值;
 *    - mapAuthResponseDtoToModel 映射登录/接管结果, 准确透传 User 及布尔标记;
 *    - mapPublicSettingsDtoToModel 映射公开设置;
 *    - login, logout, changePassword, confirmTakeover, register, getMe, getPublicSettings;
 *    - 失败 HTTP 原样透传 ApiError, 登录失败不设会话, 登出失败不清除会话。
 * 2. features/admin mappers 与 api:
 *    - mapAdminUserDtoToModel 映射管理员用户列表项(严格对齐 UserOut + project_count);
 *    - mapUserDeletePreviewDto 映射删除预告清单与确认令牌, 直接消费必需字段;
 *    - mapStorageStatusDto 映射存储对象用量(objects.total_bytes 作为使用量展示);
 *    - mapAdminHealthDto 映射服务健康及存活/就绪检查, 包含 ok/degraded 真实状态;
 *    - mapAuditEntryDto 映射审计条目(参数与元数据);
 *    - listUsers, resetPassword, deactivateUser, reactivateUser, previewUserDelete,
 *      deleteUser, getSecuritySettings, setRegistrationEnabled, getStorageStatus,
 *      getAdminHealth, queryAudit 真实参数与响应透传, 失败时不降级为空列表。
 * 3. 状态枚举与本地化文案:
 *    - active / disabled / locked 三态独立识别与展示文案对齐。
 */

import {
  mapUserDtoToModel,
  mapAuthResponseDtoToModel,
  mapPublicSettingsDtoToModel,
} from '../src/features/auth/mappers.ts'
import {
  login,
  logout,
  changePassword,
  confirmTakeover,
  register,
  getMe,
  getPublicSettings,
} from '../src/features/auth/api.ts'
import {
  mapAdminUserDtoToModel,
  mapUserDeletePreviewDto,
  mapStorageStatusDto,
  mapAdminHealthDto,
  mapAuditEntryDto,
} from '../src/features/admin/mappers.ts'
import {
  listUsers,
  resetPassword,
  deactivateUser,
  reactivateUser,
  previewUserDelete,
  deleteUser,
  getSecuritySettings,
  setRegistrationEnabled,
  getStorageStatus,
  getAdminHealth,
  queryAudit,
} from '../src/features/admin/api.ts'
import { hasSession } from '../src/shared/http.ts'
import { ApiError } from '../src/types.ts'
import { messagesZh } from '../src/i18n/messages_zh.ts'
import { messagesEn } from '../src/i18n/messages_en.ts'

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

// ---------------------------------------------------------------------------
// 1. Auth 纯映射测试 (严格键集合与空值检查)
// ---------------------------------------------------------------------------
{
  const userDto = {
    id: 101,
    username: 'test_engineer',
    display_name: '测试工程师',
    role: 'engineer',
    status: 'active',
    force_password_change: false,
    credential_version: 3,
    last_login_at: '2026-10-16T12:00:00Z',
  }
  const user = mapUserDtoToModel(userDto)
  check('mapUserDtoToModel 基础字段', user.id === 101 && user.username === 'test_engineer' && user.role === 'engineer')
  check('mapUserDtoToModel 状态与凭证版本', user.status === 'active' && user.credential_version === 3 && user.force_password_change === false)
  check('mapUserDtoToModel 登录时间', user.last_login_at === '2026-10-16T12:00:00Z')

  // 严格核对 User 键集合: 仅允许后端 UserOut 存在的 8 个字段
  const userKeys = Object.keys(user).sort()
  const expectedUserKeys = ['credential_version', 'display_name', 'force_password_change', 'id', 'last_login_at', 'role', 'status', 'username'].sort()
  check('User 仅含后端 UserOut 真实字段', userKeys.join(',') === expectedUserKeys.join(','))
  check('User 不含虚构的 locale/timezone 等字段', !('locale' in user || 'timezone' in user || 'fixed_utc_offset_minutes' in user || 'is_system' in user || 'created_at' in user))

  // 登录时间为 null 时的保真性
  const nullLoginUser = mapUserDtoToModel({ ...userDto, last_login_at: null })
  check('User last_login_at 保留真实 null', nullLoginUser.last_login_at === null)

  // 三种状态保真映射
  for (const st of ['active', 'disabled', 'locked']) {
    const u = mapUserDtoToModel({ ...userDto, status: st })
    check(`User 状态 ${st} 保真`, u.status === st)
  }

  const authRespDto = {
    token: 'tok-abc-123',
    token_type: 'bearer',
    user: userDto,
    needs_takeover_confirm: true,
  }
  const loginRes = mapAuthResponseDtoToModel(authRespDto)
  check('mapAuthResponseDtoToModel 令牌与类型', loginRes.token === 'tok-abc-123' && loginRes.token_type === 'bearer')
  check('mapAuthResponseDtoToModel 接管标记', loginRes.needs_takeover_confirm === true)
  check('mapAuthResponseDtoToModel 强制改密标记', loginRes.user.force_password_change === false)
  check('mapAuthResponseDtoToModel 内嵌用户', loginRes.user.id === 101)

  const pubDto = {
    registration_enabled: true,
    sso_enabled: false,
    sso_provider_name: 'Keycloak',
  }
  const pub = mapPublicSettingsDtoToModel(pubDto)
  check('mapPublicSettingsDtoToModel 开关与SSO', pub.registration_enabled === true && pub.sso_enabled === false && pub.sso_provider_name === 'Keycloak')
}

// ---------------------------------------------------------------------------
// 2. Admin 纯映射测试
// ---------------------------------------------------------------------------
{
  const adminUserDto = {
    id: 102,
    username: 'ops_admin',
    display_name: '运维管理',
    role: 'admin',
    status: 'active',
    force_password_change: false,
    credential_version: 1,
    last_login_at: '2026-10-16T10:00:00Z',
    project_count: 5,
  }
  const adminRow = mapAdminUserDtoToModel(adminUserDto)
  check('mapAdminUserDtoToModel 项目数与角色', adminRow.project_count === 5 && adminRow.role === 'admin')
  const expectedAdminKeys = ['credential_version', 'display_name', 'force_password_change', 'id', 'last_login_at', 'project_count', 'role', 'status', 'username'].sort()
  check('AdminUserRow 仅含后端 AdminUserOut 字段', Object.keys(adminRow).sort().join(',') === expectedAdminKeys.join(','))

  const previewDto = {
    confirm_token: 'conf-del-999',
    project_count: 2,
    projects: [
      { id: 1, name: '项目一', status: 'active' },
      { id: 2, name: '项目二', status: 'archived' },
    ],
    user_id: 103,
    username: 'to_be_deleted',
  }
  const preview = mapUserDeletePreviewDto(previewDto)
  check('mapUserDeletePreviewDto 确认令牌与计数', preview.confirm_token === 'conf-del-999' && preview.project_count === 2)
  check('mapUserDeletePreviewDto 项目清单完整', preview.projects.length === 2 && preview.projects[0].name === '项目一')

  const storageDto = {
    healthy: true,
    corrupt_count: 0,
    cleanup_candidates: 3,
    objects: {
      count: 142,
      total_bytes: 8540120,
      by_status: { active: 142 },
      orphan_count: 0,
      pending_deletion_count: 0,
    },
    capacity: {
      free_bytes: 500000000,
      safe_threshold: 10000000,
      ok: true,
    },
    refs: { count: 320, referenced_objects: 142 },
    pending_deletion_count: 0,
  }
  const storage = mapStorageStatusDto(storageDto)
  check('mapStorageStatusDto 总用量与对象数', storage.total_bytes === 8540120 && storage.used_bytes === 8540120 && storage.object_count === 142)
  check('mapStorageStatusDto 配额诚实为 null', storage.quota_bytes === null)

  const healthDto = {
    status: 'ok',
    service: 'iesplan',
    version: '0.14.0',
    time: '2026-10-16T12:00:00Z',
    liveness: { ok: true, process: 'alive' },
    readiness: { db: true },
    metrics: { tasks_by_status: {}, projects_by_status: {}, users: 2 },
    queue: {},
    storage: { capacity: {}, corrupt_count: 0, orphan_count: 0, object_count: 142, ok: true, verify: {} },
  }
  const health = mapAdminHealthDto(healthDto)
  check('mapAdminHealthDto 状态与版本', health.status === 'ok' && health.version === '0.14.0')
  check('mapAdminHealthDto 存活与就绪检查', health.checks.liveness.status === 'ok' && health.checks.readiness.status === 'ok' && health.checks.storage.status === 'ok')

  const degradedHealth = mapAdminHealthDto({
    ...healthDto,
    status: 'degraded',
    storage: { ...healthDto.storage, ok: false },
  })
  check('mapAdminHealthDto 降级状态保留', degradedHealth.status === 'degraded' && degradedHealth.checks.storage.status === 'degraded')

  const auditDto = {
    id: 501,
    entity_type: 'user',
    entity_id: 102,
    action: 'admin_deactivate_user',
    actor_id: 1,
    actor_type: 'admin',
    occurred_at: '2026-10-16T14:00:00Z',
    ip: '127.0.0.1',
    before: { status: 'active' },
    after: { status: 'disabled' },
    request_id: 'req-abc',
    trace_id: 'trc-xyz',
  }
  const audit = mapAuditEntryDto(auditDto)
  check('mapAuditEntryDto 审计动作与操作者', audit.id === 501 && audit.action === 'admin_deactivate_user' && audit.actor_type === 'admin')
  check('mapAuditEntryDto 变更前后快照', audit.before?.status === 'active' && audit.after?.status === 'disabled')
}

// ---------------------------------------------------------------------------
// 3. Auth API 桩调用与失败传播测试
// ---------------------------------------------------------------------------
{
  const calls = []
  globalThis.fetch = async (url, init = {}) => {
    const text = String(url)
    const method = String(init.method ?? 'GET').toUpperCase()
    calls.push({ method, url: text, body: init.body ? JSON.parse(String(init.body)) : null })

    if (method === 'POST' && text.endsWith('/api/auth/login')) {
      return new Response(JSON.stringify({
        token: 'login-tok',
        token_type: 'bearer',
        user: { id: 1, username: 'admin', display_name: '管理员', role: 'admin', status: 'active', force_password_change: false, credential_version: 1, last_login_at: null },
        needs_takeover_confirm: false,
      }), { status: 200 })
    }
    if (method === 'POST' && text.endsWith('/api/auth/logout')) {
      return new Response(JSON.stringify({ ok: true }), { status: 200 })
    }
    if (method === 'POST' && text.endsWith('/api/auth/change-password')) {
      return new Response(JSON.stringify({ ok: true, message_key: 'ies.diag.auth.password_changed' }), { status: 200 })
    }
    if (method === 'POST' && text.endsWith('/api/auth/confirm-takeover')) {
      return new Response(JSON.stringify({
        token: 'takeover-tok',
        token_type: 'bearer',
        user: { id: 1, username: 'admin', display_name: '管理员', role: 'admin', status: 'active', force_password_change: false, credential_version: 1, last_login_at: null },
        needs_takeover_confirm: false,
      }), { status: 200 })
    }
    if (method === 'POST' && text.endsWith('/api/auth/register')) {
      return new Response(JSON.stringify({
        id: 2, username: 'newbie', display_name: '新人', role: 'engineer', status: 'active', force_password_change: true, credential_version: 1, last_login_at: null,
      }), { status: 200 })
    }
    if (method === 'GET' && text.endsWith('/api/auth/me')) {
      return new Response(JSON.stringify({
        id: 1, username: 'admin', display_name: '管理员', role: 'admin', status: 'active', force_password_change: false, credential_version: 1, last_login_at: null,
      }), { status: 200 })
    }
    if (method === 'GET' && text.endsWith('/api/auth/public-settings')) {
      return new Response(JSON.stringify({
        registration_enabled: true, sso_enabled: false, sso_provider_name: '',
      }), { status: 200 })
    }
    return new Response('{"error":"unexpected"}', { status: 500 })
  }

  // 模拟 localStorage
  const storage = new Map()
  globalThis.window = {
    localStorage: {
      getItem: (k) => storage.get(k) ?? null,
      setItem: (k, v) => storage.set(k, String(v)),
      removeItem: (k) => storage.delete(k),
    },
  }

  const loginRes = await login({ username: 'admin', password: 'secret' })
  check('API login 成功响应', loginRes.token === 'login-tok' && loginRes.user.username === 'admin')
  check('API login 设置会话标记', hasSession() === true)

  await logout()
  check('API logout 成功清除会话标记', hasSession() === false)

  await changePassword({ old_password: 'secret', new_password: 'new-secret' })
  check('API changePassword 路径与请求体', calls.some((c) => c.url.endsWith('/api/auth/change-password') && c.body?.new_password === 'new-secret'))

  const takeoverRes = await confirmTakeover()
  check('API confirmTakeover 响应', takeoverRes.token === 'takeover-tok')

  const regRes = await register({ username: 'newbie', password: 'p' })
  check('API register 响应', regRes.id === 2 && regRes.role === 'engineer')

  const meRes = await getMe()
  check('API getMe 响应', meRes.id === 1)

  const pubRes = await getPublicSettings()
  check('API getPublicSettings 响应', pubRes.registration_enabled === true)

  // 失败传播断言: 登录 401 失败不应设置会话
  storage.delete('iesplan.session')
  globalThis.fetch = async () => new Response(JSON.stringify({
    error: { code: 'AUTH-VAL-001', message_key: 'ies.diag.auth.invalid_credentials', severity: 'error', blocking: true },
  }), { status: 401, headers: { 'Content-Type': 'application/json' } })

  let loginErr = null
  try {
    await login({ username: 'admin', password: 'wrong' })
  } catch (err) {
    loginErr = err
  }
  check('登录失败原样透传 ApiError 与状态码', loginErr instanceof ApiError && loginErr.status === 401 && loginErr.message_key === 'ies.diag.auth.invalid_credentials')
  check('登录失败不会设置会话标记', hasSession() === false)

  // 失败传播断言: 登出 500 失败不应清除会话
  storage.set('iesplan.session', '1')
  globalThis.fetch = async () => new Response(JSON.stringify({
    error: { code: 'API-500-001', message_key: 'ies.error.internal', severity: 'error', blocking: true },
  }), { status: 500, headers: { 'Content-Type': 'application/json' } })

  let logoutErr = null
  try {
    await logout()
  } catch (err) {
    logoutErr = err
  }
  check('登出失败原样透传 ApiError', logoutErr instanceof ApiError && logoutErr.status === 500)
  check('登出失败不应清除会话(保持原有错误与会话行为)', hasSession() === true)
  storage.delete('iesplan.session')
}

// ---------------------------------------------------------------------------
// 4. Admin API 桩调用与失败传播测试
// ---------------------------------------------------------------------------
{
  const calls = []
  globalThis.fetch = async (url, init = {}) => {
    const text = String(url)
    const method = String(init.method ?? 'GET').toUpperCase()
    calls.push({ method, url: text, body: init.body ? JSON.parse(String(init.body)) : null })

    if (method === 'GET' && text.endsWith('/api/auth/users')) {
      return new Response(JSON.stringify({
        users: [
          { id: 1, username: 'admin', display_name: '管理员', role: 'admin', status: 'active', force_password_change: false, credential_version: 1, last_login_at: null, project_count: 0 },
          { id: 2, username: 'user2', display_name: '用户二', role: 'engineer', status: 'disabled', force_password_change: false, credential_version: 2, last_login_at: null, project_count: 3 },
        ],
      }), { status: 200 })
    }
    if (method === 'POST' && text.includes('/api/auth/users/2/reset-password')) {
      return new Response(JSON.stringify({ ok: true, message_key: 'ies.diag.auth.password_reset' }), { status: 200 })
    }
    if (method === 'POST' && text.includes('/api/auth/users/2/deactivate')) {
      return new Response(JSON.stringify({ ok: true }), { status: 200 })
    }
    if (method === 'POST' && text.includes('/api/auth/users/2/reactivate')) {
      return new Response(JSON.stringify({ ok: true }), { status: 200 })
    }
    if (method === 'POST' && text.includes('/api/auth/users/2/delete-preview')) {
      return new Response(JSON.stringify({
        confirm_token: 'tok-del-2', project_count: 1, projects: [{ id: 8, name: 'P8', status: 'active' }], user_id: 2, username: 'user2',
      }), { status: 200 })
    }
    if (method === 'DELETE' && text.includes('/api/auth/users/2')) {
      return new Response(JSON.stringify({ ok: true, deleted_projects: 1 }), { status: 200 })
    }
    if (method === 'GET' && text.endsWith('/api/auth/settings')) {
      return new Response(JSON.stringify({ registration_enabled: false }), { status: 200 })
    }
    if (method === 'PUT' && text.endsWith('/api/auth/settings')) {
      return new Response(JSON.stringify({ registration_enabled: true }), { status: 200 })
    }
    if (method === 'GET' && text.endsWith('/api/admin/storage')) {
      return new Response(JSON.stringify({
        healthy: true, corrupt_count: 0, cleanup_candidates: 0,
        objects: { count: 10, total_bytes: 4096, by_status: {}, orphan_count: 0, pending_deletion_count: 0 },
        capacity: { free_bytes: 100000, safe_threshold: 1000, ok: true },
        refs: { count: 20, referenced_objects: 10 },
        pending_deletion_count: 0,
      }), { status: 200 })
    }
    if (method === 'GET' && text.endsWith('/api/admin/health')) {
      return new Response(JSON.stringify({
        status: 'ok', service: 'iesplan', version: '0.14.0', time: '2026-10-16T12:00:00Z',
        liveness: { ok: true, process: 'alive' }, readiness: { db: true },
        metrics: { tasks_by_status: {}, projects_by_status: {}, users: 2 }, queue: {},
        storage: { capacity: {}, corrupt_count: 0, orphan_count: 0, object_count: 10, ok: true, verify: {} },
      }), { status: 200 })
    }
    if (method === 'GET' && text.includes('/api/admin/audit')) {
      return new Response(JSON.stringify({
        items: [
          { id: 91, entity_type: 'user', entity_id: 2, action: 'login', actor_id: 2, actor_type: 'engineer', occurred_at: '2026-10-16T10:00:00Z', ip: '1.2.3.4', before: null, after: null, request_id: null, trace_id: null },
        ],
        next_cursor: 90,
      }), { status: 200 })
    }
    return new Response('{"error":"unexpected"}', { status: 500 })
  }

  const usersAll = await listUsers()
  check('API listUsers 全量', usersAll.items.length === 2)
  const usersDisabled = await listUsers({ status: 'disabled' })
  check('API listUsers 状态筛选', usersDisabled.items.length === 1 && usersDisabled.items[0].username === 'user2')

  await resetPassword(2, 'new-temp-pwd')
  check('API resetPassword 端点与请求体', calls.some((c) => c.url.includes('/users/2/reset-password') && c.body?.new_password === 'new-temp-pwd'))

  await deactivateUser(2)
  check('API deactivateUser 端点', calls.some((c) => c.url.includes('/users/2/deactivate')))

  await reactivateUser(2)
  check('API reactivateUser 端点', calls.some((c) => c.url.includes('/users/2/reactivate')))

  const delPrev = await previewUserDelete(2)
  check('API previewUserDelete 结果', delPrev.confirm_token === 'tok-del-2' && delPrev.project_count === 1)

  const delRes = await deleteUser(2, 'tok-del-2')
  check('API deleteUser 结果与删除数', delRes.deleted_projects === 1)
  check('API deleteUser 请求体携带 confirm 与令牌', calls.some((c) => c.method === 'DELETE' && c.body?.confirm === true && c.body?.confirm_token === 'tok-del-2'))

  const secRead = await getSecuritySettings()
  check('API getSecuritySettings 结果', secRead.registration_enabled === false)

  const secSet = await setRegistrationEnabled(true)
  check('API setRegistrationEnabled 结果与请求体', secSet.registration_enabled === true && calls.some((c) => c.method === 'PUT' && c.body?.registration_enabled === true))

  const storRes = await getStorageStatus()
  check('API getStorageStatus 结果', storRes.total_bytes === 4096 && storRes.object_count === 10)

  const healthRes = await getAdminHealth()
  check('API getAdminHealth 结果', healthRes.status === 'ok')

  const auditRes = await queryAudit({ action: 'login', limit: 10 })
  check('API queryAudit 结果与游标', auditRes.items.length === 1 && auditRes.next_cursor === '90')

  // 失败传播断言: 管理端接口 500 不吞错, 不伪装成空数据
  globalThis.fetch = async () => new Response(JSON.stringify({
    error: { code: 'API-500-001', message_key: 'ies.error.internal', severity: 'error', blocking: true },
  }), { status: 500, headers: { 'Content-Type': 'application/json' } })

  let userListErr = null
  try {
    await listUsers()
  } catch (err) {
    userListErr = err
  }
  check('用户列表失败直接抛出 ApiError (不降级为空列表)', userListErr instanceof ApiError && userListErr.status === 500)

  let auditErr = null
  try {
    await queryAudit()
  } catch (err) {
    auditErr = err
  }
  check('审计查询失败直接抛出 ApiError (不降级为空列表)', auditErr instanceof ApiError && auditErr.status === 500)
}

// ---------------------------------------------------------------------------
// 5. 状态与本地化文案测试
// ---------------------------------------------------------------------------
{
  check('中文 status_active 键', messagesZh['ies.admin.status_active'] === '正常')
  check('中文 status_disabled 键', messagesZh['ies.admin.status_disabled'] === '已停用')
  check('中文 status_locked 键', messagesZh['ies.admin.status_locked'] === '已锁定')
  check('英文 status_active 键', messagesEn['ies.admin.status_active'] === 'Active')
  check('英文 status_disabled 键', messagesEn['ies.admin.status_disabled'] === 'Disabled')
  check('英文 status_locked 键', messagesEn['ies.admin.status_locked'] === 'Locked')
  check('中文 disable_user 键匹配操作模式', messagesZh['ies.admin.disable_user'] === '停用账号')
  check('英文 disable_user 键匹配操作模式', messagesEn['ies.admin.disable_user'] === 'Deactivate account')
}

if (failures > 0) {
  console.error(`FAILED: ${failures} checks failed`)
  process.exit(1)
} else {
  console.log('all auth/admin contract checks passed')
}
