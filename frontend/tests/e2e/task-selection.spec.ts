// 全局任务选择：默认最新完成、无回退、显式提交才建任务。
//
// 裁决语义（本片实现，见 src/lib/taskSelection.ts）：
// - project_id 仅分组；任务/结果/导出页共享同一显式选择；
// - 无显式选择默认完成顺序最晚的已完成计算（与有无证据无关；其无结果即无结果）；
// - 显式选择未完成/无结果任务时展示无结果、Excel 禁用，永不回退他任务；
// - 选择任务即把其冻结输入载入项目草稿（评价确认与任务参数整体替换）；
//   对话框是草稿工作输入的可编辑镜像：编辑边改边存入草稿，关闭/切换页面/
//   重载不丢失；后续编辑只改草稿，提交才冻结为新任务；
// - 提交对话框不再做数据集选择（清单权威是数据页草稿绑定；旧勾选无消费者已删）；
// - 输入编辑、草稿暂存、选择切换、打开表单不创建任务，只有用户显式提交才 POST。
// 准备走公开 API（脚手架＋真实求解）；被测查看/选择/提交走 GUI，API 正文不 mock。
// 不测画布拖放。单次真实求解（T1），T2 只提交不等待完成。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

/** 记录任务创建 POST（POST /projects/{id}/tasks，排除 /cancel 与 GET；附请求正文供冻结断言）。 */
function watchTaskCreates(page: Page): Array<{ url: string; body: unknown }> {
  const creates: Array<{ url: string; body: unknown }> = []
  page.on('request', (req) => {
    if (req.method() !== 'POST') return
    if (/\/projects\/\d+\/tasks$/.test(new URL(req.url()).pathname)) {
      let body: unknown = null
      try {
        body = req.postDataJSON()
      } catch {
        body = null
      }
      creates.push({ url: req.url(), body })
    }
  })
  return creates
}

/** 任务列表卡片内的任务行（详情面板等处另有同类名表格行，需限定作用域；含表头行）。 */
function taskRows(page: Page) {
  return page
    .locator('section.ies-card')
    .filter({ has: page.getByRole('heading', { name: '任务列表' }) })
    .locator('.ies-table__row')
}

/** 按任务 id 点击任务行（表头行无 #id，不会被误中）。 */
async function clickTaskRow(page: Page, taskId: number): Promise<void> {
  await taskRows(page)
    .filter({ hasText: new RegExp(`#${taskId}(?!\\d)`) })
    .first()
    .click({ timeout: 30_000 })
}

/** 断言提交表单字段值（预填为异步到达，轮询等待）。 */
async function expectField(page: Page, selector: string, value: string): Promise<void> {
  await expect
    .poll(async () => page.locator(selector).inputValue(), { timeout: 30_000 })
    .toBe(value)
}

/**
 * 读项目草稿工作输入（GUI 上下文自带会话的 page.request，不新建登录，
 * 不触发会话接管；防抖落草稿的确定性等待点）。
 */
async function readDraftWorking(page: Page, projectId: number): Promise<Record<string, unknown> | null> {
  const resp = await page.request.get(`/api/projects/${projectId}`)
  if (!resp.ok()) return null
  const body = (await resp.json()) as Record<string, unknown>
  const draft = body.draft as Record<string, unknown> | null
  const content = draft?.content as Record<string, unknown> | null
  const working = content?.task_working_inputs
  return working !== null && typeof working === 'object' && !Array.isArray(working)
    ? (working as Record<string, unknown>)
    : null
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
  // 数据集版本清单唯一权威是脚手架经 API 绑定的草稿 dataset_bindings（v1）；
  // 对话框不再做数据集选择（旧勾选后端从未消费，已删除）。
  const submitBtn = page.getByRole('button', { name: /提交任务|Submit task/i }).last()
  await expect(submitBtn).toBeEnabled({ timeout: 10_000 })
  await submitBtn.click({ timeout: 30_000 })
  // 提交成功即关闭对话框（校验门禁/网络提交耗时不等，先等关闭再断言行）。
  await expect(page.getByRole('dialog')).toBeHidden({ timeout: 60_000 })
  await expect(taskRows(page).first()).toBeVisible({ timeout: 15_000 })
}

