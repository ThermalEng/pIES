// R5/R6 计算步序列闭环 GUI 验收: 公开提交→真实 HiGHS 求解→封存→
// API 分页→结果页按公开身份/单位选取曲线展示。
//
// 求解脚手架走公开 API(与 excel-export-lang 同模式); 被测的曲线显示走 GUI,
// 不注入页面状态、不 mock 成功业务、不注库。曲线名/单位/点数全部取自
// 真实 API 元数据, 不硬编码设备类型与字段名。

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

test.describe('R5/R6: 计算步序列闭环', () => {
  test.setTimeout(900_000)

  test('真实求解封存序列在结果页按身份单位展示', async ({ browser }) => {
    const userName = uniqueName('es-steps-doer')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)

    // 公开 API 准备: 项目 + 双泵规划 + 真实求解完成。
    const pid = await scaffoldProject(
      requestJson, apiCtx.request, userToken, uniqueName('步序项目'), uniqueName('steps-tariff'),
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
      idempotency_key: `steps-${Date.now()}`,
      parent_task_id: null,
    }, userToken)
    const taskId: number = task.task.id
    await expect(async () => {
      const detail = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}`, undefined, userToken)
      expect(detail.task.status).toBe('completed')
    }).toPass({ timeout: 420_000, intervals: [10_000] })

    // 真实 API 证据(在 GUI 接管会话前读取): 封存引用含身份/单位元数据。
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
    const expectedUnits = [...new Set(metas.map((m) => m.unit))]
    // 真实关键值: 各序列分页读全, 长度与引用一致、身份单位与元数据一致、
    // 值有限且至少一个序列非零(本脚手架为恒定负荷, 平坦最优解属真实求解;
    // 非常恒恢复已由后端封存闭环测试证明, 此处不重复要求方差)。
    // 另对首个序列做分段读取, 证明分页拼接与整读一致。
    let nonZero = 0
    for (const m of metas) {
      const hourlyRes = await apiCtx.request.get(
        `${APP}/api/projects/${pid}/tasks/${taskId}/result/hourly` +
        `?field=${encodeURIComponent(m.series_id)}&start=0&end=${refs[0].rows}&limit=${refs[0].rows}`,
        { headers: { Authorization: `Bearer ${userToken}` } },
      )
      expect(hourlyRes.ok()).toBe(true)
      const hourlyBody = (await hourlyRes.json()) as {
        values: number[]; total_rows: number; unit: string; instance_id: string; variable_id: string;
      }
      expect(hourlyBody.total_rows).toBe(refs[0].rows)
      expect(hourlyBody.values.length).toBe(refs[0].rows)
      expect(hourlyBody.unit).toBe(m.unit)
      expect(hourlyBody.instance_id).toBe(m.instance_id)
      expect(hourlyBody.variable_id).toBe(m.variable_id)
      for (const v of hourlyBody.values) expect(Number.isFinite(v)).toBe(true)
      if (hourlyBody.values.some((v) => v !== 0)) nonZero += 1
    }
    expect(nonZero).toBeGreaterThan(0)
    const half = Math.floor(refs[0].rows / 2)
    const part = async (start: number, end: number) => {
      const res = await apiCtx.request.get(
        `${APP}/api/projects/${pid}/tasks/${taskId}/result/hourly` +
        `?field=${encodeURIComponent(metas[0].series_id)}&start=${start}&end=${end}&limit=${refs[0].rows}`,
        { headers: { Authorization: `Bearer ${userToken}` } },
      )
      expect(res.ok()).toBe(true)
      return ((await res.json()) as { values: number[]; next_start: number | null }).values
    }
    const full = await part(0, refs[0].rows)
    expect([...(await part(0, half)), ...(await part(half, refs[0].rows))]).toEqual(full)
    // 空窗口(start>=total)同样返回同一身份和单位。
    const emptyRes = await apiCtx.request.get(
      `${APP}/api/projects/${pid}/tasks/${taskId}/result/hourly` +
      `?field=${encodeURIComponent(metas[0].series_id)}&start=${refs[0].rows + 100}&limit=10`,
      { headers: { Authorization: `Bearer ${userToken}` } },
    )
    expect(emptyRes.ok()).toBe(true)
    const emptyBody = (await emptyRes.json()) as {
      values: number[]; next_start: null; unit: string; instance_id: string; variable_id: string;
    }
    expect(emptyBody.values).toEqual([])
    expect(emptyBody.next_start).toBe(null)
    expect(emptyBody.unit).toBe(metas[0].unit)
    expect(emptyBody.instance_id).toBe(metas[0].instance_id)
    expect(emptyBody.variable_id).toBe(metas[0].variable_id)

    // GUI 被测显示。
    const context = await browser.newContext()
    const page = await context.newPage()
    await createSession(page, userName, password)
    await page.goto(`/projects/${pid}/results`)
    await expect(page.getByRole('button', { name: /导出 Excel/i }).first()).toBeEnabled({ timeout: 60_000 })

    const card = page.locator('.ies-section', { hasText: '计算步曲线' }).first()
    await expect(card).toBeVisible({ timeout: 60_000 })
    await expect(card.getByText(/按计算步展示/)).toBeVisible()
    const note = await card.getByText(/个数据点/).first().innerText()
    const noteCount = Number((note.match(/共 (\d+) 个数据点/) ?? [])[1])
    expect(noteCount).toBe(refs[0].rows)
    expect(note).not.toContain('168')
    expect(await card.getByText(/小时/).count()).toBe(0)
    expect(await card.getByText('逐时曲线').count()).toBe(0)
    // 横轴为步序号, 无 H 前缀。
    const svgTexts = await card.locator('svg text').allTextContents()
    expect(svgTexts.some((s) => /^H\d/.test(s))).toBe(false)
    expect(svgTexts).toContain('0')
    expect(svgTexts).toContain('计算步')
    // 图例与 API 身份一致, 不多不少; y 轴标题与 API 单位集合一致, 非硬编码 kW。
    const legends = await card.locator('.ies-chart__legend-item').allInnerTexts()
    expect(legends.length).toBe(expectedLabels.length)
    for (const label of expectedLabels) {
      expect(legends.some((t) => t.includes(label))).toBe(true)
    }
    const axisTitles = await card.locator('.ies-chart__axis-title').allTextContents()
    const yTitles = axisTitles.filter((t) => t !== '计算步')
    expect([...new Set(yTitles)].sort()).toEqual([...expectedUnits].sort())

    await apiCtx.close()
    await context.close()
  })
})
