// 0.15 S1 验收: 模板数据输入的真实 GUI 全链。
//
// API 仅做无关前置造数: 工程师/项目脚手架/模板发布/数据集版本上传与项目绑定。
// 被测动作全部走真实页面: 模板表单保存 → 画布添加实例 → 实例来源编辑
// (预定义接口 mode + 数据集版本) → 接口连接 → 任务提交。
// 最后只读核对草稿实例事实与任务快照, 不做状态注入。

import { test, expect } from '@playwright/test'
import type { BrowserContext, Page } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

const TEMPLATE_HEAD = `schema: ies.device-model
schema_version: "2.1.0"
device:
  id: __DEVICE_ID__
  names:
    zh-CN: S1 Data Load
    en-US: S1 Data Load
inputs:
  properties:
    peak_power_kw:
      value: {type: number, unit: kW, valid_range: {minimum: 0, maximum: 10000}}
  interfaces:
    electric_demand: {type: data_repeat}
properties:
  peak_power_kw:
    value: 100
    unit: kW
    valid_range: {minimum: 0, maximum: 10000}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
  electricity_in:
    type: in
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
equations:
  variables: {}
  relations:
    - id: serve
      expression: "electricity_in[t] = electric_demand[t]"
`

function ddCsv(deviceId: string): string {
  return [
    '# schema: ies.device-data',
    '# schema_version: 2.1.0',
    '# dataset_id: synth_template_data',
    `# device_id: ${deviceId}`,
    '# source_mode: data_repeat',
    '# resolution: 1h',
    '# unit.electric_demand: kW',
    'step,electric_demand',
    ...Array.from({ length: 24 }, (_, i) => `${i},${10 + (i % 3)}`),
    '',
  ].join('\n')
}

