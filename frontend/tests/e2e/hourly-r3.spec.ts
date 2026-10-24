// R3 曲线读取失败可见: 整组失败不伪装成功、合法缺席有明确说明、
// 重试仅重读曲线(不重求解/重评估)、失败不清财务摘要。
//
// 成功路径真实不 mock: 求解脚手架走公开 API(与 hourly-steps 同模式);
// failure 用定向 HTTP 500 拦截 `/result/hourly` 证明曲线错误可见;
// 合法缺席用公开 metrics-only 任务(outputs=["metrics"], 与
// financial-absence A 同源)。不注库、不注入页面状态，画布拖放不测。

import { test, expect } from '@playwright/test'
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

test.describe('R3: 曲线读取失败可见', () => {
  test.setTimeout(900_000)

  test('整组失败可见、财务保留、重试恢复、无重求解', async ({ browser }) => {
    const userName = uniqueName('es-r3fail-doer')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)

    // 公开 API 准备: 项目 + 双泵规划 + 真实求解完成(与 hourly-steps 同源)。
    const pid = await scaffoldProject(
      requestJson, apiCtx.request, userToken, uniqueName('R3失败项目'), uniqueName('r3-tariff'),
    )
    const view0 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, userToken)
    const put2 = await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: view0.draft.revision,
      commands: [{
        id: 'st-dev-hp2', unit: 'model', type: 'model.upsert_device',
        payload: {
          name: 'hp2', device_type: 'ies.device.heat_pump', kind: 'new',
          params: { rated_heat_kw: 500, cop: 4.0 }, position: { x: 6.0, y: 6.0 },
        },
      }, {
        id: 'st-conn-grid-hp2', unit: 'model', type: 'model.upsert_connection',
        payload: {
          name: 'c-grid-hp2', from_device: 'grid1', from_port: 'electricity_import',
          to_device: 'hp2', to_port: 'electricity_in',
        },
      }, {
        id: 'st-finbind2', unit: 'assembly', type: 'assembly.set_finance_binding',
        payload: {
          instances: {
            grid1: { type: 'none' },
            load1: { type: 'none' },
            heatload: { type: 'none' },
            hp1: {
              type: 'costed', finance_type: 'heat_pump_system',
              drivers: {
                heat_kw: { property: 'rated_heat_kw' },
                consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
              },
            },
            hp2: {
              type: 'costed', finance_type: 'heat_pump_system',
              drivers: {
                heat_kw: { property: 'rated_heat_kw' },
                consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
              },
            },
          },
        },
      }],
    }, userToken)
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
    }, userToken)
    void put2
    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, userToken)
    expect((precheck.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking)).toEqual([])
    const cfg = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/config`, undefined, userToken)
    const task = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/tasks`, {
      task_type: 'optimization',
      config: { config_revision: cfg.revision },
      evaluation: EVALUATION,
      idempotency_key: `r3fail-${Date.now()}`,
      parent_task_id: null,
    }, userToken)
    const taskId: number = task.task.id
    await expect(async () => {
      const detail = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}`, undefined, userToken)
      expect(detail.task.status).toBe('completed')
    }).toPass({ timeout: 420_000, intervals: [10_000] })

    // 真实封存引用(失败注入前读取, 仅取期望计数/标签, 不替代被测读取)。
    const view = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, userToken,
    )
    const refs = view.result.hourly_refs as Array<{
      fields: string[]; rows: number;
      series: Array<{ series_id: string; instance_id: string; variable_id: string; unit: string }>;
    }>
    expect(refs.length).toBeGreaterThan(0)
    const metas = refs[0].series
    expect(metas.length).toBeGreaterThan(0)
    const expectedLabels = metas.map((m) => `${m.instance_id}.${m.variable_id}`)
    // 同次真实结果的已封存财务事实(直接 API 值对照, 不重算): 失败前后必须原样保留。
    const fin = view.result.financial_evaluation as {
      construction_total: unknown; construction_total_unit: string;
      currency: string; payments_applied: Array<{ t: number; ratio: unknown; amount: unknown }>;
    } | null
    expect(fin).not.toBeNull()
    const totalLine = `${String(fin!.construction_total)} ${fin!.construction_total_unit}`
    const firstPay = fin!.payments_applied[0]
    const payLine = `${String(firstPay.ratio)} × C = ${String(firstPay.amount)} ${fin!.currency}`

    // GUI: 曲线分页请求全部定向 500, 主结果与其他端点保持真实。
    const context = await browser.newContext()
    const page = await context.newPage()
    await context.route('**/result/hourly*', (route) => route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: '{"detail":"r3 injected hourly failure"}',
    }))
    // 记录失败与重试期间的写请求: 重试只许读, 不许新的任务/评估 POST。
    const writePosts: string[] = []
    page.on('request', (req) => {
      if (req.method() === 'POST' && /\/(tasks|assessments)/.test(req.url())) {
        writePosts.push(`${req.method()} ${req.url()}`)
      }
    })
    const session = await createSession(page, userName, password)
    await page.goto(`/projects/${pid}/results`)
    await expect(page.getByRole('button', { name: /导出 Excel/i }).first()).toBeEnabled({ timeout: 60_000 })

    const card = page.locator('.ies-section', { hasText: '计算步曲线' }).first()
    await expect(card).toBeVisible({ timeout: 60_000 })
    // 失败可见: 真实错误 + 重试说明, 无曲线、无缩水图例。
    await expect(card.getByText(/计算步曲线读取失败/)).toBeVisible({ timeout: 30_000 })
    // 无图表(Alert 自带图标 svg 不计在内, 以图表容器为准)、无缩水图例。
    expect(await card.locator('.ies-chart').count()).toBe(0)
    expect(await card.locator('.ies-chart__legend-item').count()).toBe(0)
    const retryBtn = card.getByRole('button', { name: '重试', exact: true })
    await expect(retryBtn).toBeVisible()
    // 财务摘要保留: 封存财务卡标题与已封存关键值原样可见, 主结果无全局错误。
    await expect(page.getByText('评价现金流(封存)').first()).toBeVisible()
    await expect(page.getByText(totalLine).first()).toBeVisible()
    await expect(page.getByText(payLine).first()).toBeVisible()
    expect(writePosts).toEqual([])

    // 解除拦截后点击重试: 真实恢复, 图例与 API 身份一致, 仍无写请求。
    await context.unroute('**/result/hourly*')
    await retryBtn.click({ timeout: 30_000 })
    await expect(async () => {
      const items = await card.locator('.ies-chart__legend-item').allInnerTexts()
      expect(items.length).toBe(expectedLabels.length)
    }).toPass({ timeout: 60_000 })
    const legends = await card.locator('.ies-chart__legend-item').allInnerTexts()
    for (const label of expectedLabels) {
      expect(legends.some((t) => t.includes(label))).toBe(true)
    }
    const note = await card.getByText(/个数据点/).first().innerText()
    expect(note).toContain(`共 ${refs[0].rows} 个数据点`)
    // 重试后封存财务关键值仍原样保留。
    await expect(page.getByText(totalLine).first()).toBeVisible()
    await expect(page.getByText(payLine).first()).toBeVisible()
    expect(writePosts).toEqual([])

    // 预期内噪声: 首轮每序列恰一次 500(单页, rows < 后端上限)。
    session.assertNoErrors([{
      method: 'GET',
      path: `/api/projects/${pid}/tasks/${taskId}/result/hourly`,
      status: 500,
      times: metas.length,
    }])
    await apiCtx.close()
    await context.close()
  })

  test('慢曲线不阻塞主结果、恢复后为真实值', async ({ browser }) => {
    const userName = uniqueName('es-r3slow-doer')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)

    // 公开 API 准备: 与失败场景同源的双泵规划 + 真实求解完成(独立求解)。
    const pid = await scaffoldProject(
      requestJson, apiCtx.request, userToken, uniqueName('R3慢曲线项目'), uniqueName('r3slow-tariff'),
    )
    const view0 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, userToken)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: view0.draft.revision,
      commands: [{
        id: 'st-dev-hp2', unit: 'model', type: 'model.upsert_device',
        payload: {
          name: 'hp2', device_type: 'ies.device.heat_pump', kind: 'new',
          params: { rated_heat_kw: 500, cop: 4.0 }, position: { x: 6.0, y: 6.0 },
        },
      }, {
        id: 'st-conn-grid-hp2', unit: 'model', type: 'model.upsert_connection',
        payload: {
          name: 'c-grid-hp2', from_device: 'grid1', from_port: 'electricity_import',
          to_device: 'hp2', to_port: 'electricity_in',
        },
      }, {
        id: 'st-finbind2', unit: 'assembly', type: 'assembly.set_finance_binding',
        payload: {
          instances: {
            grid1: { type: 'none' },
            load1: { type: 'none' },
            heatload: { type: 'none' },
            hp1: {
              type: 'costed', finance_type: 'heat_pump_system',
              drivers: {
                heat_kw: { property: 'rated_heat_kw' },
                consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
              },
            },
            hp2: {
              type: 'costed', finance_type: 'heat_pump_system',
              drivers: {
                heat_kw: { property: 'rated_heat_kw' },
                consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
              },
            },
          },
        },
      }],
    }, userToken)
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
    }, userToken)
    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, userToken)
    expect((precheck.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking)).toEqual([])
    const cfg = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/config`, undefined, userToken)
    const task = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/tasks`, {
      task_type: 'optimization',
      config: { config_revision: cfg.revision },
      evaluation: EVALUATION,
      idempotency_key: `r3slow-${Date.now()}`,
      parent_task_id: null,
    }, userToken)
    const taskId: number = task.task.id
    await expect(async () => {
      const detail = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}`, undefined, userToken)
      expect(detail.task.status).toBe('completed')
    }).toPass({ timeout: 420_000, intervals: [10_000] })

    const view = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, userToken,
    )
    const refs = view.result.hourly_refs as Array<{
      fields: string[]; rows: number;
      series: Array<{ series_id: string; instance_id: string; variable_id: string; unit: string }>;
    }>
    expect(refs.length).toBeGreaterThan(0)
    const metas = refs[0].series
    expect(metas.length).toBeGreaterThan(0)
    const expectedLabels = metas.map((m) => `${m.instance_id}.${m.variable_id}`)
    const fin = view.result.financial_evaluation as {
      construction_total: unknown; construction_total_unit: string;
    } | null
    expect(fin).not.toBeNull()
    const totalLine = `${String(fin!.construction_total)} ${fin!.construction_total_unit}`

    // GUI: 曲线分页进可控闸门挂起(route.fetch 取真实响应, 内容不 mock,
    // 只挂起不释放); 至少一个真实请求进入闸门后, 主结果必须已显示。
    const context = await browser.newContext()
    const page = await context.newPage()
    let releaseGate!: () => void
    const gate = new Promise<void>((resolve) => { releaseGate = resolve })
    let entered = false
    await context.route('**/result/hourly*', async (route) => {
      entered = true
      const res = await route.fetch()
      await gate
      await route.fulfill({ response: res })
    })
    const session = await createSession(page, userName, password)
    try {
      await page.goto(`/projects/${pid}/results`)
      await expect(page.getByRole('button', { name: /导出 Excel/i }).first()).toBeEnabled({ timeout: 60_000 })

      // 等至少一个真实曲线请求进入闸门(响应仍被挂起, 未释放)。
      await expect.poll(async () => entered, { timeout: 30_000 }).toBe(true)

      // 挂起期间: 曲线处加载中、无图表; 封存财务与规划解变量已显示。
      // 旧代码(主结果状态位于曲线 await 之后)在此会因规划解未发布而失败。
      const card = page.locator('.ies-section', { hasText: '计算步曲线' }).first()
      await expect(card).toBeVisible({ timeout: 60_000 })
      await expect(card.getByText(/正在读取计算步曲线/)).toBeVisible({ timeout: 30_000 })
      expect(await card.locator('.ies-chart').count()).toBe(0)
      await expect(page.getByText(totalLine).first()).toBeVisible({ timeout: 30_000 })
      await expect(page.getByText('hp1_cap').first()).toBeVisible({ timeout: 30_000 })
    } finally {
      // 测试失败也释放闸门, 不留挂起请求。
      releaseGate()
    }

    // 释放后真实恢复: 图例与 API 身份一致, 点数一致。
    const card = page.locator('.ies-section', { hasText: '计算步曲线' }).first()
    await expect(async () => {
      const items = await card.locator('.ies-chart__legend-item').allInnerTexts()
      expect(items.length).toBe(expectedLabels.length)
    }).toPass({ timeout: 60_000 })
    const legends = await card.locator('.ies-chart__legend-item').allInnerTexts()
    for (const label of expectedLabels) {
      expect(legends.some((t) => t.includes(label))).toBe(true)
    }
    const note = await card.getByText(/个数据点/).first().innerText()
    expect(note).toContain(`共 ${refs[0].rows} 个数据点`)

    session.assertNoErrors()
    await apiCtx.close()
    await context.close()
  })

  test('metrics-only 合法缺席有明确说明、无失败', async ({ browser }) => {
    const username = uniqueName('es-r3abs')
    const password = strongPassword('Init')
    const projectName = uniqueName('R3缺席项目')
    const profileId = `r3abs-hp-${Date.now()}`
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    // 公开 metrics-only 配置(outputs=["metrics"]): 真实求解完成但不封存逐时引用。
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, profileId, {
      outputs: ['metrics'],
    })
    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, token)
    expect((precheck.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking)).toEqual([])

    const context = await browser.newContext()
    const page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()
    // 评价确认/提交走 UI(与 financial-absence A 同源)。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await page.locator('#task-type').selectOption('optimization')
    await page.locator('#task-construction-period').fill('1')
    await page.locator('#task-operation-years').fill('2')
    await page.locator('#task-discount-rate').fill('0.08')
    await page.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
    await page.locator('#task-maintenance-expenses').fill('[]')
    const submitBtn = page.getByRole('button', { name: /提交任务|Submit task/i }).last()
    await expect(submitBtn).toBeEnabled({ timeout: 10_000 })
    await submitBtn.click({ timeout: 30_000 })
    await expect(page.locator('.ies-table__row').first()).toBeVisible({ timeout: 15_000 })
    await page.locator('.ies-table__row').first().click({ timeout: 30_000 })
    await expect(page.locator('.ies-task-detail')).toBeVisible({ timeout: 10_000 })
    const detailBadge = page.locator('.ies-task-detail .ies-badge__label').first()
    await expect(async () => {
      const text = (await detailBadge.textContent()) ?? ''
      expect(text).toMatch(/已完成|Completed/i)
    }).toPass({ timeout: 420_000, intervals: [10_000] })
    const viewResults = page.getByRole('link', { name: /查看结果|View results/i })
    await expect(viewResults).toBeVisible({ timeout: 10_000 })
    await viewResults.click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/results/)

    // 曲线缺席有明确说明: 无错误、无图表、无重试按钮。
    const card = page.locator('.ies-section', { hasText: '计算步曲线' }).first()
    await expect(card).toBeVisible({ timeout: 60_000 })
    await expect(card.getByText(/所选任务无计算步曲线数据/)).toBeVisible({ timeout: 30_000 })
    expect(await card.locator('.ies-chart').count()).toBe(0)
    expect(await card.locator('.ies-alert--error').count()).toBe(0)
    expect(await card.getByRole('button', { name: '重试', exact: true }).count()).toBe(0)

    // HTTP 与页面一致: 结果视图 hourly_refs 为 null(合法缺席, 非失败)。
    const token2 = await loginToken(apiCtx.request, username, password)
    const listed = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks?limit=10`, undefined, token2)
    const taskId: number = (listed.items ?? [])[0].id
    expect(taskId).toBeTruthy()
    const result = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, token2,
    )
    expect(result.result.task.business_outcome).toBe('normal_completion')
    expect(result.result.hourly_refs).toBeNull()

    session.assertNoErrors()
    await apiCtx.close()
    await context.close()
  })
})
