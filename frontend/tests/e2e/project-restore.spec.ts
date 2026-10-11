// S2 恢复历史版本 GUI 验收: 确认/取消/恢复内容/重复提交/归档拒收。
//
// 场景准备(两个可观察差异的草稿/版本)走公开 API,不注数据库:
// v1=仅光伏, v2=光伏+锅炉。被测恢复选择、确认、取消必须经 GUI 完成,
// 不 mock、不注前端状态。恢复后导航到模型页断言实际草稿内容(画布节点
// 文本只读不断言拖拽, 拖拽由人工验收), 证明不是同内容改名冒充。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext, Route } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP } from './financial-scaffold'

interface Prepared {
  pid: number
  v1Id: number
  v2Id: number
}

async function prepareTwoVersions(
  browser: import('@playwright/test').Browser,
  tag: string,
): Promise<{ apiCtx: import('@playwright/test').BrowserContext; username: string; password: string } & Prepared> {
  const username = uniqueName(tag)
  const password = strongPassword('Init')
  const projectName = uniqueName('恢复S2项目')
  const apiCtx = await browser.newContext({ baseURL: APP })
  await createEngineer(apiCtx.request, username, password)
  const token = await loginToken(apiCtx.request, username, password)
  const res = await requestJson(apiCtx.request, 'POST', '/api/projects', {
    name: projectName,
    baseline_resolution: '1h',
    baseline_start_year: 2025,
    baseline_scenario_mode: 'single',
  }, token)
  const pid: number = res.project?.id ?? res.id
  const draft = async (expectedRevision: number, name: string, deviceType: string, kind: string) =>
    requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: expectedRevision,
      commands: [
        { id: `cmd-${name}`, unit: 'model', type: 'model.upsert_device', payload: { name, device_type: deviceType, kind } },
      ],
    }, token)
  await draft(1, '光伏1', 'ies.device.pv', 'new')
  const v1 = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/versions`, { name: 'v1带光伏', reason: 'manual_save' }, token)
  await draft(2, '锅炉1', 'ies.device.gas_boiler', 'existing')
  const v2 = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/versions`, { name: 'v2带锅炉', reason: 'manual_save' }, token)
  return { apiCtx, username, password, pid, v1Id: v1.version.id, v2Id: v2.version.id }
}

async function openVersions(page: Page, pid: number): Promise<void> {
  await page.goto(`/projects/${pid}/versions`)
  await expect(page).toHaveURL(/\/versions/)
}

