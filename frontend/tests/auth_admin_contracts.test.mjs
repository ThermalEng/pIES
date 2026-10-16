/**
 * 认证与管理 feature 纯契约与映射测试 (Batch 2)。
 *
 * 覆盖:
 * 1. features/auth mappers 与 api:
 *    - mapUserDtoToModel 消费真实 UserOutDto, 移除猜想字段;
 *    - mapAuthResponseDtoToModel 映射登录/接管结果;
 *    - mapPublicSettingsDtoToModel 映射公开设置;
 *    - login, logout, changePassword, confirmTakeover, register, getMe, getPublicSettings
 *      走精确端点和请求体, 正确管理会话标记。
 * 2. features/admin mappers 与 api:
 *    - mapAdminUserDtoToModel 映射管理员用户列表项(project_count, roles);
 *    - mapUserDeletePreviewDto 映射删除预告清单与确认令牌;
 *    - mapStorageStatusDto 映射存储对象用量与对象数;
 *    - mapAdminHealthDto 映射服务健康及存活/就绪检查;
 *    - mapAuditEntryDto 映射审计条目(参数与元数据);
 *    - listUsers, resetPassword, deactivateUser, reactivateUser, previewUserDelete,
 *      deleteUser, getSecuritySettings, setRegistrationEnabled, getStorageStatus,
 *      getAdminHealth, queryAudit 走精确端点。
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

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

// ---------------------------------------------------------------------------
// 1. Auth 纯映射测试
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
  check('mapUserDtoToModel 状态与凭证版本', user.status === 'active' && user.credential_version === 3)
  check('mapUserDtoToModel 登录时间', user.last_login_at === '2026-10-16T12:00:00Z')

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
  check('mapAdminUserDtoToModel 项目数与角色数组', adminRow.project_count === 5 && adminRow.roles.includes('admin'))
  check('mapAdminUserDtoToModel 活动时间对齐', adminRow.last_active_at === '2026-10-16T10:00:00Z')

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
    },
    capacity: {
      free_bytes: 500000000,
      safe_threshold: 10000000,
      ok: true,
    },
    refs: { count: 320 },
  }
  const storage = mapStorageStatusDto(storageDto)
  check('mapStorageStatusDto 总用量与对象数', storage.total_bytes === 8540120 && storage.used_bytes === 8540120 && storage.object_count === 142)

  const healthDto = {
    status: 'ok',
    service: 'iesplan',
    version: '0.14.0',
    time: '2026-10-16T12:00:00Z',
    liveness: { ok: true, process: 'alive' },
    readiness: { db: true },
    storage: { ok: true },
  }
  const health = mapAdminHealthDto(healthDto)
  check('mapAdminHealthDto 状态与版本', health.status === 'ok' && health.version === '0.14.0')
  check('mapAdminHealthDto 存活与就绪检查', health.checks.liveness.status === 'ok' && health.checks.readiness.status === 'ok')

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
// 3. Auth API 桩调用测试
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
  check('API logout 清空会话标记', hasSession() === false)

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
}

// ---------------------------------------------------------------------------
// 4. Admin API 桩调用测试
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
        objects: { count: 10, total_bytes: 4096 },
        capacity: { free_bytes: 100000, safe_threshold: 1000, ok: true },
        refs: { count: 20 },
      }), { status: 200 })
    }
    if (method === 'GET' && text.endsWith('/api/admin/health')) {
      return new Response(JSON.stringify({
        status: 'ok', service: 'iesplan', version: '0.14.0', time: '2026-10-16T12:00:00Z',
        liveness: { ok: true }, readiness: { db: true }, storage: { ok: true },
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
}

if (failures > 0) {
  console.error(`FAILED: ${failures} checks failed`)
  process.exit(1)
} else {
  console.log('all auth/admin contract checks passed')
}
