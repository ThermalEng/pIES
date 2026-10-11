// S3-A 规划解读取/参数预览与历史来源接线验收。
//
// 场景: 真实 HiGHS 优化求解, 同类型不同实例(hp1/hp2, 均为 heat_pump)
// 各带一个规划变量, 下界不同(50/200 kW), 最小化建设费 → 解唯一确定
// (hp1_cap=50, hp2_cap=200), 证明实例级绑定而非按类型猜。
// 项目/画像/财务/冻结脚手架走公开 API(复用 financial-scaffold), 求解提交、
// 预览查看、选择走 GUI; 不注库、不伪造 ComputeResult, 画布拖放不测。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

interface PlanParam {
  variable: string
  device_ref: string
  parameter: string
  value: number
  unit: string
}

test.describe('S3-A: 规划解参数预览与历史来源', () => {
  test.setTimeout(900_000)

  test('双实例规划解预览同源, 历史来源不受当前编辑影响', async ({ browser }) => {
    const username = uniqueName('es-s3a')
    const password = strongPassword('Init')
    const projectName = uniqueName('S3A规划解项目')
    const profileId = `s3a-hp-${Date.now()}`
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, profileId)

    // 加第二台同类型热泵 hp2(仅并网, 热侧不接), 财务绑定全量替换含 hp1+hp2。
    const view0 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, token)
    const put2 = await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: view0.draft.revision,
      commands: [
        {
          id: 's3a-dev-hp2', unit: 'model', type: 'model.upsert_device',
          payload: {
            name: 'hp2', device_type: 'ies.device.heat_pump', kind: 'new',
            params: { rated_heat_kw: 600, cop: 3.5 }, position: { x: 1.0, y: 2.0 },
          },
        },
        {
          id: 's3a-conn-grid-hp2', unit: 'model', type: 'model.upsert_connection',
          payload: {
            name: 'c-grid-hp2', from_device: 'grid1', from_port: 'electricity_import',
            to_device: 'hp2', to_port: 'electricity_in',
          },
        },
        {
          id: 's3a-finbind2', unit: 'assembly', type: 'assembly.set_finance_binding',
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
              hp2: {
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
      ],
    }, token)

    // 规划变量双实例、下界不同; 目标含两台建设费。
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/planning-config`, {
      planning_config: {
        objective: {
          sense: 'minimize',
          expression: 'hp1__upfront_capex + hp2__upfront_capex + 1 year * hp1__annual_fixed_om + 1 year * hp2__annual_fixed_om',
        },
        variables: {
          hp1_cap: { device_ref: 'hp1', parameter: 'rated_heat_kw', lower_bound: '50', upper_bound: '1000', unit: 'kW' },
          hp2_cap: { device_ref: 'hp2', parameter: 'rated_heat_kw', lower_bound: '200', upper_bound: '1000', unit: 'kW' },
        },
        constraints: {},
      },
      expected_revision: 1,
    }, token)
    const v2 = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/versions`, {
      name: '规划双泵', reason: 'manual_save', expected_revision: put2.revision,
    }, token)
    expect(v2.version.version_no).toBe(2)

    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, token)
    const blockers = (precheck.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking)
    expect(blockers).toEqual([])

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+(\/tasks)?$/)

    // 提交优化任务(K=0/N=1 小规模, 评价确认经 UI 填写无默认)。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await page.locator('#task-type').selectOption('optimization')
    await page.locator('#task-construction-period').fill('0')
    await page.locator('#task-operation-years').fill('1')
    await page.locator('#task-discount-rate').fill('0.08')
    await page.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 1}]')
    await page.locator('#task-maintenance-expenses').fill('[]')
    const submitBtn = page.getByRole('button', { name: /提交任务|Submit task/i }).last()
    await expect(submitBtn).toBeEnabled({ timeout: 10_000 })
    await submitBtn.click({ timeout: 30_000 })
    await expect(page.locator('.ies-table__row').first()).toBeVisible({ timeout: 15_000 })

    // 真实求解完成。
    await page.locator('.ies-table__row').first().click({ timeout: 30_000 })
    await expect(page.locator('.ies-task-detail')).toBeVisible({ timeout: 10_000 })
    const detailBadge = page.locator('.ies-task-detail .ies-badge__label').first()
    await expect(async () => {
      const text = (await detailBadge.textContent()) ?? ''
      expect(text).toMatch(/已完成|Completed/i)
    }).toPass({ timeout: 420_000, intervals: [10_000] })

    // 结果页规划解预览: 双实例值与绑定同源。
    await page.getByRole('link', { name: /查看结果|View results/i }).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/results/)
    await expect(page.getByText('规划解参数预览')).toBeVisible({ timeout: 30_000 })
    const hp1Row = page.locator('.ies-table__body .ies-table__row', { hasText: 'hp1_cap' })
    const hp2Row = page.locator('.ies-table__body .ies-table__row', { hasText: 'hp2_cap' })
    await expect(hp1Row).toBeVisible({ timeout: 10_000 })
    await expect(hp2Row).toBeVisible()
    await expect(hp1Row.getByText('hp1', { exact: true })).toBeVisible()
    await expect(hp1Row.getByText(/50\.0000/)).toBeVisible()
    await expect(hp2Row.getByText('hp2', { exact: true })).toBeVisible()
    await expect(hp2Row.getByText(/200\.0000/)).toBeVisible()
    // 单解证据无候选列表: 不提供选择, 不伪造候选。
    await expect(page.getByText('暂无候选')).toBeVisible()

    // 来源按原快照展示(版本 v2 + 样例数据集), 非当前最新(dt/dd 分元素)。
    const kv = page.locator('.ies-kv')
    await expect(kv.getByText('项目版本')).toBeVisible({ timeout: 30_000 })
    await expect(kv.getByText('v2', { exact: true })).toBeVisible()
    await expect(kv.getByText(/e2e-sample v1/)).toBeVisible()

    // HTTP 同源: 预览值/绑定/引用与后端一致。
    // 先把浏览器停到无轮询的项目列表页: 随后 API 登录顶号不产生前台 401。
    await page.goto('/')
    await expect(page.getByText(projectName)).toBeVisible({ timeout: 30_000 })
    const token2 = await loginToken(apiCtx.request, username, password)
    const listed = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks?limit=10`, undefined, token2)
    const taskId: number = listed.items[listed.items.length - 1].id
    const result = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, token2)
    const ps = result.result.planning_solution
    expect(ps.solution_id).toBe(0)
    expect(ps.single_solution).toBe(true)
    const params = ps.params as PlanParam[]
    const byVar = Object.fromEntries(params.map((p) => [p.variable, p]))
    expect(byVar.hp1_cap).toMatchObject({ device_ref: 'hp1', parameter: 'rated_heat_kw', value: 50, unit: 'kW' })
    expect(byVar.hp2_cap).toMatchObject({ device_ref: 'hp2', parameter: 'rated_heat_kw', value: 200, unit: 'kW' })
    expect(ps.refs.project_version_id).toBe(v2.version.id)
    expect(result.result.snapshot_refs.project_version_id).toBe(v2.version.id)

    // 当前草稿修改 + 新数据版本上传后, 重进历史结果来源/值/快照引用不变。
    const view1 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, token2)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: view1.draft.revision,
      commands: [{
        id: 's3a-dev-hp3', unit: 'model', type: 'model.upsert_device',
        payload: {
          name: 'hp3', device_type: 'ies.device.heat_pump', kind: 'new',
          params: { rated_heat_kw: 600, cop: 3.5 }, position: { x: 9.0, y: 9.0 },
        },
      }],
    }, token2)
    const dsList = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/datasets`, undefined, token2)
    const dsEntry = dsList.datasets.find((d: { dataset: { name: string } }) => d.dataset.name === 'e2e-sample')
    const dsid: number = dsEntry.dataset.id
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/datasets/${dsid}/sample?resolution=1h`, {}, token2)

    const result2 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, token2)
    expect(result2.result.planning_solution.refs.calc_snapshot_id).toBe(ps.refs.calc_snapshot_id)

    // token2 已顶替浏览器会话(单登录): 重新 GUI 登录后重进历史结果。
    // 重登录时 App 壳用旧会话探活各一次(下文精确声明), 属测试自身双登录
    // 的确定性后果, 非应用异常。
    await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/results/)
    await expect(page.getByText('规划解参数预览')).toBeVisible({ timeout: 30_000 })
    await expect(page.locator('.ies-table__body .ies-table__row', { hasText: 'hp1_cap' })).toBeVisible({ timeout: 10_000 })
    const kv2 = page.locator('.ies-kv')
    await expect(kv2.getByText('v2', { exact: true })).toBeVisible({ timeout: 30_000 })
    await expect(kv2.getByText(/e2e-sample v1/)).toBeVisible()

    await apiCtx.close()
    // 预期内噪声: API 登录顶号后、浏览器重登录前, App 壳用旧会话探活
    // /me 与项目列表各一次 401(单登录语义的确定性后果, 与 S2 同源);
    // harness 按 method+path+status+次数精确核对, 其余仍零容忍。
    session.assertNoErrors([
      { method: 'GET', path: '/api/auth/me', status: 401, times: 1 },
      { method: 'GET', path: '/api/projects', status: 401, times: 1 },
    ])
    await context.close()
  })
})
