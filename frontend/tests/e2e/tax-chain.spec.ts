// D3 税务主链整体验收(0.14.5-D3): 财务页录入 3.0.0 税画像 → 提交 →
// 真实求解 → 结果税额 → 系统评估 → Excel 下载对账。
//
// 物理: 光伏余电上网 + 外购热(售电计量口 grid1.electricity_export, 购热
//   计量口 boiler1.heat_out; 热负荷 heat_in 无有限上界, 不作计费端点),
//   恒定辐照 500 W/m²、恒定热需求 10 kW、无储能, 税画像经新财务页 UI
//   登记/引用(不走 API 预写税务); 规划/模型/装配/冻结/配置脚手架走 API
//   (与既有财务 spec 同模式); 画像切换后规划重存走 API(无规划 UI, 非
//   税务步骤)。求解/评估/导出全部走 UI, 不 mock。
// 断言: 提交 201; VAT 后期实缴非零、所得税非零、退款到账/期后应收、
//   税后 CF 交叉勾稽恒等、NPV 与同次解税后目标同源、页面/oracle/Excel
//   三方一致; 逐月 VAT 与逐年税基/税额/现金流为独立手算预期(Decimal
//   口径, 不从结果反推输入)。gross/net 等价复用 C2 后端证据, 此处不重复。
// 历史任务切换、无结果不可导出、财务保存错误保留编辑另立小用例。

import fs from 'node:fs'
import path from 'node:path'
import { execFileSync } from 'node:child_process'
import { test, expect } from '@playwright/test'
import type { Browser, BrowserContext, Page } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'
import { taxProfile } from './tax-chain-profile'

const OUT_DIR = process.env.E2E_OUTPUT_DIR ?? '/app/e2e/test-results'
const MAINTENANCE_JSON =
  '[{"year": 2, "amount": "100.00", "currency": "CNY"}, {"year": 3, "amount": "50.00", "currency": "CNY"}]'

// ---------------------------------------------------------------------------
// 独立手算预期(Decimal 口径, 由合成参数直接推导, 不读求解结果)
//
// 场景参数: 光伏 200 kWp(规划上界, 税后 NPV 在区间内单调递增, 最优取
// 上界)、恒定辐照 500 W/m²(STC 1000 W/m²、25 °C) → 出力 100 kW;
// 售电 0.6 CNY/kWh(不含税)、购热 0.15 CNY/kWh(不含税)、热需求 10 kW;
// 建设 3000 CNY/kWp、年固定 O&M 30 CNY/kWp·a; 评价 K=1/N=2/E=3,
// r=0.08, 付款 0.6@t0 + 0.4@t1, 维护 100(t2)/50(t3)。
// 税额: 增值税 13%(购热进项/售电销项/建设进项)、运维进项 6%、
// 维护进项 13%(金额为实际含税付款, 按 r/(1+r) 拆价); 所得税 25%。
// ---------------------------------------------------------------------------

/**
 * 逐月 VAT 实缴/结转手算(36 个税务月, 月 1..12 为建设期)。
 * carry 为扣除当月确认退款后的期末可结转留抵(封存口径)。
 */
const HAND_VAT: Array<{ paid: number; carry: number; claimed: number; arrived: number }> = [
  { paid: 0, carry: 46800, claimed: 0, arrived: 0 },
  { paid: 0, carry: 46800, claimed: 0, arrived: 0 },
  { paid: 0, carry: 46800, claimed: 0, arrived: 0 },
  { paid: 0, carry: 46800, claimed: 0, arrived: 0 },
  { paid: 0, carry: 46800, claimed: 0, arrived: 0 },
  { paid: 0, carry: 46800, claimed: 0, arrived: 0 },
  { paid: 0, carry: 46800, claimed: 0, arrived: 0 },
  { paid: 0, carry: 46800, claimed: 0, arrived: 0 },
  { paid: 0, carry: 46800, claimed: 0, arrived: 0 },
  { paid: 0, carry: 46800, claimed: 0, arrived: 0 },
  { paid: 0, carry: 46800, claimed: 0, arrived: 0 },
  { paid: 0, carry: 46800, claimed: 0, arrived: 0 },
  { paid: 0, carry: 72341.88, claimed: 0, arrived: 0 },
  { paid: 0, carry: 66731.32, claimed: 500, arrived: 0 },
  { paid: 0, carry: 61073.2, claimed: 0, arrived: 0 },
  { paid: 0, carry: 55597.6, claimed: 0, arrived: 500 },
  { paid: 0, carry: 49939.48, claimed: 0, arrived: 0 },
  { paid: 0, carry: 44463.88, claimed: 0, arrived: 0 },
  { paid: 0, carry: 38805.76, claimed: 0, arrived: 0 },
  { paid: 0, carry: 31647.64, claimed: 1500, arrived: 0 },
  { paid: 0, carry: 26172.04, claimed: 0, arrived: 0 },
  { paid: 0, carry: 20513.92, claimed: 0, arrived: 0 },
  { paid: 0, carry: 15038.32, claimed: 0, arrived: 0 },
  { paid: 0, carry: 9751.704424778761, claimed: 0, arrived: 0 },
  { paid: 0, carry: 4093.584424778761, claimed: 0, arrived: 0 },
  { paid: 1016.975575221239, carry: 0, claimed: 0, arrived: 0 },
  { paid: 5658.12, carry: 0, claimed: 0, arrived: 0 },
  { paid: 5475.6, carry: 0, claimed: 0, arrived: 0 },
  { paid: 5658.12, carry: 0, claimed: 0, arrived: 0 },
  { paid: 5475.6, carry: 0, claimed: 0, arrived: 0 },
  { paid: 5658.12, carry: 0, claimed: 0, arrived: 0 },
  { paid: 5658.12, carry: 0, claimed: 0, arrived: 0 },
  { paid: 5475.6, carry: 0, claimed: 0, arrived: 0 },
  { paid: 5658.12, carry: 0, claimed: 0, arrived: 0 },
  { paid: 5475.6, carry: 0, claimed: 0, arrived: 0 },
  { paid: 5298.12, carry: 0, claimed: 0, arrived: 0 },
]

