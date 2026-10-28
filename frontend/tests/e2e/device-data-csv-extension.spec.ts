// 0.15 第二切片(外部数据 CSV): 仓库外设备数据 step 文件的公开消费链验收。
//
// 内容对象: device-data-external/electric_load_repeat_24h.csv
// (`ies.device-data` 2.1.0 step 文件; 24h data_repeat 基线: 偶数步 30 kW、
//  奇数步 50 kW; device_id=ies.device.electric_load)。
//
// 覆盖链(现有公开路径):
//   1. 数据页真实 GUI 上传(真实文件 setInputFiles → 自描述 step 模式 →
//      质量报告“已通过校验”);
//   2. 公开草稿命令 model.upsert_device 以 {mode: data_repeat,
//      dataset_version_id} 声明预定义槽来源(builder 合成 project 相对路径),
//      同一版本 dataset.bind 满足数据门禁与内容查找;
//   3. 公开任务 API 提交 → 真实 io/compute Worker 求解;
//   4. 结果与输入对应(手算): 逐时 30/50 与 24h 重复边界; 日 960 kWh;
//      年负荷 350,400 kWh; 电网购入 425,485.714… kWh; 封存评价
//      energy_net = −0.7 × (30 + 30/3.5) × 8760 = −297,840.0 CNY(现金流口径, 购电为支出);
//      结果页真实 GUI 展示同源封存数据(摘要/规划解/封存评价卡)。
//
// 覆盖边界(2026-10-18 切片裁决):
//   - 上传与结果查看为真实 GUI; 来源声明与任务提交经公开 API —— 切片当时
//     校验预检对 dict 来源声明产出 PARAM-UNIT-002 假阻断, 任务中心 GUI 提交
//     被该校验门禁挡住(证据留存在切片报告); 修复后的 GUI 提交收口见
//     device-data-csv-gui-submit.spec.ts。
//   - 切片当时曲线被指标摘要缺席遮蔽; 现已独立修复，真实曲线/任务切换
//     与读取状态验收见 hourly-steps/hourly-r3 规格。本规格不重复曲线断言。
//   - 本规格不据此缺口做任何断言, 也不宣称完整 GUI 闭环或 0.15 完成。

import { test, expect } from '@playwright/test'
import type { BrowserContext, Page } from '@playwright/test'
import path from 'node:path'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { APP, financeProfile } from './financial-scaffold'
import { createEngineer, loginToken, requestJson } from './setup/api'

const CSV_PATH = path.join(process.cwd(), 'device-data-external', 'electric_load_repeat_24h.csv')

/** 手算关键值(见头注第 4 条)。 */
const HOURLY_ROWS = 8760
const LOAD_DAILY_KWH = 960 // 24h 基线: 12×30 + 12×50
const LOAD_ANNUAL_KWH = LOAD_DAILY_KWH * 365 // 350,400
const HP_ELECTRICITY_KW = 30 / 3.5 // 热需求 30 kW 恒定 / cop 3.5
const GRID_ANNUAL_KWH = LOAD_ANNUAL_KWH + HP_ELECTRICITY_KW * HOURLY_ROWS // 425,485.714…
const ENERGY_NET_CNY = -0.7 * GRID_ANNUAL_KWH // −297,840.0(封存现金流口径, 购电为支出)
const loadKwAt = (step: number): number => (step % 2 === 0 ? 30 : 50)

const EVALUATION = {
  evaluation_years: 1,
  construction_period: 0,
  operation_years: 1,
  discount_rate: 0.08,
  construction_payments: [{ t: 0, ratio: 1 }],
  maintenance_expenses: [],
}

function expectClose(actual: number, expected: number, tolerance: number): void {
  expect(Math.abs(actual - expected)).toBeLessThanOrEqual(tolerance)
}

