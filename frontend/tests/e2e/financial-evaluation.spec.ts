// T3 0.12 财务评价页面/导出收口验收:
//   确认(K=1/N=2/付款/同年双维护) → 提交 → 真实 Worker 求解完成 →
//   详情 → 结果页封存卡(CF[-.6C,-.4C,R1,R2] 与时点映射) → 导出 Excel。
//
// 业务步骤(确认/提交/详情/结果/导出)全部走 UI; 项目/画像/模型/装配/
// 冻结/配置脚手架走 API(与场景 6 同模式); 不注入前端状态, 不 mock
// 求解。R1!=R2 已由后端链测试覆盖, 本用例断言 CF 形态、付款比例与
// 同年维护合计及页面/导出的同一封存事实展示。

import fs from 'node:fs'
import path from 'node:path'
import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
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
interface SealedEvaluation {
  status: string
  evaluation_years: number
  construction_period: number
  operation_years: number
  construction_total: string
  annual_operation: Array<{ financial_year: number; maintenance: string }>
  evaluation: {
    rows: SealedCashflowRow[]
  }
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
    await expect(submitBtn).toBeEnabled({ timeout: 10_000 })
    await submitBtn.click({ timeout: 30_000 })
    await expect(page.locator('.ies-table__row').first()).toBeVisible({ timeout: 15_000 })

    // 完成(真实 Worker 求解, 轮询详情状态)
    await page.locator('.ies-table__row').first().click({ timeout: 30_000 })
    await expect(page.locator('.ies-task-detail')).toBeVisible({ timeout: 10_000 })
    const detailBadge = page.locator('.ies-task-detail .ies-badge__label').first()
    await expect(async () => {
      const text = (await detailBadge.textContent()) ?? ''
      expect(text).toMatch(/已完成|Completed/i)
    }).toPass({ timeout: 240_000, intervals: [10_000] })

    // 详情 → 结果页封存卡(同一封存事实展示)
    const viewResults = page.getByRole('link', { name: /查看结果|View results/i })
    await expect(viewResults).toBeVisible({ timeout: 10_000 })
    await viewResults.click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/results/)
    await expect(page.getByText(/评价现金流\(封存\)|Sealed evaluation/i)).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('CF[t=0]').first()).toBeVisible()
    await expect(page.getByText('CF[t=3]').first()).toBeVisible()
    await expect(page.getByText('3500.5').first()).toBeVisible()
    await expect(page.getByText(/t=K\+j/).first()).toBeVisible()

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
    // 封存事实落盘, 供下载内容验证逐项比对。
    fs.writeFileSync(path.join(outDir, 't3-sealed.json'), JSON.stringify(fin))

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })
})
