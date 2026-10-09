// F3 收口①：价格序列文件上传后用户可取得 ref，刷新版本详情仍可取得。
//
// - 价格 CSV 经上传对话框同口上传：头扫描识别 price-series（文件分辨率
//   只读行可见，字段/UTC 偏移隐藏），与 device-data 同一自描述路径；
// - 上传成功后结果面板显示可复制的价格对象 ID（财务 time_series.ref
//   即填该值）；关闭后打开版本详情仍可见同一 oid（DB 版本行持久化，
//   非仅返回体）；刷新页面后复查仍可见。
// 文件选择用 setInputFiles（无需拖放）；独立用户 + API 前置造数。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { buildMinimalModel, createEngineer, loginToken, requestJson } from './setup/api'

const PRICE_CSV = [
  '# schema: ies.price-series',
  '# schema_version: 1.0.0',
  '# resolution: 1h',
  '# unit: CNY/kWh',
  'step,price',
  ...Array.from({ length: 24 }, (_, i) => `${i},${i % 2 === 0 ? 0.7 : 0.35}`),
].join('\n') + '\n'

test.describe('价格序列上传：对象 ID 可取且版本详情持久可查', () => {
  test('上传显示可复制 oid，刷新后版本详情仍显示同一 oid', async ({ browser }) => {
    const username = uniqueName('es-priceupload')
    const password = strongPassword('Init')
    const projectName = uniqueName('QA 价格上传项目')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    const setupToken = await loginToken(apiCtx.request, username, password)
    const createdProj = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2026,
      baseline_scenario_mode: 'single',
    }, setupToken)
    const projectId = createdProj.project?.id ?? createdProj.id
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${projectId}/config`, {
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
    }, setupToken)
    await buildMinimalModel(apiCtx.request, projectId, setupToken)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+$/)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click()
    await expect(page).toHaveURL(/\/data$/)

    // 价格文件：进入自描述模式（文件分辨率只读，字段/偏移隐藏）并上传成功。
    await page.getByRole('button', { name: /上传数据|Upload/ }).first().click()
    await page.locator('#dset-name').fill('e2e-price-series')
    await page.locator('#dset-file').setInputFiles({
      name: 'tou.price.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from(PRICE_CSV),
    })
    await expect(page.locator('#dset-res-ro')).toBeVisible()
    await expect(page.locator('#dset-offset')).toHaveCount(0)
    await page.getByRole('button', { name: /上传数据|Upload/ }).last().click()
    await expect(page.getByText(/已通过校验|Validation passed/).first()).toBeVisible({ timeout: 30_000 })

    // 结果面板显示可复制 oid（64 位十六进制）。
    const oidInput = page.getByLabel(/价格对象 ID|Price object ID/)
    await expect(oidInput).toBeVisible()
    const oid = await oidInput.inputValue()
    expect(oid).toMatch(/^[0-9a-f]{64}$/)
    await page.getByRole('button', { name: /^确定$|^OK$/i }).click()

    // 版本详情复查同源可得（DB 持久化，非仅上传返回体）：点开版本行。
    await page.getByRole('button', { name: /版本|Versions/ }).first().click()
    const versionsDialog = page.getByRole('dialog').filter({ hasText: /版本列表/ })
    await versionsDialog.getByRole('row').filter({ hasText: /v1/ }).first().click()
    await expect(page.getByText(oid).first()).toBeVisible()

    // 刷新后仍可取得同一 oid。
    await page.reload()
    await page.getByRole('button', { name: /版本|Versions/ }).first().click()
    const versionsDialog2 = page.getByRole('dialog').filter({ hasText: /版本列表/ })
    await versionsDialog2.getByRole('row').filter({ hasText: /v1/ }).first().click()
    await expect(page.getByText(oid).first()).toBeVisible()

    session.assertNoErrors()
    await context.close()
  })
})