async function waitCompleted(page: Page): Promise<void> {
  await taskRows(page).first().click({ timeout: 30_000 })
  await expect(page.locator('.ies-task-detail')).toBeVisible({ timeout: 10_000 })
  const detailBadge = page.locator('.ies-task-detail .ies-badge__label').first()
  await expect(async () => {
    const text = (await detailBadge.textContent()) ?? ''
    expect(text).toMatch(/已完成|Completed/i)
  }).toPass({ timeout: 240_000, intervals: [10_000] })
}

/**
 * 从结果页摘要读当前绑定的任务/证据包 id。
 * 注意：测试中途不得调用 loginToken 等新建登录（会按接管语义使 GUI 会话失效，
 * 页面将被踢回登录页）；任务 id 一律从 UI 文本捕获。
 */
async function readSelectedIds(page: Page): Promise<{ taskId: number; packageId: number }> {
  const header = page.getByText(/任务\s*#\d+\s*·\s*证据包\s*#\d+/).first()
  await expect(header).toBeVisible({ timeout: 60_000 })
  const text = (await header.textContent()) ?? ''
  const m = /任务\s*#(\d+)\s*·\s*证据包\s*#(\d+)/.exec(text)
  expect(m).not.toBeNull()
  return { taskId: Number(m![1]), packageId: Number(m![2]) }
}

test.describe('全局任务选择：默认最新完成、无回退、显式提交才建任务', () => {
  test.setTimeout(900_000)

  // Real solver jobs must not leak into later tests, including when a test fails.
  let cleanup: { username: string; password: string; projectId: number } | null = null
  const terminal = new Set(['completed', 'failed', 'cancelled', 'timed_out'])
  test.beforeEach(() => { cleanup = null })
  test.afterEach(async ({ browser }) => {
    const owner = cleanup
    cleanup = null
    if (!owner) return
    const api = await browser.newContext({ baseURL: APP })
    try {
      // The GUI session superseded the setup token. Re-authenticate only once
      // assertions have finished, to preserve the browser session under test.
      const token = await loginToken(api.request, owner.username, owner.password)
      const path = `/api/projects/${owner.projectId}/tasks`
      const list = async (): Promise<{ items: Array<{ id: number; status: string }> }> =>
        requestJson(api.request, 'GET', `${path}?limit=100`, undefined, token)
      for (const row of (await list()).items) {
        if (terminal.has(row.status)) continue
        try {
          await requestJson(api.request, 'POST', `${path}/${row.id}/cancel`, {
            reason: 'e2e_end_of_scenario_cleanup',
          }, token)
        } catch (error) {
          // Racing with a completion is fine only when the task is terminal.
          const detail = await requestJson(
            api.request, 'GET', `${path}/${row.id}`, undefined, token,
          ) as { task: { status: string } }
          if (!terminal.has(detail.task.status)) throw error
        }
      }
      await expect.poll(async () =>
        (await list()).items.filter(row => !terminal.has(row.status)).map(row => row.id),
      { timeout: 60_000, intervals: [500, 1000, 2000] }).toEqual([])
    } finally {
      await api.close()
    }
  })

  test('零建任务；冻结视图与切换水合；未完成无回退且导出禁用；切换绑定；项目隔离；默认最新完成', async ({
    browser,
  }) => {
    const projectName = uniqueName('任务选择项目')
    const tag = `tsel-${Date.now()}`
    const soloUser = uniqueName('es-tsel')
    const soloPass = strongPassword('Init')
    const soloCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(soloCtx.request, soloUser, soloPass)
    const soloToken = await loginToken(soloCtx.request, soloUser, soloPass)
    const soloPid = await scaffoldProject(requestJson, soloCtx.request, soloToken, projectName, tag)
    cleanup = { username: soloUser, password: soloPass, projectId: soloPid }
    const pre = await requestJson(soloCtx.request, 'POST', `/api/projects/${soloPid}/validation/run`, {}, soloToken)
    expect((pre.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking)).toEqual([])
    // 项目切换隔离用的空项目 B（无任务；同一用户，脚手架走公开 API）。
    const projectNameB = uniqueName('任务选择项目B')
    await scaffoldProject(requestJson, soloCtx.request, soloToken, projectNameB, `${tag}-b`)

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, soloUser, soloPass)
    const creates = watchTaskCreates(page)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+/) // 项目入口可能直接重定向到默认子页（如 /tasks），仅断言项目作用域

    // 浏览各页、打开提交表单（不提交）、空列表无可切换：零任务创建 POST。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 30_000 })
    await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
    expect(creates).toEqual([])

    // 显式提交 T1：恰好一次创建。
    await submitEvaluationViaUI(page)
    expect(creates.length).toBe(1)

    // 提交后选择即跟随 T1（未完成）：结果页明示无结果，不回退；导出页禁用。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    await expect(page.getByText(/尚未提交证据包/).first()).toBeVisible({ timeout: 60_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    await expect(page.getByText(/所选任务尚未完成/).first()).toBeVisible({ timeout: 60_000 })
    await expect(page.getByRole('button', { name: '导出 Excel 报告', exact: true }).first()).toBeDisabled()

    // T1 完成后结果展示 T1（提交时已显式选中）；任务 id 从 UI 捕获。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await waitCompleted(page)
    await expect(taskRows(page)).toHaveCount(2, { timeout: 30_000 }) // 表头行 + 1 个任务
    // 任务详情冻结输入视图：展示 T1 快照冻结值（非当前草稿）。
    await expect(page.getByRole('heading', { name: /冻结输入/ })).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(/E3 · K1 · N2 · r0\.08 · 付款2笔 · 维护0笔/).first()).toBeVisible({ timeout: 30_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    const t1 = await readSelectedIds(page)

    // 选择 T1 即把其冻结输入水合为工作区当前输入：打开对话框即见 T1 冻结值（只读，不建任务）。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 30_000 })
    await expectField(page, '#task-type', 'optimization')
    await expectField(page, '#task-construction-period', '1')
    await expectField(page, '#task-operation-years', '2')
    await expectField(page, '#task-discount-rate', '0.08')
    await expectField(page, '#task-construction-payments', '[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
    await expectField(page, '#task-maintenance-expenses', '[]')
    // 对话框不再做数据集选择（清单权威是数据页草稿绑定；旧勾选已删除）。
    await expect(page.getByRole('checkbox', { name: 'e2e-sample' })).toHaveCount(0)
    // 编辑运营年限后取消：水合/编辑均不建任务，工作区编辑不碰快照。
    await page.locator('#task-operation-years').fill('3')
    await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 10_000 })
    expect(creates.length).toBe(1)
    // 对话框编辑不污染冻结输入：T1 冻结视图仍为 N=2。
    await clickTaskRow(page, t1.taskId)
    await expect(page.getByText(/E3 · K1 · N2 · r0\.08/).first()).toBeVisible({ timeout: 30_000 })

    // 草稿暂存：编辑已落草稿（先等防抖保存落定），重载页面再打开对话框仍为 N=3。
    await expect
      .poll(async () => (await readDraftWorking(page, soloPid))?.operation_years ?? null, {
        timeout: 30_000,
      })
      .toBe('3')
    await page.reload()
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 30_000 })
    await expectField(page, '#task-operation-years', '3')
    await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 10_000 })
    expect(creates.length).toBe(1)

    // 重新打开（同任务不重复水合，用户编辑保留为 N=3）并显式提交 T2；提交前仍无新任务。
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 30_000 })
    await expectField(page, '#task-operation-years', '3')
    expect(creates.length).toBe(1)
    await page.getByRole('dialog').getByRole('button', { name: /提交任务|Submit task/i }).click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 60_000 })
    expect(creates.length).toBe(2)
    // 提交冻结工作区当前输入为新任务：T2 正文含编辑后的 N=3 与 T1 载入付款
    // （未改即沿用），且无 dataset_ids（对话框不再发送）；评价与类型同样来自工作区。
    {
      // 注意：线上正文用 task_type（见 api client create），非表单内的 type。
      const t2body = creates[1].body as Record<string, unknown>
      const t2eval = t2body.evaluation as Record<string, unknown>
      expect(t2body.task_type).toBe('optimization')
      expect(t2eval.operation_years).toBe(3)
      expect(t2eval.construction_period).toBe(1)
      expect(t2eval.discount_rate).toBe(0.08)
      expect(t2eval.construction_payments).toEqual([
        { t: 0, ratio: 0.6 },
        { t: 1, ratio: 0.4 },
      ])
      // 对话框不再发送 dataset_ids（提交快照的版本清单唯一来自草稿绑定）。
      expect((t2body.config as Record<string, unknown> | null)?.dataset_ids).toBeUndefined()
    }
    expect(creates.length).toBe(2)
    await expect(taskRows(page)).toHaveCount(3, { timeout: 30_000 }) // 表头行 + 2 个任务
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    await expect(page.getByText(/尚未提交证据包/).first()).toBeVisible({ timeout: 60_000 })
    // 结果页无结果分支不渲染导出区（所选 T2 无结果）：断言导出按钮缺席而非仅禁用。
    await expect(page.getByRole('button', { name: '导出 Excel', exact: true })).toHaveCount(0)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    await expect(page.getByText(/所选任务尚未完成/).first()).toBeVisible({ timeout: 60_000 })
    await expect(page.getByRole('button', { name: '导出 Excel 报告', exact: true }).first()).toBeDisabled()

    // 任务页切回 T1：结果与导出同绑定恢复。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await expect(taskRows(page)).toHaveCount(3, { timeout: 30_000 }) // 表头行 + 2 个任务
    await clickTaskRow(page, t1.taskId)
    // 切回即重新载入：工作区回到 T1 冻结值（N=2），覆盖此前 T2 的 N=3。
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 30_000 })
    await expectField(page, '#task-operation-years', '2')
    await expectField(page, '#task-construction-period', '1')
    await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 10_000 })
    expect(creates.length).toBe(2)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    const back = await readSelectedIds(page)
    expect(back.taskId).toBe(t1.taskId)
    expect(back.packageId).toBe(t1.packageId)
    await expect(page.getByText(/尚未提交证据包/)).toHaveCount(0)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: '导出 Excel 报告', exact: true }).first()).toBeEnabled({ timeout: 60_000 })
    await expect(page.getByText(/所选任务尚未完成/)).toHaveCount(0)

    // 注：旧“目录迟到补解”用例已删除——对话框数据集勾选及其挂起/补解逻辑
    // 已随 Gap 2 删除（清单权威是草稿绑定，无需目录到达补解）。

    // 项目切换：B 项目无任务，结果/导出为空态，不泄漏 A 的选择。
    await page.getByText('项目列表').click({ timeout: 30_000 })
    await page.getByText(projectNameB).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    await expect(page.getByText('该项目暂无结果').first()).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText(/任务\s*#\d+/)).toHaveCount(0)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    await expect(page.getByText(/暂无可用结果/).first()).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText(/所选任务尚未完成/)).toHaveCount(0)

    // 无显式选择（全新上下文，本地无存储选择）：默认最新完成 T1。
    const fresh: BrowserContext = await browser.newContext()
    const freshPage: Page = await fresh.newPage()
    const freshSession = await createSession(freshPage, soloUser, soloPass)
    await freshPage.getByText(projectName).click({ timeout: 30_000 })
    await freshPage.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    const freshSel = await readSelectedIds(freshPage)
    expect(freshSel.taskId).toBe(t1.taskId)
    expect(freshSel.packageId).toBe(t1.packageId)

    await soloCtx.close()
    session.assertNoErrors()
    freshSession.assertNoErrors()
    await fresh.close()
    await context.close()
  })

  test('提交落定与防抖保存串行：在途草稿保存被延迟时提交仍成功', async ({ browser }) => {
    // 回归：工作输入每次保存都是先 GET 修订再 PUT；防抖保存与提交落定
    // 重叠即一方 409，提交落定失败会中止提交（对话框卡住）。同页保存
    // 串行后，提交落定等待在途保存再重读修订执行，不自竞。
    const projectName = uniqueName('串行保存项目')
    const tag = `ser-${Date.now()}`
    const soloUser = uniqueName('es-ser')
    const soloPass = strongPassword('Init')
    const soloCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(soloCtx.request, soloUser, soloPass)
    const soloToken = await loginToken(soloCtx.request, soloUser, soloPass)
    const soloPid = await scaffoldProject(requestJson, soloCtx.request, soloToken, projectName, tag)
    cleanup = { username: soloUser, password: soloPass, projectId: soloPid }

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, soloUser, soloPass)
    const creates = watchTaskCreates(page)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+/) // 项目入口可能直接重定向到默认子页（如 /tasks），仅断言项目作用域
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })

    // 延迟全部草稿 PUT：在途防抖保存必与提交落定重叠；无串行时提交落定
    // 读到旧修订，PUT 409，对话框卡住。
    await page.route('**/api/projects/*/draft', async (route) => {
      await new Promise((res) => setTimeout(res, 800))
      await route.continue()
    })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await page.locator('#task-type').selectOption('optimization')
    await page.locator('#task-construction-period').fill('1')
    await page.locator('#task-operation-years').fill('2')
    await page.locator('#task-discount-rate').fill('0.08')
    await page.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
    await page.locator('#task-maintenance-expenses').fill('[]')
    // 等防抖（700ms）触发、在途 PUT 被路由延迟卡住时再点提交。
    await page.waitForTimeout(1000)
    const submitBtn = page.getByRole('button', { name: /提交任务|Submit task/i }).last()
    await expect(submitBtn).toBeEnabled({ timeout: 10_000 })
    await submitBtn.click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 60_000 })
    expect(creates.length).toBe(1)
    const frozen = (creates[0].body as Record<string, unknown>)?.evaluation as Record<string, unknown>
    expect(frozen).toMatchObject({ construction_period: 1, operation_years: 2, discount_rate: 0.08 })

    await soloCtx.close()
    session.assertNoErrors()
    await context.close()
  })

  test('并发选择串行：载入延迟时第二次选择被忽略，所选与服务端草稿一致', async ({ browser }) => {
    // A1 回归：同一时刻最多一份任务载入在途。在途载入期间的新选择必须
    // 被忽略（不发起第二份载入 POST），最终所选任务与服务端草稿必须同属
    // 第一次载入；不断言所选标签了事，以服务端工作输入为准。第二次点击
    // 选 T2（全局选择正跟随 T3，故 T2 既非当前选择又非在途任务，必走忙态
    // 忽略分支；无此约束时会交错提交第二份载入，loadPosts 计数即变红）。
    const projectName = uniqueName('并发选择项目')
    const tag = `conc-${Date.now()}`
    const soloUser = uniqueName('es-conc')
    const soloPass = strongPassword('Init')
    const soloCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(soloCtx.request, soloUser, soloPass)
    const soloToken = await loginToken(soloCtx.request, soloUser, soloPass)
    const soloPid = await scaffoldProject(requestJson, soloCtx.request, soloToken, projectName, tag)
    cleanup = { username: soloUser, password: soloPass, projectId: soloPid }

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, soloUser, soloPass)
    const creates = watchTaskCreates(page)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+/) // 项目入口可能直接重定向到默认子页（如 /tasks），仅断言项目作用域
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    // 空任务列表无表格行：以提交按钮可见作为页面就绪信号。
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible({ timeout: 30_000 })

    // 提交 T1(N=2)、T2(N=5)、T3(N=3)：输入互异，服务端草稿可区分载入归属。
    // 三个任务是必要的：提交后全局选择自动跟随最后提交者；要让第二次点击
    // 落在“在途忙态且非当前选择”的分支上，需要第三个任务。T3 取小年限，
    // 使单 worker 求解耗时与常规任务相当，不堵塞后续用例的完成等待。
    await submitEvaluationViaUI(page)
    for (const n of ['5', '3']) {
      await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
      await expect(page.getByRole('dialog')).toBeVisible({ timeout: 30_000 })
      await page.locator('#task-operation-years').fill(n)
      const submitBtn = page.getByRole('button', { name: /提交任务|Submit task/i }).last()
      await expect(submitBtn).toBeEnabled({ timeout: 10_000 })
      await submitBtn.click({ timeout: 30_000 })
      await expect(page.getByRole('dialog')).toBeHidden({ timeout: 60_000 })
    }
    expect(creates.length).toBe(3)
    await expect(taskRows(page)).toHaveCount(4, { timeout: 30_000 })

    // 任务 id 与 N 的对应经只读冻结输入确定（GUI 会话，不新建登录）。
    const rowTexts = await taskRows(page).allTextContents()
    const ids = rowTexts
      .map((t) => /#(\d+)(?!\d)/.exec(t)?.[1])
      .filter((x): x is string => x !== undefined)
      .map(Number)
    expect(ids.length).toBe(3)
    let tN2 = -1
    let tN5 = -1
    for (const id of ids) {
      const resp = await page.request.get(`/api/projects/${soloPid}/tasks/${id}/inputs`)
      expect(resp.ok()).toBe(true)
      const body = (await resp.json()) as {
        inputs: { snapshot: { evaluation: Record<string, unknown> } }
      }
      const n = body.inputs.snapshot.evaluation.operation_years
      if (n === 2) tN2 = id
      if (n === 5) tN5 = id
    }
    expect(tN2).toBeGreaterThan(0)
    expect(tN5).toBeGreaterThan(0)

    // 扣住第一份载入 POST：先选 T1，在途期间再点 T2 行必须被忽略。
    let releaseLoad!: () => void
    const loadGate = new Promise<void>((res) => {
      releaseLoad = res
    })
    const loadPosts: string[] = []
    let heldFirst = false
    await page.route('**/api/projects/*/tasks/*/load-draft', async (route) => {
      loadPosts.push(route.request().url())
      if (!heldFirst) {
        heldFirst = true
        await loadGate
      }
      await route.continue()
    })
    try {
      await clickTaskRow(page, tN2)
      // 第一份载入请求已发出（被扣住）；此时载入忙态。
      await expect.poll(async () => loadPosts.length, { timeout: 30_000 }).toBe(1)
      await clickTaskRow(page, tN5)
      // 被忽略的选择不发起第二份载入。
      await page.waitForTimeout(1500)
      expect(loadPosts.length).toBe(1)
      releaseLoad()
      // 第一次载入落定（flash 为落定信号，须指向 T1）。
      await expect(page.getByRole('status').filter({ hasText: /载入工作草稿/ }).last()).toContainText(
        new RegExp(`#${tN2}.*载入工作草稿`),
        { timeout: 60_000 },
      )
      expect(loadPosts.length).toBe(1)
      // 所选 T1 与服务端草稿同属第一次载入：草稿为 T1 冻结值 N=2（非 T2 的 5）。
      // 载入写入的是快照冻结值（数字 2），与对话框保存的文本 '3' 不同类型。
      await expect
        .poll(async () => (await readDraftWorking(page, soloPid))?.operation_years ?? null, {
          timeout: 30_000,
        })
        .toBe(2)
      // 对话框所见即服务端草稿（水合一致，非标签游戏）。
      await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
      await expect(page.getByRole('dialog')).toBeVisible({ timeout: 30_000 })
      await expectField(page, '#task-operation-years', '2')
      await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
      await expect(page.getByRole('dialog')).toBeHidden({ timeout: 10_000 })
      expect(creates.length).toBe(3)
    } finally {
      await page.unroute('**/api/projects/*/tasks/*/load-draft')
    }

    await soloCtx.close()
    session.assertNoErrors()
    await context.close()
  })

  test('最终编辑 durability：在途保存落定后的新编辑在导航前落定', async ({ browser }) => {
    // A2 回归：保存 f1 在途时编辑为 f2，f1 落定不得清掉 f2 的脏态；
    // 导航前 f2 必须落定（服务端与重开工作区均为 f2）。只覆盖“导航时
    // 首次保存仍在途”的旧 A2 未覆盖的顺序。
    const projectName = uniqueName('最终编辑项目')
    const tag = `fedit-${Date.now()}`
    const soloUser = uniqueName('es-fedit')
    const soloPass = strongPassword('Init')
    const soloCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(soloCtx.request, soloUser, soloPass)
    const soloToken = await loginToken(soloCtx.request, soloUser, soloPass)
    const soloPid = await scaffoldProject(requestJson, soloCtx.request, soloToken, projectName, tag)
    cleanup = { username: soloUser, password: soloPass, projectId: soloPid }

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, soloUser, soloPass)
    const creates = watchTaskCreates(page)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+/) // 项目入口可能直接重定向到默认子页（如 /tasks），仅断言项目作用域
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    // 空任务列表无表格行：以提交按钮可见作为页面就绪信号。
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible({ timeout: 30_000 })

    // 只扣住第一份草稿 PUT；其余放行。
    let releasePut!: () => void
    const putGate = new Promise<void>((res) => {
      releasePut = res
    })
    const putBodies: string[] = []
    let heldFirstPut = false
    await page.route('**/api/projects/*/draft', async (route) => {
      if (route.request().method() === 'PUT') {
        putBodies.push(route.request().postData() ?? '')
        if (!heldFirstPut) {
          heldFirstPut = true
          await putGate
        }
      }
      await route.continue()
    })
    try {
      await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
      await expect(page.getByRole('dialog')).toBeVisible({ timeout: 30_000 })
      await page.locator('#task-operation-years').fill('7')
      // 第一份保存已发出（被扣住）：此时 f1 在途。
      await expect.poll(async () => putBodies.length, { timeout: 30_000 }).toBe(1)
      expect(putBodies[0]).toContain('"operation_years":"7"')
      // 在途期间再编辑为 f2，并反复触碰以推迟其防抖计时器，直到确认 f1 已落定。
      releasePut()
      for (let i = 0; i < 40; i++) {
        await page.locator('#task-operation-years').fill('8')
        const w = await readDraftWorking(page, soloPid)
        if (w?.operation_years === '7') break
        await page.waitForTimeout(250)
      }
      // f1 已落定（服务端为 7），f2 防抖尚未触发：立即取消并导航。
      await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
      await expect(page.getByRole('dialog')).toBeHidden({ timeout: 10_000 })
      await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
      // 服务端最终为 f2（恰好两份 PUT：f1 与 f2，无杂散写入）。
      await expect
        .poll(async () => (await readDraftWorking(page, soloPid))?.operation_years ?? null, {
          timeout: 30_000,
        })
        .toBe('8')
      expect(putBodies.length).toBe(2)
      expect(putBodies[1]).toContain('"operation_years":"8"')
      // 重开工作区即见 f2。
      await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
      await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
      await expect(page.getByRole('dialog')).toBeVisible({ timeout: 30_000 })
      await expectField(page, '#task-operation-years', '8')
      await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
      await expect(page.getByRole('dialog')).toBeHidden({ timeout: 10_000 })
      expect(creates.length).toBe(0)
    } finally {
      await page.unroute('**/api/projects/*/draft')
    }

    await soloCtx.close()
    session.assertNoErrors()
    await context.close()
  })

  test('监控精确性：已登记中止精确核销，登记期内未登记失败仍判失败', async ({ browser }) => {
    // C 回归：中止必须按实际请求的 method + 路径逐条登记；一条登记只核销
    // 一条匹配失败及其同路径回显。登记期内（存在有效登记时）未登记的同文本
    // 回显仍判失败——无跨 URL 预算共用。窗外证明见上一用例。
    const soloUser = uniqueName('es-mon2')
    const soloPass = strongPassword('Init')
    const soloCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(soloCtx.request, soloUser, soloPass)
    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, soloUser, soloPass)
    await page.route('**/api/__probe_registered**', (route) => {
      session.registerExpectedAbort(route.request().method(), route.request().url())
      route.abort()
    })
    try {
      const failedSeen = page.waitForEvent('requestfailed', {
        predicate: (r) => r.url().includes('__probe_registered'),
        timeout: 30_000,
      })
      await page.evaluate(() => fetch('/api/__probe_registered?q=1').catch(() => 'aborted'))
      await failedSeen
      // 仅有已登记中止时断言通过（轮询等回显到达）。
      await expect(async () => session.assertNoErrors()).toPass({ timeout: 15_000, intervals: [500] })
      // 同文本回显但资源路径无配额（页面自身 URL）：必须判失败。
      await page.evaluate(() => {
        console.error('Failed to load resource: net::ERR_FAILED')
      })
      expect(() => session.assertNoErrors()).toThrow()
    } finally {
      await page.unroute('**/api/__probe_registered**')
    }
    await soloCtx.close()
    await context.close()
  })

  test('直达失败单次可见：载入 500 只请求一次、无自动重试，显式重试后成功', async ({ browser }) => {
    // Finding-2 回归：?package= 初始化只拥有一次载入尝试。首份载入 500 时
    // 失败可见、草稿/选择不变、不自动循环重发；用户恢复响应后重新打开同
    // 一条直达链接显式重试并成功（本场景仅 T1 有证据包，无选择器可切）。
    // 500 回显用精确 expected-response 机制核销。
    const projectName = uniqueName('直达失败项目')
    const tag = `link-${Date.now()}`
    const soloUser = uniqueName('es-link')
    const soloPass = strongPassword('Init')
    const soloCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(soloCtx.request, soloUser, soloPass)
    const soloToken = await loginToken(soloCtx.request, soloUser, soloPass)
    const soloPid = await scaffoldProject(requestJson, soloCtx.request, soloToken, projectName, tag)
    cleanup = { username: soloUser, password: soloPass, projectId: soloPid }

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, soloUser, soloPass)
    const creates = watchTaskCreates(page)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+/) // 项目入口可能直接重定向到默认子页（如 /tasks），仅断言项目作用域
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    // 空任务列表无表格行：以提交按钮可见作为页面就绪信号。
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible({ timeout: 30_000 })

    // 提交 T1(N=2) 并等完成（直达需要证据包），任务 id 与包 id 从 UI 捕获。
    await submitEvaluationViaUI(page)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await waitCompleted(page)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    const t1 = await readSelectedIds(page)
    // 提交 T2(N=5，未完成）：全局选择跟随 T2，直达 T1 即为一次切换。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 30_000 })
    // 对话框应水合 T1 冻结类型（optimization）；若此处为 calc，说明水合异常，
    // 直接失败而非继续一次语义不明的提交。
    await expectField(page, '#task-type', 'optimization')
    await page.locator('#task-operation-years').fill('5')
    const submitBtn2 = page.getByRole('button', { name: /提交任务|Submit task/i }).last()
    await expect(submitBtn2).toBeEnabled({ timeout: 10_000 })
    await submitBtn2.click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 60_000 })
    expect(creates.length).toBe(2)
    // 直达前草稿即 T2 时代工作输入（失败后须原样不变）。
    const beforeWorking = await readDraftWorking(page, soloPid)
    expect(beforeWorking?.operation_years).toBe('5')

    // 只让第一份载入 500；其余放行（供显式重试成功）。
    let failedOnce = false
    const loadPosts: string[] = []
    await page.route('**/api/projects/*/tasks/*/load-draft', async (route) => {
      loadPosts.push(route.request().url())
      if (!failedOnce) {
        failedOnce = true
        await route.fulfill({
          status: 500,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'e2e-probe-load-failure' }),
        })
        return
      }
      await route.continue()
    })
    try {
      await page.goto(`/projects/${soloPid}/results?package=${t1.packageId}`)
      // 失败可见：错误提示出现；只发了一份载入，不自动循环重试。
      const errAlert = page.locator('.ies-alert--error').first()
      await expect(errAlert).toBeVisible({ timeout: 60_000 })
      expect((await errAlert.textContent())?.trim().length).toBeGreaterThan(0)
      await page.waitForTimeout(2000)
      expect(loadPosts.length).toBe(1)
      // 草稿/选择不变：仍为直达前 T2 工作输入，未混入 T1；无成功 flash。
      expect(await readDraftWorking(page, soloPid)).toEqual(beforeWorking)
      await expect(page.getByRole('status').filter({ hasText: /载入工作草稿/ })).toHaveCount(0)
      // 显式重试：恢复响应后用户刷新同一条直达链接（仅 T1 有证据包，
      // 无第二个选项可供选择器切换；整页重载即一次新的显式直达尝试。
      // 注意必须用 reload：同 URL 的 goto 可能被浏览器优化为空操作，
      // 不会重挂载、不会发起新的尝试）。
      // 结果页成功不闪现载入提示，以任务·证据包绑定文本为落定信号。
      // 路由保持注册：首份之后一律放行并计数，重试的第二份同样被记录。
      await page.reload()
      await expect(page.getByText(new RegExp(`任务\\s*#${t1.taskId}\\s*·\\s*证据包\\s*#${t1.packageId}`)).first()).toBeVisible({
        timeout: 60_000,
      })
      // 成功后直达参数被清除并交还用户选择。
      await expect(page).toHaveURL(/\/results$/)
      expect(loadPosts.length).toBe(2)
      // 重试成功：草稿为 T1 冻结值（数字 2）。
      await expect
        .poll(async () => (await readDraftWorking(page, soloPid))?.operation_years ?? null, {
          timeout: 30_000,
        })
        .toBe(2)
      expect(creates.length).toBe(2)
    } finally {
      await page.unroute('**/api/projects/*/tasks/*/load-draft')
    }

    await soloCtx.close()
    // 首份载入 500 回显精确核销一次；重试成功为 200，不在核销内。
    session.assertNoErrors([
      {
        method: 'POST',
        path: `/api/projects/${soloPid}/tasks/${t1.taskId}/load-draft`,
        status: 500,
        times: 1,
      },
    ])
    await context.close()
  })

  test('监控完整性：未登记的中止回显仍判失败', async ({ browser }) => {
    // C 的反向证明：没有任何已知中止登记时，一条同文本的控制台回显
    // 必须判失败——不能把真实网络失败误当成测试主动中止忽略。
    // 全新会话、零中止窗口，确定性触发（不依赖网络）。
    const soloUser = uniqueName('es-mon')
    const soloPass = strongPassword('Init')
    const soloCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(soloCtx.request, soloUser, soloPass)
    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, soloUser, soloPass)
    await page.evaluate(() => {
      console.error('Failed to load resource: net::ERR_FAILED')
    })
    expect(() => session.assertNoErrors()).toThrow()
    await soloCtx.close()
    await context.close()
  })
})
