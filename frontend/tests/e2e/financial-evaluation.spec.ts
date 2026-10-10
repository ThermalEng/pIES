// T3 0.12 财务评价页面/导出收口验收:
//   确认(K=1/N=2/付款/同年双维护) → 提交 → 真实 Worker 求解完成 →
//   详情 → 结果页封存卡(CF[-.6C,-.4C,R1,R2] 与时点映射) → 导出 Excel。
//
// 业务步骤(确认/提交/详情/结果/导出)全部走 UI; 项目/画像/模型/装配/
// 冻结/配置脚手架走 API(与场景 6 同模式); 不注入前端状态, 不 mock
// 求解。R1!=R2 已由后端链测试覆盖, 本用例断言 CF 形态、付款比例与
// 同年维护合计及页面/导出的同一封存事实展示。

import fs from 'node:fs'
import { test, expect } from '@playwright/test'
import type { Page, BrowserContext, APIRequestContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'

function dbg(line: string): void {
  fs.appendFileSync('/app/e2e/test-results/t3-debug.log', `${line}\n`)
}

const APP = process.env.E2E_APP_URL ?? 'http://web:80'

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
      heat_pump_system: {
        upfront_capex: {
          linear: { heat_kw: { unit_cost: { value: '2800', unit: 'CNY/kW' } } },
        },
        annual_fixed_om: {
          linear: { heat_kw: { unit_cost: { value: '28', unit: 'CNY/kW·a' } } },
        },
        period_variable_om: {
          linear: { consumed_kwh: { unit_cost: { value: '0.008', unit: 'CNY/kWh' } } },
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
    },
    taxes: {},
  }
}

