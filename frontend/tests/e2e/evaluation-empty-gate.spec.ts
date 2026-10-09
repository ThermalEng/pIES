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

    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+$/)

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
    await page.locator('#calc-generator').fill('ies.generator.highs_milp@1.0.0')
    await page.locator('#calc-solver').fill('ies.solver.highs@1.7.2')
    await page.locator('#calc-precision').selectOption('medium')
    await page.locator('#calc-time-limit').fill('600')
    await page.locator('#calc-outputs').fill('["solution"]')
    await page.getByRole('button', { name: /保存计算配置|Save calculation config/i }).click()
    await expect(page.getByText(/已保存 revision v\d+|Saved revision v\d+/i).first()).toBeVisible({ timeout: 10_000 })

    // 任务中心 → 提交对话框, 评价两栏留空提交
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click()
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click()
    const dialog = page.getByRole('dialog')
    await expect(dialog.locator('#task-eval-years')).toHaveValue('')
    await expect(dialog.locator('#task-discount-rate')).toHaveValue('')
    await dialog.getByRole('button', { name: /^提交任务$|^Submit task$/i }).click()
    // 评价非法提示出现, 对话框保持打开, 未产生任务
    await expect(dialog.getByText('请填写正整数评价年限(≥1)与 0..1 区间内折现率(均必填)')).toBeVisible({ timeout: 10_000 })
    await expect(dialog).toBeVisible()

    // 折现率留空(0 被静默接受的回归点): 只填年限仍被拒绝
    await dialog.locator('#task-eval-years').fill('1')
    await dialog.getByRole('button', { name: /^提交任务$|^Submit task$/i }).click()
    await expect(dialog.getByText('请填写正整数评价年限(≥1)与 0..1 区间内折现率(均必填)')).toBeVisible({ timeout: 10_000 })

    // 填入合法评价后提交: 先断言发出请求的形态(本切片契约)——评价确认走顶层
    // evaluation 字段(不在 config 内), 幂等键落在后端格式集内; 再断言任务
    // 创建成功并出现在列表(造数已备齐冻结引用)。
    await dialog.locator('#task-discount-rate').fill('0.08')
    const [submitResponse] = await Promise.all([
      page.waitForResponse(
        (res) => res.url().includes(`/api/projects/${projectId}/tasks`) && res.request().method() === 'POST',
      ),
      dialog.getByRole('button', { name: /^提交任务$|^Submit task$/i }).click(),
    ])
    const submitBody = submitResponse.request().postDataJSON() as Record<string, unknown>
    expect(submitBody.evaluation).toEqual({ evaluation_years: 1, discount_rate: 0.08 })
    expect(submitBody.config).not.toHaveProperty('evaluation')
    expect(submitBody.idempotency_key).toMatch(/^[A-Za-z0-9._:-]{1,128}$/)
    expect(submitResponse.status()).toBe(201)
    await expect(page.locator('.ies-table__row').first()).toBeVisible({ timeout: 15_000 })

    // 预期内噪声(与 config-revision.spec 同构, 精确到本项目路径与次数):
    // 空白项目在保存前读配置(工作台落地 + 配置页挂载各一次)必为“尚未保存”
    // 404(公开契约); 其余任何 console/pageerror/失败请求仍零容忍。
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${projectId}/config`, status: 404, times: 2 },
    ])
    await context.close()
  })
})
