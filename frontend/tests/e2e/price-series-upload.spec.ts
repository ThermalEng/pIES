// F3 收口①：价格序列文件上传后用户可取得 ref，刷新版本详情仍可取得。
//
// - 价格 CSV 经上传对话框同口上传：头扫描识别 price-series（文件分辨率
//   只读行可见，字段/UTC 偏移隐藏），与 device-data 同一自描述路径；
// - 上传成功后结果面板显示可复制的价格对象 ID（财务 time_series.ref
//   即填该值）；关闭后打开版本详情仍可见同一 oid（DB 版本行持久化，
//   非仅上传返回体）；刷新页面后复查仍可见。
// F3 复制语义收口：成功态只在真实复制成功后出现；复制不可用时给出
// 可见失败反馈（结果面板与版本详情共用同一局部复制块）。
// 文件选择用 setInputFiles（无需拖放）；独立用户 + API 前置造数。

import { test, expect } from '@playwright/test'
import type { Browser, Page, BrowserContext } from '@playwright/test'
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

function appUrl(): string {
  return process.env.E2E_APP_URL ?? 'http://web:80'
}

async function prepareProject(browser: Browser, tag: string): Promise<{ username: string; password: string; projectName: string }> {
  const username = uniqueName(tag)
  const password = strongPassword('Init')
  const projectName = uniqueName('QA 价格项目')
  const apiCtx = await browser.newContext({ baseURL: appUrl() })
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
      schema_version: '2.0.0',
      mode: 'planning',
      predictions: [],
      generator: 'ies.generator.highs_milp@7.0.0',
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
  return { username, password, projectName }
}

async function gotoDataPage(page: Page, username: string, password: string, projectName: string) {
  const session = await createSession(page, username, password)
  await page.getByText(projectName).click()
  await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
  await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click()
  await expect(page).toHaveURL(/\/data$/)
  return session
}

/** 上传价格文件并返回结果面板中的 oid（上传对话框保持打开）。 */
async function uploadPriceAndReadOid(page: Page, datasetName: string): Promise<string> {
  await page.getByRole('button', { name: /上传数据|Upload/ }).first().click()
  await page.locator('#dset-name').fill(datasetName)
  await page.locator('#dset-file').setInputFiles({
    name: 'tou.price.csv',
    mimeType: 'text/csv',
    buffer: Buffer.from(PRICE_CSV),
  })
  await expect(page.locator('#dset-res-ro')).toBeVisible()
  await expect(page.locator('#dset-offset')).toHaveCount(0)
  await page.getByRole('button', { name: /上传数据|Upload/ }).last().click()
  await expect(page.getByText(/已通过校验|Validation passed/).first()).toBeVisible({ timeout: 30_000 })
  const oidInput = page.getByLabel(/价格对象 ID|Price object ID/)
  await expect(oidInput).toBeVisible()
  const oid = await oidInput.inputValue()
  expect(oid).toMatch(/^[0-9a-f]{64}$/)
  return oid
}

test.describe('价格序列上传：对象 ID 可取且版本详情持久可查', () => {
  test('上传显示可复制 oid，刷新后版本详情仍显示同一 oid', async ({ browser }) => {
    const { username, password, projectName } = await prepareProject(browser, 'es-priceupload')
    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await gotoDataPage(page, username, password, projectName)
    const oid = await uploadPriceAndReadOid(page, 'e2e-price-series')
    await page.getByRole('button', { name: /^确定$|^OK$/i }).click()

    // 版本详情复查同源可得（DB 持久化，非仅上传返回体）：点开版本行，
    // 详情内的可复制输入框值与上传所得为同一 oid。
    await page.getByRole('button', { name: /版本|Versions/ }).first().click()
    const versionsDialog = page.getByRole('dialog').filter({ hasText: /版本列表/ })
    await versionsDialog.getByRole('row').filter({ hasText: /v1/ }).first().click()
    await expect(page.locator('#price-oid-detail')).toHaveValue(oid)
    // 版本详情也能实际复制：点击后出现已复制。
    await versionsDialog.getByRole('button', { name: /复制|Copy/ }).click()
    await expect(versionsDialog.getByText(/已复制|Copied/)).toBeVisible()

    // 刷新后仍可取得同一 oid。
    await page.reload()
    await page.getByRole('button', { name: /版本|Versions/ }).first().click()
    const versionsDialog2 = page.getByRole('dialog').filter({ hasText: /版本列表/ })
    await versionsDialog2.getByRole('row').filter({ hasText: /v1/ }).first().click()
    await expect(page.locator('#price-oid-detail')).toHaveValue(oid)

    session.assertNoErrors()
    await context.close()
  })

  test('复制按钮真实成功后才显示已复制', async ({ browser }) => {
    const { username, password, projectName } = await prepareProject(browser, 'es-pricecopy-ok')
    const context: BrowserContext = await browser.newContext({ permissions: ['clipboard-read', 'clipboard-write'] })
    const page: Page = await context.newPage()
    const session = await gotoDataPage(page, username, password, projectName)
    await uploadPriceAndReadOid(page, 'e2e-price-copy-ok')
    await page.getByRole('button', { name: /复制|Copy/ }).first().click()
    // 本环境无 Clipboard API，走框选回退：浏览器返回成功信号才显示已复制。
    await expect(page.getByText(/已复制|Copied/).first()).toBeVisible()
    await expect(page.getByText(/复制失败|Copy failed/)).toHaveCount(0)
    session.assertNoErrors()
    await context.close()
  })

  test('复制不可用时给出可见失败反馈且不显示已复制', async ({ browser }) => {
    const { username, password, projectName } = await prepareProject(browser, 'es-pricecopy-fail')
    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    await page.addInitScript(() => {
      Object.defineProperty(window.navigator, 'clipboard', { value: undefined, configurable: true })
      document.execCommand = () => false
    })
    const session = await gotoDataPage(page, username, password, projectName)
    await uploadPriceAndReadOid(page, 'e2e-price-copy-fail')
    await page.getByRole('button', { name: /复制|Copy/ }).first().click()
    await expect(page.getByText(/复制失败|Copy failed/).first()).toBeVisible()
    await expect(page.getByText(/已复制|Copied/)).toHaveCount(0)
    session.assertNoErrors()
    await context.close()
  })
})
