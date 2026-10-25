// ISSUE-02: 仅通过真实画布 UI 选择模型、填写容量、绑定曲线，保存并重开。
// API 只创建用户/项目和独立合成数据集，绝不写入被测设备或页面内部状态。

import { test, expect } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP } from './financial-scaffold'

const CURVE_TYPE = 'ies.device.power_curve'

function syntheticCsv(): string {
  return [
    '# schema: ies.device-data',
    '# schema_version: 2.1.0',
    '# dataset_id: synthetic_output_issue02',
    `# device_id: ${CURVE_TYPE}`,
    '# source_mode: data_repeat',
    '# resolution: 1h',
    '# unit.unit_output: 1',
    'step,unit_output',
    ...Array.from({ length: 24 }, (_, i) => `${i},${[0, 0.25, 0.75, 1][i % 4]}`),
    '',
  ].join('\n')
}

test('select curve model, edit capacity, bind unit-output dataset and reopen', async ({ browser }) => {
  const username = uniqueName('curve02')
  const password = strongPassword('Init')
  const projectName = uniqueName('Curve model acceptance')
  const api = await browser.newContext({ baseURL: APP })
  await createEngineer(api.request, username, password)
  const token = await loginToken(api.request, username, password)
  const created = await requestJson(api.request, 'POST', '/api/projects', {
    name: projectName, baseline_resolution: '1h', baseline_start_year: 2026,
    baseline_scenario_mode: 'single',
  }, token)
  const projectId = created.project.id as number
  const dataset = await requestJson(api.request, 'POST', `/api/projects/${projectId}/datasets`, {
    name: 'Synthetic unit output', source_category: 'user_upload', license: 'CC-BY-4.0',
  }, token)
  const dsId = dataset.dataset.id as number
  const upload = await api.request.post(`${APP}/api/projects/${projectId}/datasets/${dsId}/versions`, {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      resolution: '1h', utc_offset_minutes: '480',
      file: { name: 'unit-output.csv', mimeType: 'text/csv', buffer: Buffer.from(syntheticCsv()) },
    },
  })
  if (!upload.ok()) throw new Error(`合成数据上传失败: ${upload.status()} ${await upload.text()}`)
  const versionId = (await upload.json()).dataset_version.id as number
  const view = await requestJson(api.request, 'GET', `/api/projects/${projectId}`, undefined, token)
  await requestJson(api.request, 'PUT', `/api/projects/${projectId}/draft`, {
    expected_revision: view.draft.revision,
    commands: [{
      id: 'curve-dataset-prerequisite', unit: 'dataset', type: 'dataset.bind',
      payload: { dataset_version_id: versionId, role: 'unit-output-source' },
    }],
  }, token)
  await api.close()

  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  await page.goto(`/projects/${projectId}/model`)
  const palette = page.locator('.mp-palette')
  const curve = palette.locator(`[aria-label*="单位装机出力"]`).first()
  await expect(curve).toBeVisible({ timeout: 30_000 })
  // 原气象驱动光伏仍是另一条可明确选择的模型。
  await expect(palette.locator('[aria-label*="光伏"]')).toBeVisible()

  const canvas = page.locator('.mp-canvas-wrap')
  const from = await curve.boundingBox()
  const to = await canvas.boundingBox()
  expect(from && to).toBeTruthy()
  await page.mouse.move(from!.x + from!.width / 2, from!.y + from!.height / 2)
  await page.mouse.down()
  await page.mouse.move(to!.x + to!.width * 0.52, to!.y + to!.height * 0.55, { steps: 12 })
  await page.mouse.up()
  const node = page.locator('.react-flow__node', { hasText: '单位装机出力' }).first()
  await expect(node).toBeVisible({ timeout: 30_000 })
  await node.click()
  const capacity = page.getByRole('spinbutton', { name: 'installed_capacity_kw (kW)' })
  await expect(capacity).toBeVisible()
  await capacity.fill('240')
  await capacity.blur()
  await expect(page.locator('.mp-save-state--saved, .wb-autosave--saved').first()).toBeVisible({ timeout: 20_000 })

  await page.locator('#mp-source-mode-unit_output').selectOption('data_repeat')
  await page.locator('#mp-source-version-unit_output').selectOption(String(versionId))
  await expect(page.locator('.mp-save-state--saved, .wb-autosave--saved').first()).toBeVisible({ timeout: 20_000 })
  // 只读核对真实公开草稿，不用 API 注入被测步骤。
  await expect.poll(async () => {
    const response = await page.request.get(`${APP}/api/projects/${projectId}`)
    if (!response.ok()) return null
    const body = await response.json()
    const device = (body.draft?.content?.model?.devices ?? []).find((item: any) => item.device_type === CURVE_TYPE)
    return device ? [device.params.installed_capacity_kw, device.params.unit_output?.mode,
      device.params.unit_output?.dataset_version_id] : null
  }).toEqual([240, 'data_repeat', versionId])

  await page.reload()
  const reopened = page.locator('.react-flow__node', { hasText: '单位装机出力' }).first()
  await expect(reopened).toBeVisible({ timeout: 30_000 })
  await reopened.click()
  await expect(page.getByRole('spinbutton', { name: 'installed_capacity_kw (kW)' })).toHaveValue('240')
  await expect(page.locator('#mp-source-mode-unit_output')).toHaveValue('data_repeat')
  await expect(page.locator('#mp-source-version-unit_output')).toHaveValue(String(versionId))
  session.assertNoErrors()
  await context.close()
})
