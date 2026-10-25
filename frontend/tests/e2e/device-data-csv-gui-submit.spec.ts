// 0.15 外部数据 CSV 修复收口: 合法来源声明经任务中心 GUI 提交闭环。
//
// 缺陷(本轮修复): 项目模型参数中的合法数据序列来源声明
// (`{mode: data_repeat, dataset_version_id}` dict) 被预检以 PARAM-UNIT-002
// 误判为数值常量单位不匹配, 任务中心 GUI 提交被校验门禁挡住
// (“存在 1 项阻断问题,当前禁止提交计算任务”; 修复前 GUI 证据见切片报告,
//  backend 回归测试见 tests/test_source_declaration.py 与
//  tests/test_task_config_revision.py)。
//
// 覆盖链(修复后验收):
//   1. 数据页真实 GUI 上传 device-data-external/electric_load_repeat_24h.csv
//      (`ies.device-data` 2.1.0, 24h data_repeat: 偶数步 30 kW、奇数步 50 kW,
//      device_id=ies.device.electric_load);
//   2. 公开草稿命令声明预定义槽来源 + 装配/数据绑定/基线确认/配置
//      (与 device-data-csv-extension.spec.ts 同脚手架);
//   3. 任务中心真实 GUI 提交(K=0/N=1/折现 8%/付款 [{t:0,ratio:1}]/无维护):
//      校验门禁放行合法声明(无阻断弹层), 提交后任务入列;
//   4. 真实 io/compute Worker 求解, GUI 详情轮询“已完成”;
//   5. 公开 API 核对: 逐时 30/50 与 24h 重复边界; 年负荷 350,400 kWh;
//      电网年购 425,485.714… kWh; 封存 energy_net −297,840.0 CNY。
//
// 覆盖边界: 项目/模型/装配/配置脚手架与数值核对经公开 API(造数/读回),
// 上传与任务提交为真实 GUI(被测动作); 结果页 GUI 展示面由既有
// device-data-csv-extension.spec.ts 覆盖, 本规格不重复断言。

import { test, expect } from '@playwright/test'
import type { BrowserContext, Page } from '@playwright/test'
import path from 'node:path'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { APP, financeProfile } from './financial-scaffold'
import { createEngineer, loginToken, requestJson } from './setup/api'

const CSV_PATH = path.join(process.cwd(), 'device-data-external', 'electric_load_repeat_24h.csv')

/** 手算关键值(同 device-data-csv-extension.spec.ts 头注)。 */
const HOURLY_ROWS = 8760
const LOAD_DAILY_KWH = 960 // 24h 基线: 12×30 + 12×50
const LOAD_ANNUAL_KWH = LOAD_DAILY_KWH * 365 // 350,400
const HP_ELECTRICITY_KW = 30 / 3.5 // 热需求 30 kW 恒定 / cop 3.5
const GRID_ANNUAL_KWH = LOAD_ANNUAL_KWH + HP_ELECTRICITY_KW * HOURLY_ROWS // 425,485.714…
const ENERGY_NET_CNY = -0.7 * GRID_ANNUAL_KWH // −297,840.0(封存现金流口径, 购电为支出)
const loadKwAt = (step: number): number => (step % 2 === 0 ? 30 : 50)

/** GUI 评价输入(K=0/N=1; 与 device-data-csv-extension 的 API 提交同口径)。 */
const EVALUATION_INPUTS = {
  constructionPeriod: '0',
  operationYears: '1',
  discountRate: '0.08',
  constructionPayments: '[{"t": 0, "ratio": 1}]',
  maintenanceExpenses: '[]',
}

function expectClose(actual: number, expected: number, tolerance: number): void {
  expect(Math.abs(actual - expected)).toBeLessThanOrEqual(tolerance)
}

interface HourlySeriesMeta {
  series_id: string
  instance_id: string
  variable_id: string
  unit: string
}

interface HourlyBody {
  field: string
  values: number[]
  total_rows: number
  unit: string
  instance_id: string
  variable_id: string
}