test.describe('S2: 恢复历史版本', () => {
  test.setTimeout(600_000)

  test('恢复 v1: 取消零POST, 确认产生v3并实际回滚草稿内容', async ({ browser }) => {
    // 本文件经直达路由进入版本页, 不挂载任务页, 无空白配置 404 噪声。
    const { apiCtx, username, password, pid, v1Id } = await prepareTwoVersions(browser, 'es-s2a')
    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    let restorePosts = 0
    await page.route(`**/api/projects/${pid}/versions/*/restore`, async (route: Route) => {
      if (route.request().method() === 'POST') restorePosts++
      await route.continue()
    })

    await openVersions(page, pid)
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(2, { timeout: 30_000 })
    const dialog = page.getByRole('dialog')
    const rowV1 = page.locator('.ies-table__body .ies-table__row', { hasText: 'v1带光伏' })

    // 取消: 影响提示可见, 零 POST, 版本数不变。
    await rowV1.click({ timeout: 30_000 })
    await expect(dialog).toBeVisible({ timeout: 10_000 })
    await dialog.getByRole('button', { name: '恢复此版本' }).click({ timeout: 10_000 })
    await expect(dialog.getByText(/将版本 v1/)).toBeVisible({ timeout: 10_000 })
    await expect(dialog.getByText(/当前可编辑草稿将被替换/)).toBeVisible()
    await expect(dialog.getByText(/已有版本、任务与结果保持不变/)).toBeVisible()
    await dialog.getByRole('button', { name: '取消' }).click({ timeout: 10_000 })
    await expect(dialog).toBeHidden({ timeout: 10_000 })
    expect(restorePosts).toBe(0)
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(2)

    // 确认: 产生 v3, reason=restore, parent=v1, 新草稿修订, 历史保留。
    await rowV1.click({ timeout: 30_000 })
    await expect(dialog).toBeVisible({ timeout: 10_000 })
    await dialog.getByRole('button', { name: '恢复此版本' }).click({ timeout: 10_000 })
    await dialog.getByRole('button', { name: '确认恢复' }).click({ timeout: 10_000 })
    await expect(dialog.getByText('恢复已完成')).toBeVisible({ timeout: 30_000 })
    expect(restorePosts).toBe(1)
    await dialog.getByRole('button', { name: '关闭' }).last().click({ timeout: 10_000 })
    await expect(dialog).toBeHidden({ timeout: 10_000 })

    const rowV3 = page.locator('.ies-table__body .ies-table__row', { hasText: '恢复: v1带光伏' })
    await expect(rowV3).toBeVisible({ timeout: 30_000 })
    await expect(rowV3.getByText('v3', { exact: true })).toBeVisible()
    await expect(rowV3.getByText('restore')).toBeVisible()
    await expect(rowV3.getByText('当前版本')).toBeVisible()
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(3)
    await expect(page.locator('.wb-header__meta').getByText(/版本: v3/)).toBeVisible()

    // 导航到模型页看到实际恢复内容(非旧缓存): 有光伏、无锅炉。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('系统建模').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/model/)
    await expect(page.getByText('光伏1').first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('锅炉1')).toHaveCount(0)

    // 刷新/重新进入仍保持。
    await page.goto(`/projects/${pid}/versions`)
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(3, { timeout: 30_000 })
    await expect(page.locator('.wb-header__meta').getByText(/版本: v3/)).toBeVisible()

    // HTTP 同源(放最后: 重新登录会接管浏览器会话, 之后不再做 GUI 动作):
    // v3 parent=v1, 新草稿修订, 草稿内容回滚到仅光伏。
    const token2 = await loginToken(apiCtx.request, username, password)
    const listed = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/versions`, undefined, token2)
    expect(listed.versions.map((v: { version_no: number }) => v.version_no).sort()).toEqual([1, 2, 3])
    const v3 = listed.versions.find((v: { version_no: number }) => v.version_no === 3)
    expect(v3.reason).toBe('restore')
    expect(v3.parent_version_id).toBe(v1Id)
    const view = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, token2)
    expect(view.draft.revision).toBe(4)
    const names = (view.draft.content.model.devices as Array<{ name: string }>).map((d) => d.name)
    expect(names).toEqual(['光伏1'])

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })

  test('恢复处理中不可重复提交', async ({ browser }) => {
    const { apiCtx, username, password, pid } = await prepareTwoVersions(browser, 'es-s2b')
    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    // 挂起恢复 POST(真实请求, 稍后放行), 计数实际发送次数。
    let restorePosts = 0
    let held: Route | null = null
    await page.route(`**/api/projects/${pid}/versions/*/restore`, async (route: Route) => {
      if (route.request().method() !== 'POST') {
        await route.continue()
        return
      }
      restorePosts++
      held = route
    })

    await openVersions(page, pid)
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(2, { timeout: 30_000 })
    const dialog = page.getByRole('dialog')
    // 恢复仍是当前版本的 v2(任何已保存版本都可恢复, 不回避)。
    await page.locator('.ies-table__body .ies-table__row', { hasText: 'v2带锅炉' }).click({ timeout: 30_000 })
    await expect(dialog).toBeVisible({ timeout: 10_000 })
    await dialog.getByRole('button', { name: '恢复此版本' }).click({ timeout: 10_000 })
    const confirm = dialog.getByRole('button', { name: '确认恢复' })
    await confirm.click({ timeout: 10_000 })
    // 处理中按钮禁用, 不可重复提交。
    await expect(confirm).toBeDisabled({ timeout: 10_000 })
    await expect(dialog.getByRole('button', { name: '取消' })).toBeDisabled()
    expect(held).toBeTruthy()
    await held!.continue()
    await expect(dialog.getByText('恢复已完成')).toBeVisible({ timeout: 30_000 })
    expect(restorePosts).toBe(1)
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(3, { timeout: 30_000 })

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })

  test('恢复仍是当前的v2: 撤回版本后未固化的草稿修改', async ({ browser }) => {
    // v2 仍是当前版本, 但其后草稿经公开 API 修改未再固化; 恢复 v2 必须允许,
    // 产生 v3 parent=v2, 后续修改消失(可观察内容证明)。
    const { apiCtx, username, password, pid, v2Id } = await prepareTwoVersions(browser, 'es-s2d')
    const token = await loginToken(apiCtx.request, username, password)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: 3,
      commands: [
        { id: 'cmd-fj', unit: 'model', type: 'model.upsert_device', payload: { name: '风机1', device_type: 'ies.device.pv', kind: 'new' } },
      ],
    }, token)

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await openVersions(page, pid)
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(2, { timeout: 30_000 })
    // v2 仍标当前版本, 但其恢复按钮必须可用。
    const rowV2 = page.locator('.ies-table__body .ies-table__row', { hasText: 'v2带锅炉' })
    await expect(rowV2.getByText('当前版本')).toBeVisible()
    await expect(page.locator('.wb-header__meta').getByText(/版本: v2/)).toBeVisible()
    const dialog = page.getByRole('dialog')
    await rowV2.click({ timeout: 30_000 })
    await expect(dialog).toBeVisible({ timeout: 10_000 })
    const restoreBtn = dialog.getByRole('button', { name: '恢复此版本' })
    await expect(restoreBtn).toBeEnabled({ timeout: 10_000 })
    await restoreBtn.click({ timeout: 10_000 })
    await dialog.getByRole('button', { name: '确认恢复' }).click({ timeout: 10_000 })
    await expect(dialog.getByText('恢复已完成')).toBeVisible({ timeout: 30_000 })
    await dialog.getByRole('button', { name: '关闭' }).last().click({ timeout: 10_000 })

    const rowV3 = page.locator('.ies-table__body .ies-table__row', { hasText: '恢复: v2带锅炉' })
    await expect(rowV3).toBeVisible({ timeout: 30_000 })
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(3)
    await expect(page.locator('.wb-header__meta').getByText(/版本: v3/)).toBeVisible()

    const token2 = await loginToken(apiCtx.request, username, password)
    const listed = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/versions`, undefined, token2)
    const v3 = listed.versions.find((v: { version_no: number }) => v.version_no === 3)
    expect(v3.reason).toBe('restore')
    expect(v3.parent_version_id).toBe(v2Id)
    const view = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, token2)
    expect(view.draft.revision).toBe(5)
    const names = (view.draft.content.model.devices as Array<{ name: string }>).map((d) => d.name)
    expect(names).toEqual(['光伏1', '锅炉1'])

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })

  test('恢复成功但摘要刷新失败: 已发生明确展示, 不诱导再提交', async ({ browser }) => {
    // 故障只注入恢复 POST 成功之后的一次项目摘要 GET(500 真实失败响应);
    // 列表 GET 直通成功。恢复 POST 必须恰 1 次。
    const { apiCtx, username, password, pid, v1Id } = await prepareTwoVersions(browser, 'es-s2e')
    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    let restorePosts = 0
    await page.route(`**/api/projects/${pid}/versions/*/restore`, async (route: Route) => {
      if (route.request().method() === 'POST') restorePosts++
      await route.continue()
    })
    let failSummaryOnce = false
    let summaryFailed = false
    await page.route(`**/api/projects/${pid}`, async (route: Route) => {
      const req = route.request()
      if (req.method() === 'GET' && failSummaryOnce && !summaryFailed) {
        summaryFailed = true
        await route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
        return
      }
      await route.continue()
    })

    await openVersions(page, pid)
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(2, { timeout: 30_000 })
    const dialog = page.getByRole('dialog')
    await page.locator('.ies-table__body .ies-table__row', { hasText: 'v1带光伏' }).click({ timeout: 30_000 })
    await expect(dialog).toBeVisible({ timeout: 10_000 })
    await dialog.getByRole('button', { name: '恢复此版本' }).click({ timeout: 10_000 })
    const restoreDone = page.waitForResponse(
      (res) => res.url().endsWith(`/api/projects/${pid}/versions/${v1Id}/restore`) && res.request().method() === 'POST',
      { timeout: 30_000 },
    )
    await dialog.getByRole('button', { name: '确认恢复' }).click({ timeout: 10_000 })
    await restoreDone
    // 其后的一次项目摘要 GET 注入失败, 列表 GET 仍成功。
    failSummaryOnce = true
    await expect(dialog.getByText('恢复已完成')).toBeVisible({ timeout: 30_000 })
    await expect(dialog.getByText(/摘要\/列表刷新失败/)).toBeVisible({ timeout: 10_000 })
    expect(restorePosts).toBe(1)
    // 恢复已发生: 列表仍成功刷新出 3 行, 不是空态; 头部摘要丢失可见。
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(3, { timeout: 30_000 })
    await expect(page.locator('.wb-header__meta')).toHaveCount(0)
    await dialog.getByRole('button', { name: '关闭' }).last().click({ timeout: 10_000 })

    // 解除拦截并重新加载: 真实新草稿/版本可见。
    await page.unrouteAll({ behavior: 'wait' })
    await page.reload()
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(3, { timeout: 30_000 })
    await expect(page.locator('.wb-header__meta').getByText(/版本: v3/)).toBeVisible({ timeout: 30_000 })

    await apiCtx.close()
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${pid}`, status: 500, times: 1 },
    ])
    await context.close()
  })

  test('归档项目恢复拒收: 错误可见, 无新增版本不切换草稿', async ({ browser }) => {
    const { apiCtx, username, password, pid, v1Id } = await prepareTwoVersions(browser, 'es-s2c')
    const token = await loginToken(apiCtx.request, username, password)
    const before = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, token)
    const beforeRevision: number = before.draft.revision
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/archive`, {}, token)

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await openVersions(page, pid)
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(2, { timeout: 30_000 })

    const dialog = page.getByRole('dialog')
    await page.locator('.ies-table__body .ies-table__row', { hasText: 'v1带光伏' }).click({ timeout: 30_000 })
    await expect(dialog).toBeVisible({ timeout: 10_000 })
    await dialog.getByRole('button', { name: '恢复此版本' }).click({ timeout: 10_000 })
    await dialog.getByRole('button', { name: '确认恢复' }).click({ timeout: 10_000 })
    await expect(dialog.getByText('恢复失败')).toBeVisible({ timeout: 30_000 })
    // 选择保留可重试, 对话框不关闭, 版本数不变。
    await expect(dialog.getByRole('button', { name: '重试' })).toBeVisible()
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(2)

    const token2 = await loginToken(apiCtx.request, username, password)
    const listed = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/versions`, undefined, token2)
    expect(listed.versions).toHaveLength(2)
    const view = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, token2)
    expect(view.draft.revision).toBe(beforeRevision)

    await apiCtx.close()
    session.assertNoErrors([
      { method: 'POST', path: `/api/projects/${pid}/versions/${v1Id}/restore`, status: 409, times: 1 },
    ])
    await context.close()
  })
})
