// B5 收口 B: 数据页面上传两种格式(§14.3 Docker 浏览器验收)。
//
// - step CSV: 选择文件后进入自描述模式(字段/UTC 偏移隐藏,
//   文件分辨率只读), 上传成功(质量报告“已通过校验”);
// - 通用时间戳 CSV: 保持原路径(字段/UTC 偏移可见), 上传成功。
// 文件选择用 setInputFiles(无需拖放); 上传/质量报告/结果面板全走 UI;
// 独立用户 + API 前置造数(建用户/建项目/占位配置, 避免工作台落地噪声),
// 与场景 4 同模式。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { buildMinimalModel, createEngineer, loginToken, requestJson } from './setup/api'

const STEP_CSV = [
  '# schema: ies.device-data',
  '# schema_version: 2.0.0',
  '# dataset_id: e2e_history',
  '# device_id: ies.device.electric_load',
  '# source_mode: data_predict',
  '# resolution: 1h',
  '# unit.electricity_demand: kW',
  'step,electricity_demand',
  ...Array.from({ length: 24 }, (_, i) => `${i},${i % 2 === 0 ? 48 : 49}`),
].join('\n') + '\n'

// 通用时间戳路径要求全部标准列齐全(缺列阻断, 基线行为): 按模板列顺序
// 生成 7 列全量 CSV(e_load/h_load/c_load/t_ambient/ghi/
// electricity_price/grid_emission_factor), 数值均在允许范围内。
function timestampCsv(rows: number): string {
  const lines = [
    'timestamp,e_load,h_load,c_load,t_ambient,ghi,electricity_price,grid_emission_factor',
  ]
  const start = Date.UTC(2025, 0, 1, 0, 0)
  for (let i = 0; i < rows; i += 1) {
    const at = new Date(start + i * 3600 * 1000)
    const hour = i % 24
    const ghi = hour >= 6 && hour <= 18 ? 200 + ((i * 7) % 600) : 0
    lines.push(
      [
        at.toISOString().slice(0, 16).replace('T', ' '),
        100 + hour,
        50 + (i % 12),
        30 + (i % 12),
        10 + (i % 20),
        ghi,
        (0.5 + (i % 4) * 0.1).toFixed(1),
        '0.581',
      ].join(','),
    )
  }
  return lines.join('\n') + '\n'
}

test.describe('数据上传两种格式: step 自描述与通用时间戳', () => {
  test('step 文件自描述上传成功，时间戳文件保持原路径', async ({ browser }) => {
    const username = uniqueName('es-ddupload')
    const password = strongPassword('Init')
    const projectName = uniqueName('QA 上传格式项目')
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

    // step 文件: 进入自描述模式并上传成功。
    await page.getByRole('button', { name: /上传数据|Upload/ }).first().click()
    await page.locator('#dset-name').fill('e2e-step-history')
    await page.locator('#dset-file').setInputFiles({
      name: 'history.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from(STEP_CSV),
    })
    await expect(page.getByText(/设备数据 step 文件|Device-data step file/).first()).toBeVisible()
    await expect(page.locator('#dset-offset')).toHaveCount(0)
    await page.getByRole('button', { name: /上传数据|Upload/ }).last().click()
    await expect(page.getByText(/已通过校验|Validation passed/).first()).toBeVisible({ timeout: 30_000 })
    await page.getByRole('button', { name: /^确定$|^OK$/i }).click()

    // 通用时间戳文件: 原路径(字段/UTC 偏移可见)并上传成功。
    await page.getByRole('button', { name: /上传数据|Upload/ }).first().click()
    await page.locator('#dset-name').fill('e2e-generic')
    await expect(page.locator('#dset-offset')).toBeVisible()
    await page.locator('#dset-file').setInputFiles({
      name: 'generic.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from(timestampCsv(8760)),
    })
    await expect(page.locator('#dset-offset')).toBeVisible()
    await page.getByRole('button', { name: /上传数据|Upload/ }).last().click()
    await expect(page.getByText(/已通过校验|Validation passed/).first()).toBeVisible({ timeout: 60_000 })

    session.assertNoErrors()
    await context.close()
  })
})