test.describe('0.15 外部数据 CSV: 合法来源声明经任务中心 GUI 提交', () => {
  test.setTimeout(900_000)

  test('GUI 上传 → 声明/装配(公开 API) → GUI 提交(无阻断) → Worker 完成 → 关键值对应', async ({ browser }) => {
    const username = uniqueName('es-csvgui')
    const password = strongPassword('Init')
    const projectName = uniqueName('CSV 提交项目')
    const profileId = `csv-gui-${Date.now()}`

    // ---- 0. 公开 API 前置: 用户/项目/画像/占位配置 ----
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const setupToken = await loginToken(apiCtx.request, username, password)
    const created = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2025,
      baseline_scenario_mode: 'single',
    }, setupToken)
    const pid: number = created.project?.id ?? created.id
    await requestJson(apiCtx.request, 'POST', '/api/finance-profiles', {
      finance_profile: financeProfile(profileId),
    }, setupToken)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/finance-profile`, {
      profile_ref: { id: profileId },
    }, setupToken)

    // 占位计算配置: 任务中心对话框需要已保存配置 revision(无配置时提交按钮
    // 保持禁用); 装配完成后以最终配置重写。outputs 含 metrics(公开配置词汇)。
    const placeholderConfig = await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/config`, {
      calculation_config: {
        schema: 'ies.calculation-config',
        schema_version: '2.0.0',
        mode: 'planning',
        predictions: [],
        generator: 'ies.generator.highs_milp@5.0.0',
        solver: 'ies.solver.highs@1.7.2',
        precision: 'medium',
        convergence: null,
        time_limit_seconds: 600,
        options: {},
        seed: null,
        outputs: ['solution', 'metrics'],
      },
      expected_revision: null,
    }, setupToken)

    // ---- 1. 真实 GUI: 数据页上传仓库外 step CSV ----
    const context1: BrowserContext = await browser.newContext()
    const page1: Page = await context1.newPage()
    const uploadSession = await createSession(page1, username, password)
    await page1.getByText(projectName).click()
    await expect(page1).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page1.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()
    await page1.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click()
    await expect(page1).toHaveURL(/\/data$/)
    await page1.getByRole('button', { name: /上传数据|Upload/ }).first().click()
    await page1.locator('#dset-name').fill('e2e-device-data')
    await page1.locator('#dset-file').setInputFiles(CSV_PATH)
    await expect(page1.getByText(/设备数据 step 文件|Device-data step file/).first()).toBeVisible()
    await expect(page1.locator('#dset-offset')).toHaveCount(0)
    await page1.getByRole('button', { name: /上传数据|Upload/ }).last().click()
    await expect(page1.getByText(/已通过校验|Validation passed/).first()).toBeVisible({ timeout: 30_000 })
    await page1.getByRole('button', { name: /^确定$|^OK$/i }).click()
    uploadSession.assertNoErrors()
    await context1.close()

    // GUI 登录会替换该用户的既有 API 会话(每用户单活动会话), 重新登录取会话。
    const token = await loginToken(apiCtx.request, username, password)

    // ---- 2. 公开列表读取上传版本 id ----
    const dsList = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/datasets`, undefined, token)
    const entry = (dsList.datasets as Array<{
      dataset: { name: string }
      latest_version: { id: number; resolution: string }
    }>).find((d) => d.dataset.name === 'e2e-device-data')
    expect(entry, 'GUI 上传的数据集应出现在公开列表').toBeTruthy()
    const dvid = Number(entry!.latest_version.id)
    expect(entry!.latest_version.resolution).toBe('1h')

    // ---- 3. 公开草稿命令: 来源声明 + 装配 + 计算基线/确认/配置 ----
    const view = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, token)
    const put = await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: view.draft.revision,
      commands: [
        {
          id: 'guicsv-dev-grid1', unit: 'model', type: 'model.upsert_device',
          payload: {
            name: 'grid1', device_type: 'ies.device.grid_connection', kind: 'existing',
            params: { max_import_power_kw: 800, max_export_power_kw: 200 },
            position: { x: 0.0, y: 0.0 },
          },
        },
        {
          // 外部 CSV 消费入口: 预定义槽 electricity_demand 的数据来源声明
          // (本轮修复的预检对象; 合法 dict 声明必须经校验门禁放行)。
          id: 'guicsv-dev-load1', unit: 'model', type: 'model.upsert_device',
          payload: {
            name: 'load1', device_type: 'ies.device.electric_load', kind: 'new',
            params: {
              peak_power_kw: 200,
              electricity_demand: { mode: 'data_repeat', dataset_version_id: dvid },
            },
            position: { x: 2.0, y: 0.0 },
          },
        },
        {
          id: 'guicsv-dev-hp1', unit: 'model', type: 'model.upsert_device',
          payload: {
            name: 'hp1', device_type: 'ies.device.heat_pump', kind: 'new',
            params: { rated_heat_kw: 600, cop: 3.5 },
            position: { x: 1.0, y: 1.0 },
          },
        },
        {
          id: 'guicsv-dev-heatload', unit: 'model', type: 'model.upsert_device',
          payload: {
            name: 'heatload', device_type: 'ies.device.heat_load', kind: 'new',
            params: { heat_demand: 30 },
            position: { x: 3.0, y: 1.0 },
          },
        },
        {
          id: 'guicsv-conn-grid-load1', unit: 'model', type: 'model.upsert_connection',
          payload: {
            name: 'c-grid-load1', from_device: 'grid1', from_port: 'electricity_import',
            to_device: 'load1', to_port: 'electricity_in',
          },
        },
        {
          id: 'guicsv-conn-grid-hp1', unit: 'model', type: 'model.upsert_connection',
          payload: {
            name: 'c-grid-hp1', from_device: 'grid1', from_port: 'electricity_import',
            to_device: 'hp1', to_port: 'electricity_in',
          },
        },
        {
          id: 'guicsv-conn-hp-heat', unit: 'model', type: 'model.upsert_connection',
          payload: {
            name: 'c-hp-heat', from_device: 'hp1', from_port: 'heat_out',
            to_device: 'heatload', to_port: 'heat_in',
          },
        },
        {
          id: 'guicsv-calc', unit: 'config', type: 'config.patch',
          payload: {
            mode: 'fixed_operation',
            generator: 'ies.generator.highs_milp@5.0.0',
            solver: 'ies.solver.highs@1.7.2',
            time_axis: { resolution: '1h', start: '2025-01-01T00:00:00Z' },
          },
        },
        {
          id: 'guicsv-finbind', unit: 'assembly', type: 'assembly.set_finance_binding',
          payload: {
            instances: {
              grid1: { type: 'none' },
              load1: { type: 'none' },
              heatload: { type: 'none' },
              hp1: {
                type: 'costed',
                finance_type: 'heat_pump_system',
                drivers: {
                  heat_kw: { property: 'rated_heat_kw' },
                  consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
                },
              },
            },
          },
        },
        {
          id: 'guicsv-tariff', unit: 'assembly', type: 'assembly.set_tariff_bindings',
          payload: {
            bindings: {
              grid_import: {
                price: 'grid_import', instance: 'grid1', interface: 'electricity_import',
                aggregation: 'integrate_positive',
              },
            },
          },
        },
      ],
    }, token)
    let rev: number = put.revision

    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/planning-config`, {
      planning_config: {
        objective: {
          sense: 'minimize',
          expression: 'grid_import__period_energy_purchase + hp1__upfront_capex + 1 year * hp1__annual_fixed_om + hp1__period_variable_om',
        },
        variables: {
          hp_cap: {
            device_ref: 'hp1', parameter: 'rated_heat_kw',
            lower_bound: '50', upper_bound: '1000', unit: 'kW',
          },
        },
        constraints: {},
      },
      expected_revision: null,
    }, token)
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/versions`, {
      name: '计算基线', reason: 'snapshot_freeze', expected_revision: rev,
    }, token)
    const bindPut = await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: rev,
      commands: [{
        id: 'guicsv-bind-data', unit: 'dataset', type: 'dataset.bind',
        payload: { dataset_version_id: dvid, role: 'main' },
      }],
    }, token)
    rev = bindPut.revision
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/baseline-confirm`, {
      assumptions: {},
    }, token)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/config`, {
      calculation_config: {
        schema: 'ies.calculation-config',
        schema_version: '2.0.0',
        mode: 'planning',
        predictions: [],
        generator: 'ies.generator.highs_milp@5.0.0',
        solver: 'ies.solver.highs@1.7.2',
        precision: 'medium',
        convergence: null,
        time_limit_seconds: 600,
        options: {},
        seed: null,
        outputs: ['solution', 'metrics'],
      },
      expected_revision: placeholderConfig.revision,
    }, token)

    // ---- 4. 真实 GUI: 任务中心提交(本轮修复的被测动作) ----
    const context2: BrowserContext = await browser.newContext()
    const page2: Page = await context2.newPage()
    const submitSession = await createSession(page2, username, password)
    await page2.getByText(projectName).click({ timeout: 30_000 })
    await expect(page2).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page2.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()
    await page2.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page2.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await page2.locator('#task-type').selectOption('optimization')
    await page2.locator('#task-construction-period').fill(EVALUATION_INPUTS.constructionPeriod)
    await page2.locator('#task-operation-years').fill(EVALUATION_INPUTS.operationYears)
    await page2.locator('#task-discount-rate').fill(EVALUATION_INPUTS.discountRate)
    await page2.locator('#task-construction-payments').fill(EVALUATION_INPUTS.constructionPayments)
    await page2.locator('#task-maintenance-expenses').fill(EVALUATION_INPUTS.maintenanceExpenses)
    const submitBtn = page2.getByRole('button', { name: /提交任务|Submit task/i }).last()
    // 对话框打开即触发的门禁预检须完成(检查中按钮 loading 禁用), 且合法来源
    // 声明不得产出阻断(修复前此处渲染 PARAM-UNIT-002 阻断弹层, 提交被驳回)。
    await expect(submitBtn).toBeEnabled({ timeout: 30_000 })
    await expect(page2.getByText(/正在检查项目校验门禁/)).toHaveCount(0)
    await expect(page2.getByText(/阻断问题|blocking issue/i)).toHaveCount(0)
    await submitBtn.click({ timeout: 30_000 })
    // 提交成功: 任务入列(修复前校验门禁驳回, 无新任务行, 弹层保留)。
    await expect(page2.locator('.ies-table__row').first()).toBeVisible({ timeout: 15_000 })

    // ---- 5. 真实 Worker: GUI 详情轮询“已完成” ----
    await page2.locator('.ies-table__row').first().click({ timeout: 30_000 })
    await expect(page2.locator('.ies-task-detail')).toBeVisible({ timeout: 10_000 })
    const detailBadge = page2.locator('.ies-task-detail .ies-badge__label').first()
    await expect(async () => {
      const text = (await detailBadge.textContent()) ?? ''
      expect(text).toMatch(/已完成|Completed/i)
    }).toPass({ timeout: 420_000, intervals: [10_000] })
    submitSession.assertNoErrors()
    await context2.close()

    // ---- 6. 公开 API 核对: 任务身份与逐时/年度关键值 ----
    const verifyToken = await loginToken(apiCtx.request, username, password)
    const tasks = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks`, undefined, verifyToken)
    const items = tasks.items as Array<{ id: number; status: string }>
    // 新项目仅本次 GUI 提交产生 1 个任务, 且已完成(GUI 轮询与此交叉印证)。
    expect(items.length).toBe(1)
    expect(items[0].status).toBe('completed')
    const taskId = Number(items[0].id)

    const inputs = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/inputs`, undefined, verifyToken)
    expect((inputs.inputs.snapshot.dataset_version_ids as number[]).map(Number)).toContain(dvid)

    const viewRes = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, verifyToken)
    const result = viewRes.result
    const refs = result.hourly_refs as Array<{ rows: number; series: HourlySeriesMeta[] }>
    expect(refs.length).toBeGreaterThan(0)
    expect(refs[0].rows).toBe(HOURLY_ROWS)
    const metas = refs[0].series
    // 封存逐时引用按真实运行留存的公开输出精确匹配身份
    // (series_id = "<instance_id>.<variable_id>"), 不设同实例兜底。
    const loadMeta = metas.find((m) => m.instance_id === 'load1' && m.variable_id === 'electricity_in')
    expect(loadMeta, '封存引用应含 load1.electricity_in').toMatchObject({
      instance_id: 'load1',
      variable_id: 'electricity_in',
      series_id: 'load1.electricity_in',
      unit: 'kW',
    })
    const gridMeta = metas.find((m) => m.instance_id === 'grid1' && m.variable_id === 'electricity_import')
    expect(gridMeta, '封存引用应含 grid1.electricity_import').toMatchObject({
      instance_id: 'grid1',
      variable_id: 'electricity_import',
      series_id: 'grid1.electricity_import',
      unit: 'kW',
    })

    // 逐时响应身份/单位须与所选封存引用同源一致(field 为服务端对请求的回显)。
    const hourly = async (meta: HourlySeriesMeta): Promise<HourlyBody> => {
      const res = await apiCtx.request.get(
        `${APP}/api/projects/${pid}/tasks/${taskId}/result/hourly` +
        `?field=${encodeURIComponent(meta.series_id)}&start=0&end=${HOURLY_ROWS}&limit=${HOURLY_ROWS}`,
        { headers: { Authorization: `Bearer ${verifyToken}` } },
      )
      expect(res.ok()).toBe(true)
      const body = (await res.json()) as HourlyBody
      expect(body.field).toBe(meta.series_id)
      expect(body.instance_id).toBe(meta.instance_id)
      expect(body.variable_id).toBe(meta.variable_id)
      expect(body.unit).toBe(meta.unit)
      return body
    }

    // 电负荷: 与 CSV 的 24h 重复基线逐时一致(含跨日重复边界)。
    const loadBody = await hourly(loadMeta!)
    expect(loadBody.total_rows).toBe(HOURLY_ROWS)
    expect(loadBody.values.length).toBe(HOURLY_ROWS)
    expect(loadBody.unit).toBe('kW')
    for (const step of [0, 1, 2, 23, 24, 25, 8758, 8759]) {
      expectClose(loadBody.values[step], loadKwAt(step), 1e-3)
    }
    const loadSum = loadBody.values.reduce((a, b) => a + b, 0)
    expectClose(loadSum, LOAD_ANNUAL_KWH, LOAD_ANNUAL_KWH * 1e-6)

    // 电网购入 = 电负荷 + 热泵电耗(热需求 30 kW 恒定 / cop 3.5)。
    const gridBody = await hourly(gridMeta!)
    expect(gridBody.values.length).toBe(HOURLY_ROWS)
    expect(gridBody.unit).toBe('kW')
    for (const step of [0, 1, 2, 23, 24, 25, 8758, 8759]) {
      expectClose(gridBody.values[step], loadKwAt(step) + HP_ELECTRICITY_KW, 1e-3)
    }
    const gridSum = gridBody.values.reduce((a, b) => a + b, 0)
    expectClose(gridSum, GRID_ANNUAL_KWH, GRID_ANNUAL_KWH * 1e-6)

    // 封存评价: 年购电现金流 = −0.7 CNY/kWh × 年购入电量(手算 −297,840.0 CNY)。
    const fin = result.financial_evaluation
    expect(fin, '应产出封存财务评价').toBeTruthy()
    expect(fin.annual_operation.length).toBe(1)
    expectClose(Number(fin.annual_operation[0].energy_net), ENERGY_NET_CNY, 0.5)

    await apiCtx.close()
  })
})
