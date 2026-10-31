// QA-E2E-01 0.12-A1 配置收口验收 + 0.12-A2 绑定验收(13.3 范围外新增聚焦场景):
//   计算配置 revision 编辑器: 空白新建无预填 / 填写保存 / 重读 /
//   409 保留表单 + 用户主动重载;
//   A2: GUI 真实保存后提交, 任务冻结所引 revision, 详情展示。
//
// 前置: 独立用户(API 造数); 测试 1 项目由 UI 创建, 并发修改由同一用户的
// 第二个标签页经 UI 保存产生(真实用户行为, 无 API 并发写); 测试 2 的
// 模型/财务/规划/装配/基准走 API 造数(场景 6 同模式), 数据绑定、配置
// 保存、任务提交、详情查看全走 UI。

import { test, expect } from '@playwright/test'
import type { APIRequestContext, Page, BrowserContext } from '@playwright/test'
import { createSession, attachTab, uniqueName, strongPassword } from './fixtures'
import { buildMinimalModel, createEngineer, loginToken, requestJson, seedAssembly } from './setup/api'


const SAVE_BTN = /保存计算配置|Save calculation config/i
const SAVED_OK = /已保存 revision v\d+|Saved revision v\d+/i
const CONFLICT = /当前表单编辑已保留|your edits are kept/i
const RELOAD_BTN = /重新加载最新配置|Reload latest config/i

/** UI 创建项目并进入计算配置页, 返回项目 id(起始年必填, 无默认值, 必须显式填写)。 */
async function createProjectEnterConfig(page: Page, projectName: string): Promise<string> {
  await page.getByRole('button', { name: /新建项目|New project/i }).first().click()
  await page.getByLabel(/名称|Name/i).fill(projectName)
  await page.locator('#pp-create-start-year').fill('2026')
  await page.getByRole('button', { name: /确认|Confirm/i }).click()
  await expect(page.getByText(projectName)).toBeVisible()
  await page.getByText(projectName).click()
  await expect(page).toHaveURL(/\/projects\/\d+(\/tasks)?$/)
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('计算配置').click()
  await expect(page).toHaveURL(/\/config$/)
  const m = page.url().match(/\/projects\/(\d+)\/config$/)
  if (!m) throw new Error(`未解析出项目 id: ${page.url()}`)
  return m[1]
}

/** 填写全部必填字段(精度由用户明确选择 medium)。 */
async function fillConfig(page: Page, mode: string, timeLimit: string): Promise<void> {
  await page.locator('#calc-mode').fill(mode)
  await page.locator('#calc-generator').fill('ies.generator.highs_milp@7.1.0')
  await page.locator('#calc-solver').fill('ies.solver.highs@1.7.2')
  await page.locator('#calc-precision').selectOption('medium')
  await page.locator('#calc-time-limit').fill(timeLimit)
  await page.locator('#calc-outputs').fill('["solution"]')
}

