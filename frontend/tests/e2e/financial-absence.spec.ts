// 0.12 缺席验收(A/B): 既有 financial_absence 状态的真实浏览器验收。
//
// A: outputs=["metrics"] 的真实合法任务 → 真实 Worker 完成 → 结果页明确
//    “未请求 solution 所以未评价”, 不显示封存财务卡/零现金流/“评价通过”;
//    HTTP financial_evaluation=null、financial_absence=solution_not_requested。
// B: 合法 capacity_limit 约束 pin(变量上界 10 倍, 公开装配通过)→ 真实
//    HiGHS 不可行 → 业务结局无推荐方案 → 结果页显示非正常原因, 财务不伪装
//    完成; HTTP 与页面一致。不是非法装配 422, 不 mock、不注库、不注状态。
// C(missing_unknown): 不人为破坏可信封存流程, 无浏览器场景, 保留后端定向
//    派生测试(见 test_results_api.py)及本文件注释的未覆盖说明。
//
// 脚手架复用 financial-scaffold(与 T3 同源); 评价确认/提交/结果走 UI。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

interface TaskListItem {
  id: number
}

async function submitEvaluationViaUI(page: Page): Promise<void> {
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
  await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
  await page.locator('#task-type').selectOption('optimization')
  await page.locator('#task-construction-period').fill('1')
  await page.locator('#task-operation-years').fill('2')
  await page.locator('#task-discount-rate').fill('0.08')
  await page.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
  await page.locator('#task-maintenance-expenses').fill('[]')
  const submitBtn = page.getByRole('button', { name: /提交任务|Submit task/i }).last()
  await expect(submitBtn).toBeEnabled({ timeout: 10_000 })
  await submitBtn.click({ timeout: 30_000 })
  await expect(page.locator('.ies-table__row').first()).toBeVisible({ timeout: 15_000 })
}

async function waitCompleted(page: Page): Promise<void> {
  await page.locator('.ies-table__row').first().click({ timeout: 30_000 })
  await expect(page.locator('.ies-task-detail')).toBeVisible({ timeout: 10_000 })
  const detailBadge = page.locator('.ies-task-detail .ies-badge__label').first()
  await expect(async () => {
    const text = (await detailBadge.textContent()) ?? ''
    expect(text).toMatch(/已完成|Completed/i)
  }).toPass({ timeout: 240_000, intervals: [10_000] })
}

async function openResults(page: Page): Promise<void> {
  const viewResults = page.getByRole('link', { name: /查看结果|View results/i })
  await expect(viewResults).toBeVisible({ timeout: 10_000 })
  await viewResults.click({ timeout: 30_000 })
  await expect(page).toHaveURL(/\/results/)
}

test.describe('缺席验收: 未评价不伪装通过', () => {
  test.setTimeout(900_000)

  test('A: metrics-only 完成→未请求 solution 所以未评价', async ({ browser }) => {
    const username = uniqueName('es-absa')
    const password = strongPassword('Init')
    const projectName = uniqueName('缺席A项目')
    const profileId = `absa-hp-${Date.now()}`
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, profileId, {
      outputs: ['metrics'],
    })
    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, token)
    const blockers = (precheck.report?.diagnostics ?? []).filter(
      (d: { blocking: boolean }) => d.blocking,
    )
    expect(blockers).toEqual([])

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+$/)
    await submitEvaluationViaUI(page)
    await waitCompleted(page)
    await openResults(page)

    // 页面: 缺席说明出现, 封存财务卡与现金流明细不出现。
    await expect(page.getByText(/快照未请求 solution/).first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(/评价现金流\(封存\)|Sealed evaluation/i)).toHaveCount(0)
    await expect(page.getByText(/CF\[t=/)).toHaveCount(0)

    // HTTP 与页面一致。
    const token2 = await loginToken(apiCtx.request, username, password)
    const listed = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks?limit=10`, undefined, token2)
    const items = (listed.items ?? []) as TaskListItem[]
    const taskId: number = items[0].id
    expect(taskId).toBeTruthy()
    const result = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, token2,
    )
    expect(result.result.task.business_outcome).toBe('normal_completion')
    expect(result.result.financial_evaluation).toBeNull()
    expect(result.result.financial_absence).toBe('solution_not_requested')

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })

  test('B: 真实不可行→无推荐方案→财务不伪装完成', async ({ browser }) => {
    const username = uniqueName('es-absb')
    const password = strongPassword('Init')
    const projectName = uniqueName('缺席B项目')
    const profileId = `absb-hp-${Date.now()}`
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    // 合法公开约束: 容量变量被 pin 到上界 10 倍, 装配通过、求解不可行。
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, profileId, {
      planningConstraints: {
        e2e_inf_pin: { type: 'capacity_limit', expression: 'hp_cap >= 10000 kW' },
      },
    })
    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, token)
    const blockers = (precheck.report?.diagnostics ?? []).filter(
      (d: { blocking: boolean }) => d.blocking,
    )
    expect(blockers).toEqual([])

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+$/)
    await submitEvaluationViaUI(page)
    await waitCompleted(page)
    await openResults(page)

    // 页面: 真实任务结局“无推荐方案”徽章 + 非正常缺席说明; 无封存卡。
    await expect(page.getByText('无推荐方案').first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(/未产生财务评价/).first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(/评价现金流\(封存\)|Sealed evaluation/i)).toHaveCount(0)
    await expect(page.getByText(/CF\[t=/)).toHaveCount(0)

    // HTTP 与页面一致。
    const token2 = await loginToken(apiCtx.request, username, password)
    const listed = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks?limit=10`, undefined, token2)
    const items = (listed.items ?? []) as TaskListItem[]
    const taskId: number = items[0].id
    expect(taskId).toBeTruthy()
    const result = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, token2,
    )
    expect(result.result.task.business_outcome).toBe('no_recommendation')
    expect(result.result.financial_evaluation).toBeNull()
    expect(result.result.financial_absence).toBe('outcome_not_normal')

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })
})
