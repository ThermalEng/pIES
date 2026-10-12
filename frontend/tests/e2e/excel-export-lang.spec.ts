// R8 Excel 导出语言契约验收: 结果页与独立导出页真实选择中/英文并导出,
// 核对 POST 语言值与选择一致、下载文件对应语言的真实标记且同一证据来源;
// 缺省走原始 API 调用证明既有 zh 语义。
//
// 求解脚手架走公开 API(与审计 T1 同模式); 被测的选择/导出动作走 GUI;
// 下载内容用纯 node 解压逐字节核对, 不 mock 成功响应、不断言字段存在。

import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import * as zlib from 'node:zlib'
import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'
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

/** xlsx(zip) 各成员解压拼合(纯 node; openpyxl 输出无 data-descriptor)。
 * 解压异常直接抛出使测试失败, 不吞错。 */
function xlsxText(buf: Buffer): string {
  const parts: string[] = []
  let off = 0
  while (off + 30 <= buf.length) {
    if (buf.readUInt32LE(off) !== 0x04034b50) break
    const method = buf.readUInt16LE(off + 8)
    const compSize = buf.readUInt32LE(off + 18)
    const nameLen = buf.readUInt16LE(off + 26)
    const extraLen = buf.readUInt16LE(off + 28)
    const start = off + 30 + nameLen + extraLen
    const data = buf.subarray(start, start + compSize)
    parts.push((method === 8 ? zlib.inflateRawSync(data) : data).toString('utf8'))
    off = start + compSize
  }
  return parts.join('\n')
}

// 后端写入的生成语言行全文(transfers.py): zh 为“生成语言: 简体中文 /
// Language: zh”, en 为“生成语言: English / Language: en”; 报告其余
// 标题/列名固定中英双语, 本片只核对该语言行, 不宣称整份报告已本地化。
const ZH_LANG_LINE = '生成语言: 简体中文 / Language: zh'
const EN_LANG_LINE = '生成语言: English / Language: en'

interface ExcelPost {
  lang: string
  evidence_package_id: number
}

/** 监听 Excel 导出 POST, 记录语言与证据包。 */
function watchExcelPosts(page: Page): ExcelPost[] {
  const posts: ExcelPost[] = []
  page.on('request', (req) => {
    if (req.method() === 'POST' && new URL(req.url()).pathname.endsWith('/exports/excel')) {
      const body = req.postDataJSON() as { lang?: unknown; evidence_package_id?: unknown }
      posts.push({ lang: String(body.lang), evidence_package_id: Number(body.evidence_package_id) })
    }
  })
  return posts
}

async function exportAndRead(page: Page, buttonName: RegExp): Promise<Buffer> {
  const downloadPromise = page.waitForEvent('download', { timeout: 120_000 })
  await page.getByRole('button', { name: buttonName }).click({ timeout: 30_000 })
  const download = await downloadPromise
  expect(download.suggestedFilename()).toMatch(/\.xlsx$/)
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pies-exclang-')), 'report.xlsx')
  await download.saveAs(file)
  return fs.readFileSync(file)
}

