// T3 0.12 财务评价页面/导出收口验收(0.14 补充: 封存卡四指标精确断言 +
// 正收益可回收场景):
//   A 现有无解/未回收场景: 确认(K=1/N=2/付款/同年双维护) → 提交 → 真实
//     Worker 求解完成 → 详情 → 结果页封存卡(CF[-.6C,-.4C,R1,R2] 与时点
//     映射) → 导出 Excel。结果页会话内按标签读取封存卡 NPV(含单位)/
//     IRR/静态回收期/动态回收期及两张明细表, 随后以同任务 HTTP 封存结果
//     为 oracle 逐项对账: 值、单位、状态、口径(年限/建设总额/折现率/
//     价格口径)与现金流/运营年度分量逐行同源。该场景现金流全负, 期望
//     无解与未回收, 页面按缺席值“—”展示, 不把状态字段当数值。
//   B 正收益可回收场景: 真实公开输入(热泵+热负荷+售热价格+售热计费
//     绑定, 不建电负荷)真实求解回收, 验证非空 IRR 与静态/动态回收期
//     真实数值, 且页面两字段展示值不同(不是同一个字段); 不 mock 结果、
//     不注入页面状态跳过求解。
//
// 业务步骤(确认/提交/详情/结果/导出)全部走 UI; 项目/画像/模型/装配/
// 冻结/配置脚手架走 API(与场景 6 同模式); 不注入前端状态, 不 mock
// 求解。R1!=R2 已由后端链测试覆盖, 本用例断言 CF 形态、付款比例与
// 同年维护合计及页面/导出的同一封存事实展示。

import fs from 'node:fs'
import path from 'node:path'
import { test, expect } from '@playwright/test'
import type { BrowserContext, Locator, Page } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

// 公开任务列表/结果视图的契约形态(后端返回 {items, next_cursor},
// 列表项为 task_summary 含 id; 与前端 client 的 asItems 接线一致)。
interface TaskListItem {
  id: number
}
interface SealedCashflowRow {
  year: number
  net: string
  discounted: string
  cumulative: string
  cumulative_discounted: string
}
interface SealedAnnualOperation {
  financial_year: number
  energy_net: string
  variable_om: string
  fixed_om: string
  maintenance: string
  net: string
}
interface SealedEvaluation {
  status: string
  evaluation_years: number
  construction_period: number
  operation_years: number
  construction_total: string
  construction_total_unit: string
  currency: string
  price_basis: string
  base_year: number
  discount_rate: number
  payments_applied: Array<{ t: number; ratio: string; amount: string }>
  annual_operation: SealedAnnualOperation[]
  evaluation: {
    rows: SealedCashflowRow[]
    npv: string
    npv_unit: string
    irr: number | null
    irr_status: string
    static_payback: number | null
    static_payback_unit: string | null
    static_status: string
    dynamic_payback: number | null
    dynamic_payback_unit: string | null
    dynamic_status: string
    discount_rate: string
    price_basis: string
    base_year: number
    currency: string
  }
}

/** 已知封存 irr_status → 页面徽章标签(测试 oracle; 未知状态直接失败, 不猜测)。 */
const IRR_LABEL: Record<string, string> = {
  normal: '唯一解',
  no_solution: '无解',
  multiple: '多解',
  degenerate: '退化',
  out_of_domain: '超出定义域',
  numeric_failure: '数值失败',
}

/** 已知封存回收期状态 → 页面状态标签(测试 oracle; 未知状态直接失败)。 */
const PAYBACK_LABEL: Record<string, string> = {
  recovered: '已回收',
  not_recovered: '未回收',
}

function norm(text: string | null): string {
  return (text ?? '').replace(/\s+/g, ' ').trim()
}

/** 页面回收期展示口径: 值 + 年单位 + 状态(未回收时缺席值“—”)。 */
function paybackText(value: number | null, status: string): string {
  const label = PAYBACK_LABEL[status]
  expect(label, `未知封存回收期状态: ${status}`).toBeTruthy()
  return value === null ? `— ${label}` : `${value} 年 ${label}`
}

interface SealedCardSnapshot {
  header: Record<string, string>
  npv: string
  irr: string
  irrStatus: string
  staticPayback: string
  dynamicPayback: string
  annualRows: Array<{ label: string; value: string }>
  cfRows: Array<{ label: string; value: string }>
}

/**
 * 结果页会话内读取封存卡实际展示文本(按公开中文标签定位):
 * 概览口径、四指标与状态、运营年度分量表、逐年现金流表。
 * 读取值随后与同任务 HTTP 封存 oracle 对账, 不依赖页面内部状态。
 */