/** 逐年手算: 能源净额/固定 O&M/维护/净额、税基/税额、折旧、税后现金流。 */
const HAND_ANNUAL = {
  /** 售电毛收 − 购热毛付 = 876000×0.678 − 87600×0.1695。 */
  energyNet: 579079.8,
  /** 30 CNY/kWp·a × 200 kWp × 1.06(运维进项加成)。 */
  fixedOm: 6360,
  constructionTotal: 678000,
  annualFixedOm: 6360,
  /** 税基 = 净售电 − 净购热 − 净固定 O&M − 净折旧 − 可扣维护。 */
  incomeBase: { 1: 0, 2: 476371.504424778761, 3: 476460 },
  incomeTax: { 1: 0, 2: 119092.876106194690, 3: 119115 },
  depreciation: { 2: 30000, 3: 30000 },
  /** 税后现金流: 建设付款 + 运行净额 − VAT 实缴 + 到账退款 − 所得税。 */
  cashFlows: [
    -406800,
    -271200,
    454026.923893805310,
    397046.704424778761,
  ],
  /** 全周期税后 NPV(r=0.08)。 */
  npv: 46532.271368446514,
}

interface TaskListItem {
  id: number
}
interface SealedEvaluation {
  status: string
  evaluation_years: number
  construction_period: number
  operation_years: number
  construction_total: string
  construction_total_unit: string
  annual_fixed_om: string
  currency: string
  price_basis: string
  base_year: number
  discount_rate: number
  payments_applied: Array<{ t: number; ratio: string; amount: string }>
  net_cashflows: Array<{ id: string; t: number; direction: 'inflow' | 'outflow'; amount: string }>
  annual_operation: Array<{ financial_year: number; energy_net: string; variable_om: string; fixed_om: string; maintenance: string; demand_charge: string; net: string }>
  tax_cash: {
    vat: Array<{ pool: string; month: number; paid: string; refund_claimed: string; refund_arrived: string; carry: string }>
    refunds: Array<{ pool: string; period: number; arrival_period: number; amount: string }>
    income: Array<{ rule: string; year: number; taxable_base: string; loss_used: string; loss_generated: string; loss_expired: string; tax: string }>
    depreciation: Array<{ rule: string; base_from: { kind: 'upfront_capex_net'; finance_type: string } | { kind: 'maintenance_expense_net'; year: number; position: number }; year: number; amount: string }>
    loss_balances: Array<{ rule: string; vintage_id: string; year: number; last_usable_year: number; amount: string }>
  }
  annual_net_flows: string[]
  evaluation: {
    rows: Array<{ year: number; net: string; discounted: string; cumulative: string; cumulative_discounted: string }>
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
  }
}

function norm(text: string | null): string {
  return (text ?? '').replace(/\s+/g, ' ').trim()
}

function approx(actual: number, expected: number, rel = 1e-6, abs = 1e-6): void {
  const diff = Math.abs(actual - expected)
  expect(diff, `actual=${actual} expected=${expected}`).toBeLessThanOrEqual(Math.abs(expected) * rel + abs)
}

/** 财务页: 登记税画像全文 → 引用 → 规划失效提示可见。 */
async function registerAndBindTaxProfile(page: Page, profile: Record<string, unknown>): Promise<void> {
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('财务配置').click({ timeout: 30_000 })
  await expect(page).toHaveURL(/\/finance$/)
  await page.locator('#finance-profile-doc').fill(JSON.stringify(profile, null, 2), { timeout: 30_000 })
  await page.getByRole('button', { name: '登记', exact: true }).click({ timeout: 30_000 })
  await expect(page.getByText('登记成功')).toBeVisible({ timeout: 30_000 })
  const profileId = (profile.profile as { id: string }).id
  await page.locator('#finance-profile-select').selectOption(profileId, { timeout: 30_000 })
  await page.getByRole('button', { name: '引用/切换', exact: true }).click({ timeout: 30_000 })
  await expect(page.getByText(`已引用画像 ${profileId}`)).toBeVisible({ timeout: 30_000 })
  // 画像切换导致既有规划失效: 页面必须明示, 不暗中重建。
  await expect(page.getByText(/规划已失效|规划失效/).first()).toBeVisible({ timeout: 30_000 })
}

/** 页面当前真实登录会话下的只读/准备请求，不重新登录或替换会话。 */
async function browserJson<T>(page: Page, method: string, url: string, body?: unknown): Promise<T> {
  return page.evaluate(async ({ method, url, body }) => {
    const response = await fetch(url, {
      method,
      credentials: 'include',
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const text = await response.text()
    if (!response.ok) throw new Error(`${method} ${url} -> ${response.status}: ${text.slice(0, 500)}`)
    return (text ? JSON.parse(text) : null) as T
  }, { method, url, body })
}

async function activeSessionToken(context: BrowserContext): Promise<string> {
  const cookie = (await context.cookies(APP)).find((item) => item.name === 'ies_session')
  expect(cookie?.value).toBeTruthy()
  return cookie!.value
}

async function saveAfterTaxPlanningObjective(page: Page, projectId: number): Promise<void> {
  const saved = await browserJson<{ revision: number }>(page, 'PUT', `/api/projects/${projectId}/planning-config`, {
    planning_config: {
      objective: { sense: 'maximize', expression: 'system.after_tax_npv' },
      variables: {
        pv_cap: {
          device_ref: 'pv1', parameter: 'rated_capacity_kwp',
          lower_bound: '50', upper_bound: '200', unit: 'kWp',
        },
      },
      constraints: {},
    },
    expected_revision: null,
  })
  expect(saved.revision).toBeGreaterThan(0)
}

/** 任务中心提交(K=1/N=2/付款/维护), 走 UI。 */
async function submitEvaluationViaUI(page: Page, maintenanceJson = MAINTENANCE_JSON): Promise<void> {
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
  // 提交被阻断时对话框保留并显示诊断: 断言对话框关闭才算提交受理。
  await expect(page.getByRole('dialog')).toBeHidden({ timeout: 30_000 })
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
  }).toPass({ timeout: 300_000, intervals: [10_000] })
}

/** 详情 → 结果页。 */
async function openResults(page: Page): Promise<void> {
  const viewResults = page.getByRole('link', { name: /查看结果|View results/i })
  await expect(viewResults).toBeVisible({ timeout: 10_000 })
  await viewResults.click({ timeout: 30_000 })
  await expect(page).toHaveURL(/\/results/)
}

