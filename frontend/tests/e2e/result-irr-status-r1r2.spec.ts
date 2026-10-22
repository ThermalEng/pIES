// R1/R2 结果页 IRR 状态来源收口验收: 旧摘要徽章不再从诊断猜测;
// 指标摘要缺席时封存卡承载唯一 IRR 状态展示, 与同一封存
// evaluation.irr_status 精确一致；无财务块时不展示状态。
//
// A(指标摘要缺席时封存 IRR 仍正常展示): 公开 API 脚手架＋UI 确认/提交→
//   真实 Worker 求解→结果页, HTTP metrics_summary=null、旧摘要财务卡不
//   显示, 封存卡出现且其 IRR 状态标签精确等于 HTTP 封存 irr_status 的
//   已知标签(未知状态直接失败, 不猜测), 该标签全页仅一处。
// B(无块不伪造): outputs=['metrics'] 真实任务→结果页无“IRR 计算状态”行,
//   HTTP financial_evaluation=null 且缺席原因一致。
// 求解/页面走真实链路, 不注入状态、不 mock 业务、不注库。D1/D2 不重跑。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

interface TaskListItem {
  id: number
}

/** 已知封存 irr_status → 页面徽章标签(测试 oracle；未知状态即失败)。 */
const IRR_LABEL: Record<string, string> = {
  normal: '唯一解',
  no_solution: '无解',
  multiple: '多解',
  degenerate: '退化',
  out_of_domain: '超出定义域',
  numeric_failure: '数值失败',
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

/**
 * 当前浏览器会话(cookie)下的只读 GET。
 *
 * 不再二次登录: 登录按窗口接管规则撤销旧会话, 会使 UI 会话失效
 * (结果页跳登录页, 封存卡定位不到)。这里复用页面已登录的会话取
 * 同一 HTTP oracle, 断言与页面同源。
 */
async function sessionGet(page: Page, path: string): Promise<any> {
  const res = await page.request.get(`${APP}${path}`)
  const text = await res.text()
  if (!res.ok()) {
    throw new Error(`GET ${path} -> ${res.status()}: ${text.slice(0, 300)}`)
  }
  return text ? JSON.parse(text) : null
}

test.describe('R1/R2: IRR 状态走封存字段', () => {
  test.setTimeout(900_000)

  test('A: 指标摘要缺席时封存 IRR 仍正常展示', async ({ browser }) => {
    const username = uniqueName('es-irr-a')
    const password = strongPassword('Init')
    const projectName = uniqueName('IRR同源项目')
    const profileId = `irra-hp-${Date.now()}`
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, profileId)
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

    // 封存卡出现(旧摘要卡缺席, 由下方 HTTP metrics_summary=null 与
    // 页面卡计数钉死)。
    await expect(page.getByText(/评价现金流\(封存\)|Sealed evaluation/i).first()).toBeVisible({ timeout: 30_000 })

    // HTTP 封存 irr_status 是唯一依据；未知状态直接失败。
    // 取 oracle 复用页面已登录的会话(不再二次登录, 免撤销 UI 会话)。
    const listed = await sessionGet(page, `/api/projects/${pid}/tasks?limit=10`)
    const items = (listed.items ?? []) as TaskListItem[]
    const taskId: number = items[0].id
    expect(taskId).toBeTruthy()
    const result = await sessionGet(page, `/api/projects/${pid}/tasks/${taskId}/result`)
    const irrStatus: string = result.result.financial_evaluation?.evaluation?.irr_status
    expect(typeof irrStatus).toBe('string')
    const expectedLabel = IRR_LABEL[irrStatus]
    expect(expectedLabel, `未知封存 irr_status: ${irrStatus}`).toBeTruthy()
    // 指标摘要缺席是当前内置计算链的真实契约(runner 只发布 compute_result 块);
    // 旧摘要卡因此整块不渲染, 不属于缺陷也不补默认。
    expect(result.result.metrics_summary).toBeNull()

    // 旧摘要卡整块缺席; 封存卡是唯一展示处, 其 IRR 状态标签与 HTTP 封存
    // facts 精确一致, 全页仅一处该标签。
    const summaryCard = page.locator('section.ies-card').filter({
      has: page.getByRole('heading', { name: '财务', exact: true }),
    })
    await expect(summaryCard).toHaveCount(0)
    const sealedCard = page.locator('section.ies-card').filter({
      has: page.getByRole('heading', { name: /评价现金流\(封存\)|Sealed evaluation/i }),
    })
    await expect(sealedCard).toBeVisible({ timeout: 30_000 })
    await expect(sealedCard.getByText('IRR 计算状态')).toBeVisible({ timeout: 30_000 })
    await expect(sealedCard.getByText(expectedLabel!).first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(expectedLabel!)).toHaveCount(1)

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })

  test('B: 无财务块时不展示 IRR 状态', async ({ browser }) => {
    const username = uniqueName('es-irr-b')
    const password = strongPassword('Init')
    const projectName = uniqueName('IRR无块项目')
    const profileId = `irrb-hp-${Date.now()}`
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

    // 缺席说明出现, 封存卡不出现, 全页无 IRR 状态行(不补默认)。
    await expect(page.getByText(/快照未请求 solution/).first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(/评价现金流\(封存\)|Sealed evaluation/i)).toHaveCount(0)
    expect(await page.getByText('IRR 计算状态').count()).toBe(0)

    // HTTP 与页面一致(复用页面会话, 不二次登录)。
    const listed = await sessionGet(page, `/api/projects/${pid}/tasks?limit=10`)
    const items = (listed.items ?? []) as TaskListItem[]
    const taskId: number = items[0].id
    expect(taskId).toBeTruthy()
    const result = await sessionGet(page, `/api/projects/${pid}/tasks/${taskId}/result`)
    expect(result.result.financial_evaluation).toBeNull()
    expect(result.result.financial_absence).toBe('solution_not_requested')

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })
})
