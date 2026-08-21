// QA-E2E-01 测试基线扩展: 基础设施自检(会话服务可用性)。
// 仅检查环境可用, 不覆盖任何业务验收场景(业务场景在各 spec 文件)。

import { test, expect } from '@playwright/test'
import { APP_URL } from './fixtures/env'

test.describe('E2E 基础设施', () => {
  test('应用公开入口可访问, 登录页渲染', async ({ page }) => {
    const errors: string[] = []
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text())
    })
    await page.goto(APP_URL)
    await expect(page).toHaveTitle(/IES|登录|Sign in/i)
    // 未登录访问首页应被重定向到登录页(RequireAuth)
    await expect(page).toHaveURL(/\/login/)
    await expect(page.getByRole('heading', { name: /登录|Sign in/i })).toBeVisible()
    expect(errors).toEqual([])
  })
})