/** 结果页读税额节实际展示(按公开中文标题定位)。 */
async function captureTaxCard(page: Page): Promise<{
  vatRows: Array<{ label: string; value: string }>
  refundRows: Array<{ label: string; value: string }>
  incomeRows: Array<{ label: string; value: string }>
  flowRows: Array<{ label: string; value: string }>
  npv: string
}> {
  const card = page.locator('section.ies-card').filter({
    has: page.getByRole('heading', { name: /评价现金流\(封存\)|Sealed evaluation/i }),
  })
  await expect(card).toBeVisible({ timeout: 30_000 })
  const block = (heading: string) =>
    card.locator(`xpath=.//h3[contains(normalize-space(.), "${heading}")]/following-sibling::table[1]`)
  const rowsOf = async (scope: ReturnType<typeof block>) => {
    const count = await scope.locator('tbody tr').count()
    const rows: Array<{ label: string; value: string }> = []
    for (let i = 0; i < count; i += 1) {
      const tds = scope.locator('tbody tr').nth(i).locator('td')
      rows.push({ label: norm(await tds.nth(0).textContent()), value: norm(await tds.nth(1).textContent()) })
    }
    return rows
  }
  const metricsDl = card.locator(
    'xpath=.//h3[contains(normalize-space(.), "指标(封存)")]/following-sibling::dl[1]',
  )
  const npv = norm(
    await metricsDl.locator('xpath=./dt[normalize-space()="NPV"]/following-sibling::dd[1]').textContent(),
  )
  return {
    // 标题按公开中文标签前缀定位(实际标题带补充说明, 如
    // "增值税(按月, 实缴/确认/到账/结转)")。
    vatRows: await rowsOf(block('增值税(按月')),
    refundRows: await rowsOf(block('退款计划')),
    incomeRows: await rowsOf(block('所得税(按年')),
    flowRows: await rowsOf(block('完整税后现金流')),
    npv,
  }
}

/** 税后 CF 交叉勾稽: CF[t] = -付款 + 经营净额 - 月实缴 + 到账退款 - 所得税(同源恒等)。
 *
 * 全部输入取自同一封存事实(VAT 行同时给出当月实缴与到账现金); 到账按
 * 月落年, 期后年份本就不在 CF 数组下标内, 自然不计入。
 */
function expectCashIdentity(fin: SealedEvaluation): void {
  const yearOfMonth = (m: number): number => Math.ceil(m / 12)
  const opNet = new Map(fin.annual_operation.map((a) => [a.financial_year, Number(a.net)]))
  const taxByYear = new Map(fin.tax_cash.income.map((r) => [r.year, Number(r.tax)]))
  const paidByYear = new Map<number, number>()
  const arrivedByYear = new Map<number, number>()
  for (const row of fin.tax_cash.vat) {
    const y = yearOfMonth(row.month)
    paidByYear.set(y, (paidByYear.get(y) ?? 0) + Number(row.paid))
    arrivedByYear.set(y, (arrivedByYear.get(y) ?? 0) + Number(row.refund_arrived))
  }
  const payByT = new Map<number, number>()
  for (const p of fin.payments_applied) {
    payByT.set(p.t, (payByT.get(p.t) ?? 0) + Number(p.amount))
  }
  expect(fin.annual_net_flows.length).toBe(fin.evaluation_years + 1)
  fin.annual_net_flows.forEach((amount, t) => {
    const expected =
      -(payByT.get(t) ?? 0) +
      (opNet.get(t) ?? 0) -
      (paidByYear.get(t) ?? 0) +
      (arrivedByYear.get(t) ?? 0) -
      (taxByYear.get(t) ?? 0)
    approx(Number(amount), expected, 1e-6, 1e-4)
  })
}