async function captureSealedCard(page: Page): Promise<SealedCardSnapshot> {
  const card = page.locator('section.ies-card').filter({
    has: page.getByRole('heading', { name: /评价现金流\(封存\)|Sealed evaluation/i }),
  })
  await expect(card).toBeVisible({ timeout: 30_000 })

  const block = (heading: string, tag: 'dl' | 'table') =>
    card.locator(
      `xpath=.//h3[contains(normalize-space(.), "${heading}")]/following-sibling::${tag}[1]`,
    )
  const dd = async (scope: Locator, label: string) =>
    norm(
      await scope
        .locator(`xpath=./dt[normalize-space()="${label}"]/following-sibling::dd[1]`)
        .textContent(),
    )
  const rowsOf = async (scope: Locator) => {
    const count = await scope.locator('tbody tr').count()
    const rows: Array<{ label: string; value: string }> = []
    for (let i = 0; i < count; i += 1) {
      const tds = scope.locator('tbody tr').nth(i).locator('td')
      rows.push({
        label: norm(await tds.nth(0).textContent()),
        value: norm(await tds.nth(1).textContent()),
      })
    }
    return rows
  }

  const headerDl = card.locator('dl.ies-kv').first()
  const header: Record<string, string> = {}
  for (const label of [
    '评价年限 E',
    '建设期 K',
    '运营年限 N',
    '建设总额 C',
    '折现率(冻结)',
    '价格口径',
  ]) {
    header[label] = await dd(headerDl, label)
  }
  const metricsDl = block('指标(封存)', 'dl')
  return {
    header,
    npv: await dd(metricsDl, 'NPV'),
    irr: await dd(metricsDl, '项目投资 IRR'),
    irrStatus: await dd(metricsDl, 'IRR 计算状态'),
    staticPayback: await dd(metricsDl, '静态回收期'),
    dynamicPayback: await dd(metricsDl, '动态回收期'),
    annualRows: await rowsOf(block('运营年度分量', 'table')),
    cfRows: await rowsOf(block('逐年现金流', 'table')),
  }
}

/** 封存卡快照与同任务 HTTP 封存事实(oracle)逐项对账(值/单位/状态/口径/逐行同源)。 */
function expectSealedCardMatches(snapshot: SealedCardSnapshot, fin: SealedEvaluation): void {
  const ev = fin.evaluation
  expect(snapshot.header['评价年限 E']).toBe(String(fin.evaluation_years))
  expect(snapshot.header['建设期 K']).toBe(String(fin.construction_period))
  expect(snapshot.header['运营年限 N']).toBe(String(fin.operation_years))
  expect(snapshot.header['建设总额 C']).toBe(
    `${fin.construction_total} ${fin.construction_total_unit}`,
  )
  expect(snapshot.header['折现率(冻结)']).toBe(String(fin.discount_rate))
  expect(snapshot.header['价格口径']).toBe(`${fin.price_basis} / 基年: ${fin.base_year}`)
  expect(snapshot.npv).toBe(`${ev.npv} ${ev.npv_unit}`)
  expect(snapshot.irr).toBe(ev.irr === null ? '—' : String(ev.irr))
  const irrLabel = IRR_LABEL[ev.irr_status]
  expect(irrLabel, `未知封存 irr_status: ${ev.irr_status}`).toBeTruthy()
  expect(snapshot.irrStatus).toBe(irrLabel)
  expect(snapshot.staticPayback).toBe(paybackText(ev.static_payback, ev.static_status))
  expect(snapshot.dynamicPayback).toBe(paybackText(ev.dynamic_payback, ev.dynamic_status))
  expect(snapshot.annualRows.map((r) => r.label)).toEqual(
    fin.annual_operation.map((a, i) => `第${i + 1}年(t=${a.financial_year})`),
  )
  expect(snapshot.annualRows.map((r) => r.value)).toEqual(
    fin.annual_operation.map(
      (a) =>
        `${a.energy_net} − ${a.variable_om} − ${a.fixed_om} − ${a.maintenance} = ${a.net} ${fin.currency}`,
    ),
  )
  expect(snapshot.cfRows.map((r) => r.label)).toEqual(ev.rows.map((r) => `CF[t=${r.year}]`))
  expect(snapshot.cfRows.map((r) => r.value)).toEqual(
    ev.rows.map(
      (r) =>
        `${r.net} / ${r.discounted} / ${r.cumulative} / ${r.cumulative_discounted} ${fin.currency}`,
    ),
  )
}

