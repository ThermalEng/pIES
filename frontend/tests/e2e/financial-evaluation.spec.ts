// T3 0.12 财务评价页面/导出收口验收(0.14 补充: 封存卡四指标精确断言 +
// 正收益可回收场景):
//   A ISSUE-06 S3: 沿原一次真实求解, 从现有 JSON 入口组合确定金额、
//     规划容量单位费和本实例建设比例(同年第二笔不变); 草稿保存重开、
//     提交冻结后修改当前草稿, 历史结果与导出不漂移。提交 → 真实
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
import { execFileSync } from 'node:child_process'
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
  demand_charge: string
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
  annual_net_flows: string[]
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

/** 既有 JSON 入口的组合维护 + 同年固定维护; S3 不增加逐笔报告字段。 */
const AFFINE_MAINTENANCE = [
  {
    year: 3, amount: '1000.00', currency: 'CNY', terms: [
      { kind: 'scale_unit_cost', instance_id: 'hp1', property: 'rated_heat_kw', unit_cost: { value: '2', unit: 'CNY/kW' } },
      { kind: 'cost_component_share', instance_id: 'hp1', component: 'upfront_capex', ratio: 0.01 },
    ],
  },
  { year: 3, amount: '2500.50', currency: 'CNY', terms: [] },
]