/** 同任务 HTTP 封存 oracle(结果视图)。 */
async function fetchResult(
  page: Page,
  pid: number,
  taskId: number,
): Promise<any> {
  const result = await browserJson<any>(page, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`)
  const fin = result.result.financial_evaluation as SealedEvaluation
  expect(fin.status).toBe('evaluated')
  expect(result.result.financial_absence).toBeNull()
  return result
}

/** 同任务 HTTP 封存 oracle(结果视图)。 */
async function fetchSealed(
  page: Page,
  pid: number,
  taskId: number,
): Promise<SealedEvaluation> {
  const result = await fetchResult(page, pid, taskId)
  return result.result.financial_evaluation as SealedEvaluation
}

/** Xlsx’s financial summary is read from the downloaded ZIP/XML and compared row-for-row. */
function excelFinancialPairs(xlsxPath: string): Array<[string, string | number]> {
  const script = String.raw`
import json, sys, zipfile, xml.etree.ElementTree as ET
f = zipfile.ZipFile(sys.argv[1])
ns = {'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main', 'r':'http://schemas.openxmlformats.org/officeDocument/2006/relationships', 'p':'http://schemas.openxmlformats.org/package/2006/relationships'}
wb = ET.fromstring(f.read('xl/workbook.xml'))
rels = ET.fromstring(f.read('xl/_rels/workbook.xml.rels'))
targets = {x.attrib['Id']: x.attrib['Target'] for x in rels}
sheet = next(s for s in wb.find('m:sheets', ns) if s.attrib['name'] == '财务摘要')
target = targets[sheet.attrib['{'+ns['r']+'}id']]
path = target.lstrip('/') if target.startswith('/') else 'xl/' + target
shared = []
if 'xl/sharedStrings.xml' in f.namelist():
  root = ET.fromstring(f.read('xl/sharedStrings.xml'))
  shared = [''.join(t.text or '' for t in si.findall('.//m:t', ns)) for si in root]
rows = []
root = ET.fromstring(f.read(path))
for row in root.findall('.//m:sheetData/m:row', ns):
  if int(row.attrib['r']) < 3: continue
  cells = {}
  for c in row.findall('m:c', ns):
    col = ''.join(x for x in c.attrib['r'] if x.isalpha())
    v = c.find('m:v', ns)
    inline = c.find('m:is', ns)
    if c.attrib.get('t') == 's' and v is not None: value = shared[int(v.text)]
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
  return JSON.parse(execFileSync('python3', ['-c', script, xlsxPath], { encoding: 'utf8' })) as Array<[string, string | number]>
}

function expectedExcelFinancialPairs(fin: SealedEvaluation): Array<[string, string | number]> {
  const ccy = fin.currency
  const rows: Array<[string, string | number]> = [
    ['评价年限 E / Evaluation years', fin.evaluation_years],
    ['建设期 K(财务时点 t=0..K) / Construction period', fin.construction_period],
    ['运营年限 N(第 j 年落 t=K+j) / Operation years', fin.operation_years],
    ['币种 / Currency', ccy],
    ['价格口径 / Price basis', fin.price_basis],
    ['基年 / Base year', fin.base_year],
    ['折现率(冻结) / Discount rate', fin.discount_rate],
    ['建设总额 C / Construction total', `${fin.construction_total} ${fin.construction_total_unit}`],
  ]
  for (const item of fin.payments_applied) {
    rows.push([`建设付款 t=${item.t} / Payment t=${item.t}`, `${item.ratio} × C = ${item.amount} ${ccy}`])
  }
  for (let i = 0; i < fin.annual_operation.length; i += 1) {
    const item = fin.annual_operation[i]
    rows.push([
      `运营年第 ${i + 1} 年(t=${item.financial_year}) / Year ${i + 1}`,
      `能源 ${item.energy_net} − 可变 ${item.variable_om} ` +
        `− 固定 ${item.fixed_om} − 维护 ${item.maintenance} ` +
        `− 需量 ${item.demand_charge} = 净额 ${item.net} ${ccy}`,
    ])
  }
  for (const item of fin.evaluation.rows) {
    rows.push([`CF[t=${item.year}] 净额 / Net`, `${item.net} ${ccy}`])
    rows.push([
      `CF[t=${item.year}] 贴现/累计/贴现累计`,
      `${item.discounted} / ${item.cumulative} / ${item.cumulative_discounted} ${ccy}`,
    ])
  }
  for (const item of fin.net_cashflows) {
    rows.push([`显式净现金 ${item.id} t=${item.t} ${item.direction} / Explicit net cashflow`, `${item.amount} ${ccy}`])
  }
  if (fin.net_cashflows.length === 0) rows.push(['显式净现金 / Explicit net cashflows', '无项目 / None'])
  for (let i = 0; i < fin.annual_net_flows.length; i += 1) {
    rows.push([`税后现金流 CF[t=${i}] / After-tax CF[t=${i}]`, `${fin.annual_net_flows[i]} ${ccy}`])
  }
  for (const item of fin.tax_cash.vat) {
    const head = `增值税 ${item.pool} m=${item.month} / VAT ${item.pool} m=${item.month}`
    rows.push([`${head} 实缴 / paid`, `${item.paid} ${ccy}`])
    rows.push([`${head} 确认退款 / claimed`, `${item.refund_claimed} ${ccy}`])
    rows.push([`${head} 到账退款 / arrived`, `${item.refund_arrived} ${ccy}`])
    rows.push([`${head} 期末结转 / carry`, `${item.carry} ${ccy}`])
  }
  if (fin.tax_cash.vat.length === 0) rows.push(['增值税 / VAT', '无适用规则 / No applicable rules'])
  for (const item of fin.tax_cash.refunds) {
    const horizon = fin.evaluation_years * 12
    const post = item.arrival_period > horizon ? '(期后应收，非期内现金)' : ''
    rows.push([
      `退款 ${item.pool} 确认m=${item.period}→到账m=${item.arrival_period}${post} / Refund ${item.pool} confirm m=${item.period}→arrive m=${item.arrival_period}`,
      `${item.amount} ${ccy}`,
    ])
  }
  if (fin.tax_cash.refunds.length === 0) rows.push(['退款计划 / Refunds', '无适用规则 / No applicable rules'])
  for (const item of fin.tax_cash.income) {
    const head = `所得税 ${item.rule} y=${item.year} / Income ${item.rule} y=${item.year}`
    rows.push([`${head} 税基 / base`, `${item.taxable_base} ${ccy}`])
    rows.push([`${head} 抵亏 / used`, `${item.loss_used} ${ccy}`])
    rows.push([`${head} 新生亏损 / generated`, `${item.loss_generated} ${ccy}`])
    rows.push([`${head} 到期核销 / expired`, `${item.loss_expired} ${ccy}`])
    rows.push([`${head} 税额 / tax`, `${item.tax} ${ccy}`])
  }
  if (fin.tax_cash.income.length === 0) rows.push(['所得税 / Income tax', '无适用规则 / No applicable rules'])
  for (const item of fin.tax_cash.depreciation) {
    const source = item.base_from
    const identity = source.kind === 'upfront_capex_net'
      ? `${source.kind}:${source.finance_type}`
      : `${source.kind}:y=${source.year},#${source.position}`
    rows.push([
      `计税折旧 ${item.rule} ${identity} y=${item.year} / Depreciation ${item.rule} ${identity} y=${item.year}`,
      `${item.amount} ${ccy}`,
    ])
  }
  if (fin.tax_cash.depreciation.length === 0) rows.push(['计税折旧 / Depreciation', '无适用规则 / No applicable rules'])
  for (const item of fin.tax_cash.loss_balances) {
    rows.push([
      `亏损余额 ${item.rule} ${item.vintage_id} y=${item.year} (可用至y=${item.last_usable_year}) / Loss balance`,
      `${item.amount} ${ccy}`,
    ])
  }
  if (fin.tax_cash.loss_balances.length === 0) rows.push(['亏损余额 / Loss balances', '无适用规则 / No applicable rules'])
  rows.push(['净现值 NPV / NPV', `${fin.evaluation.npv} ${fin.evaluation.npv_unit}`])
  rows.push([
    '内部收益率 IRR / IRR',
    fin.evaluation.irr !== null ? `${fin.evaluation.irr} / ${fin.evaluation.irr_status}` : `— / ${fin.evaluation.irr_status}`,
  ])
  for (const [key, title, value, unit, status] of [
    ['static', '静态回收期', fin.evaluation.static_payback, fin.evaluation.static_payback_unit, fin.evaluation.static_status],
    ['dynamic', '动态回收期', fin.evaluation.dynamic_payback, fin.evaluation.dynamic_payback_unit, fin.evaluation.dynamic_status],
  ] as const) {
    rows.push([
      `${title} / ${key} payback`,
      value !== null ? `${value} ${unit} / ${status}` : `— / ${status}`,
    ])
  }
  return rows.map(([label, value]) => [label, typeof value === 'string' && /^[=+\-@]/.test(value) ? `'${value}` : value])
}

test.describe('D3: 税务主链(财务页→提交→求解→税额→评估→Excel)', () => {
  test.setTimeout(900_000)
  test('非零税额全链 + 三方对账', async ({ browser }) => {
    const username = uniqueName('d3-tax')
    const password = strongPassword('Init')
    const projectName = uniqueName('D3 税务主链项目')
    const profileId = `d3_base_${Date.now()}`
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    // 基础脚手架走 API(无税务画像, 不绕过被测财务步骤)，复用当前 UI 登录会话。
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, profileId, {
      scenario: 'pv_export',
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

    // 税画像经财务页 UI 登记/引用(被测财务步骤)，规划目标选择现有税后 NPV。
    const profile = taxProfile(`d3_tax_${Date.now()}`)
    await registerAndBindTaxProfile(page, profile)
    await saveAfterTaxPlanningObjective(page, pid)

    // 提交 → 真实求解 → 结果页。
    await submitEvaluationViaUI(page)
    await waitCompleted(page)
    await openResults(page)
    await expect(page.getByText(/评价现金流\(封存\)|Sealed evaluation/i)).toBeVisible({ timeout: 30_000 })
    const taxCard = await captureTaxCard(page)

    // 同任务 oracle 使用当前浏览器会话的只读请求，不另行登录接管会话。
    const listed = await browserJson<{ items: TaskListItem[] }>(page, 'GET', `/api/projects/${pid}/tasks?limit=10`)
    const taskId: number = listed.items[0].id
    expect(taskId).toBeTruthy()
    const fin = await fetchSealed(page, pid, taskId)
    const taskDetail = await browserJson<any>(page, 'GET', `/api/projects/${pid}/tasks/${taskId}`)
    const taskInputs = await browserJson<any>(page, 'GET', `/api/projects/${pid}/tasks/${taskId}/inputs`)
    expect(fin.evaluation_years).toBe(3)
    expect(fin.construction_period).toBe(1)
    expect(fin.operation_years).toBe(2)
    expect(fin.tax_cash.vat.length).toBe(36)

    // 独立合成手算: 光伏 200 kWp、恒定辐照 500 W/m² → 出力 100 kW;
    // 售电 0.6、购热 0.15(不含税), 热需求 10 kW, 8760 h/年。
    // 不从结果派生输入; 能源净额为售电毛收减购热毛付。
    expect(fin.annual_operation).toHaveLength(2)
    for (const op of fin.annual_operation) {
      approx(Number(op.energy_net), HAND_ANNUAL.energyNet, 1e-9, 1e-2)
      approx(Number(op.fixed_om), HAND_ANNUAL.fixedOm, 1e-9, 1e-2)
      approx(Number(op.variable_om), 0, 1e-9, 1e-6)
    }
    approx(Number(fin.annual_operation[0].maintenance), 100, 1e-9, 1e-6)
    approx(Number(fin.annual_operation[1].maintenance), 50, 1e-9, 1e-6)
    approx(Number(fin.construction_total), HAND_ANNUAL.constructionTotal, 1e-9, 1e-2)
    approx(Number(fin.annual_fixed_om), HAND_ANNUAL.annualFixedOm, 1e-9, 1e-2)

    // 逐月 VAT 实缴/结转/确认退款/到账退款: 36 个税务月逐月对手算。
    expect(fin.tax_cash.vat.length).toBe(36)
    for (const row of fin.tax_cash.vat) {
      const hand = HAND_VAT[row.month - 1]
      expect(hand, `手算缺第 ${row.month} 月`).toBeTruthy()
      approx(Number(row.paid), hand.paid, 1e-9, 1e-4)
      approx(Number(row.carry), hand.carry, 1e-9, 1e-4)
      approx(Number(row.refund_claimed), hand.claimed, 1e-9, 1e-6)
      approx(Number(row.refund_arrived), hand.arrived, 1e-9, 1e-6)
      // 互斥精确: 实缴与结转不得同时为正(不靠目标压紧)。
      expect(Math.min(Number(row.paid), Number(row.carry))).toBeLessThan(1e-6)
    }
    const paidTotal = fin.tax_cash.vat.reduce((s, r) => s + Number(r.paid), 0)
    expect(paidTotal).toBeGreaterThan(10000)
    const paidLate = fin.tax_cash.vat.filter((r) => r.month >= 26).map((r) => Number(r.paid))
    expect(paidLate.length).toBe(11)
    for (const v of paidLate) expect(v).toBeGreaterThan(0)

    // 所得税: 逐年税基/税额对手算(建设年无所得, 运营年无亏损结转)。
    expect(fin.tax_cash.income.length).toBe(3)
    for (const row of fin.tax_cash.income) {
      const year = row.year
      approx(Number(row.taxable_base), HAND_ANNUAL.incomeBase[year], 1e-9, 1e-2)
      approx(Number(row.tax), HAND_ANNUAL.incomeTax[year], 1e-9, 1e-2)
      // 恒等: 税额 = 税率 × (税基 - 抵亏); 本场景无期初亏损, 抵亏/新生/到期为零。
      approx(Number(row.tax), 0.25 * (Number(row.taxable_base) - Number(row.loss_used)), 1e-9, 1e-2)
      expect(Number(row.loss_used)).toBe(0)
      expect(Number(row.loss_generated)).toBe(0)
      expect(Number(row.loss_expired)).toBe(0)
    }
    // 计税折旧只解释税基, 不是现金支出。
    expect(fin.tax_cash.depreciation.length).toBe(2)
    for (const row of fin.tax_cash.depreciation) {
      approx(Number(row.amount), HAND_ANNUAL.depreciation[row.year], 1e-9, 1e-2)
    }

    // 退款: 确认期与到账期分离, 期后到账不计期内现金。
    const refundIn = fin.tax_cash.refunds.find((r) => r.period === 14)
    approx(Number(refundIn?.amount), 500, 1e-9, 1e-6)
    expect(refundIn?.arrival_period).toBe(16)
    const refundPost = fin.tax_cash.refunds.find((r) => r.period === 20)
    approx(Number(refundPost?.amount), 1500, 1e-9, 1e-6)
    expect(refundPost?.arrival_period).toBe(40)
    const arrivedY2 = fin.tax_cash.vat
      .filter((r) => r.month >= 13 && r.month <= 24)
      .reduce((s, r) => s + Number(r.refund_arrived), 0)
    expect(arrivedY2).toBe(500)
    const arrivedY3 = fin.tax_cash.vat
      .filter((r) => r.month >= 25 && r.month <= 36)
      .reduce((s, r) => s + Number(r.refund_arrived), 0)
    expect(arrivedY3).toBe(0)

    // 税后现金流逐期对手算(含 VAT 实缴、到账退款、所得税)。
    expect(fin.annual_net_flows.length).toBe(fin.evaluation_years + 1)
    fin.annual_net_flows.forEach((amount, t) => {
      approx(Number(amount), HAND_ANNUAL.cashFlows[t], 1e-9, 1e-3)
    })
    // 税后 NPV 与同次解税后目标同源(手算 NPV, 非税前口径)。
    approx(Number(fin.evaluation.npv), HAND_ANNUAL.npv, 1e-9, 1e-3)
    for (const row of fin.evaluation.rows) {
      approx(Number(row.discounted), Number(row.net) / Math.pow(1.08, row.year), 1e-9, 1e-3)
    }

    // 目标语义由解本身证明: 税后 NPV 目标下最优取规划上界 200 kWp
    // (成本最小化目标会取下界 50 kWp), 且封存 NPV 与税后手算一致。
    const fullResult = await fetchResult(page, pid, taskId)
    const pvCap = (fullResult.result.planning_solution?.params ?? []).find(
      (item: { variable: string }) => item.variable === 'pv_cap',
    )
    expect(pvCap, '封存解缺 pv_cap 规划变量').toBeTruthy()
    approx(Number(pvCap.value), 200, 1e-9, 1e-2)
    // 任务输入快照冻结了规划 revision 与财务画像(改工作区不改历史任务)。
    expect(taskInputs.inputs.frozen_refs.planning_revision).toBeGreaterThan(0)
    expect(taskInputs.inputs.frozen_refs.finance_profile_id).toBe(
      (profile.profile as { id: string }).id,
    )
    // 税后 CF 交叉勾稽恒等。
    expectCashIdentity(fin)

    // 页面税额节与 oracle 对账(行数与关键值同源)。
    expect(taxCard.vatRows.length).toBe(36)
    expect(taxCard.incomeRows.length).toBe(3)
    expect(taxCard.refundRows.length).toBe(2)
    expect(taxCard.flowRows.length).toBe(4)
    const pageCarry30 = taxCard.vatRows.find((r) => r.label.includes('m=30'))
    const oracleCarry30 = fin.tax_cash.vat.find((r) => r.month === 30)
    expect(pageCarry30?.value).toContain(String(oracleCarry30?.carry))
    expect(pageCarry30?.value).toContain(fin.currency)
    const pageRefundPost = taxCard.refundRows.find((r) => r.label.includes('m=20'))
    expect(pageRefundPost?.label).toMatch(/期后应收|receivable/i)
    const pageRefundIn = taxCard.refundRows.find((r) => r.label.includes('m=14'))
    expect(pageRefundIn?.label).not.toMatch(/期后应收|receivable/i)
    expect(taxCard.npv).toContain(fin.evaluation.npv)

    // 系统评估(结果页显式重评估) → 导出页下载 Excel。
    const historyCard = page.locator('section.ies-card').filter({
      has: page.getByRole('heading', { name: '评估历史', exact: true }),
    })
    await historyCard.getByRole('button', { name: '重新评估', exact: true }).click({ timeout: 30_000 })
    const dialog = page.getByRole('dialog')
    await expect(dialog).toBeVisible({ timeout: 30_000 })
    await dialog.getByRole('button', { name: /重新运行系统评估|Re-run/i }).click({ timeout: 30_000 })
    await expect(historyCard.locator('.ies-assess-item')).toHaveCount(2, { timeout: 60_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/export$/)
    const downloadPromise = page.waitForEvent('download', { timeout: 120_000 })
    await page.getByRole('button', { name: /导出 Excel 报告|Export Excel report/i }).click({ timeout: 30_000 })
    const download = await downloadPromise
    expect(download.suggestedFilename()).toMatch(/\.xlsx$/)
    const outPath = path.join(OUT_DIR, 'd3-tax-report.xlsx')
    await download.saveAs(outPath)
    fs.writeFileSync(path.join(OUT_DIR, 'd3-tax-sealed.json'), JSON.stringify(fin))
    const workbookPairs = excelFinancialPairs(outPath)
    expect(workbookPairs).toEqual(expectedExcelFinancialPairs(fin))
    fs.writeFileSync(path.join(OUT_DIR, 'd3-tax-xlsx-financial-pairs.json'), JSON.stringify(workbookPairs, null, 2))
    fs.writeFileSync(
      path.join(OUT_DIR, 'd3-tax-refs.json'),
      JSON.stringify({
        project: pid,
        task: taskId,
        snapshot: taskInputs.inputs.snapshot.id,
        attempts: taskDetail.task.attempts.map((attempt: { id: number; attempt_no: number; status: string }) => attempt),
        frozen_finance_profile_id: taskInputs.inputs.frozen_refs.finance_profile_id,
        profile: (profile.profile as { id: string }).id,
      }, null, 2),
    )
    console.log(
      `[evidence] D3 tax chain: project=${pid} task=${taskId} ` +
        `paid_total=${paidTotal} npv=${fin.evaluation.npv}`,
    )

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })

  test('历史任务切换：旧结果不变', async ({ browser }) => {
    const username = uniqueName('d3-switch')
    const password = strongPassword('Init')
    const projectName = uniqueName('D3 切换项目')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    // 本用例只验任务切换，税画像经 API 准备（财务页 UI 输入由主链用例覆盖）。
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, `d3-sw-${Date.now()}`, {
      scenario: 'pv_export',
    })
    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()
    const profile = taxProfile(`d3_switch_${Date.now()}`)
    await registerAndBindTaxProfile(page, profile)
    await saveAfterTaxPlanningObjective(page, pid)
    // 任务1(维护 100/50)→ 完成。
    await submitEvaluationViaUI(page)
    await waitCompleted(page)
    // 任务2(维护 200/300 差异输入)→ 完成。维护清单项数与位置必须与
    // 所得税规则的显式维护项对应(提交边界核对), 否则提交被阻断。
    await submitEvaluationViaUI(
      page,
      '[{"year": 2, "amount": "200.00", "currency": "CNY"}, {"year": 3, "amount": "300.00", "currency": "CNY"}]',
    )
    await waitCompleted(page)

    const listed = await browserJson<{ items: TaskListItem[] }>(page, 'GET', `/api/projects/${pid}/tasks?limit=10`)
    const ids = (listed.items as TaskListItem[]).map((t) => t.id).sort((a, b) => a - b)
    expect(ids.length).toBe(2)
    const task1 = ids[0]
    const finBefore = await fetchSealed(page, pid, task1)
    const finSecond = await fetchSealed(page, pid, ids[1])
    expect(Number(finBefore.annual_operation[0].maintenance)).toBe(100)
    expect(Number(finSecond.annual_operation[0].maintenance)).toBe(200)

    // 更改当前工作区画像后，任务仍读取各自的冻结财务快照。
    const changedProfile = taxProfile(`d3_switch_edited_${Date.now()}`)
    ;(changedProfile.energy_prices as Record<string, any>).elec_export.value.value = '0.4'
    await registerAndBindTaxProfile(page, changedProfile)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.locator('.ies-table__row').first().click({ timeout: 30_000 })

    // 结果页切换证据包：任务1显示维护 100，任务2显示 200。
    await openResults(page)
    const selector = page.getByLabel('证据包状态', { exact: true })
    await expect(selector).toBeVisible({ timeout: 30_000 })
    expect(await selector.locator('option').count()).toBe(2)
    const annualTable = async (): Promise<string[]> => {
      const card = page.locator('section.ies-card').filter({
        has: page.getByRole('heading', { name: /评价现金流\(封存\)|Sealed evaluation/i }),
      })
      const table = card.locator('xpath=.//h3[contains(normalize-space(.), "运营年度分量")]/following-sibling::table[1]')
      const count = await table.locator('tbody tr').count()
      const values: string[] = []
      for (let i = 0; i < count; i += 1) {
        values.push(norm(await table.locator('tbody tr').nth(i).locator('td').nth(1).textContent()))
      }
      return values
    }
    await selector.selectOption({ index: 0 })
    await expect.poll(async () => (await annualTable()).join('|'), { timeout: 30_000 }).toContain(`− ${finBefore.annual_operation[0].maintenance} − ${finBefore.annual_operation[0].demand_charge} =`)
    await selector.selectOption({ index: 1 })
    await expect.poll(async () => (await annualTable()).join('|'), { timeout: 30_000 }).toContain(`− ${finSecond.annual_operation[0].maintenance} − ${finSecond.annual_operation[0].demand_charge} =`)
    // 切回任务1：旧结果不变（与切换前封存逐项一致）。
    await selector.selectOption({ index: 0 })
    await expect.poll(async () => (await annualTable()).join('|'), { timeout: 30_000 }).toContain(`− ${finBefore.annual_operation[0].maintenance} − ${finBefore.annual_operation[0].demand_charge} =`)
    const finAfter = await fetchSealed(page, pid, task1)
    expect(finAfter).toEqual(finBefore)

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })

  test('无可用结果不可导出', async ({ browser }) => {
    const username = uniqueName('d3-noexp')
    const password = strongPassword('Init')
    const projectName = uniqueName('D3 无结果项目')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    await scaffoldProject(requestJson, apiCtx.request, token, projectName, `d3-ne-${Date.now()}`)

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/export$/)
    // 无可用结果：显示缺结果引导，不发起下载。
    await expect(page.getByText(/暂无可用结果/).first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: /导出 Excel 报告|Export Excel report/i })).toBeDisabled()

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })

  test('财务保存错误保留编辑并显示诊断', async ({ browser }) => {
    const username = uniqueName('d3-finerr')
    const password = strongPassword('Init')
    const projectName = uniqueName('D3 财务纠错项目')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, `d3-err-${Date.now()}`)

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('财务配置').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/finance$/)

    // 非法 JSON: 本地拦截, 输入保留。
    const badJson = '{"schema": "ies.finance-profile", broken'
    await page.locator('#finance-profile-doc').fill(badJson, { timeout: 30_000 })
    await page.getByRole('button', { name: '登记', exact: true }).click({ timeout: 30_000 })
    await expect(page.getByText(/JSON 解析失败|parse/i).first()).toBeVisible({ timeout: 10_000 })
    expect(await page.locator('#finance-profile-doc').inputValue()).toBe(badJson)

    // 后端字段诊断: 未知税则 kind, 输入保留并展示真实 code/字段。
    const profile = taxProfile(`d3_error_${Date.now()}`)
    const badDoc = JSON.stringify({ ...(profile as Record<string, unknown>),
      tax_rules: { bogus: { kind: 'no_such_kind' } } })
    await page.locator('#finance-profile-doc').fill(badDoc, { timeout: 30_000 })
    await page.getByRole('button', { name: '登记', exact: true }).click({ timeout: 30_000 })
    await expect(page.locator('.ies-alert--error').first()).toBeVisible({ timeout: 30_000 })
    expect(await page.locator('#finance-profile-doc').inputValue()).toBe(badDoc)

    // 合法覆盖保存 → 有效合并; 404 有效(未生成)与读取失败区分展示。
    await page.locator('#finance-profile-doc').fill(JSON.stringify(profile, null, 2), { timeout: 30_000 })
    await page.getByRole('button', { name: '登记', exact: true }).click({ timeout: 30_000 })
    await expect(page.getByText('登记成功')).toBeVisible({ timeout: 30_000 })
    const profileId = (profile.profile as { id: string }).id
    await page.locator('#finance-profile-select').selectOption(profileId, { timeout: 30_000 })
    await page.getByRole('button', { name: '引用/切换', exact: true }).click({ timeout: 30_000 })
    await expect(page.getByText(`已引用画像 ${profileId}`)).toBeVisible({ timeout: 30_000 })

    await apiCtx.close()
    // 预期内的后端 400（未知税则 kind 注册）不计入会话错误。
    session.assertNoErrors([
      { method: 'POST', path: '/api/finance-profiles', status: 400, times: 1 },
    ])
    await context.close()
    void pid
    void token
  })

  test('S2: 非空维护净来源折旧与净现金→文档编辑→Worker→结果→Excel→草稿隔离', async ({ browser }) => {
    const username = uniqueName('s2-fin')
    const password = strongPassword('Init')
    const projectName = uniqueName('S2 合成净现金')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, 's2-base-' + Date.now(), {
      scenario: 'pv_export',
    })
    const context = await browser.newContext()
    const page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })

    const profile: Record<string, any> = taxProfile('s2-net-' + Date.now())
    // S2 synthetic arithmetic: gross maintenance=100 at y=2#1, VAT=13%
    // deductible. Net source=100/1.13; half recovered at E=3.
    // Replacement depreciation: net*(1-10%)/2, operating years 1/2
    // map to evaluation years K+1=2 and K+2=3. Neither is a second payment.
    profile.net_cashflows = {
      replacement_recovery: {
        at: { kind: 'evaluation_end' }, direction: 'inflow',
        amount: {
          kind: 'source_share',
          base_from: { kind: 'maintenance_expense_net', year: 2, position: 1 },
          ratio: { value: '0.5', unit: '1' },
        },
      },
    }
    profile.tax_rules.income.depreciation.push({
      base_from: { kind: 'maintenance_expense_net', year: 2, position: 1 },
      method: 'straight_line', first_year: 2, life_years: 2,
      residual: { value: '0.1', unit: '1' },
    })
    // A real full-document edit through FinancePage; no API-write shortcut.
    await registerAndBindTaxProfile(page, profile)
    await saveAfterTaxPlanningObjective(page, pid)
    await submitEvaluationViaUI(page)
    await waitCompleted(page)
    await openResults(page)
    const listed = await browserJson<{ items: TaskListItem[] }>(page, 'GET', '/api/projects/' + pid + '/tasks?limit=10')
    const taskId = Number(listed.items[0].id)
    const fin = await fetchSealed(page, pid, taskId)
    const frozenInputs = await browserJson<any>(page, 'GET', '/api/projects/' + pid + '/tasks/' + taskId + '/inputs')
    expect(frozenInputs.inputs.frozen_refs.finance_profile_id).toBe(profile.profile.id)
    expect(fin.evaluation_years).toBe(3)
    expect(fin.construction_period).toBe(1)
    expect(fin.operation_years).toBe(2)
    expect(fin.net_cashflows).toHaveLength(1)

    const sourceNet = 100 / 1.13
    const expectedReceipt = sourceNet * 0.5
    const expectedDepreciation = sourceNet * 0.9 / 2
    const receipt = fin.net_cashflows[0]
    expect(receipt).toMatchObject({ id: 'replacement_recovery', direction: 'inflow', t: 3 })
    approx(Number(receipt.amount), expectedReceipt, 1e-9, 1e-6)
    const replacementDepreciation = fin.tax_cash.depreciation.filter((row) =>
      row.rule === 'income' && row.base_from.kind === 'maintenance_expense_net' &&
      row.base_from.year === 2 && row.base_from.position === 1,
    )
    expect(replacementDepreciation.map((row) => row.year)).toEqual([2, 3])
    replacementDepreciation.forEach((row) =>
      approx(Number(row.amount), expectedDepreciation, 1e-9, 1e-6),
    )
    expect(Number(fin.annual_operation[0].maintenance)).toBe(100)
    expect(Number(fin.annual_operation[1].maintenance)).toBe(50)
    const income3 = fin.tax_cash.income.find((row) => row.rule === 'income' && row.year === 3)!
    const vat3 = fin.tax_cash.vat.filter((row) => row.month >= 25 && row.month <= 36)
    const paid3 = vat3.reduce((sum, row) => sum + Number(row.paid), 0)
    const refund3 = vat3.reduce((sum, row) => sum + Number(row.refund_arrived), 0)
    const cash3 = Number(fin.annual_operation[1].net) - paid3 + refund3 -
      Number(income3.tax) + expectedReceipt
    approx(Number(fin.annual_net_flows[3]), cash3, 1e-9, 1e-3)
    approx(Number(fin.evaluation.npv),
      fin.annual_net_flows.reduce((sum, cf, year) => sum + Number(cf) / Math.pow(1.08, year), 0),
      1e-9, 1e-3)

    // Verify visible rows in ResultsPage, including the typed source identity.
    const card = page.locator('section.ies-card').filter({
      has: page.getByRole('heading', { name: /评价现金流\(封存\)|Sealed evaluation/i }),
    })
    await expect(card.locator('tr').filter({ hasText: 'replacement_recovery / CF[t=3]' }))
      .toContainText(receipt.amount + ' ' + fin.currency)
    for (const row of replacementDepreciation) {
      await expect(card.locator('tr').filter({
        hasText: 'maintenance_expense_net: y=2, #1 / y=' + row.year,
      })).toContainText(row.amount + ' ' + fin.currency)
    }

    // Change only current editable task draft; the frozen old result and export remain unchanged.
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    const changed = '[{"year":2,"amount":"999.00","currency":"CNY"},{"year":3,"amount":"50.00","currency":"CNY"}]'
    await page.locator('#task-maintenance-expenses').fill(changed)
    await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
    await expect.poll(async () => {
      const p = await browserJson<any>(page, 'GET', '/api/projects/' + pid)
      return p.draft.content.task_working_inputs?.maintenance_expenses
    }, { timeout: 30_000 }).toBe(changed)
    const frozenAgain = await browserJson<any>(page, 'GET', '/api/projects/' + pid + '/tasks/' + taskId + '/inputs')
    expect(frozenAgain.inputs.snapshot.evaluation).toEqual(frozenInputs.inputs.snapshot.evaluation)
    expect(await fetchSealed(page, pid, taskId)).toEqual(fin)

    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    const downloadPromise = page.waitForEvent('download', { timeout: 120_000 })
    await page.getByRole('button', { name: /导出 Excel 报告|Export Excel report/i }).click({ timeout: 30_000 })
    const downloaded = await downloadPromise
    expect(downloaded.suggestedFilename()).toMatch(/\.xlsx$/)
    const outPath = path.join(OUT_DIR, 's2-net-source-report.xlsx')
    await downloaded.saveAs(outPath)
    const excel = new Map(excelFinancialPairs(outPath))
    expect(excel.get('显式净现金 replacement_recovery t=3 inflow / Explicit net cashflow'))
      .toBe(receipt.amount + ' ' + fin.currency)
    for (const row of replacementDepreciation) {
      const identity = 'maintenance_expense_net:y=2,#1 y=' + row.year
      expect(excel.get('计税折旧 income ' + identity + ' / Depreciation income ' + identity))
        .toBe(row.amount + ' ' + fin.currency)
    }
    expect(excel.get('税后现金流 CF[t=3] / After-tax CF[t=3]'))
      .toBe(fin.annual_net_flows[3] + ' ' + fin.currency)
    expect(await fetchSealed(page, pid, taskId)).toEqual(fin)

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })

})