/** 确认 → 提交(K=1/N=2/付款 0.6+0.4/维护入参), 业务步骤经 UI。 */
async function submitEvaluationViaUI(page: Page, maintenanceJson: string): Promise<void> {
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
  await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
  await page.locator('#task-type').selectOption('optimization')
  await page.locator('#task-construction-period').fill('1')
  await page.locator('#task-operation-years').fill('2')
  await page.locator('#task-discount-rate').fill('0.08')
  await page.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
  await page.locator('#task-maintenance-expenses').fill(maintenanceJson)
  const submitBtn = page.getByRole('button', { name: /提交任务|Submit task/i }).last()
  await expect(submitBtn).toBeEnabled({ timeout: 10_000 })
  await submitBtn.click({ timeout: 30_000 })
  await expect(page.locator('.ies-table__row').first()).toBeVisible({ timeout: 15_000 })
}

/** 完成(真实 Worker 求解, 轮询详情状态)。 */
async function waitCompleted(page: Page): Promise<void> {
  await page.locator('.ies-table__row').first().click({ timeout: 30_000 })
  await expect(page.locator('.ies-task-detail')).toBeVisible({ timeout: 10_000 })
  const detailBadge = page.locator('.ies-task-detail .ies-badge__label').first()
  await expect(async () => {
    const text = (await detailBadge.textContent()) ?? ''
    expect(text).toMatch(/已完成|Completed/i)
  }).toPass({ timeout: 240_000, intervals: [10_000] })
}