test.describe('R8: Excel 导出语言', () => {
  test.setTimeout(900_000)

  test('结果页中英导出语言一致且同源, 缺省 API 为中文', async ({ browser }) => {
    const userName = uniqueName('es-exclang-doer')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)

    // 公开 API 准备: 项目 + 双泵规划 + 真实求解完成(与审计 T1 同模式)。
    const pid = await scaffoldProject(
      requestJson, apiCtx.request, userToken, uniqueName('导出语言项目'), uniqueName('exclang-tariff'),
    )
    const view0 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, userToken)
    const put2 = await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: view0.draft.revision,
      commands: [{
        id: 'exl-dev-hp2', unit: 'model', type: 'model.upsert_device',
        payload: {
          name: 'hp2', device_type: 'ies.device.heat_pump', kind: 'new',
          params: { rated_heat_kw: 500, cop: 4.0 }, position: { x: 6.0, y: 6.0 },
        },
      }, {
        id: 'exl-conn-grid-hp2', unit: 'model', type: 'model.upsert_connection',
        payload: {
          name: 'c-grid-hp2', from_device: 'grid1', from_port: 'electricity_import',
          to_device: 'hp2', to_port: 'electricity_in',
        },
      }, {
        id: 'exl-finbind2', unit: 'assembly', type: 'assembly.set_finance_binding',
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
      idempotency_key: `exclang-${Date.now()}`,
      parent_task_id: null,
    }, userToken)
    const taskId: number = task.task.id
    await expect(async () => {
      const detail = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}`, undefined, userToken)
      expect(detail.task.status).toBe('completed')
    }).toPass({ timeout: 420_000, intervals: [10_000] })

    const context = await browser.newContext()
    const page = await context.newPage()
    await createSession(page, userName, password)

    // 结果页: 默认中文导出。
    await page.goto(`/projects/${pid}/results`)
    await expect(page.getByRole('button', { name: /导出 Excel/i }).first()).toBeEnabled({ timeout: 60_000 })
    const posts = watchExcelPosts(page)
    const zhBuf = await exportAndRead(page, /导出 Excel/i)
    expect(posts.length).toBe(1)
    expect(posts[0]?.lang).toBe('zh')
    const zhPkg = posts[0]?.evidence_package_id
    expect(zhPkg).toBeGreaterThan(0)
    const zhText = xlsxText(zhBuf)
    expect(zhText).toContain(ZH_LANG_LINE)
    expect(zhText).not.toContain(EN_LANG_LINE)

    // 结果页: 切换英文导出, 同一证据来源。
    await page.locator('#export-lang').selectOption('en')
    const enBuf = await exportAndRead(page, /导出 Excel/i)
    expect(posts.length).toBe(2)
    expect(posts[1]?.lang).toBe('en')
    expect(posts[1]?.evidence_package_id).toBe(zhPkg)
    const enText = xlsxText(enBuf)
    expect(enText).toContain(EN_LANG_LINE)
    expect(enText).not.toContain(ZH_LANG_LINE)

    // 独立导出页: 先走中文默认选择→真实下载, 再切换英文导出, 同一证据来源。
    await page.goto(`/projects/${pid}/export`)
    const expZhBuf = await exportAndRead(page, /导出 Excel 报告|Export Excel report/i)
    expect(posts.length).toBe(3)
    expect(posts[2]?.lang).toBe('zh')
    expect(posts[2]?.evidence_package_id).toBe(zhPkg)
    const expZhText = xlsxText(expZhBuf)
    expect(expZhText).toContain(ZH_LANG_LINE)
    expect(expZhText).not.toContain(EN_LANG_LINE)
    await page.locator('#export-lang').selectOption('en')
    const expBuf = await exportAndRead(page, /导出 Excel 报告|Export Excel report/i)
    expect(posts.length).toBe(4)
    expect(posts[3]?.lang).toBe('en')
    expect(posts[3]?.evidence_package_id).toBe(zhPkg)
    const expText = xlsxText(expBuf)
    expect(expText).toContain(EN_LANG_LINE)
    expect(expText).not.toContain(ZH_LANG_LINE)

    // 缺省语言: 原始 API 调用不带 lang, 既有 zh 语义生效(不新增默认)。
    // GUI 登录已接管会话, API 再读前重登(此后无 GUI 动作)。
    const freshToken = await loginToken(apiCtx.request, userName, password)
    const assessments = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result/assessments`, undefined, freshToken,
    )
    const items = assessments.items as Array<{ id: number }>
    // GUI 导出前序已创建评估: 直接消费真实列表并断言存在, 不补建备用分支。
    expect(items.length).toBeGreaterThan(0)
    const assessmentId = (items[0] as { id: number }).id
    const rawExport = await apiCtx.request.post(`${APP}/api/projects/${pid}/exports/excel`, {
      headers: { Authorization: `Bearer ${freshToken}` },
      data: { evidence_package_id: zhPkg, assessment_id: assessmentId },
    })
    expect(rawExport.ok()).toBe(true)
    const token = ((await rawExport.json()) as { token: string }).token
    const dl = await apiCtx.request.fetch(
      `${APP}/api/projects/${pid}/exports/excel/download?token=${token}`,
      { headers: { Authorization: `Bearer ${freshToken}` } },
    )
    if (!dl.ok()) throw new Error(`download excel -> ${dl.status()}`)
    const defaultText = xlsxText(Buffer.from(await dl.body()))
    expect(defaultText).toContain(ZH_LANG_LINE)
    expect(defaultText).not.toContain(EN_LANG_LINE)

    await apiCtx.close()
    await context.close()
  })
})