/** 从下载的 xlsx 财务摘要提取 A/B 单元格；仅用 Docker 内 Python 标准库。 */
function excelSummaryPairs(xlsxPath: string): Map<string, string | number> {
  const script = String.raw`
import json, sys, zipfile, xml.etree.ElementTree as ET
z = zipfile.ZipFile(sys.argv[1])
ns = {'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main', 'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
wb = ET.fromstring(z.read('xl/workbook.xml'))
rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
targets = {x.attrib['Id']: x.attrib['Target'] for x in rels}
sheet = next(s for s in wb.find('m:sheets', ns) if s.attrib['name'] == '财务摘要')
target = targets[sheet.attrib['{'+ns['r']+'}id']]
sheet_path = target.lstrip('/') if target.startswith('/') else 'xl/' + target
shared = []
if 'xl/sharedStrings.xml' in z.namelist():
  root = ET.fromstring(z.read('xl/sharedStrings.xml'))
  shared = [''.join(t.text or '' for t in si.findall('.//m:t', ns)) for si in root]
rows = []
for row in ET.fromstring(z.read(sheet_path)).findall('.//m:sheetData/m:row', ns):
  if int(row.attrib['r']) < 3: continue
  cells = {}
  for cell in row.findall('m:c', ns):
    col = ''.join(x for x in cell.attrib['r'] if x.isalpha())
    v = cell.find('m:v', ns)
    inline = cell.find('m:is', ns)
    if cell.attrib.get('t') == 's' and v is not None: value = shared[int(v.text)]
    elif inline is not None: value = ''.join(t.text or '' for t in inline.findall('.//m:t', ns))
    elif v is not None:
      raw = v.text or ''
      try: value = int(raw)
      except ValueError:
        try: value = float(raw)
        except ValueError: value = raw
    else: value = None
    cells[col] = value
  if cells.get('A') is None: break
  rows.append([cells.get('A'), cells.get('B')])
print(json.dumps(rows, ensure_ascii=False))
`
  return new Map(JSON.parse(execFileSync('python3', ['-c', script, xlsxPath], { encoding: 'utf8' })) as Array<[string, string | number]>)
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
        `${a.energy_net} − ${a.variable_om} − ${a.fixed_om} − ${a.maintenance} − ${a.demand_charge} = ${a.net} ${fin.currency}`,
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
async function submitEvaluationViaUI(page: Page, maintenanceJson: string, verifyDraft = false): Promise<void> {
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
  await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
  await page.locator('#task-type').selectOption('optimization')
  await page.locator('#task-construction-period').fill('1')
  await page.locator('#task-operation-years').fill('2')
  await page.locator('#task-discount-rate').fill('0.08')
  await page.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
  await page.locator('#task-maintenance-expenses').fill(maintenanceJson)
  if (verifyDraft) {
    // 先取消关闭当前编辑，再等自动草稿保存完成；重新加载页面验证是服务端草稿，
    // 而非同一个未卸载的 React 输入框仍握着这份 JSON。
    await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 10_000 })
    const pid = Number(new URL(page.url()).pathname.match(/\/projects\/(\d+)/)?.[1])
    expect(pid).toBeGreaterThan(0)
    await expect.poll(async () => {
      const project = await requestJson(page.request, 'GET', `/api/projects/${pid}`)
      return project.draft.content.task_working_inputs?.maintenance_expenses
    }, { timeout: 30_000 }).toBe(maintenanceJson)
    await page.reload()
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await expect(page.locator('#task-maintenance-expenses')).toHaveValue(maintenanceJson)
    await expect(page.locator('#task-construction-payments')).toHaveValue('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
    expect(JSON.parse(await page.locator('#task-maintenance-expenses').inputValue())).toEqual(AFFINE_MAINTENANCE)
  }
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
  test('S3 组合维护草稿→提交→同次解封存→评估→Excel／旧任务不漂移', async ({ browser }) => {
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
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()

    // 确认 → 草稿保存/重开 → 提交(原入口, 固定+规划容量+本实例capex组合)。
    await submitEvaluationViaUI(page, JSON.stringify(AFFINE_MAINTENANCE), true)
    await waitCompleted(page)

    // 详情 → 结果页封存卡(同一封存事实展示)
    await openResults(page)
    await expect(page.getByText(/评价现金流\(封存\)|Sealed evaluation/i)).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(/t=K\+j/).first()).toBeVisible()
    // 会话内读取封存卡实际展示(与 HTTP oracle 的对账在其后完成)。
    const snapshot = await captureSealedCard(page)
    await expect(page.getByText(/未绑定需量计量点|No demand meter was bound/i)).toBeVisible()

    // 同任务冻结输入、实际规划容量与年度金额分别取不同封存来源对账；
    // 手算系数来自创建的财务画像: hp1 upfront_capex=2800 CNY/kW。
    const listedBefore = await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks?limit=10`)
    const taskId: number = (listedBefore.items as TaskListItem[])[0].id
    const before = await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`)
    const frozen = await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/inputs`)
    const frozenExpenses = frozen.inputs.snapshot.evaluation.maintenance_expenses as typeof AFFINE_MAINTENANCE
    expect(frozen.inputs.snapshot.schema_version).toBe('1.0.0')
    expect(frozenExpenses.map((entry) => entry.year)).toEqual([3, 3])
    expect(frozenExpenses.map((entry) => entry.terms)).toEqual(AFFINE_MAINTENANCE.map((entry) => entry.terms))
    expect(frozenExpenses.map((entry) => Number(entry.amount))).toEqual([1000, 2500.5])
    const finBefore = before.result.financial_evaluation as SealedEvaluation
    const cap = (before.result.planning_solution?.params ?? []).find(
      (item: { variable: string }) => item.variable === 'hp_cap',
    )
    expect(cap, '封存主解应含 hp_cap 规划容量').toBeTruthy()
    const capacity = Number(cap.value)
    expect(capacity).toBeGreaterThanOrEqual(50)
    expect(capacity).toBeLessThanOrEqual(1000)
    expect(Number(finBefore.construction_total)).toBeCloseTo(2800 * capacity, 5)
    const handMaintenance = 1000 + 2 * capacity + 0.01 * (2800 * capacity) + 2500.5
    expect(Number(finBefore.annual_operation[0].maintenance)).toBe(0)
    expect(Number(finBefore.annual_operation[1].maintenance)).toBeCloseTo(handMaintenance, 5)
    const op3 = finBefore.annual_operation[1]
    expect(Number(op3.fixed_om)).toBeGreaterThan(0)
    expect(Number(op3.variable_om)).toBeGreaterThan(0)
    expect(Number(op3.demand_charge)).toBe(0)
    expect(Number(op3.net)).toBeCloseTo(
      Number(op3.energy_net) - Number(op3.variable_om) - Number(op3.fixed_om) - handMaintenance - Number(op3.demand_charge), 5,
    )
    expect(Number(finBefore.annual_net_flows[3])).toBeCloseTo(Number(op3.net), 5)
    expect(finBefore.annual_net_flows.map(Number)).toEqual(finBefore.evaluation.rows.map((row) => Number(row.net)))
    const handNpv = finBefore.annual_net_flows.reduce(
      (sum, flow, year) => sum + Number(flow) / Math.pow(1.08, year), 0,
    )
    expect(Number(finBefore.evaluation.npv)).toBeCloseTo(handNpv, 4)
    expectSealedCardMatches(snapshot, finBefore)

    // 提交后修改当前草稿, 不创建第二个大模型任务；结果页旧任务必须不变。
    const changedMaintenance = '[{"year":3,"amount":"9999.00","currency":"CNY"}]'
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await page.locator('#task-maintenance-expenses').fill(changedMaintenance)
    await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
    await expect.poll(async () => {
      const p = await requestJson(page.request, 'GET', `/api/projects/${pid}`)
      return p.draft.content.task_working_inputs?.maintenance_expenses
    }, { timeout: 30_000 }).toBe(changedMaintenance)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/results/)
    expectSealedCardMatches(await captureSealedCard(page), finBefore)
    const after = await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`)
    expect(after.result.financial_evaluation).toEqual(finBefore)
    const inputsAfter = await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/inputs`)
    expect(inputsAfter.inputs.snapshot.evaluation.maintenance_expenses).toEqual(frozenExpenses)

    // 结果页已有系统评估; 显式重评估后再导出, 不重复求解全期模型。
    const historyCard = page.locator('section.ies-card').filter({
      has: page.getByRole('heading', { name: '评估历史', exact: true }),
    })
    await expect(historyCard.locator('.ies-assess-item')).toHaveCount(1, { timeout: 30_000 })
    await historyCard.getByRole('button', { name: '重新评估', exact: true }).click({ timeout: 30_000 })
    await page.getByRole('dialog').getByRole('button', { name: /重新运行系统评估|Re-run/i }).click({ timeout: 30_000 })
    await expect(historyCard.locator('.ies-assess-item')).toHaveCount(2, { timeout: 60_000 })

    // 导出页: Excel 报告下载并落盘, 供下载内容验证消费同一封存事实。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/export$/)
    const downloadPromise = page.waitForEvent('download', { timeout: 120_000 })
    await page.getByRole('button', { name: /导出 Excel 报告|Export Excel report/i }).click({ timeout: 30_000 })
    const download = await downloadPromise
    expect(download.suggestedFilename()).toMatch(/\.xlsx$/)
    const outDir = process.env.E2E_OUTPUT_DIR ?? '/app/e2e/test-results'
    const reportPath = path.join(outDir, 't3-report.xlsx')
    await download.saveAs(reportPath)

    // 真正解析下载的工作簿单元格: 关联金额应出现在同任务的年度净额、
    // 完整税后 CF、NPV；不能仅检查下载成功或页面与自身互证。
    const workbook = excelSummaryPairs(reportPath)
    const excelText = (value: string) => (/^[=+\-@]/.test(value) ? `'${value}` : value)
    expect(workbook.get('运营年第 2 年(t=3) / Year 2')).toBe(
      `能源 ${op3.energy_net} − 可变 ${op3.variable_om} ` +
      `− 固定 ${op3.fixed_om} − 维护 ${op3.maintenance} ` +
      `− 需量 ${op3.demand_charge} = 净额 ${op3.net} ${finBefore.currency}`,
    )
    expect(workbook.get('税后现金流 CF[t=3] / After-tax CF[t=3]')).toBe(
      excelText(`${finBefore.annual_net_flows[3]} ${finBefore.currency}`),
    )
    expect(workbook.get('CF[t=3] 净额 / Net')).toBe(
      excelText(`${finBefore.evaluation.rows[3].net} ${finBefore.currency}`),
    )
    expect(workbook.get('净现值 NPV / NPV')).toBe(
      excelText(`${finBefore.evaluation.npv} ${finBefore.evaluation.npv_unit}`),
    )

    // 当前草稿变化未生成新任务，仍指向原任务的同一证据包。
    const listed = await requestJson(page.request, 'GET', `/api/projects/${pid}/tasks?limit=10`)
    const items = (listed.items ?? []) as TaskListItem[]
    expect(items.map((item) => item.id)).toEqual([taskId])
    const fin = finBefore
    expect((fin as SealedEvaluation & { monthly_demand: unknown[] }).monthly_demand).toEqual([])
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
    expect(Number(fin.annual_operation[1].maintenance)).toBeCloseTo(handMaintenance, 5)
    expect(before.result.financial_absence).toBeNull()
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
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()

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
