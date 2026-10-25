// 0.12-F1 修正验收(非画布 GUI): 起始年与评价确认的空值门禁。
//
// 空字符串经 Number() 会静默变为 0, 前端必须先判空并给出内联错误,
// 不得提交; 后端仍为权威校验(400 TASK-REQ-015)。
// 业务动作全部走 UI; API 仅用于造数(建用户/建项目/最小模型), 与被测动作隔离。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { buildMinimalModel, createEngineer, loginToken, requestJson, seedAssembly } from './setup/api'

const APP_URL = process.env.E2E_APP_URL ?? 'http://web:80'

test.describe('空值门禁(非画布 GUI)', () => {
  test('新建项目起始年留空被拒绝, 填入后可创建', async ({ browser }) => {
    const username = uniqueName('es-emptygate')
    const password = strongPassword('Init')
    const projectName = uniqueName('QA 空值门禁项目')
    const apiCtx = await browser.newContext({ baseURL: APP_URL })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    // 打开新建项目对话框, 只填名称, 起始年留空提交
    await page.getByRole('button', { name: /新建项目|New project/i }).first().click()
    const dialog = page.getByRole('dialog')
    await dialog.locator('#pp-create-name').fill(projectName)
    await expect(dialog.locator('#pp-create-start-year')).toHaveValue('')
    await dialog.getByRole('button', { name: /确认|Confirm|OK/i }).click()
    // 内联错误出现, 对话框保持打开, 项目未创建
    await expect(dialog.getByText('请输入公历整数起始年(如 2026)')).toBeVisible()
    await expect(dialog).toBeVisible()

    // 填入合法起始年后可创建
    await dialog.locator('#pp-create-start-year').fill('2026')
    await dialog.getByRole('button', { name: /确认|Confirm|OK/i }).click()
    await expect(page.getByText('项目创建成功')).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText(projectName).first()).toBeVisible({ timeout: 10_000 })

    session.assertNoErrors()
    await context.close()
  })

  test('提交任务评价留空被拒绝, 合法提交走顶层字段', async ({ browser }) => {
    const username = uniqueName('es-emptyeval')
    const password = strongPassword('Init')
    const projectName = uniqueName('QA 评价空值项目')
    // 造数(UI 登录前): 建用户 → API 建项目 → 最小模型(设备/财务/规划声明)
    const apiCtx = await browser.newContext({ baseURL: APP_URL })
    await createEngineer(apiCtx.request, username, password)
    const userTok = await loginToken(apiCtx.request, username, password)
    const createdProj = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2026,
      baseline_scenario_mode: 'single',
    }, userTok)
    const projectId = createdProj.project?.id ?? createdProj.id
    await buildMinimalModel(apiCtx.request, projectId, userTok)
    // 造数: 显式计算声明 + 储能/负荷设备 + 装配节 + 财务画像引用 + 规划配置
    // (与 config-revision 提交场景同源), 使提交装配闸门有冻结引用可用。
    await seedAssembly(apiCtx.request, projectId, userTok, `cn-e2e-${uniqueName('fin')}`)
    // 造数: 记录财务基准确认证据(评价年限/折现率关联本次计算), 使提交校验
    // 门禁通过; 被测动作是评价确认栏的空值门禁, 与该确认入口隔离。
    // (显式确认 UI 已暂停, 待评价输入切片恢复; 见 ValidationPage 注释)
    await requestJson(apiCtx.request, 'POST', `/api/projects/${projectId}/validation/baseline-confirm`, {
      assumptions: { evaluation_years: 1, discount_rate: 0.08 },
    }, userTok)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    // Invalid submissions must never enter the HTTP submit path, even when
    // an earlier async validation races with later valid form edits.
    const taskPosts: Array<{ body: Record<string, unknown> }> = []
    page.on('request', (request) => {
      if (request.method() === 'POST' && new URL(request.url()).pathname === `/api/projects/${projectId}/tasks`) {
        taskPosts.push({ body: request.postDataJSON() as Record<string, unknown> })
      }
    })

    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()

    // 数据: 生成样例并绑定(与场景 4 同流程, 使提交校验门禁通过)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click()
    await page.getByRole('button', { name: /内置样例数据|Built-in sample data/i }).click()
    await page.getByRole('button', { name: /生成并上传|Generate & upload/i }).click()
    await expect(page.getByText(/已通过校验|Validation passed|Good quality/i).first()).toBeVisible({ timeout: 30_000 })
    await page.getByRole('button', { name: /^确定$|^OK$/i }).click()
    await page.getByRole('button', { name: /绑定到项目|Bind to project/i }).first().click()
    await expect(page.getByText(/已绑定|Bound/i).first()).toBeVisible({ timeout: 10_000 })

    // 配置: 填写全部必填字段并保存(精度由用户明确选择, 无预填默认值)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('计算配置').click()
    await expect(page).toHaveURL(/\/config$/)
    await page.locator('#calc-mode').fill('planning')
    await page.locator('#calc-generator').fill('ies.generator.highs_milp@4.1.0')
    await page.locator('#calc-solver').fill('ies.solver.highs@1.7.2')
    await page.locator('#calc-precision').selectOption('medium')
    await page.locator('#calc-time-limit').fill('600')
    await page.locator('#calc-outputs').fill('["solution"]')
    await page.getByRole('button', { name: /保存计算配置|Save calculation config/i }).click()
    await expect(page.getByText(/已保存 revision v\d+|Saved revision v\d+/i).first()).toBeVisible({ timeout: 10_000 })

    // 任务中心 → 提交对话框, 评价各栏留空提交
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click()
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click()
    const dialog = page.getByRole('dialog')
    await expect(dialog.locator('#task-construction-period')).toHaveValue('')
    await expect(dialog.locator('#task-operation-years')).toHaveValue('')
    await expect(dialog.locator('#task-discount-rate')).toHaveValue('')
    await dialog.getByRole('button', { name: /^提交任务$|^Submit task$/i }).click()
    // 评价非法提示出现, 对话框保持打开, 未产生任务
    await expect(dialog.getByText('请填写建设期(非负整数)、运营年限(正整数)、0..1 区间内折现率与合法付款/维护 JSON(均必填)')).toBeVisible({ timeout: 10_000 })
    await expect(dialog).toBeVisible()
    expect(taskPosts).toHaveLength(0)

    // 折现率留空(0 被静默接受的回归点): 只填年限仍被拒绝
    await dialog.locator('#task-construction-period').fill('0')
    await dialog.locator('#task-operation-years').fill('1')
    await dialog.getByRole('button', { name: /^提交任务$|^Submit task$/i }).click()
    await expect(dialog.getByText('请填写建设期(非负整数)、运营年限(正整数)、0..1 区间内折现率与合法付款/维护 JSON(均必填)')).toBeVisible({ timeout: 10_000 })
    expect(taskPosts).toHaveLength(0)

    // 填入合法评价后提交: 先断言发出请求的形态(本切片契约)——评价确认走顶层
    // evaluation 字段(不在 config 内), 评价年限 E=K+N 随提交明确发送,
    // 幂等键落在后端格式集内; 再断言任务创建成功并出现在列表(造数已备齐冻结引用)。
    await dialog.locator('#task-discount-rate').fill('0.08')
    await dialog.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 1}]')
    await dialog.locator('#task-maintenance-expenses').fill('[]')
    const [submitResponse] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/projects/${projectId}/tasks`) && res.request().method() === 'POST',
      ),
      dialog.getByRole('button', { name: /^提交任务$|^Submit task$/i }).click(),
    ])
    const submitBody = submitResponse.request().postDataJSON() as Record<string, unknown>
    expect(taskPosts).toHaveLength(1)
    expect(taskPosts[0].body).toEqual(submitBody)
    expect(submitBody.evaluation).toEqual({
      evaluation_years: 1,
      construction_period: 0,
      operation_years: 1,
      discount_rate: 0.08,
      construction_payments: [{ t: 0, ratio: 1 }],
      maintenance_expenses: [],
    })
    expect(submitBody.config).not.toHaveProperty('evaluation')
    expect(submitBody.idempotency_key).toMatch(/^[A-Za-z0-9._:-]{1,128}$/)
    expect(submitResponse.status()).toBe(201)
    await expect(page.locator('.ies-table__row').first()).toBeVisible({ timeout: 15_000 })

    // K=1/N=2/E=3 二次提交: 付款 0.6/0.4 + 一条 amount 字符串维护。
    // 幂等键不得含原始 JSON(引号/空格/超长 → 后端 422), 必须合法且 201;
    // 不同评价输入签名必须与上一次不同(对话框重开换新随机段)。
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click()
    const dialog2 = page.getByRole('dialog')
    await dialog2.locator('#task-construction-period').fill('1')
    await dialog2.locator('#task-operation-years').fill('2')
    await dialog2.locator('#task-discount-rate').fill('0.08')
    await dialog2.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
    await dialog2.locator('#task-maintenance-expenses').fill('[{"year": 3, "amount": "1000.00", "currency": "CNY"}]')
    const [submitResponse2] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/projects/${projectId}/tasks`) && res.request().method() === 'POST',
      ),
      dialog2.getByRole('button', { name: /^提交任务$|^Submit task$/i }).click(),
    ])
    const submitBody2 = submitResponse2.request().postDataJSON() as Record<string, unknown>
    expect(submitBody2.evaluation).toEqual({
      evaluation_years: 3,
      construction_period: 1,
      operation_years: 2,
      discount_rate: 0.08,
      construction_payments: [{ t: 0, ratio: 0.6 }, { t: 1, ratio: 0.4 }],
      maintenance_expenses: [{ year: 3, amount: '1000.00', currency: 'CNY' }],
    })
    expect(submitBody2.idempotency_key).toMatch(/^[A-Za-z0-9._:-]{1,128}$/)
    expect(submitBody2.idempotency_key).not.toBe(submitBody.idempotency_key)
    expect(submitResponse2.status()).toBe(201)
    await expect(page.locator('.ies-table__row').nth(1)).toBeVisible({ timeout: 15_000 })

    // 数值写法边界('+1'/合法长小数): 接受口径与展示 E 同源, 发出的键仍
    // 合法且不超 128。展示 E 应为 3(此前正则会显示 '—' 却接受提交)。
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click()
    const dialog3 = page.getByRole('dialog')
    await dialog3.locator('#task-construction-period').fill('+1')
    await dialog3.locator('#task-operation-years').fill('2')
    await dialog3.locator('#task-discount-rate').fill('0.08000000000000000000000001')
    await dialog3.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
    await dialog3.locator('#task-maintenance-expenses').fill('[]')
    await expect(dialog3.getByText(/评价年限 E=K\+N.*：3/)).toBeVisible({ timeout: 10_000 })
    const [submitResponse3] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/projects/${projectId}/tasks`) && res.request().method() === 'POST',
      ),
      dialog3.getByRole('button', { name: /^提交任务$|^Submit task$/i }).click(),
    ])
    const submitBody3 = submitResponse3.request().postDataJSON() as Record<string, unknown>
    expect(submitBody3.evaluation).toEqual({
      evaluation_years: 3,
      construction_period: 1,
      operation_years: 2,
      discount_rate: 0.08,
      construction_payments: [{ t: 0, ratio: 0.6 }, { t: 1, ratio: 0.4 }],
      maintenance_expenses: [],
    })
    const key3 = submitBody3.idempotency_key as string
    expect(key3).toMatch(/^[A-Za-z0-9._:-]{1,128}$/)
    expect(key3.length).toBeLessThanOrEqual(128)
    expect(submitResponse3.status()).toBe(201)
    await expect(page.locator('.ies-table__row').nth(2)).toBeVisible({ timeout: 15_000 })

    // 签名稳定性: 重开对话框填完全相同输入 → 重复提交提示出现(同签名);
    // 确认后新键提交 201。改一个字段(费率) → 无提示直接提交(签名区分)。
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click()
    const dialog4 = page.getByRole('dialog')
    await dialog4.locator('#task-construction-period').fill('1')
    await dialog4.locator('#task-operation-years').fill('2')
    await dialog4.locator('#task-discount-rate').fill('0.08')
    await dialog4.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
    await dialog4.locator('#task-maintenance-expenses').fill('[]')
    await dialog4.getByRole('button', { name: /^提交任务$|^Submit task$/i }).click()
    await expect(page.getByText('检测到相同任务')).toBeVisible({ timeout: 10_000 })
    const [submitResponse4] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/projects/${projectId}/tasks`) && res.request().method() === 'POST',
      ),
      page.getByRole('button', { name: '仍然提交' }).click(),
    ])
    expect(submitResponse4.status()).toBe(201)
    await expect(page.locator('.ies-table__row').nth(3)).toBeVisible({ timeout: 15_000 })

    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click()
    const dialog5 = page.getByRole('dialog')
    await dialog5.locator('#task-construction-period').fill('1')
    await dialog5.locator('#task-operation-years').fill('2')
    await dialog5.locator('#task-discount-rate').fill('0.09')
    await dialog5.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
    await dialog5.locator('#task-maintenance-expenses').fill('[]')
    const [submitResponse5] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/projects/${projectId}/tasks`) && res.request().method() === 'POST',
      ),
      dialog5.getByRole('button', { name: /^提交任务$|^Submit task$/i }).click(),
    ])
    expect(submitResponse5.status()).toBe(201)
    await expect(page.getByText('检测到相同任务')).toHaveCount(0)
    await expect(page.locator('.ies-table__row').nth(4)).toBeVisible({ timeout: 15_000 })

    // 预期内噪声(与 config-revision.spec 同构, 精确到本项目路径与次数):
    // 空白项目在保存前读配置(工作台落地 + 配置页挂载各一次)必为“尚未保存”
    // 404(公开契约); 其余任何 console/pageerror/失败请求仍零容忍。
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${projectId}/config`, status: 404, times: 2 },
    ])
    await context.close()
  })
})