test.describe('0.12-A1: 计算配置 revision 编辑器', () => {
  test('部署初值预填时间其余无预填 / 保存 / 重读 / 409 保留表单并主动重载', async ({ browser }) => {
    const username = uniqueName('es-cfgrev')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    const projectId = await createProjectEnterConfig(page, uniqueName('QA 配置收口项目'))

    // 1) 空白新建: 无已保存配置, 精度/mode 不得预填; 时间限制取统一部署配置公开初值
    await expect(page.getByText(/尚未保存|Not saved yet/i)).toBeVisible()
    await expect(page.locator('#calc-precision')).toHaveValue('')
    const defaultsResp = await page.request.get('/api/deployment/defaults')
    const defaultsBody = await defaultsResp.json()
    const expectedInitial = String(defaultsBody.calculation_defaults.time_limit_seconds)
    await expect(page.locator('#calc-time-limit')).toHaveValue(expectedInitial, { timeout: 15000 })
    await expect(page.locator('#calc-mode')).toHaveValue('')

    // 2) 填写并保存 → v1
    await fillConfig(page, 'planning', '600')
    await page.getByRole('button', { name: SAVE_BTN }).click()
    await expect(page.getByText(SAVED_OK).first()).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText(/当前 revision: v1|Current revision.*v1/i)).toBeVisible()

    // 3) 重读(刷新) → 值保持
    await page.reload()
    await expect(page.locator('#calc-mode')).toHaveValue('planning')
    await expect(page.locator('#calc-precision')).toHaveValue('medium')
    await expect(page.locator('#calc-time-limit')).toHaveValue('600')
    // 重读经 pretty() 美化缩进，内容一致即可
    await expect(page.locator('#calc-outputs')).toHaveValue('[\n  "solution"\n]')

    // 4) 第二标签页保存 v2(时间限制 600→900)
    const tabB = await context.newPage()
    attachTab(session, tabB)
    await tabB.goto(`/projects/${projectId}/config`)
    await expect(tabB.locator('#calc-mode')).toHaveValue('planning')
    await tabB.locator('#calc-time-limit').fill('900')
    await tabB.getByRole('button', { name: SAVE_BTN }).click()
    await expect(tabB.getByText(/已保存 revision v2|Saved revision v2/i).first()).toBeVisible({ timeout: 10_000 })
    await tabB.close()

    // 5) 原页(仍基于 v1)修改并保存 → 409, 表单保留未提交编辑
    await page.locator('#calc-time-limit').fill('650')
    await page.getByRole('button', { name: SAVE_BTN }).click()
    await expect(page.getByText(CONFLICT).first()).toBeVisible({ timeout: 10_000 })
    await expect(page.getByRole('button', { name: RELOAD_BTN })).toBeVisible()
    // 未提交编辑未被覆盖, 远端 v2 也未自动灌入
    await expect(page.locator('#calc-mode')).toHaveValue('planning')
    await expect(page.locator('#calc-time-limit')).toHaveValue('650')

    // 6) 用户主动重载 → 表单切换为 v2 内容
    await page.getByRole('button', { name: RELOAD_BTN }).click()
    await expect(page.locator('#calc-mode')).toHaveValue('planning')
    await expect(page.locator('#calc-time-limit')).toHaveValue('900')
    await expect(page.getByText(/当前 revision: v2|Current revision.*v2/i)).toBeVisible()

    session.assertNoErrors()
    await context.close()
  })

  test('GUI 真实保存后提交: 引用提示 + 任务创建 + 详情显示 revision', async ({ browser }) => {
    const username = uniqueName('es-cfgsub')
    const password = strongPassword('Init')
    const projectName = uniqueName('QA 提交绑定项目')
    const profileId = `cn-e2e-${uniqueName('fin')}`
    // 造数阶段(UI 登录前, 场景 6 同模式): 用户/项目/模型/财务/规划/
    // 装配声明/基准确认走 API, 之后 UI 登录自然顶掉 API 会话。
    // 数据绑定、配置保存、提交对话框全走 UI。
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    const setupToken = await loginToken(apiCtx.request, username, password)
    const createdProj = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2026,
      baseline_scenario_mode: 'single',
    }, setupToken)
    const projectId = createdProj.project?.id ?? createdProj.id
    await buildMinimalModel(apiCtx.request, projectId, setupToken)
    await seedAssembly(apiCtx.request, projectId, setupToken, profileId)
    await requestJson(apiCtx.request, 'POST', `/api/projects/${projectId}/validation/baseline-confirm`, {
      assumptions: { period_years: 20, discount_rate: 0.08, currency: 'CNY' },
    }, setupToken)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    // 打开项目(已由 API 造数)
    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+(\/tasks)?$/)

    // 数据: 内置样例生成并绑定(场景 4 同流程)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click()
    await expect(page).toHaveURL(/\/data$/)
    await page.getByRole('button', { name: /内置样例数据|Built-in sample data/i }).click()
    await page.getByRole('button', { name: /生成并上传|Generate & upload/i }).click()
    await expect(page.getByText(/已通过校验|Validation passed|Good quality/i).first()).toBeVisible({ timeout: 30_000 })
    await page.getByRole('button', { name: /^确定$|^OK$/i }).click()
    await page.getByRole('button', { name: /绑定到项目|Bind to project/i }).first().click()
    await expect(page.getByText(/已绑定|Bound/i).first()).toBeVisible({ timeout: 10_000 })

    // 配置: GUI 真实填写保存 → v1
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('计算配置').click()
    await expect(page).toHaveURL(/\/config$/)
    await fillConfig(page, 'planning', '600')
    await page.getByRole('button', { name: SAVE_BTN }).click()
    await expect(page.getByText(SAVED_OK).first()).toBeVisible({ timeout: 10_000 })

    // 任务中心 → 提交对话框展示所引用的 revision, 提交可用
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click()
    await expect(page).toHaveURL(/\/tasks$/)
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click()
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByText(/本次提交将引用已保存配置 revision v1/i)).toBeVisible()
    await dialog.locator('#task-construction-period').fill('0')
    await dialog.locator('#task-operation-years').fill('1')
    await dialog.locator('#task-discount-rate').fill('0.08')
    await dialog.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 1}]')
    await dialog.locator('#task-maintenance-expenses').fill('[]')
    const submitBtn = dialog.getByRole('button', { name: /^提交任务$|^Submit task$/i })
    await expect(submitBtn).toBeEnabled()
    await submitBtn.click()
    // 任务出现在列表, 打开详情展示冻结的 revision(任务排队等待执行, 不声称已计算)
    const taskTable = page.getByRole('table')
    const firstBodyRow = taskTable.getByRole('row').nth(1)
    await expect(firstBodyRow).toBeVisible({ timeout: 15_000 })
    await firstBodyRow.click()
    await expect(page.getByRole('heading', { name: /任务详情|Task detail/ })).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText(/配置 revision v1/i).first()).toBeVisible({ timeout: 10_000 })

    // 预期内噪声: 空白项目在保存前读配置(工作台落地 + 配置页挂载各一次)
    // 必为“尚未保存”404(公开契约); 精确到本项目路径与确切次数, 其余
    // 任何 console/pageerror/失败请求仍零容忍。
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${projectId}/config`, status: 404, times: 2 },
    ])
    await context.close()
  })
})

test.describe('PR #26: 生成器静态能力在保存边界反馈', () => {
  test('不兼容模式/精度/solver/outputs 可定位，保留编辑，修正后保存', async ({ browser }) => {
    const username = uniqueName('es-config-caps')
    const password = strongPassword('Init')
    const setup = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(setup.request, username, password)
    await setup.close()

    const context = await browser.newContext()
    const page = await context.newPage()
    const session = await createSession(page, username, password)
    const projectId = await createProjectEnterConfig(page, uniqueName('QA 能力反馈项目'))
    await fillConfig(page, 'planning', '600')

    await page.locator('#calc-precision').selectOption('high')
    await page.getByRole('button', { name: SAVE_BTN }).click()
    await expect(page.getByText(/不支持精度|does not support precision/i).first()).toBeVisible()
    await expect(page.locator('#calc-precision')).toHaveValue('high')
    await expect(page.getByText(/尚未保存|Not saved yet/i)).toBeVisible()

    await page.locator('#calc-precision').selectOption('medium')
    await page.locator('#calc-mode').fill('unsupported_mode')
    await page.getByRole('button', { name: SAVE_BTN }).click()
    await expect(page.getByText(/不支持模式|does not support mode/i).first()).toBeVisible()
    await expect(page.locator('#calc-mode')).toHaveValue('unsupported_mode')

    await page.locator('#calc-mode').fill('planning')
    await page.locator('#calc-solver').fill('test.solver.unknown@1.0.0')
    await page.getByRole('button', { name: SAVE_BTN }).click()
    await expect(page.getByText(/不支持求解器|does not support solver/i).first()).toBeVisible()
    await expect(page.locator('#calc-solver')).toHaveValue('test.solver.unknown@1.0.0')

    await page.locator('#calc-solver').fill('ies.solver.highs@1.7.2')
    await page.locator('#calc-outputs').fill('["solution", "unknown"]')
    await page.getByRole('button', { name: SAVE_BTN }).click()
    await expect(page.getByText(/不支持输出|does not support outputs/i).first()).toBeVisible()
    await expect(page.locator('#calc-outputs')).toHaveValue('["solution", "unknown"]')

    await page.locator('#calc-outputs').fill('["solution"]')
    await page.getByRole('button', { name: SAVE_BTN }).click()
    await expect(page.getByText(SAVED_OK).first()).toBeVisible()
    await expect(page.getByText(/当前 revision: v1|Current revision.*v1/i)).toBeVisible()

    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${projectId}/config`, status: 404, times: 2 },
      { method: 'PUT', path: `/api/projects/${projectId}/config`, status: 422, times: 4 },
    ])
    await context.close()
  })
})