function norm(text: string | null): string {
  return (text ?? '').replace(/\s+/g, ' ').trim()
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

test.describe('0.15 外部数据 CSV: data_repeat step 文件公开消费链', () => {
  test.setTimeout(900_000)

  test('真实 GUI 上传 → API 来源声明/绑定/提交 → 真实 Worker → 关键值对应', async ({ browser }) => {
    const username = uniqueName('es-csvdata')
    const password = strongPassword('Init')
    const projectName = uniqueName('CSV 数据项目')
    const profileId = `csv-hp-${Date.now()}`
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

    // 占位计算配置: 工作台/数据页在无配置时按 404-as-absence 读配置, 会产生
    // 预期外控制台噪声(与 device-data-upload 同处理); 装配完成后以最终配置重写。
    // outputs 含 metrics(公开配置词汇)。结果页“工程/计算步曲线”区块以
    // metrics_summary(封存载荷 "metrics" 键)为渲染门, 当前封存链未写该键,
    // 区块实测不渲染(既有缺口, 见头注); 此处仅按公开配置词汇请求 metrics。
    const placeholderConfig = await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/config`, {
      calculation_config: {
        schema: 'ies.calculation-config',
        schema_version: '2.0.0',
        mode: 'planning',
        predictions: [],
        generator: 'ies.generator.highs_milp@6.0.0',
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
    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click()
    await expect(page).toHaveURL(/\/data$/)
    await page.getByRole('button', { name: /上传数据|Upload/ }).first().click()
    await page.locator('#dset-name').fill('e2e-device-data')
    await page.locator('#dset-file').setInputFiles(CSV_PATH)
    await expect(page.getByText(/设备数据 step 文件|Device-data step file/).first()).toBeVisible()
    await expect(page.locator('#dset-offset')).toHaveCount(0)
    await page.getByRole('button', { name: /上传数据|Upload/ }).last().click()
    await expect(page.getByText(/已通过校验|Validation passed/).first()).toBeVisible({ timeout: 30_000 })
    await page.getByRole('button', { name: /^确定$|^OK$/i }).click()
    session.assertNoErrors()

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
          id: 'csv-dev-grid1', unit: 'model', type: 'model.upsert_device',
          payload: {
            name: 'grid1', device_type: 'ies.device.grid_connection', kind: 'existing',
            params: { max_import_power_kw: 800, max_export_power_kw: 200 },
            position: { x: 0.0, y: 0.0 },
          },
        },
        {
          // 外部 CSV 消费入口: 预定义槽 electricity_demand 的数据来源声明。
          id: 'csv-dev-load1', unit: 'model', type: 'model.upsert_device',
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
          id: 'csv-dev-hp1', unit: 'model', type: 'model.upsert_device',
          payload: {
            name: 'hp1', device_type: 'ies.device.heat_pump', kind: 'new',
            params: { rated_heat_kw: 600, cop: 3.5 },
            position: { x: 1.0, y: 1.0 },
          },
        },
        {
          id: 'csv-dev-heatload', unit: 'model', type: 'model.upsert_device',
          payload: {
            name: 'heatload', device_type: 'ies.device.heat_load', kind: 'new',
            params: { heat_demand: 30 },
            position: { x: 3.0, y: 1.0 },
          },
        },
        {
          id: 'csv-conn-grid-load1', unit: 'model', type: 'model.upsert_connection',
          payload: {
            name: 'c-grid-load1', from_device: 'grid1', from_port: 'electricity_import',
            to_device: 'load1', to_port: 'electricity_in',
          },
        },
        {
          id: 'csv-conn-grid-hp1', unit: 'model', type: 'model.upsert_connection',
          payload: {
            name: 'c-grid-hp1', from_device: 'grid1', from_port: 'electricity_import',
            to_device: 'hp1', to_port: 'electricity_in',
          },
        },
        {
          id: 'csv-conn-hp-heat', unit: 'model', type: 'model.upsert_connection',
          payload: {
            name: 'c-hp-heat', from_device: 'hp1', from_port: 'heat_out',
            to_device: 'heatload', to_port: 'heat_in',
          },
        },
        {
          id: 'csv-calc', unit: 'config', type: 'config.patch',
          payload: {
            mode: 'fixed_operation',
            generator: 'ies.generator.highs_milp@6.0.0',
            solver: 'ies.solver.highs@1.7.2',
            time_axis: { resolution: '1h', start: '2025-01-01T00:00:00Z' },
          },
        },
        {
          id: 'csv-finbind', unit: 'assembly', type: 'assembly.set_finance_binding',
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
          id: 'csv-tariff', unit: 'assembly', type: 'assembly.set_tariff_bindings',
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
        id: 'csv-bind-data', unit: 'dataset', type: 'dataset.bind',
        payload: { dataset_version_id: dvid, role: 'main' },
      }],
    }, token)
    rev = bindPut.revision
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/baseline-confirm`, {
      assumptions: {},
    }, token)
    const cfg = await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/config`, {
      calculation_config: {
        schema: 'ies.calculation-config',
        schema_version: '2.0.0',
        mode: 'planning',
        predictions: [],
        generator: 'ies.generator.highs_milp@6.0.0',
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

    // ---- 4. 公开任务 API 提交 → 真实 Worker 求解(见头注覆盖边界) ----
    const task = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/tasks`, {
      task_type: 'optimization',
      config: { config_revision: cfg.revision },
      evaluation: EVALUATION,
      idempotency_key: `csv-${Date.now()}`,
      parent_task_id: null,
    }, token)
    const taskId: number = task.task.id
    await expect(async () => {
      const detail = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}`, undefined, token)
      expect(detail.task.status).toBe('completed')
    }).toPass({ timeout: 420_000, intervals: [10_000] })

    // ---- 5. 冻结身份与关键值(同任务 HTTP 结果) ----
    const inputs = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/inputs`, undefined, token)
    expect((inputs.inputs.snapshot.dataset_version_ids as number[]).map(Number)).toContain(dvid)

    const viewRes = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, token)
    const result = viewRes.result
    const refs = result.hourly_refs as Array<{ rows: number; series: HourlySeriesMeta[] }>
    expect(refs.length).toBeGreaterThan(0)
    expect(refs[0].rows).toBe(HOURLY_ROWS)
    const metas = refs[0].series
    // 封存逐时引用按真实运行留存的公开输出精确匹配身份
    // (series_id = "<instance_id>.<variable_id>"), 不设同实例兜底:
    // 身份不符即显式失败, 不静默退回任意 load1 序列。
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
        { headers: { Authorization: `Bearer ${token}` } },
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

    // ---- 6. 真实 GUI: 结果页(API 重登替换了上传页会话, 新开 context 重登,
    //        结果页仅断言当前真实渲染的区块; 见头注覆盖边界) ----
    const context2: BrowserContext = await browser.newContext()
    const page2: Page = await context2.newPage()
    const session2 = await createSession(page2, username, password)
    await page2.goto(`/projects/${pid}/results`)
    await expect(page2.getByRole('button', { name: /导出 Excel/i }).first()).toBeEnabled({ timeout: 60_000 })

    // 结果摘要: 任务编号可见, 且“数据版本”解析为 GUI 上传的数据集版本。
    const summaryCard = page2.locator('section.ies-card', { hasText: '结果摘要' }).first()
    await expect(summaryCard).toBeVisible()
    await expect(summaryCard.getByText(new RegExp(`任务\\s*#${taskId}\\b`))).toBeVisible()
    await expect(summaryCard.getByText(/e2e-device-data v1/)).toBeVisible()

    // 规划解参数预览: 真实求解的规划变量(热需求 30 kW 之上取下界 50 kW)。
    const planCard = page2.locator('section.ies-card', { hasText: '规划解参数预览' }).first()
    await expect(planCard).toBeVisible({ timeout: 30_000 })
    const planRow = planCard.locator('.ies-table__body .ies-table__row', { hasText: 'hp_cap' })
    await expect(planRow).toBeVisible({ timeout: 10_000 })
    const planCells = planRow.locator('td')
    await expect(planCells.nth(0)).toHaveText('hp_cap')
    await expect(planCells.nth(1)).toHaveText('hp1')
    await expect(planCells.nth(2)).toHaveText('rated_heat_kw')
    await expect(planCells.nth(3)).toHaveText(/^50(\.0+)? kW$/)

    // 封存卡: 年度运营分量逐行与 HTTP oracle 同源, 且首行电费为手算值。
    const sealedCard = page2.locator('section.ies-card').filter({
      has: page2.getByRole('heading', { name: /评价现金流\(封存\)|Sealed evaluation/i }),
    })
    await expect(sealedCard).toBeVisible({ timeout: 30_000 })
    const annualTable = sealedCard.locator(
      'xpath=.//h3[contains(normalize-space(.), "运营年度分量")]/following-sibling::table[1]',
    )
    await expect(annualTable).toBeVisible()
    const rowCount = await annualTable.locator('tbody tr').count()
    const annualRows: Array<{ label: string; value: string }> = []
    for (let i = 0; i < rowCount; i += 1) {
      const tds = annualTable.locator('tbody tr').nth(i).locator('td')
      annualRows.push({
        label: norm(await tds.nth(0).textContent()),
        value: norm(await tds.nth(1).textContent()),
      })
    }
    type SealedAnnual = {
      financial_year: number
      energy_net: string
      variable_om: string
      fixed_om: string
      maintenance: string
      demand_charge: string
      net: string
    }
    expect(annualRows.map((r) => r.label)).toEqual(
      (fin.annual_operation as SealedAnnual[]).map((a, i) => `第${i + 1}年(t=${a.financial_year})`),
    )
    expect(annualRows.map((r) => r.value)).toEqual(
      (fin.annual_operation as SealedAnnual[]).map(
        (a) => `${a.energy_net} − ${a.variable_om} − ${a.fixed_om} − ${a.maintenance} − ${a.demand_charge} = ${a.net} ${fin.currency}`,
      ),
    )
    expectClose(Number(annualRows[0].value.split('−')[0].trim()), ENERGY_NET_CNY, 0.5)

    session2.assertNoErrors()
    await apiCtx.close()
    await context.close()
    await context2.close()
  })
})
