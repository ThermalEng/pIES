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
import { buildMinimalModel, createEngineer, loginToken, requestJson } from './setup/api'

/** buildMinimalModel 名键设备关键字(名称 `E2E ${kw}`, 与 setup/api.ts 同源)。 */
const E2E_DEVICE_KEYWORDS = [
  'grid_connection',
  'electric_load',
  'heat_pump',
  'heat_load',
  'electric_chiller',
  'cooling_load',
]

/** 装配实例引用派生(与后端 builder._slugify_id 同规则: 小写 + 非法字符折叠)。 */
function assemblySlug(name: string): string {
  const cleaned = name.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '_').replace(/^[-_]+|[-_]+$/g, '')
  if (!/^[a-z][a-z0-9_-]*$/.test(cleaned)) throw new Error(`无法派生装配实例引用: ${name}`)
  return cleaned
}

/** 装配声明: finance_binding 全覆盖(none) + 空 tariff_bindings(后端显式声明门禁)。 */
function assemblyCommands(): unknown[] {
  const instances: Record<string, unknown> = {}
  for (const kw of E2E_DEVICE_KEYWORDS) instances[assemblySlug(`E2E ${kw}`)] = { type: 'none' }
  instances['bat1'] = { type: 'none' }
  return [
    {
      id: 'c-finbind',
      unit: 'assembly',
      type: 'assembly.set_finance_binding',
      payload: { instances },
    },
    {
      id: 'c-tariff',
      unit: 'assembly',
      type: 'assembly.set_tariff_bindings',
      payload: { bindings: {} },
    },
  ]
}

/** 装配就绪(API 造数): 显式计算声明 + 储能设备 + 装配节声明 + 财务画像引用 + 规划配置。 */
async function seedAssembly(
  request: APIRequestContext, projectId: number, token: string, profileId: string,
): Promise<void> {
  const view = await requestJson(request, 'GET', `/api/projects/${projectId}`, undefined, token)
  await requestJson(request, 'PUT', `/api/projects/${projectId}/draft`, {
    expected_revision: view.draft.revision,
    commands: [EXPLICIT_CALC_PATCH, BATTERY_COMMAND, ...DEMAND_COMMANDS, ...assemblyCommands()],
  }, token)
  await requestJson(request, 'POST', '/api/finance-profiles', {
    finance_profile: financeProfile(profileId),
  }, token)
  await requestJson(request, 'PUT', `/api/projects/${projectId}/finance-profile`, {
    profile_ref: { id: profileId },
  }, token)
  await requestJson(request, 'PUT', `/api/projects/${projectId}/planning-config`, {
    planning_config: PLANNING_PAYLOAD,
    expected_revision: null,
  }, token)
}

/** 现行装配边界要求的显式计算声明(与后端契约测试同值, 经 draft 命令声明)。 */
const EXPLICIT_CALC_PATCH = {
  id: 'c-calc',
  unit: 'config',
  type: 'config.patch',
  payload: {
    mode: 'fixed_operation',
    generator: 'ies.algo.milp_hybrid@1.0.0',
    solver: 'ies.solver.highs@1.7.2',
    time_axis: { resolution: '1h', start: '2025-01-01T00:00:00Z' },
  },
}

/** 负荷需求常量(预定义需求接口的显式来源声明: 标量 params → constant 模式;
 * 预检与装配双门禁均认可, 见后端 _validate_device_params 对齐例外)。 */
const DEMAND_COMMANDS = [
  {
    id: 'c-dem-electric',
    unit: 'model',
    type: 'model.upsert_device',
    payload: { name: 'E2E electric_load', params: { electricity_demand: 50 } },
  },
  {
    id: 'c-dem-heat',
    unit: 'model',
    type: 'model.upsert_device',
    payload: { name: 'E2E heat_load', params: { heat_demand: 30 } },
  },
  {
    id: 'c-dem-cool',
    unit: 'model',
    type: 'model.upsert_device',
    payload: { name: 'E2E cooling_load', params: { cool_demand: 20 } },
  },
]

/** 储能设备(规划变量 cap 引用的 device_ref bat1)。 */
const BATTERY_COMMAND = {
  id: 'c-bat1',
  unit: 'model',
  type: 'model.upsert_device',
  payload: {
    name: 'bat1',
    device_type: 'ies.device.battery',
    kind: 'new',
    params: { capacity_kwh: 200, rated_power_kw: 100 },
    position: { x: 1, y: 1 },
  },
}

