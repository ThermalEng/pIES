// QA-E2E-01 场景 7-11(13.3):
//   7. 管理员: 用户管理(停用/启用) + 存储健康
//   8. 帮助中心: 三个一级文档入口、Markdown 表格/代码、章节跳转和返回
//   9. 帮助章节深链接 + 刷新保持
//   10. 语言切换(zh/en)与缺失翻译提示
//   11. 桌面/移动视口 + 键盘焦点
//
// 前置: global-setup 已确保 admin 首登改密完成(E2E_ADMIN_PASSWORD)。
// 场景 7 用户管理: 用 admin 停用/启用一个独立用户(UI)。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer } from './setup/api'

// ---------------------------------------------------------------------------
// 场景 7: 管理员用户切换 + 存储健康
// ---------------------------------------------------------------------------

test.describe('场景 7: 管理员', () => {
  test('停用/启用用户并查看存储健康', async ({ browser }) => {
    const adminPassword = process.env.E2E_ADMIN_PASSWORD ?? 'Iesplan-Admin-2026!'
    // 独立目标用户
    const targetUser = uniqueName('es-admin-tgt')
    const targetPassword = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    const created = await createEngineer(apiCtx.request, targetUser, targetPassword)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, 'admin', adminPassword)

    // 进入系统设置
    await page.getByRole('navigation', { name: /项目列表|Projects/i }).getByText('系统设置').click()
    await expect(page).toHaveURL(/\/settings$/)

    // 7a. 存储健康卡片
    const healthCard = page.locator('.ies-settings-grid').locator('div', { hasText: /服务健康|Service health/i }).first()
    await expect(healthCard).toBeVisible({ timeout: 15_000 })
    await expect(healthCard.getByText(/已用空间|Used/i)).toBeVisible({ timeout: 15_000 })
    await expect(healthCard.getByText(/对象数|Objects/i)).toBeVisible()

    // 7b. 用户管理: 找到目标用户 → 停用
    const usersTable = page.locator('div', { hasText: /账号管理|Accounts/i }).first()
    await expect(usersTable.getByText(targetUser)).toBeVisible({ timeout: 15_000 })
    const row = usersTable.locator('tr', { hasText: targetUser })
    await row.getByRole('button', { name: /停用|Deactivate/i }).click()
    await expect(usersTable.getByText(/已停用账号|deactivated/i).first()).toBeVisible({ timeout: 10_000 })
    // 状态列变为停用
    await expect(row.getByText(/disabled|停用/i)).toBeVisible()

    // 7c. 重新启用
    await row.getByRole('button', { name: /启用|Reactivate/i }).click()
    await expect(usersTable.getByText(/已重新启用|reactivated/i).first()).toBeVisible({ timeout: 10_000 })

    session.assertNoErrors()
    await context.close()
  })
})

// ---------------------------------------------------------------------------
// 场景 8: 帮助中心 - 三个文档入口 + Markdown 渲染 + 章节跳转
// ---------------------------------------------------------------------------

test.describe('场景 8: 帮助中心', () => {
  test('三个一级文档入口、Markdown 表格/代码、章节跳转与返回', async ({ browser }) => {
    const username = uniqueName('es-help')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    // 顶部导航进入帮助中心(规范路由 /help, 无尾斜杠)
    await page.getByRole('navigation', { name: /项目列表|Projects/i }).getByText('帮助中心').click()
    await expect(page).toHaveURL(/\/help\/?$/)
    // 三个一级入口: 使用者指南 / 开发者指南 / 更新日志
    await expect(page.getByText('使用者指南').first()).toBeVisible()
    await expect(page.getByText('开发者指南').first()).toBeVisible()
    await expect(page.getByText('更新日志').first()).toBeVisible()

    // 进入使用者指南 → 快速开始
    await page.getByText('快速开始').first().click()
    // RR-P2-10: 规范 locale 为 zh-CN(manual/SUMMARY.zh-CN.md 登记), 不是 zh
    await expect(page).toHaveURL(/\/help\/zh-CN\/[^/]+/i)

    // Markdown 内容渲染(标题 / 代码块)
    const content = page.locator('.ies-help__content')
    await expect(content).toBeVisible()
    await expect(content.locator('.ies-help__title, .ies-help__h1').first()).toBeVisible({ timeout: 10_000 })
    await expect(content.locator('pre.ies-help__code, code').first()).toBeVisible()

    // 表格渲染: 快速开始无表格, 跳转到"架构宪法"(含 Markdown 表格)验证
    await page.getByText('架构宪法').first().click()
    await expect(page.locator('.ies-help__content table.ies-help__table').first()).toBeVisible({ timeout: 10_000 })

    // 章节跳转(下一页)
    const nextBtn = page.getByRole('link', { name: /下一页|Next/i })
    await expect(nextBtn).toBeVisible()
    const urlBefore = page.url()
    await nextBtn.click()
    await expect(page).not.toHaveURL(urlBefore)

    // 返回(上一页)
    const prevBtn = page.getByRole('link', { name: /上一页|Previous/i })
    await expect(prevBtn).toBeVisible()
    await prevBtn.click()
    await expect(page).toHaveURL(urlBefore)

    // 返回应用(顶部按钮)
    await page.getByRole('link', { name: /返回应用|Back to app/i }).click()
    await expect(page).toHaveURL(/\/$/)

    session.assertNoErrors()
    await context.close()
  })
})

