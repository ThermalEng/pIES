// A2 接管确认无请求体: 同一账号第二窗口登录触发接管, 确认 POST 不再
// 携带被服务端忽略的 token body；确认后当前登录响应设置的会话继续有效。
// 准备走公开 API(建工程师)；两次登录与接管走真实 GUI；只观察请求体，
// 不 mock 业务、不修改请求。不跑全量。

import { test, expect } from '@playwright/test'
import type { BrowserContext, Page } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer } from './setup/api'
import { APP } from './financial-scaffold'

test.describe('A2: 接管确认无请求体', () => {
  test.setTimeout(300_000)

  test('第二窗口接管 POST 无 body 且会话继续有效', async ({ browser }) => {
    const username = uniqueName('es-takeover')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    // 第一窗口正常登录。
    const ctx1: BrowserContext = await browser.newContext()
    const page1: Page = await ctx1.newPage()
    const session1 = await createSession(page1, username, password)
    await expect(page1.getByText('项目列表').first()).toBeVisible({ timeout: 30_000 })

    // 第二窗口登录触发接管：记录确认 POST 请求体后放行。
    const ctx2: BrowserContext = await browser.newContext()
    let confirmBody: string | null | undefined = 'NOT-SENT'
    await ctx2.route('**/auth/confirm-takeover', async (route) => {
      confirmBody = route.request().postData()
      await route.continue()
    })
    const page2: Page = await ctx2.newPage()
    const session2 = await createSession(page2, username, password)

    // 确认请求发出且无 body（服务端忽略 body，身份走 Cookie/Bearer）。
    await expect.poll(() => confirmBody !== 'NOT-SENT' ? 'sent' : 'wait', { timeout: 30_000 }).toBe('sent')
    expect(confirmBody === null || confirmBody === '').toBe(true)

    // 接管后当前会话继续有效：第二窗口正常进入应用。
    await expect(page2.getByText('项目列表').first()).toBeVisible({ timeout: 30_000 })

    session1.assertNoErrors()
    session2.assertNoErrors()
    await ctx1.close()
    await ctx2.close()
  })
})
