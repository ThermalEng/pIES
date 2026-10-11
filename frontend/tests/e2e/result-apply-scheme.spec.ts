// S3-B: 应用方案(来源版本基线派生新版本)GUI 真实验收。
//
// 场景: 双 heat_pump 实例下界 50/200 kW, 最小化建设费得唯一最优
// hp1_cap=50/hp2_cap=200(真实 HiGHS 求解)。求解提交与计算后编辑走公开
// API(场景准备), 应用确认/取消走 GUI; 不注库、不伪造 ComputeResult。
// 断言: 确认框展示真实参数/来源版本/替换影响; 取消零请求; 确认后新版本
// v3 为当前、双实例参数写回、计算后新增(hp3/新数据版本/新规划修订)未
// 混入、来源与历史不变; 刷新失败与成功分开报告且不重复应用。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

const EVALUATION = {
  evaluation_years: 1,
  construction_period: 0,
  operation_years: 1,
  discount_rate: 0.08,
  construction_payments: [{ t: 0, ratio: 1 }],
  maintenance_expenses: [],
}

test.describe('S3-B: 应用方案写回与派生', () => {
  test.setTimeout(900_000)

  test('确认应用派生新版本, 计算后编辑不混入', async ({ browser }) => {
    const username = uniqueName('es-s3b')
    const password = strongPassword('Init')
    const projectName = uniqueName('s3b')
    const profileId = `s3b-hp-${Date.now()}`
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, profileId)

    const view0 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, token)
    const put2 = await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: view0.draft.revision,
      commands: [{
        id: 's3b-dev-hp2', unit: 'model', type: 'model.upsert_device',
        payload: {
          name: 'hp2', device_type: 'ies.device.heat_pump', kind: 'new',
          params: { rated_heat_kw: 500, cop: 4.0 }, position: { x: 6.0, y: 6.0 },
        },
      }, {
        id: 's3b-conn-grid-hp2', unit: 'model', type: 'model.upsert_connection',
        payload: {
          name: 'c-grid-hp2', from_device: 'grid1', from_port: 'electricity_import',
          to_device: 'hp2', to_port: 'electricity_in',
        },
      }, {
        id: 's3b-finbind2', unit: 'assembly', type: 'assembly.set_finance_binding',
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
      }],
    }, token)
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
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/versions`, {
      name: '规划双泵', reason: 'manual_save', expected_revision: put2.revision,
    }, token)
    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, token)
    expect((precheck.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking)).toEqual([])

    const cfg = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/config`, undefined, token)
    const task = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/tasks`, {
      task_type: 'optimization',
      config: { config_revision: cfg.revision },
      evaluation: EVALUATION,
      idempotency_key: `s3b-${Date.now()}`,
      parent_task_id: null,
    }, token)
    const taskId: number = task.task.id
    await expect(async () => {
      const detail = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}`, undefined, token)
      expect(detail.task.status).toBe('completed')
    }).toPass({ timeout: 420_000, intervals: [10_000] })

    // 计算后编辑: 新增 hp3、数据集新版本、规划新修订(均不得带入派生状态)。
    const view1 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, token)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: view1.draft.revision,
      commands: [{
        id: 's3b-dev-hp3', unit: 'model', type: 'model.upsert_device',
        payload: {
          name: 'hp3', device_type: 'ies.device.heat_pump', kind: 'new',
          params: { rated_heat_kw: 600, cop: 3.5 }, position: { x: 9.0, y: 9.0 },
        },
      }],
    }, token)
    const dsList = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/datasets`, undefined, token)
    const dsEntry = dsList.datasets.find((d: { dataset: { name: string } }) => d.dataset.name === 'e2e-sample')
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/datasets/${dsEntry.dataset.id}/sample?resolution=1h`, {}, token)
    const planning = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/planning-config`, undefined, token)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/planning-config`, {
      planning_config: planning.planning_config, expected_revision: planning.revision,
    }, token)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    let applyPosts = 0
    await page.route('**/apply-result', async (route) => {
      if (route.request().method() === 'POST') applyPosts += 1
      await route.continue()
    })
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/results/)
    await expect(page.getByText('规划解参数预览')).toBeVisible({ timeout: 30_000 })
    const hp1Row = page.locator('.ies-table__body .ies-table__row', { hasText: 'hp1_cap' })
    await expect(hp1Row).toBeVisible({ timeout: 10_000 })
    await expect(hp1Row.getByText(/50\.0000/)).toBeVisible()
    const kv = page.locator('.ies-kv')
    await expect(kv.getByText('v2', { exact: true })).toBeVisible({ timeout: 30_000 })

    // 确认框展示真实参数/来源/影响; 取消零请求。
    await page.getByRole('button', { name: '应用方案' }).click({ timeout: 10_000 })
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible({ timeout: 10_000 })
    await expect(dialog.getByText(/来源版本:/)).toBeVisible()
    await expect(dialog.getByText(/来源版本 v2 为基线/)).toBeVisible()
    await expect(dialog.getByText(/hp1_cap/)).toBeVisible()
    await expect(dialog.getByText(/50\.0000/)).toBeVisible()
    await expect(dialog.getByText(/200\.0000/)).toBeVisible()
    await expect(dialog.getByText(/计算后新增/)).toBeVisible()
    await expect(dialog.getByText(/历史版本与证据保持不变/)).toBeVisible()
    await dialog.getByRole('button', { name: /取消/ }).click()
    await expect(dialog).toHaveCount(0)
    expect(applyPosts).toBe(0)

    // 确认应用: 成功横幅 + 摘要刷新为 v3, 只发一次请求。
    await page.getByRole('button', { name: '应用方案' }).click({ timeout: 10_000 })
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 10_000 })
    await page.getByRole('dialog').getByRole('button', { name: /确定|确认/ }).click()
    await expect(page.getByText(/已应用方案/)).toBeVisible({ timeout: 30_000 })
    expect(applyPosts).toBe(1)
    await expect(page.locator('.wb-header__meta').getByText(/版本: v3/)).toBeVisible({ timeout: 30_000 })

    // 版本历史: v3 为当前, v1/v2 保留。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('版本历史').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/versions/)
    await expect(page.locator('.ies-table__body .ies-table__row')).toHaveCount(3, { timeout: 30_000 })
    const rowV3 = page.locator('.ies-table__body .ies-table__row', { hasText: '应用' })
    await expect(rowV3).toBeVisible()
    await expect(rowV3.getByText('v3', { exact: true })).toBeVisible()
    await expect(rowV3.getByText('当前版本')).toBeVisible()

    // 模型页: hp1/hp2 在、hp3 不在(计算后编辑未混入)。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('系统建模').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/model/)
    await expect(page.getByText('hp1').first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('hp2').first()).toBeVisible()
    await expect(page.getByText('hp3')).toHaveCount(0)

    // 参数表单看到真实写回值(节点点选, 非拖放)。
    const sideStack = page.locator('.mp-side-stack')
    await page.getByText('hp1').first().click({ timeout: 10_000 })
    await expect(sideStack.locator('input[value="50"]')).toBeVisible({ timeout: 10_000 })
    await page.getByText('hp2').first().click({ timeout: 10_000 })
    await expect(sideStack.locator('input[value="200"]')).toBeVisible({ timeout: 10_000 })

    // 刷新失败与成功分开报告且不重复应用: 第二次应用时摘要 GET 500。
    await page.unrouteAll({ behavior: 'wait' })
    let projectGets = 0
    await page.route('**/apply-result', async (route) => {
      if (route.request().method() === 'POST') applyPosts += 1
      await route.continue()
    })
    await page.route('**/api/projects/*', async (route) => {
      const url = route.request().url()
      if (route.request().method() === 'GET' && /\/api\/projects\/\d+$/.test(url)) {
        projectGets += 1
        if (projectGets === 1) {
          await route.fulfill({ status: 500, contentType: 'application/json', body: '{"detail":"s3b-boom"}' })
          return
        }
      }
      await route.continue()
    })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/results/)
    await expect(page.getByText('规划解参数预览')).toBeVisible({ timeout: 30_000 })
    await page.getByRole('button', { name: '应用方案' }).click({ timeout: 10_000 })
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 10_000 })
    await page.getByRole('dialog').getByRole('button', { name: /确定|确认/ }).click()
    await expect(page.getByText(/已应用方案/)).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(/摘要刷新失败/)).toBeVisible({ timeout: 30_000 })
    expect(applyPosts).toBe(2)
    await page.goto('/')
    await expect(page.getByText(projectName)).toBeVisible({ timeout: 30_000 })

    // HTTP 同源: 父子关系/指针/来源不变/hp3 缺席(放最后, 登录接管会话)。
    const apiCtx2 = await browser.newContext({ baseURL: APP })
    const token3 = await loginToken(apiCtx2.request, username, password)
    const listed = await requestJson(apiCtx2.request, 'GET', `/api/projects/${pid}/versions`, undefined, token3)
    expect(listed.versions.map((v: { version_no: number }) => v.version_no).sort()).toEqual([1, 2, 3, 4])
    const v3 = listed.versions.find((v: { version_no: number }) => v.version_no === 3)
    expect(v3.reason).toBe('apply_result')
    const v2 = listed.versions.find((v: { version_no: number }) => v.version_no === 2)
    expect(v3.parent_version_id).toBe(v2.id)
    const view = await requestJson(apiCtx2.request, 'GET', `/api/projects/${pid}`, undefined, token3)
    const devices = view.draft.content.model.devices as Array<{ name: string; params: Record<string, number> }>
    expect(devices.map((d) => d.name)).not.toContain('hp3')
    expect(devices.find((d) => d.name === 'hp1')?.params.rated_heat_kw).toBe(50)
    expect(devices.find((d) => d.name === 'hp2')?.params.rated_heat_kw).toBe(200)
    const v2Now = listed.versions.find((v: { version_no: number }) => v.version_no === 2)
    expect(v2Now.content_object_id).toBe(v2.content_object_id)
    await apiCtx2.close()

    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${pid}`, status: 500, times: 1 },
    ])
    await context.close()
  })
})
