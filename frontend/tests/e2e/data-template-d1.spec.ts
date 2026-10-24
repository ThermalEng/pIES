// D1 数据模板下载失败不得本地 fallback 伪成功。
//
// 成功路径真实不 mock: 模板字节以后端公开端点实际响应为准,
// 下载文件与端点响应全文比对。failure 仅用定向 HTTP 500 拦截
// `/api/datasets/template`; 不注库、不注入页面状态、不 mock 成功正文。
// 无需求解与财务, 只建空项目进数据页。

import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP } from './financial-scaffold'

test.describe('D1: 模板下载失败可见', () => {
  test.setTimeout(300_000)

  test('失败显错零下载无成功通知、重试下载与端点一致', async ({ browser }) => {
    const userName = uniqueName('es-d1-doer')
    const password = strongPassword('Init')
    const projectName = uniqueName('D1模板项目')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)
    const created = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2026,
      baseline_scenario_mode: 'single',
    }, userToken)
    const pid: number = created.project?.id ?? created.id
    expect(pid).toBeTruthy()

    const templatePath = '/api/datasets/template'
    // 单一拦截器按标志失败/放行: 失败只注入真实 500, 放行走真实响应。
    let failTemplate = false
    const context = await browser.newContext()
    const page = await context.newPage()
    await context.route(
      (url) => url.pathname === templatePath,
      (route) => failTemplate
        ? route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: '{"detail":"d1 injected template failure"}',
        })
        : route.fallback(),
    )
    // 全程写请求: 模板下载与重试只许 GET, 不许上传/版本写入 POST。
    const writePosts: string[] = []
    page.on('request', (req) => {
      if (req.method() === 'POST' && /\/(datasets|versions|draft|tasks)/.test(req.url())) {
        writePosts.push(`${req.method()} ${req.url()}`)
      }
    })
    const downloads: string[] = []
    page.on('download', (dl) => downloads.push(dl.url))
    const session = await createSession(page, userName, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/data$/)

    // 阶段一: 模板 500 → 明确错误、无成功通知、零下载、按钮可再操作。
    const templateBtn = page.getByRole('button', { name: '下载模板' })
    failTemplate = true
    await templateBtn.click({ timeout: 30_000 })
    const errorAlert = page.locator('.ies-alert--error').first()
    await expect(errorAlert).toBeVisible({ timeout: 30_000 })
    expect(await page.getByText('模板已下载').count()).toBe(0)
    await expect(templateBtn).toBeEnabled({ timeout: 30_000 })
    await page.waitForTimeout(2000)
    expect(downloads).toEqual([])
    expect(writePosts).toEqual([])

    // 阶段二: 解除故障后重试 → 真实下载, 内容与端点实际响应全文一致。
    failTemplate = false
    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 60_000 }),
      templateBtn.click({ timeout: 30_000 }),
    ])
    await expect(page.getByText('模板已下载')).toBeVisible({ timeout: 30_000 })
    const downloadPath = await download.path()
    expect(downloadPath).toBeTruthy()
    const expected = await apiCtx.request.get(`${APP}${templatePath}?resolution=1h`)
    expect(expected.ok()).toBe(true)
    expect(readFileSync(downloadPath as string, 'utf-8')).toBe(await expected.text())
    expect(writePosts).toEqual([])

    // 预期内噪声: 模板 500×1; 空项目数据页读尚无保存的计算配置 404
    // (D1 外既有行为, 计数锁定防漂移)。
    session.assertNoErrors([
      { method: 'GET', path: templatePath, status: 500, times: 1 },
      { method: 'GET', path: `/api/projects/${pid}/config`, status: 404, times: 1 },
    ])
    await apiCtx.close()
    await context.close()
  })
})
