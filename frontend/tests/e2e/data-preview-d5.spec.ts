// D5 数据集预览无行级能力明示: 后端详情只返版本元数据, 前端不得用
// 空表/零行数冒充真实空数据集。预览对话框必须明确提示暂不可用,
// 仅展示真实版本字段信息。
//
// 准备走公开 API(建项目/数据集/真实采样版本, 无求解); 被测的预览打开
// 与展示走 GUI, API 正文不 mock。字段名以同一版本响应为准逐字对照。

import { test, expect } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP } from './financial-scaffold'

test.describe('D5: 数据预览无行级能力明示', () => {
  test.setTimeout(300_000)

  test('预览入口提示暂不可用、不显示空表、字段与版本响应一致', async ({ browser }) => {
    const userName = uniqueName('es-d5-doer')
    const password = strongPassword('Init')
    const projectName = uniqueName('D5预览项目')
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
    const dsName = uniqueName('D5预览集')
    const ds = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/datasets`, {
      name: dsName,
    }, userToken)
    const dsId: number = ds.dataset?.id ?? ds.id
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/datasets/${dsId}/sample?resolution=1h`, {}, userToken)
    const detail = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/datasets/${dsId}`, undefined, userToken)
    const ver = (detail.versions ?? [])[0]
    expect(ver?.id).toBeTruthy()
    const fieldNames = Object.keys(ver.fields ?? {})
    expect(fieldNames.length).toBeGreaterThan(0)

    const context = await browser.newContext()
    const page = await context.newPage()
    const session = await createSession(page, userName, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/data$/)
    await expect(page.getByText(dsName)).toBeVisible({ timeout: 60_000 })

    // 真实入口打开预览。
    const dsRow = page.locator('tr', { hasText: dsName })
    await dsRow.getByRole('button', { name: '数据预览' }).click({ timeout: 30_000 })
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible({ timeout: 30_000 })

    // 暂不可用说明明确可见, 对话框内无表格、无行数宣称。
    await expect(dialog.getByText(/数据预览暂不可用/).first()).toBeVisible({ timeout: 30_000 })
    expect(await dialog.locator('table').count()).toBe(0)
    expect(await dialog.getByText('行数').count()).toBe(0)

    // 版本字段信息与同一公开版本响应一致(取首个字段名逐字对照)。
    await expect(dialog.getByText('版本字段').first()).toBeVisible({ timeout: 30_000 })
    await expect(dialog.getByText(fieldNames[0]).first()).toBeVisible({ timeout: 30_000 })

    await apiCtx.close()
    session.assertNoErrors([
      // 进项目读尚无保存的计算配置 404(既有行为, 计数锁定防漂移)。
      { method: 'GET', path: `/api/projects/${pid}/config`, status: 404, times: 1 },
    ])
    await context.close()
  })
})