/** 详情 → 结果页。 */
async function openResults(page: Page): Promise<void> {
  const viewResults = page.getByRole('link', { name: /查看结果|View results/i })
  await expect(viewResults).toBeVisible({ timeout: 10_000 })
  await viewResults.click({ timeout: 30_000 })
  await expect(page).toHaveURL(/\/results/)
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
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, profileId)
    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, token)
    const blockers = (precheck.report?.diagnostics ?? []).filter(
      (d: { blocking: boolean }) => d.blocking,
    )
    expect(blockers).toEqual([])

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+$/)

    // 确认 → 提交(评价确认块经 UI 填写, 无业务默认)
    await submitEvaluationViaUI(
      page,
      '[{"year": 3, "amount": "1000.00", "currency": "CNY"}, {"year": 3, "amount": "2500.50", "currency": "CNY"}]',
    )
    await waitCompleted(page)

    // 详情 → 结果页封存卡(同一封存事实展示)
    await openResults(page)
    await expect(page.getByText(/评价现金流\(封存\)|Sealed evaluation/i)).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(/t=K\+j/).first()).toBeVisible()
    // 会话内读取封存卡实际展示(与 HTTP oracle 的对账在其后完成)。
    const snapshot = await captureSealedCard(page)

    // 导出页: Excel 报告下载并落盘, 供下载内容验证消费同一封存事实。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/export$/)
    const downloadPromise = page.waitForEvent('download', { timeout: 120_000 })
    await page.getByRole('button', { name: /导出 Excel 报告|Export Excel report/i }).click({ timeout: 30_000 })
    const download = await downloadPromise
    expect(download.suggestedFilename()).toMatch(/\.xlsx$/)
    const outDir = process.env.E2E_OUTPUT_DIR ?? '/app/e2e/test-results'
    await download.saveAs(path.join(outDir, 't3-report.xlsx'))

    // 结构断言经 API(UI 会话已用完, 重登拿新 token; 展示断言已在页面完成)
    const token2 = await loginToken(apiCtx.request, username, password)
    const listed = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks?limit=10`, undefined, token2)
    const items = (listed.items ?? []) as TaskListItem[]
    const taskId: number = items[0].id
    expect(taskId).toBeTruthy()
    const result = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, token2,
    )
    const fin = result.result.financial_evaluation as SealedEvaluation
    expect(fin.status).toBe('evaluated')
    expect(fin.evaluation_years).toBe(3)
    expect(fin.construction_period).toBe(1)
    expect(fin.operation_years).toBe(2)
    const total = Number(fin.construction_total)
    expect(total).toBeGreaterThan(0)
    const rows = fin.evaluation.rows
    expect(rows.map((r) => r.year)).toEqual([0, 1, 2, 3])
    expect(Number(rows[0].net)).toBeCloseTo(-0.6 * total, 6)
    expect(Number(rows[1].net)).toBeCloseTo(-0.4 * total, 6)
    expect(fin.annual_operation[1].maintenance).toBe('3500.5')
    expect(result.result.financial_absence).toBeNull()
    // 负控: 该场景现金流全负(建设付款+全成本, 无收益), 封存期望无解与
    // 未回收——IRR/回收期值为 null, 状态字段是状态不是数值。
    expect(fin.evaluation.irr).toBeNull()
    expect(fin.evaluation.irr_status).toBe('no_solution')
    expect(fin.evaluation.static_payback).toBeNull()
    expect(fin.evaluation.static_status).toBe('not_recovered')
    expect(fin.evaluation.dynamic_payback).toBeNull()
    expect(fin.evaluation.dynamic_status).toBe('not_recovered')
    // 页面封存卡与同轮 HTTP oracle 对账: 值、单位、状态、口径、逐行同源。
    expectSealedCardMatches(snapshot, fin)
    // 缺席值展示: 页面以“—”呈现无解/未回收, 不把状态字段当数值。
    expect(snapshot.irr).toBe('—')
    expect(snapshot.irrStatus).toBe('无解')
    expect(snapshot.staticPayback).toBe('— 未回收')
    expect(snapshot.dynamicPayback).toBe('— 未回收')
    // 封存事实落盘, 供下载内容验证逐项比对。
    fs.writeFileSync(path.join(outDir, 't3-sealed.json'), JSON.stringify(fin))
    console.log(
      `[evidence] financial-evaluation A: project=${pid} task=${taskId} profile=${profileId} ` +
        `irr_status=${fin.evaluation.irr_status} static=${fin.evaluation.static_status} ` +
        `dynamic=${fin.evaluation.dynamic_status}`,
    )

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })

  test('B: 正收益可回收→非空 IRR 与两种回收期数值', async ({ browser }) => {
    const username = uniqueName('es-fin-pos')
    const password = strongPassword('Init')
    const projectName = uniqueName('T3 售热正收益项目')
    const profileId = `t3-hp-pos-${Date.now()}`
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    // 真实公开输入(公开契约内): 售热价格(carrier heat / direction sale,
    // CNY/kWh)与售热计费绑定(绑定热负荷 in 接口, 满足方向/载体相容规则);
    // 不建电负荷, 项目为“建热泵售热”正收益场景; 求解仍由真实 Worker 完成。
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, profileId, {
      includeElectricLoad: false,
      extraEnergyPrices: {
        heat_sale: {
          carrier: 'heat', direction: 'sale', kind: 'constant',
          value: { value: '0.6', unit: 'CNY/kWh' },
        },
      },
      extraTariffBindings: {
        heat_sale: {
          price: 'heat_sale', instance: 'heatload', interface: 'heat_in',
          aggregation: 'integrate_positive',
        },
      },
    })
    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, token)
    const blockers = (precheck.report?.diagnostics ?? []).filter(
      (d: { blocking: boolean }) => d.blocking,
    )
    expect(blockers).toEqual([])

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+$/)

    // 确认 → 提交(K=1/N=2/付款 0.6+0.4/无维护)。
    await submitEvaluationViaUI(page, '[]')
    await waitCompleted(page)
    await openResults(page)

    // 会话内读取封存卡实际展示(与 HTTP oracle 的对账在其后完成)。
    const snapshot = await captureSealedCard(page)

    // oracle: 同任务 HTTP 封存事实。
    const token2 = await loginToken(apiCtx.request, username, password)
    const listed = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks?limit=10`, undefined, token2)
    const items = (listed.items ?? []) as TaskListItem[]
    const taskId: number = items[0].id
    expect(taskId).toBeTruthy()
    const result = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, token2,
    )
    const fin = result.result.financial_evaluation as SealedEvaluation
    expect(fin.status).toBe('evaluated')
    const ev = fin.evaluation
    // 正收益可回收: 非空 IRR(唯一解)与两种回收期真实数值, 单位随值出现。
    expect(ev.irr_status).toBe('normal')
    expect(typeof ev.irr).toBe('number')
    expect(ev.irr as number).toBeGreaterThan(0)
    expect(ev.static_status).toBe('recovered')
    expect(ev.dynamic_status).toBe('recovered')
    expect(ev.static_payback_unit).toBe('year')
    expect(ev.dynamic_payback_unit).toBe('year')
    expect(typeof ev.static_payback).toBe('number')
    expect(typeof ev.dynamic_payback).toBe('number')
    expect(ev.static_payback as number).toBeGreaterThan(0)
    // 静态回收期与动态回收期不是同一个字段: 折现率 0.08>0 时动态严格更大。
    expect(ev.dynamic_payback as number).toBeGreaterThan(ev.static_payback as number)
    // 页面封存卡与同轮 HTTP oracle 对账: 值、单位、状态、口径、逐行同源。
    expectSealedCardMatches(snapshot, fin)
    expect(snapshot.irrStatus).toBe('唯一解')
    // 页面两字段展示值不同, 与 oracle 的“不是同一个字段”一致。
    expect(snapshot.staticPayback).not.toBe(snapshot.dynamicPayback)

    const outDir = process.env.E2E_OUTPUT_DIR ?? '/app/e2e/test-results'
    fs.writeFileSync(path.join(outDir, 't3-positive-sealed.json'), JSON.stringify(fin))
    console.log(
      `[evidence] financial-evaluation B: project=${pid} task=${taskId} profile=${profileId} ` +
        `irr=${ev.irr} static=${ev.static_payback} dynamic=${ev.dynamic_payback}`,
    )

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })
})