// ---------------------------------------------------------------------------
// 场景 9: 帮助章节深链接 + 刷新
// ---------------------------------------------------------------------------

test.describe('场景 9: 帮助深链接', () => {
  test('直接打开章节 URL 并刷新仍停留在同一章节', async ({ browser }) => {
    const username = uniqueName('es-deeplink')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    // 从帮助中心进入一个具体章节拿到 URL
    await page.getByRole('navigation', { name: /项目列表|Projects/i }).getByText('帮助中心').click()
    await page.getByText('快速开始').first().click()
    const chapterUrl = page.url()
    // RR-P2-10: 规范 locale 为 zh-CN
    expect(chapterUrl).toMatch(/\/help\/zh-CN\//)

    // 新标签页直接打开深链接(未登录也可读, 帮助中心独立无外壳)
    const page2 = await context.newPage()
    await page2.goto(chapterUrl)
    await expect(page2).toHaveURL(chapterUrl)
    const headingBefore = await page2.locator('.ies-help__title').first().textContent()
    await page2.reload()
    await expect(page2).toHaveURL(chapterUrl)
    const headingAfter = await page2.locator('.ies-help__title').first().textContent()
    expect(headingAfter).toBe(headingBefore)

    session.assertNoErrors()
    await context.close()
  })
})

// ---------------------------------------------------------------------------
// 场景 10: 语言切换
// ---------------------------------------------------------------------------

test.describe('场景 10: 语言切换', () => {
  test('切换中文/英文, 帮助中心跟随语言', async ({ browser }) => {
    const username = uniqueName('es-lang')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    // 10a. 顶部语言切换按钮: zh → EN
    const langBtn = page.getByRole('button', { name: /语言|Language/i })
    await expect(langBtn).toContainText('EN') // zh 下显示 EN
    await langBtn.click()
    await expect(page.getByText(/Projects/i).first()).toBeVisible()
    await expect(page.getByRole('button', { name: /New project/i }).first()).toBeVisible()

    // 10b. 帮助中心(界面 en, 但 manifest 只有 zh-CN): 应提示可用语言, 不静默回退
    await page.getByRole('navigation', { name: /Projects/i }).getByText('Help Center').click()
    await expect(page).toHaveURL(/\/help\/?$/)
    // 缺失翻译提示明确显示可用语言(ies.help.locale_missing)
    await expect(page.getByText(/可用语言|available languages/i).first()).toBeVisible({ timeout: 10_000 })
    // 不出现空内容冒充英文版(目录不存在时无 tree)
    await expect(page.getByText('User Guide').first()).not.toBeVisible()
    await expect(page.getByText('Developer Guide').first()).not.toBeVisible()

    session.assertNoErrors()
    await context.close()
  })
})

// ---------------------------------------------------------------------------
// 场景 11: 视口与键盘焦点
// ---------------------------------------------------------------------------

test.describe('场景 11: 视口与键盘', () => {
  test('桌面视口下项目列表键盘可达', async ({ browser }) => {
    const username = uniqueName('es-kbd')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    // 新建项目(创建后列表有行)
    const projectName = uniqueName('QA 键盘项目')
    await page.getByRole('button', { name: /新建项目|New project/i }).first().click()
    await page.getByLabel(/名称|Name/i).fill(projectName)
    await page.getByRole('button', { name: /确认|Confirm/i }).click()
    await expect(page.getByText(projectName)).toBeVisible()

    // 键盘焦点: Tab 到行(行 tabIndex=0), Enter 打开
    const row = page.locator('tr', { hasText: projectName })
    await row.focus()
    await expect(row).toBeFocused()
    await page.keyboard.press('Enter')
    await expect(page).toHaveURL(/\/projects\/\d+$/)

    // 返回列表, 验证登录表单/对话框键盘可达
    await page.goto('/')
    await page.getByRole('button', { name: /新建项目|New project/i }).focus()
    await page.keyboard.press('Enter')
    await expect(page.getByLabel(/名称|Name/i)).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(page.getByLabel(/名称|Name/i)).not.toBeVisible()

    session.assertNoErrors()
    await context.close()
  })

  test('移动视口下页面可滚动与导航', async ({ browser }) => {
    // 独立 context: 移动视口
    const context: BrowserContext = await browser.newContext({
      viewport: { width: 390, height: 844 },
      userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15',
    })
    const page: Page = await context.newPage()
    const session = await createSession(page, 'admin', process.env.E2E_ADMIN_PASSWORD ?? 'Iesplan-Admin-2026!')

    // 登录后项目列表在移动视口可见(顶部导航可点击)
    await expect(page.getByRole('navigation').first()).toBeVisible()
    // 打开帮助中心(移动端布局)
    await page.goto('/help/zh/')
    await expect(page.locator('.ies-help').first()).toBeVisible({ timeout: 10_000 })
    // 页面可滚动(内容高度 > 视口)
    const scrollable = await page.evaluate(() => document.documentElement.scrollHeight > window.innerHeight)
    expect(scrollable).toBe(true)

    session.assertNoErrors()
    await context.close()
  })
})
