import { expect, test } from '@playwright/test'
import { createSession, strongPassword, uniqueName } from './fixtures'
import { createEngineer } from './setup/api'

test('退出失败可见且保留会话，重试成功后退出', async ({ browser }) => {
  const username = uniqueName('es-logout')
  const password = strongPassword('Logout')
  const setup = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
  await createEngineer(setup.request, username, password)
  await setup.close()
  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  let fail = true
  await page.route('**/api/auth/logout', async (route) => {
    if (fail) {
      await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'logout-failed' }) })
    } else {
      await route.continue()
    }
  })
  await page.getByRole('button', { name: /退出登录|Logout/i }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await expect(page).not.toHaveURL(/\/login/)
  // 真实刷新受保护页：失败不能清除登录标记，也不能伪装已撤销服务端会话。
  await page.goto('/settings')
  await expect(page.getByRole('button', { name: /退出登录|Logout/i })).toBeVisible()
  await expect(page).toHaveURL(/\/settings/)
  fail = false
  await page.getByRole('button', { name: /退出登录|Logout/i }).click()
  await expect(page).toHaveURL(/\/login/)
  await page.goto('/settings')
  await expect(page).toHaveURL(/\/login/)
  session.assertNoErrors([{ method: 'POST', path: '/api/auth/logout', status: 500, times: 1 }])
  await context.close()
})