async function json(
  context: BrowserContext, method: 'GET' | 'POST' | 'PUT' | 'DELETE', path: string, body?: unknown,
): Promise<any> {
  const res = await context.request.fetch(`${APP}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    data: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await res.text()
  const parsed = text ? JSON.parse(text) : null
  if (!res.ok()) throw new Error(`${method} ${path} -> ${res.status()}: ${text.slice(0, 2000)}`)
  return parsed
}

async function waitDraft(
  page: Page, pid: number, predicate: (content: any) => boolean, label: string,
): Promise<void> {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const res = await page.request.get(`${APP}/api/projects/${pid}`)
    if (res.ok()) {
      const body = await res.json()
      if (predicate(body.draft?.content ?? {})) return
    }
    await page.waitForTimeout(500)
  }
  throw new Error(`${label} 未持久化到项目草稿`)
}

async function publishTemplate(
  api: Parameters<typeof requestJson>[0],
  token: string,
): Promise<{ templateId: string; revision: number; deviceId: string }> {
  const slug = `s1-data-load-${Date.now().toString(36)}`
  const payload = (deviceId: string) => ({
    slug,
    model_yaml: TEMPLATE_HEAD.replace('__DEVICE_ID__', deviceId),
    description: 'S1 模板数据输入验收',
  })
  let created: any
  let deviceId = `user.tmp.device.${slug}`
  try {
    created = await requestJson(api, 'POST', '/api/model-templates', payload(deviceId), token)
  } catch (err) {
    const expected = /"expected":"([^"]+)"/.exec(String(err))?.[1]
    if (!expected) throw err
    deviceId = expected
    created = await requestJson(api, 'POST', '/api/model-templates', payload(deviceId), token)
  }
  const templateId = String((created.template as { template_id?: unknown }).template_id)
  const pub = await requestJson(
    api, 'POST',
    `/api/model-templates/${encodeURIComponent(templateId)}/publish`,
    { expected_revision: 1, idempotency_key: `s1-pub-${templateId}` }, token,
  )
  return { templateId, revision: (pub.revision as { revision: number }).revision, deviceId }
}

async function uploadAndBind(
  api: Parameters<typeof requestJson>[0], token: string, pid: number, deviceId: string,
): Promise<number> {
  const ds = await requestJson(api, 'POST', `/api/projects/${pid}/datasets`, {
    name: 'S1 模板数据', source_category: 'user_upload', license: 'CC-BY-4.0',
  }, token)
  const dsId: number = ds.dataset.id
  const res = await api.post(`${APP}/api/projects/${pid}/datasets/${dsId}/versions`, {
    headers: { Authorization: `Bearer ${token}` },
    multipart: {
      resolution: '1h',
      utc_offset_minutes: '480',
      file: { name: 'load.csv', mimeType: 'text/csv', buffer: Buffer.from(ddCsv(deviceId)) },
    },
  })
  const version = await res.json()
  if (!res.ok()) throw new Error(`上传数据版本失败: ${res.status()} ${JSON.stringify(version)}`)
  const vid: number = version.dataset_version.id
  const project = await requestJson(api, 'GET', `/api/projects/${pid}`, undefined, token)
  await requestJson(api, 'PUT', `/api/projects/${pid}/draft`, {
    expected_revision: project.draft.revision,
    commands: [{
      id: 's1-bind-data', unit: 'dataset', type: 'dataset.bind',
      payload: { dataset_version_id: vid, role: 's1-template-data' },
    }],
  }, token)
  return vid
}

async function saveTemplateModelViaUi(page: Page, deviceId: string, dataPath: string): Promise<void> {
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText(/系统建模|System Modeling/i).click({ timeout: 30_000 })
  await expect(page).toHaveURL(/\/model$/)
  await page.getByRole('button', { name: /新建模型|New model/i }).click({ timeout: 30_000 })
  await expect(page).toHaveURL(/\/model\/new$/)
  await page.getByRole('button', { name: new RegExp(deviceId) }).click({ timeout: 30_000 })
  const numberInput = page.getByLabel('properties.peak_power_kw.value [kW]')
  await expect(numberInput).toBeVisible({ timeout: 30_000 })
  await numberInput.fill('150')
  const dataInput = page.getByPlaceholder('data/example.csv')
  await expect(dataInput).toBeVisible({ timeout: 15_000 })
  await dataInput.fill(dataPath)
  await page.getByRole('button', { name: /提交为候选|Submit as candidate/i }).click({ timeout: 30_000 })
  await expect(page.getByRole('status', { name: /正式已保存|Saved/i })).toBeVisible({ timeout: 30_000 })
  await expect(page.locator('.ies-modeling__saved').getByText(new RegExp(`${deviceId}_1`)).first()).toBeVisible()
}

async function addModelDeviceViaCanvas(page: Page, pid: number): Promise<{ nodeName: string }> {
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText(/系统建模|System Modeling/i).click({ timeout: 30_000 })
  await expect(page).toHaveURL(/\/model$/)
  const canvas = page.locator('.mp-canvas-wrap')
  const palette = page.locator('.mp-palette')
  await expect(palette).toBeVisible({ timeout: 30_000 })
  const item = palette.locator('[aria-label*="S1 Data Load"]').first()
  await item.scrollIntoViewIfNeeded()
  await expect(item).toBeVisible()
  const box = await item.boundingBox()
  const canvasBox = await canvas.boundingBox()
  expect(box && canvasBox).toBeTruthy()
  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2)
  await page.mouse.down()
  await page.mouse.move(canvasBox!.x + canvasBox!.width * 0.75, canvasBox!.y + canvasBox!.height * 0.65, { steps: 10 })
  await page.mouse.up()
  const nodeName = 'S1 Data Load 1'
  const node = page.locator('.react-flow__node', { hasText: nodeName }).first()
  await expect(node).toBeVisible({ timeout: 15_000 })
  await waitDraft(page, pid, (content) =>
    (content.model?.devices ?? []).some((d: any) => d.name === nodeName), '新建设备')
  await expect(page.locator('.mp-save-state--saved, .wb-autosave--saved').first()).toBeVisible({ timeout: 15_000 })
  await page.waitForTimeout(600)
  return { nodeName }
}

async function editSourceViaSidebar(page: Page, pid: number, vid: number): Promise<void> {
  const node = page.locator('.react-flow__node', { hasText: 'S1 Data Load 1' }).first()
  await node.click()
  const mode = page.locator('#mp-source-mode-electric_demand')
  await expect(mode).toBeVisible({ timeout: 15_000 })
  await mode.selectOption('data_repeat')
  await expect(page.locator('.mp-save-state--saved, .wb-autosave--saved').first()).toBeVisible({ timeout: 15_000 })
  await page.waitForTimeout(600)
  const version = page.locator('#mp-source-version-electric_demand')
  await expect(version).toBeVisible({ timeout: 15_000 })
  await version.selectOption(String(vid))
  await waitDraft(page, pid, (content) => {
    const dev = (content.model?.devices ?? []).find((d: any) => d.name === 'S1 Data Load 1')
    return dev?.params?.electric_demand?.dataset_version_id === vid
  }, '实例来源声明')
  await expect(page.locator('.mp-save-state--saved, .wb-autosave--saved').first()).toBeVisible({ timeout: 15_000 })
  await page.waitForTimeout(600)
}

async function connectToGridViaCanvas(page: Page, pid: number): Promise<void> {
  // 关闭参数侧栏并适应视图, 保证两侧句柄都在可视区域。
  await page.locator('.react-flow__pane').click({ position: { x: 30, y: 30 } })
  const fit = page.getByRole('button', { name: /适应视图|Fit view/i })
  if (await fit.isVisible().catch(() => false)) await fit.click()
  await page.waitForTimeout(600)
  const gridNode = page.locator('.react-flow__node', { hasText: 'grid1' }).first()
  const loadNode = page.locator('.react-flow__node', { hasText: 'S1 Data Load 1' }).first()
  const gridOut = gridNode.locator('.react-flow__handle-right.mp-handle--electricity').first()
  const loadIn = loadNode.locator('.react-flow__handle-left.mp-handle--electricity').first()
  await expect(gridOut).toBeVisible({ timeout: 10_000 })
  await expect(loadIn).toBeVisible({ timeout: 10_000 })
  const src = await gridOut.boundingBox()
  const dst = await loadIn.boundingBox()
  expect(src && dst).toBeTruthy()
  await page.mouse.move(src!.x + src!.width / 2, src!.y + src!.height / 2)
  await page.mouse.down()
  await page.mouse.move(dst!.x + dst!.width / 2, dst!.y + dst!.height / 2, { steps: 12 })
  await page.mouse.up()
  await expect(page.locator('.react-flow__edge').first()).toBeVisible({ timeout: 10_000 })
  await waitDraft(page, pid, (content) =>
    (content.model?.connections ?? []).some((c: any) =>
      c.from_device === 'grid1' && c.to_device === 'S1 Data Load 1'
      && c.from_port === 'electricity_import' && c.to_port === 'electricity_in'), '接口连接')
}

async function submitViaUi(page: Page): Promise<void> {
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
  await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
  await page.locator('#task-type').selectOption('optimization')
  await page.locator('#task-construction-period').fill('1')
  await page.locator('#task-operation-years').fill('2')
  await page.locator('#task-discount-rate').fill('0.08')
  await page.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
  await page.locator('#task-maintenance-expenses').fill('[]')
  const submitBtn = page.getByRole('button', { name: /提交任务|Submit task/i }).last()
  await expect(submitBtn).toBeEnabled({ timeout: 10_000 })
  const submitResp = page.waitForResponse(
    (res) => res.request().method() === 'POST' && new URL(res.url()).pathname.endsWith('/tasks'),
    { timeout: 120_000 },
  )
  await submitBtn.click({ timeout: 30_000 })
  const response = await submitResp
  if (response.status() >= 400) {
    throw new Error(`提交失败 ${response.status()}: ${(await response.text()).slice(0, 2000)}`)
  }
  await expect(page.locator('.ies-table__row').first()).toBeVisible({ timeout: 30_000 })
}

test.describe('S1 模板数据输入 GUI 全链', () => {
  test.setTimeout(600_000)

  test('保存 → 添加实例 → 来源编辑 → 连线 → 提交', async ({ browser }) => {
    const username = uniqueName('s1data')
    const password = strongPassword('Init')
    const projectName = uniqueName('S1 数据模板项目')
    const profileId = `s1-data-${Date.now()}`

    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, profileId)
    const { deviceId } = await publishTemplate(apiCtx.request, token)
    // CSV device_id 用稳定基础 ID, 不猜项目作用域 _N 编号。
    const vid = await uploadAndBind(apiCtx.request, token, pid, deviceId)
    const dataPath = `data/ds${vid}.data.csv`

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+$/)
    await saveTemplateModelViaUi(page, deviceId, dataPath)
    await addModelDeviceViaCanvas(page, pid)
    await editSourceViaSidebar(page, pid, vid)
    await connectToGridViaCanvas(page, pid)

    // 装配财务绑定为提交支撑数据(与被测数据来源无关; 通过公开草稿命令)。
    const project = await json(context, 'GET', `/api/projects/${pid}`)
    await json(context, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: project.draft.revision,
      commands: [{
        id: 's1-finbind', unit: 'assembly', type: 'assembly.set_finance_binding',
        payload: {
          instances: {
            grid1: { type: 'none' },
            load1: { type: 'none' },
            s1_data_load_1: { type: 'none' },
            heatload: { type: 'none' },
            hp1: {
              type: 'costed',
              finance_type: 'heat_pump_system',
              drivers: {
                heat_kw: { property: 'rated_heat_kw' },
                consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
              },
            },
          },
        },
      }],
    })

    // 草稿实例事实: 页面来源编辑写入 params, 与装配同一来源声明机制。
    const view = await json(context, 'GET', `/api/projects/${pid}`)
    const devices = view.draft.content.model.devices as Array<Record<string, any>>
    const load2 = devices.find((d) => d.name === 'S1 Data Load 1')
    expect(load2).toBeTruthy()
    expect(load2!.params.electric_demand).toEqual({ mode: 'data_repeat', dataset_version_id: vid })

    await submitViaUi(page)
    const listed = await json(context, 'GET', `/api/projects/${pid}/tasks?limit=10`)
    const taskId = listed.items[0].id as number
    const inputs = await json(context, 'GET', `/api/projects/${pid}/tasks/${taskId}/inputs`)
    expect(inputs.inputs.snapshot.dataset_version_ids).toContain(vid)

    await apiCtx.close()
    await context.close()
  })
})