async function scaffoldProject(
  api: APIRequestContext, token: string, projectName: string, profileId: string,
): Promise<number> {
  const created = await requestJson(api, 'POST', '/api/projects', {
    name: projectName,
    baseline_resolution: '1h',
    baseline_start_year: 2025,
    baseline_scenario_mode: 'single',
  }, token)
  const pid: number = created.project?.id ?? created.id
  await requestJson(api, 'POST', '/api/finance-profiles', {
    finance_profile: financeProfile(profileId),
  }, token)
  await requestJson(api, 'PUT', `/api/projects/${pid}/finance-profile`, {
    profile_ref: { id: profileId },
  }, token)

  let view = await requestJson(api, 'GET', `/api/projects/${pid}`, undefined, token)
  const devices = [
    {
      id: 'e2e-dev-grid1', unit: 'model', type: 'model.upsert_device',
      payload: {
        name: 'grid1', device_type: 'ies.device.grid_connection', kind: 'existing',
        params: { max_import_power_kw: 800, max_export_power_kw: 200 },
        position: { x: 0.0, y: 0.0 },
      },
    },
    {
      id: 'e2e-dev-load1', unit: 'model', type: 'model.upsert_device',
      payload: {
        name: 'load1', device_type: 'ies.device.electric_load', kind: 'new',
        params: { peak_power_kw: 200, electricity_demand: 50 },
        position: { x: 2.0, y: 0.0 },
      },
    },
    {
      id: 'e2e-dev-hp1', unit: 'model', type: 'model.upsert_device',
      payload: {
        name: 'hp1', device_type: 'ies.device.heat_pump', kind: 'new',
        params: { rated_heat_kw: 600, cop: 3.5 },
        position: { x: 1.0, y: 1.0 },
      },
    },
    {
      id: 'e2e-conn-grid-load1', unit: 'model', type: 'model.upsert_connection',
      payload: {
        name: 'c-grid-load1', from_device: 'grid1', from_port: 'electricity_import',
        to_device: 'load1', to_port: 'electricity_in',
      },
    },
    {
      id: 'e2e-conn-grid-hp1', unit: 'model', type: 'model.upsert_connection',
      payload: {
        name: 'c-grid-hp1', from_device: 'grid1', from_port: 'electricity_import',
        to_device: 'hp1', to_port: 'electricity_in',
      },
    },
    {
      id: 'e2e-dev-heatload', unit: 'model', type: 'model.upsert_device',
      payload: {
        name: 'heatload', device_type: 'ies.device.heat_load', kind: 'new',
        params: { heat_demand: 30 },
        position: { x: 3.0, y: 1.0 },
      },
    },
    {
      id: 'e2e-conn-hp-heat', unit: 'model', type: 'model.upsert_connection',
      payload: {
        name: 'c-hp-heat', from_device: 'hp1', from_port: 'heat_out',
        to_device: 'heatload', to_port: 'heat_in',
      },
    },
    {
      id: 'e2e-calc', unit: 'config', type: 'config.patch',
      payload: {
        mode: 'fixed_operation',
        generator: 'ies.generator.highs_milp@1.0.0',
        solver: 'ies.solver.highs@1.7.2',
        time_axis: { resolution: '1h', start: '2025-01-01T00:00:00Z' },
      },
    },
    {
      id: 'e2e-finbind', unit: 'assembly', type: 'assembly.set_finance_binding',
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
      id: 'e2e-tariff', unit: 'assembly', type: 'assembly.set_tariff_bindings',
      payload: {
        bindings: {
          grid_import: {
            price: 'grid_import', instance: 'grid1', interface: 'electricity_import',
            aggregation: 'integrate_positive',
          },
        },
      },
    },
  ]
  let rev: number = view.draft.revision
  const put = await requestJson(api, 'PUT', `/api/projects/${pid}/draft`, {
    expected_revision: rev, commands: devices,
  }, token)
  rev = put.revision

  await requestJson(api, 'PUT', `/api/projects/${pid}/planning-config`, {
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
  await requestJson(api, 'POST', `/api/projects/${pid}/versions`, {
    name: '计算基线', reason: 'snapshot_freeze', expected_revision: rev,
  }, token)
  // 预检门禁要求项目绑定数据集版本: 生成内置样例并绑定(恒定负荷不消费
  // 该数据, 绑定仅满足项目级数据可用门禁; 数据驱动路径由后端链测试覆盖)。
  const ds = await requestJson(api, 'POST', `/api/projects/${pid}/datasets`, {
    name: 'e2e-sample',
  }, token)
  const dsid: number = ds.dataset?.id ?? ds.id
  const sampled = await requestJson(
    api, 'POST', `/api/projects/${pid}/datasets/${dsid}/sample?resolution=1h`, {}, token,
  )
  const dvid: number = sampled.dataset_version?.id
  const bindPut = await requestJson(api, 'PUT', `/api/projects/${pid}/draft`, {
    expected_revision: rev,
    commands: [{
      id: 'e2e-bind-data', unit: 'dataset', type: 'dataset.bind',
      payload: { dataset_version_id: dvid, role: 'main' },
    }],
  }, token)
  rev = bindPut.revision
  // 提交预检门禁要求已确认财务基准(与校验页一致), 由 API 记录确认。
  await requestJson(api, 'POST', `/api/projects/${pid}/validation/baseline-confirm`, {
    assumptions: {},
  }, token)
  await requestJson(api, 'PUT', `/api/projects/${pid}/config`, {
    calculation_config: {
      schema: 'ies.calculation-config',
      schema_version: '1.0.0',
      mode: 'planning',
      predictions: [],
      generator: 'ies.generator.highs_milp@1.0.0',
      solver: 'ies.solver.highs@1.7.2',
      precision: 'medium',
      convergence: null,
      time_limit_seconds: 600,
      options: {},
      seed: null,
      outputs: ['solution'],
    },
    expected_revision: null,
  }, token)
  return pid
}

test.describe('T3: K1N2 财务评价确认/求解/结果/导出', () => {
  // 真实 HiGHS 全期求解耗时超过默认 90s 用例超时, 本文件单独放宽。
  test.setTimeout(900_000)
  test('确认→提交→完成→封存卡→Excel', async ({ browser }) => {
    const username = uniqueName('es-fin')
    const password = strongPassword('Init')
    const projectName = uniqueName('T3 财务项目')
    const profileId = `t3-hp-${Date.now()}`
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    const pid = await scaffoldProject(apiCtx.request, token, projectName, profileId)
    const pv = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, token)
    const dc = pv.draft?.content ?? pv.draft ?? {}
    dbg(`DRAFT devices=${JSON.stringify(Object.keys(dc.model?.devices ?? dc.devices ?? {}))} ` +
      `conns=${JSON.stringify((dc.model?.connections ?? dc.connections ?? []).map((c: any) => c.name ?? c))} ` +
      `bindings=${JSON.stringify(dc.dataset_bindings ?? dc.model?.dataset_bindings)} ` +
      `finbind=${JSON.stringify(Object.keys(dc.assembly?.finance_binding?.instances ?? {}))}`)
    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, token)
    const blockers = (precheck.report?.diagnostics ?? []).filter((d: any) => d.blocking)
    dbg(`PRECHECK status=${precheck.report?.status} blockers=${JSON.stringify(blockers).slice(0, 1500)}`)
    expect(blockers).toEqual([])

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+$/)

    // 确认 → 提交(评价确认块经 UI 填写, 无业务默认)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await page.locator('#task-type').selectOption('optimization')
    await page.locator('#task-construction-period').fill('1')
    await page.locator('#task-operation-years').fill('2')
    await page.locator('#task-discount-rate').fill('0.08')
    await page.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
    await page.locator('#task-maintenance-expenses').fill(
      '[{"year": 3, "amount": "1000.00", "currency": "CNY"}, {"year": 3, "amount": "2500.50", "currency": "CNY"}]',
    )
    const submitBtn = page.getByRole('button', { name: /提交任务|Submit task/i }).last()
    dbg(`SUBMIT disabled=${await submitBtn.isDisabled()}`)
    await expect(submitBtn).toBeEnabled({ timeout: 10_000 })
    await submitBtn.click({ timeout: 30_000 })
    // 提交成功后对话框关闭: 只在对话框仍存在时取文本(显式短超时, 不无期限等待)。
    const dlg = await page
      .getByRole('dialog')
      .textContent({ timeout: 5_000 })
      .catch(() => '(no dialog)')
    dbg(`DIALOG after submit: ${(dlg ?? '').slice(0, 900)}`)
    await expect(page.locator('.ies-table__row').first()).toBeVisible({ timeout: 15_000 })

    // 完成(真实 Worker 求解, 轮询详情状态)
    await page.locator('.ies-table__row').first().click({ timeout: 30_000 })
    await expect(page.locator('.ies-task-detail')).toBeVisible({ timeout: 10_000 })
    const detailBadge = page.locator('.ies-task-detail .ies-badge__label').first()
    await expect(async () => {
      const text = (await detailBadge.textContent()) ?? ''
      dbg(`POLL badge=${text.slice(0, 60)}`)
      expect(text).toMatch(/已完成|Completed/i)
    }).toPass({ timeout: 240_000, intervals: [10_000] })
    dbg('POLL passed')
    const detailText = await page.locator('.ies-task-detail').textContent().catch(() => '')
    dbg(`DETAIL full=${(detailText ?? '').slice(0, 500)}`)

    // 详情 → 结果页封存卡(同一封存事实展示)
    const viewResults = page.getByRole('link', { name: /查看结果|View results/i })
    await expect(viewResults).toBeVisible({ timeout: 10_000 })
    await viewResults.click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/results/)
    dbg('AT results page')
    await expect(page.getByText(/评价现金流\(封存\)|Sealed evaluation/i)).toBeVisible({ timeout: 30_000 })
    dbg('SEALED card visible')
    await expect(page.getByText('CF[t=0]').first()).toBeVisible()
    await expect(page.getByText('CF[t=3]').first()).toBeVisible()
    await expect(page.getByText('3500.5').first()).toBeVisible()
    await expect(page.getByText(/t=K\+j/).first()).toBeVisible()

    // 导出页: Excel 报告下载
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/export$/)
    const downloadPromise = page.waitForEvent('download', { timeout: 120_000 })
    await page.getByRole('button', { name: /导出 Excel 报告|Export Excel report/i }).click({ timeout: 30_000 })
    const download = await downloadPromise
    expect(download.suggestedFilename()).toMatch(/\.xlsx$/)

    // 结构断言经 API(UI 会话已用完, 重登拿新 token; 展示断言已在页面完成)
    const token2 = await loginToken(apiCtx.request, username, password)
    const listed = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks?limit=10`, undefined, token2)
    const raw = listed.tasks ?? listed
    const items: any[] = Array.isArray(raw) ? raw : (raw?.items ?? [])
    dbg(`TASKS count=${items.length} first=${JSON.stringify(items[0]).slice(0, 200)}`)
    const taskId: number = items[0]?.task_id ?? items[0]?.id ?? items[0]?.task?.id
    expect(taskId).toBeTruthy()
    const result = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, token2,
    )
    const fin = result.result?.financial_evaluation
    expect(fin?.status).toBe('evaluated')
    expect(fin?.evaluation_years).toBe(3)
    expect(fin?.construction_period).toBe(1)
    expect(fin?.operation_years).toBe(2)
    const total = Number(fin?.construction_total)
    expect(total).toBeGreaterThan(0)
    const rows: any[] = fin?.evaluation?.rows ?? []
    expect(rows.map((r) => r.year)).toEqual([0, 1, 2, 3])
    expect(Number(rows[0].net)).toBeCloseTo(-0.6 * total, 6)
    expect(Number(rows[1].net)).toBeCloseTo(-0.4 * total, 6)
    expect(fin?.annual_operation?.[1]?.maintenance).toBe('3500.5')
    expect(result.result?.financial_absence).toBeNull()

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })
})
