// 0.15 S0 入口修复验收: 财务 Profile 列表/登记/引用在非标准端口部署下的
// 真实浏览器流程。
//
// 运行方式(仅 Docker): compose 覆盖文件在 nginx 内新增非标准端口监听
// (如 8081, 保留 80 健康), e2e 以 E2E_APP_URL=http://web:8081 运行本 spec:
//   docker compose -p pies-015-adversarial -f docker-compose.yml \
//     -f <override.yml> up -d --build e2e
//   docker compose ... run --rm e2e npx playwright test finance-entry-port.spec.ts
//
// 覆盖:
// A) 尾示例尾斜杠请求的合法重定向 Location 保持实际请求的 Host 与端口
//    (反代 $http_host 透传), 不再丢失部署端口;
// B) 财务页 UI 全链路: 列表加载 → 登记画像 → 列表出现 → 引用到项目
//    (状态变更 PUT 经 CSRF Host:端口同源推导, 不因非标准端口 403)。
//
// 派工: docs/development/0.15-entry-unblock.md S0——前端路径统一消除尾
// 斜杠重定向后, 正常请求不再触发重定向; 本 spec 仍保留 A 以锁定非标准
// 端口下"合法重定向回到同一部署"的反代行为。

import { test, expect } from '@playwright/test'
import type { BrowserContext, Page } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, createProject, loginToken } from './setup/api'
import { APP, financeProfile } from './financial-scaffold'

const NON_STANDARD = new URL(APP).port !== '' && new URL(APP).port !== '80'

test.describe('S0 财务入口: 非标准端口部署', () => {
  test.skip(
    () => !NON_STANDARD,
    '本 spec 面向非标准端口部署; 标准端口布局下由 compose 覆盖启用(见文件头)',
  )

  test('A: 尾示例尾斜杠重定向 Location 保持实际请求的 Host 与端口', async ({ request }) => {
    await test.step('未跟随重定向时, Location 不得丢失部署端口', async () => {
      const res = await request.get('/api/finance-profiles/', { maxRedirects: 0 })
      const origin = new URL(APP).origin
      const loc = res.headers()['location']
      if (res.ok()) {
        // 若路由已消除重定向则无 Location; 不存在旧形状的跨端口跳转
        expect(loc).toBeUndefined()
      } else {
        expect(res.status()).toBe(307)
        expect(loc?.startsWith(origin)).toBe(true)
      }
    })
    await test.step('跟随重定向后回到同一部署并成功(列表 401 为未登录, 不是跨端口 404)', async () => {
      const res = await request.get('/api/finance-profiles/')
      // 未携带会话 → 401; 关键是请求未被重定向到错误端口而 404/5xx
      expect([200, 401]).toContain(res.status())
    })
  })

  test('B: 财务页列表/登记/引用全流程(真实浏览器)', async ({ browser }) => {
    const username = uniqueName('es-finport')
    const password = strongPassword('Init')
    const projectName = uniqueName('S0 财务入口项目')
    const profileId = `s0_port_${Date.now()}`

    // 前置造数(工程师 + 项目)走 API, 与被测 UI 动作隔离。
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    const created = await createProject(apiCtx.request, token, projectName)

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(new RegExp(`/projects/${created.id}$`))
    await page.goto(`${APP.replace(/\/$/, '')}/projects/${created.id}/finance`)
    await expect(page.locator('#finance-profile-select')).toBeVisible({ timeout: 30_000 })

    // 登记(合成画像全文 JSON; 与 financial-scaffold 同一公开契约形状)。
    await page.locator('#finance-profile-doc').fill(JSON.stringify(financeProfile(profileId), null, 2))
    await page.getByRole('button', { name: /登记|Register/ }).click({ timeout: 30_000 })
    await expect(page.getByText(/登记成功|Registered/).first()).toBeVisible({ timeout: 30_000 })

    // 列表出现新画像 → 引用到当前项目(状态变更请求经网络真实往返)。
    const select = page.locator('#finance-profile-select')
    await select.selectOption(profileId, { timeout: 30_000 })
    await page.getByRole('button', { name: /引用\/切换|Reference/ }).click({ timeout: 30_000 })
    await expect(page.getByText(/已引用画像|Profile .* referenced/).first()).toBeVisible({
      timeout: 30_000,
    })

    // "当前引用"区域展示绑定结果。
    await expect(page.locator('dd').filter({ hasText: profileId }).first()).toBeVisible({
      timeout: 15_000,
    })

    await apiCtx.close()
    await context.close()
  })
})
