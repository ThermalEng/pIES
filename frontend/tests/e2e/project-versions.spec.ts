// S1 版本历史 GUI 验收: 创建/列表/详情 + 归档拒收失败路径。
//
// 脚手架(用户/项目)走公开 API, 版本创建与详情的被测动作必须经 GUI 完成,
// 不 mock、不注库、不注前端状态。详情断言只覆盖版本元数据(名称/原因/
// 来源草稿修订/基线), 不声称完整模型内容。恢复/应用/导入仍未交付, 不测。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP } from './financial-scaffold'

async function openVersions(page: Page, projectName: string): Promise<void> {
  await page.getByText(projectName).click({ timeout: 30_000 })
  await expect(page).toHaveURL(/\/projects\/\d+/)
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('版本历史').click({ timeout: 30_000 })
  await expect(page).toHaveURL(/\/versions/)
}

async function createVersionViaUI(page: Page, name: string, description: string): Promise<void> {
  await page.getByRole('button', { name: '创建版本' }).first().click({ timeout: 30_000 })
  await page.locator('#version-name').fill(name)
  await page.locator('#version-description').fill(description)
  const submit = page.getByRole('button', { name: /^创建$/ })
  await expect(submit).toBeEnabled({ timeout: 10_000 })
  await submit.click({ timeout: 30_000 })
}

test.describe('S1: 项目版本创建/列表/详情', () => {
  test.setTimeout(600_000)

  test('创建两版本: 递增编号/原因/来源修订共存, 详情与刷新持久', async ({ browser }) => {
    const username = uniqueName('es-ver')
    const password = strongPassword('Init')
    const projectName = uniqueName('版本S1项目')
    const apiCtx = await browser.newContext({ baseURL: APP })
    const created = await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    const res = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2025,
      baseline_scenario_mode: 'single',
    }, token)
    const pid: number = res.project?.id ?? res.id
    expect(pid).toBeTruthy()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await openVersions(page, projectName)

    // 空列表态(请求成功且无版本)。
    await expect(page.getByText('尚无版本')).toBeVisible({ timeout: 30_000 })

    // GUI 创建 v1。
    await createVersionViaUI(page, 'v1 基线', '首个版本')
    const row1 = page.locator('.ies-table__body .ies-table__row', { hasText: 'v1 基线' })
    await expect(row1).toBeVisible({ timeout: 30_000 })
    await expect(row1.getByText('v1', { exact: true })).toBeVisible()
    await expect(row1.getByText('manual_save')).toBeVisible()
    await expect(row1.getByText('r1', { exact: true })).toBeVisible()
    await expect(page.getByText('当前版本')).toBeVisible()
    await expect(page.locator('.wb-header__meta').getByText(/版本: v1/)).toBeVisible()

    // GUI 创建 v2: 旧版本不消失, 当前版本前移。
    await createVersionViaUI(page, 'v2 调整', '第二个版本')
    const row2 = page.locator('.ies-table__body .ies-table__row', { hasText: 'v2 调整' })
    await expect(row2).toBeVisible({ timeout: 30_000 })
    await expect(row2.getByText('v2', { exact: true })).toBeVisible()
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(2)
    await expect(row2.getByText('当前版本')).toBeVisible()
    await expect(page.locator('.wb-header__meta').getByText(/版本: v2/)).toBeVisible()

    // 详情: v1 元数据(名称/原因/来源修订/基线), 非完整模型。
    await row1.click({ timeout: 30_000 })
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible({ timeout: 10_000 })
    await expect(dialog.getByText('v1 基线')).toBeVisible()
    await expect(dialog.getByText('manual_save')).toBeVisible()
    await expect(dialog.getByText(/草稿修订 r1/)).toBeVisible()
    await expect(dialog.getByText(/1h/)).toBeVisible()
    await dialog.getByRole('button', { name: '关闭' }).last().click()

    // 刷新/重新打开可持久读取; HTTP 同源一致。
    await page.reload()
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(2, { timeout: 30_000 })
    const token2 = await loginToken(apiCtx.request, username, password)
    const listed = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/versions`, undefined, token2)
    const nos = (listed.versions as Array<{ version_no: number; reason: string }>).map((v) => v.version_no).sort()
    expect(nos).toEqual([1, 2])
    expect(listed.versions.every((v: { reason: string }) => v.reason === 'manual_save')).toBe(true)
    const detail = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/versions/${listed.versions[0].id}`, undefined, token2)
    expect(detail.version.created_by).toBe(created.id)
    expect(typeof detail.version.created_at).toBe('string')

    await apiCtx.close()
    // 预期内噪声: 工作台经任务页落地时读空白项目配置一次(公开契约,
    // 与 config-revision.spec 同源); 其余仍零容忍。
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${pid}/config`, status: 404, times: 1 },
    ])
    await context.close()
  })

  test('归档项目创建拒收: 后端错误可见, 不新增版本', async ({ browser }) => {
    const username = uniqueName('es-verrej')
    const password = strongPassword('Init')
    const projectName = uniqueName('版本拒收项目')
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
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/archive`, {}, token)

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await openVersions(page, projectName)
    await expect(page.getByText('尚无版本')).toBeVisible({ timeout: 30_000 })

    await page.getByRole('button', { name: '创建版本' }).first().click({ timeout: 30_000 })
    await page.locator('#version-name').fill('不应成功')
    await page.getByRole('button', { name: /^创建$/ }).click({ timeout: 30_000 })
    // 后端拒收必须可见, 对话框保留输入不关闭。
    await expect(page.getByText('创建版本失败')).toBeVisible({ timeout: 30_000 })
    await expect(page.locator('#version-name')).toHaveValue('不应成功')
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(0)

    await apiCtx.close()
    // 预期内噪声(与 config-revision.spec 同源):
    // - 工作台经任务页落地时读空白项目配置一次, “尚未保存”404(公开契约);
    // - 归档项目创建版本被后端拒收一次(公开 ConflictError 契约)。
    // 其余任何 console/pageerror/失败请求仍零容忍。
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${pid}/config`, status: 404, times: 1 },
      { method: 'POST', path: `/api/projects/${pid}/versions`, status: 409, times: 1 },
    ])
    await context.close()
  })
})