test.describe('0.12-B: 部署公开初值装配时序', () => {
  test('部署初值不可用时明确报错而非静默空白', async ({ browser }) => {
    const username = uniqueName('es-cfgdep-fail')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.route('**/api/deployment/defaults', (route) =>
      route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"boom"}' }),
    )
    const projectId = await createProjectEnterConfig(page, uniqueName('QA 部署初值失败项目'))

    // 失败可见: 错误横幅出现, 时间栏保持未填入的空白(非正常初值)
    await expect(page.getByText(/页面加载失败|Load failed/i)).toBeVisible({ timeout: 15000 })
    await expect(page.locator('#calc-time-limit')).toHaveCount(0)

    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${projectId}/config`, status: 404, times: 2 },
      { method: 'GET', path: '/api/deployment/defaults', status: 500, times: 1 },
    ])
    await context.close()
  })

  test('迟到部署初值原子填入且不覆盖用户编辑', async ({ browser }) => {
    const username = uniqueName('es-cfgdep-late')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    // 部署初值延迟 1.5s 到达(使用非常值, 不硬编码生产初值)
    await page.route('**/api/deployment/defaults', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 1500))
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ calculation_defaults: { time_limit_seconds: 77777 } }),
      })
    })
    const projectId = await createProjectEnterConfig(page, uniqueName('QA 部署初值迟到项目'))

    // 同一次 load 原子填入延迟到达的初值
    await expect(page.locator('#calc-time-limit')).toHaveValue('77777', { timeout: 15000 })
    // 用户编辑后不再有写入: 等待超过原延迟后编辑值保持
    await page.locator('#calc-time-limit').fill('123')
    await page.waitForTimeout(2500)
    await expect(page.locator('#calc-time-limit')).toHaveValue('123')

    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${projectId}/config`, status: 404, times: 2 },
    ])
    await context.close()
  })
})