/** 财务画像(与后端契约测试同构, id 按运行唯一)。 */
function financeProfile(profileId: string): Record<string, unknown> {
  return {
    schema: 'ies.finance-profile',
    schema_version: '1.0.0',
    profile: {
      id: profileId,
      region: 'CN-North',
      currency: 'CNY',
      base_year: 2025,
      price_basis: 'tax_inclusive',
      cost_method: 'fixed_plus_linear',
    },
    finance_types: {
      battery_system: {
        upfront_capex: {
          linear: { energy_kwh: { unit_cost: { value: '900', unit: 'CNY/kWh' } } },
        },
        annual_fixed_om: {
          linear: { energy_kwh: { unit_cost: { value: '18', unit: 'CNY/kWh/a' } } },
        },
      },
    },
    energy_prices: {
      grid_import: {
        carrier: 'electricity',
        direction: 'purchase',
        kind: 'constant',
        value: { value: '0.7', unit: 'CNY/kWh' },
      },
      pv_export: {
        carrier: 'electricity',
        direction: 'sale',
        kind: 'constant',
        value: { value: '0.4', unit: 'CNY/kWh' },
      },
    },
    taxes: {},
  }
}

/** 规划配置(单变量, 与后端契约测试同构)。 */
const PLANNING_PAYLOAD = {
  objective: { sense: 'minimize', expression: '0' },
  variables: {
    cap: {
      device_ref: 'bat1',
      parameter: 'capacity_kwh',
      lower_bound: '0',
      upper_bound: '1000',
      unit: 'kWh',
    },
  },
  constraints: {},
}

const SAVE_BTN = /保存计算配置|Save calculation config/i
const SAVED_OK = /已保存 revision v\d+|Saved revision v\d+/i
const CONFLICT = /当前表单编辑已保留|your edits are kept/i
const RELOAD_BTN = /重新加载最新配置|Reload latest config/i

/** UI 创建项目并进入计算配置页, 返回项目 id。 */
async function createProjectEnterConfig(page: Page, projectName: string): Promise<string> {
  await page.getByRole('button', { name: /新建项目|New project/i }).first().click()
  await page.getByLabel(/名称|Name/i).fill(projectName)
  await page.getByRole('button', { name: /确认|Confirm/i }).click()
  await expect(page.getByText(projectName)).toBeVisible()
  await page.getByText(projectName).click()
  await expect(page).toHaveURL(/\/projects\/\d+$/)
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('计算配置').click()
  await expect(page).toHaveURL(/\/config$/)
  const m = page.url().match(/\/projects\/(\d+)\/config$/)
  if (!m) throw new Error(`未解析出项目 id: ${page.url()}`)
  return m[1]
}

/** 填写全部必填字段(精度由用户明确选择 medium)。 */
async function fillConfig(page: Page, mode: string, timeLimit: string): Promise<void> {
  await page.locator('#calc-mode').fill(mode)
  await page.locator('#calc-generator').fill('ies.algo.milp_hybrid@1.0.0')
  await page.locator('#calc-solver').fill('ies.solver.highs@1.7.2')
  await page.locator('#calc-precision').selectOption('medium')
  await page.locator('#calc-time-limit').fill(timeLimit)
  await page.locator('#calc-outputs').fill('["solution"]')
}

test.describe('0.12-A1: 计算配置 revision 编辑器', () => {
  test('空白无预填 / 保存 / 重读 / 409 保留表单并主动重载', async ({ browser }) => {
    const username = uniqueName('es-cfgrev')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    const projectId = await createProjectEnterConfig(page, uniqueName('QA 配置收口项目'))

    // 1) 空白新建: 无已保存配置, 精度/时间限制不得预填
    await expect(page.getByText(/尚未保存|Not saved yet/i)).toBeVisible()
    await expect(page.locator('#calc-precision')).toHaveValue('')
    await expect(page.locator('#calc-time-limit')).toHaveValue('')
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
    await page.locator('#calc-mode').fill('planning-local')
    await page.getByRole('button', { name: SAVE_BTN }).click()
    await expect(page.getByText(CONFLICT).first()).toBeVisible({ timeout: 10_000 })
    await expect(page.getByRole('button', { name: RELOAD_BTN })).toBeVisible()
    // 未提交编辑未被覆盖, 远端 v2 也未自动灌入
    await expect(page.locator('#calc-mode')).toHaveValue('planning-local')
    await expect(page.locator('#calc-time-limit')).toHaveValue('600')

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
      baseline_leap_year: false,
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
    await expect(page).toHaveURL(/\/projects\/\d+$/)

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
    const submitBtn = dialog.getByRole('button', { name: /^提交任务$|^Submit task$/i })
    await expect(submitBtn).toBeEnabled()
    await submitBtn.click()
    // 任务出现在列表, 打开详情展示冻结的 revision(任务排队等待执行, 不声称已计算)
    await expect(page.locator('.ies-table__row').first()).toBeVisible({ timeout: 15_000 })
    await page.locator('.ies-table__row').first().click()
    await expect(page.locator('.ies-task-detail')).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText(/配置 revision v1/i).first()).toBeVisible({ timeout: 10_000 })

    session.assertNoErrors()
    await context.close()
  })
})
