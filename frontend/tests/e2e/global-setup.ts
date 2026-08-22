// QA-E2E-01 全局前置(setup): 在运行测试前一次性完成
// 管理员首登强制改密(admin 种子用户 requires_change=True)。
//
// 场景 1(登录/首登改密/会话失效)自建独立用户完成 UI 改密验收;
// 此处的管理员改密属于测试环境前置(13.2 允许 API 前置造数,
// 与被测动作隔离——被测的改密流程在场景 1 内, 不经过此调用)。

import { chromium, type APIRequestContext, type BrowserContext } from '@playwright/test'
import { APP_URL } from './fixtures/env'

const ADMIN_INITIAL_PASSWORD = process.env.E2E_ADMIN_INITIAL_PASSWORD ?? 'iesplan-admin-initial'
const ADMIN_NEW_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? 'Iesplan-Admin-2026!'

export default async function globalSetup(): Promise<void> {
  const browser = await chromium.launch()
  const context: BrowserContext = await browser.newContext()
  const page = await context.newPage()
  const api: APIRequestContext = context.request

  // 0) 环境自检: 应用入口与公开设置可读。
  //    RR-P2-13: compose 已通过 healthcheck 保证 web→backend ready 链路;
  //    此处仍保留有上限、带退避的 ready 轮询(首次构建/迁移/注册稍慢时
  //    不会随机失败, 30 次 × 2s = 最长等待 60s)。
  let probe
  for (let attempt = 0; attempt < 30; attempt += 1) {
    probe = await api.get(`${APP_URL}/api/auth/public-settings`)
    if (probe.ok()) break
    await new Promise((resolve) => setTimeout(resolve, 2000))
  }
  if (!probe!.ok()) {
    throw new Error(
      `E2E 环境不可用: GET /api/auth/public-settings -> ${probe!.status()} ` +
        `(请先 docker compose up -d --build web backend, 并等待就绪)`,
    )
  }

  // 1) 以种子初始密码尝试登录; 若成功(首次运行)则完成强制改密,
  //    之后用新密码验证登录并清理会话。
  // 2) 幂等: 若初始密码已失效(改密已完成), 直接用新密码登录确认即可。
  const tryLogin = async (password: string) => {
    const res = await api.post(`${APP_URL}/api/auth/login`, {
      data: { username: 'admin', password },
    })
    if (!res.ok()) return null
    const body = await res.json()
    return {
      token: body.token as string,
      needsChange: body.user?.force_password_change === true,
      needsTakeover: body.needs_takeover_confirm === true,
    }
  }

  let login = await tryLogin(ADMIN_INITIAL_PASSWORD)
  if (login?.needsChange) {
    const changed = await api.post(`${APP_URL}/api/auth/change-password`, {
      headers: { Authorization: `Bearer ${login.token}` },
      data: { old_password: ADMIN_INITIAL_PASSWORD, new_password: ADMIN_NEW_PASSWORD },
    })
    if (!changed.ok()) {
      throw new Error(`管理员首登改密失败: ${changed.status()} ${(await changed.text()).slice(0, 300)}`)
    }
    // 改密后旧凭证失效, 重新以新密码登录(会话供后续清理用)
    login = await tryLogin(ADMIN_NEW_PASSWORD)
  } else if (!login) {
    // 初始密码已不可用(改密已完成或手动重置): 直接尝试新密码
    login = await tryLogin(ADMIN_NEW_PASSWORD)
  }
  if (!login) {
    throw new Error(
      `管理员登录失败(初始密码与 E2E_ADMIN_PASSWORD 均不可用), ` +
        `请核对 docker-compose e2e 服务的 E2E_ADMIN_PASSWORD`,
    )
  }
  // 若新会话为 takeover_pending(旧窗口会话仍在), 确认接管激活, 保证清理 token 可用
  if (login.needsTakeover) {
    await api.post(`${APP_URL}/api/auth/confirm-takeover`, {
      headers: { Authorization: `Bearer ${login.token}` },
      data: { token: login.token },
    })
  }

  // 3) 清理历史测试残留(幂等键前缀 es_ 开头的用户, 级联删除其项目)
  // B1 上线后删除须先 delete-preview 取得签名令牌, 再 DELETE 携带 confirm。
  const usersRes = await api.get(`${APP_URL}/api/auth/users`, {
    headers: { Authorization: `Bearer ${login.token}` },
  })
  if (usersRes.ok()) {
    const users = await usersRes.json()
    const stale = (users.users ?? []).filter((u: any) => String(u.username).startsWith('es_'))
    for (const u of stale) {
      const prev = await api.post(`${APP_URL}/api/auth/users/${u.id}/delete-preview`, {
        headers: { Authorization: `Bearer ${login.token}` },
      })
      if (!prev.ok()) continue
      const preview = await prev.json()
      await api.delete(`${APP_URL}/api/auth/users/${u.id}`, {
        headers: { Authorization: `Bearer ${login.token}` },
        data: { confirm: true, confirm_token: preview.confirm_token },
      })
    }
  }

  await browser.close()
}
