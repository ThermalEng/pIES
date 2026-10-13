// R7 导出先评估再导出: Excel 导出不再隐式创建 full 评估。
//
// 现实链约束(后端证据提交同事务必写系统评估,
// backend/iesplan/application/worker/attempt_cases.py 步骤 5):
// 新鲜求解恒带评估, “空列表”在真实链上不可达, 故不伪造空列表。
// A(结果页入口, 真实链): 导出成功、零 assessment POST,
//   Excel 请求携带评估历史最新一条 id(引用既有, 不新建)。
// B(独立导出页入口, 定向失败注入): 评估列表 GET 500 时导出显示真实
//   错误而非“缺评估”引导, 零 assessment POST、零 Excel POST、无下载。
//   （旧代码把该失败吞成缺评估引导；失败注入沿 R3/R4/D3 定向先例，
//   只模拟传输失败，不伪造业务状态。）
// 准备走公开 API(脚手架＋真实求解); 被测点击/导出走 GUI, API 正文不 mock。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

interface TaskListItem {
  id: number
}

interface PostRecord {
  url: string
  body: string
}

function watchExportPosts(page: Page): PostRecord[] {
  const posts: PostRecord[] = []
  page.on('request', (req) => {
    if (req.method() !== 'POST') return
    if (req.url().includes('/result/assess') || req.url().includes('/exports/excel')) {
      posts.push({ url: req.url(), body: req.postData() ?? '' })
    }
  })
  return posts
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

/** 单账号全流程脚手架: 公开 API 建项目(真实求解在 GUI 提交后发生)。 */
async function scaffoldSolvedBase(
  browser: import('@playwright/test').Browser,
  tag: string,
  projectName: string,
) {
  const soloUser = uniqueName(`es-r7-${tag}`)
  const soloPass = strongPassword('Init')
  const soloCtx = await browser.newContext({ baseURL: APP })
  await createEngineer(soloCtx.request, soloUser, soloPass)
  const soloToken = await loginToken(soloCtx.request, soloUser, soloPass)
  const soloPid = await scaffoldProject(
    requestJson, soloCtx.request, soloToken, projectName, `r7${tag}-hp-${Date.now()}`,
  )
  const pre = await requestJson(soloCtx.request, 'POST', `/api/projects/${soloPid}/validation/run`, {}, soloToken)
  expect(
    (pre.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking),
  ).toEqual([])
  return { soloCtx, soloUser, soloPass, soloToken, soloPid }
}

test.describe('R7: 导出先评估再导出', () => {
  test.setTimeout(900_000)

  test('A: 结果页导出引用既有最新评估、不新建', async ({ browser }) => {
    const projectName = uniqueName('R7导出A项目')
    const { soloCtx, soloUser, soloPass, soloToken, soloPid } = await scaffoldSolvedBase(browser, 'a', projectName)

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, soloUser, soloPass)
    const posts = watchExportPosts(page)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+$/)
    await submitEvaluationViaUI(page)
    await waitCompleted(page)
    await openResults(page)

    // 导出成功, 且全程无 assessment POST(引用既有, 不新建)。
    const exportCard = page.locator('section.ies-card').filter({
      has: page.getByRole('heading', { name: '导出', exact: true }),
    })
    const downloadPromise = page.waitForEvent('download', { timeout: 120_000 })
    await exportCard.getByRole('button', { name: '导出 Excel', exact: true }).click({ timeout: 30_000 })
    const download = await downloadPromise
    expect(download.suggestedFilename()).toMatch(/\.xlsx$/)
    expect(posts.filter((p) => p.url.includes('/result/assess'))).toEqual([])
    expect(await exportCard.getByText(/尚无系统评估记录/).count()).toBe(0)

    // Excel 请求携带评估历史最新一条 id。
    const excelPosts = posts.filter((p) => p.url.includes('/exports/excel'))
    expect(excelPosts.length).toBe(1)
    const sentAssessmentId = (JSON.parse(excelPosts[0].body) as { assessment_id: number }).assessment_id
    expect(sentAssessmentId).toBeGreaterThan(0)
    // GUI 登录后旧 API token 已失效, 重登拿新 token(既有用例同模式)。
    const soloToken2 = await loginToken(soloCtx.request, soloUser, soloPass)
    const listed = await requestJson(soloCtx.request, 'GET', `/api/projects/${soloPid}/tasks?limit=10`, undefined, soloToken2)
    const taskId: number = ((listed.items ?? []) as TaskListItem[])[0].id
    const assessments = await requestJson(
      soloCtx.request, 'GET', `/api/projects/${soloPid}/tasks/${taskId}/result/assessments`, undefined, soloToken2,
    )
    const latestId: number = (assessments.items ?? [])[0]?.id
    expect(latestId).toBe(sentAssessmentId)

    await soloCtx.close()
    session.assertNoErrors()
    await context.close()
  })

  test('B: 评估列表读取失败不吞成缺评估引导', async ({ browser }) => {
    const projectName = uniqueName('R7导出B项目')
    const { soloCtx, soloUser, soloPass, soloPid } = await scaffoldSolvedBase(browser, 'b', projectName)

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, soloUser, soloPass)
    const posts = watchExportPosts(page)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+$/)
    await submitEvaluationViaUI(page)
    await waitCompleted(page)

    // 独立导出页, 仅对导出触发的评估列表 GET 定向 500。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/export$/)
    await context.route(
      (url) => url.pathname.includes('/result/assessments'),
      async (route) => {
        await route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: '{"detail":"r7 injected assessments failure"}',
        })
      },
    )
    await page.getByRole('button', { name: '导出 Excel 报告', exact: true }).click({ timeout: 30_000 })

    // 显示真实错误而非缺评估引导, 零创建/导出 POST、无下载。
    await expect(page.getByText(/导出失败/).first()).toBeVisible({ timeout: 30_000 })
    expect(await page.getByText(/尚无系统评估记录|导出不会自动创建评估/).count()).toBe(0)
    expect(posts.filter((p) => p.url.includes('/result/assess'))).toEqual([])
    expect(posts.filter((p) => p.url.includes('/exports/excel'))).toEqual([])

    // 定向注入的 500 按真实任务 id 精确计数(真实错误, 不得吞成缺评估)。
    const soloTokenB = await loginToken(soloCtx.request, soloUser, soloPass)
    const listedB = await requestJson(soloCtx.request, 'GET', `/api/projects/${soloPid}/tasks?limit=10`, undefined, soloTokenB)
    const taskIdB: number = ((listedB.items ?? []) as TaskListItem[])[0].id
    expect(taskIdB).toBeTruthy()
    await soloCtx.close()
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${soloPid}/tasks/${taskIdB}/result/assessments`, status: 500, times: 1 },
    ])
    await context.close()
  })
})
