// 0.15 S1 修正验收: 模板数据输入的真实 GUI 路径与装配消费。
//
// 前置造数(API): 项目脚手架 + 数据集版本(自造 1h 完整日 CSV) + 绑定。
// 被测 UI: 真实浏览器在模板表单填写数值与项目内 CSV 路径 → 提交保存。
// 消费证明(API): 用保存出的项目模型加入设备实例(不手工补 predefined 绑定),
// 提交计算任务; 装配闸门必须读取模型回执中的数据绑定与数据集字节并通过,
// 任务快照的 dataset_version_ids 包含该版本。
//
// 数据全部为合成值; 不读取其他工作树/验收环境/案例。

import { test, expect } from '@playwright/test'
import type { BrowserContext, Page } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

/** 浏览器上下文(Cookie 会话)的 JSON 调用; UI 登录后避免 Bearer 会话被接管失效。 */
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
  if (!res.ok()) throw new Error(`${method} ${path} -> ${res.status()}: ${text.slice(0, 4000)}`)
  return parsed
}

const TEMPLATE_HEAD = `schema: ies.device-model
schema_version: "2.1.0"
device:
  id: __DEVICE_ID__
  names:
    zh-CN: S1 数据模板负荷
    en-US: S1 Data Template Load
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
  const rows = Array.from({ length: 24 }, (_, i) => `${i},${10 + (i % 3)}`)
  return [
    '# schema: ies.device-data',
    '# schema_version: 2.1.0',
    '# dataset_id: synth_template_data',
    `# device_id: ${deviceId}`,
    '# source_mode: data_repeat',
    '# resolution: 1h',
    '# unit.electric_demand: kW',
    'step,electric_demand',
    ...rows,
    '',
  ].join('\n')
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
    api,
    'POST',
    `/api/model-templates/${encodeURIComponent(templateId)}/publish`,
    { expected_revision: 1, idempotency_key: `s1-pub-${templateId}` },
    token,
  )
  return { templateId, revision: (pub.revision as { revision: number }).revision, deviceId }
}

async function uploadAndBind(
  api: Parameters<typeof requestJson>[0],
  token: string,
  pid: number,
  deviceId: string,
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
  const rev: number = project.draft.revision
  await requestJson(api, 'PUT', `/api/projects/${pid}/draft`, {
    expected_revision: rev,
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

  // 模板表单按 inputs 生成: 数值必填 + 数据叶子(项目内 CSV 路径)。
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

test.describe('S1 模板数据输入', () => {
  test.setTimeout(600_000)

  test('GUI 填写数据路径 → 保存 → 装配消费', async ({ browser }) => {
    const username = uniqueName('s1data')
    const password = strongPassword('Init')
    const projectName = uniqueName('S1 数据模板项目')
    const profileId = `s1-data-${Date.now()}`

    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    const pid = await scaffoldProject(
      requestJson, apiCtx.request, token, projectName, profileId,
    )
    const { deviceId } = await publishTemplate(apiCtx.request, token)
    const vid = await uploadAndBind(apiCtx.request, token, pid, `${deviceId}_1`)
    const dataPath = `data/ds${vid}.data.csv`

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+$/)
    await saveTemplateModelViaUi(page, deviceId, dataPath)

    // UI 登录会接管该用户的窗口会话, 之后统一用浏览器上下文的 Cookie 会话调 API。
    const project = await json(context, 'GET', `/api/projects/${pid}`)
    const config = await json(context, 'GET', `/api/projects/${pid}/config`)
    const rev: number = project.draft.revision
    await json(context, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: rev,
      commands: [
        {
          id: 's1-add-load2', unit: 'model', type: 'model.upsert_device',
          payload: {
            name: 'load2', device_type: `${deviceId}_1`, kind: 'new',
            params: { peak_power_kw: 150 }, position: { x: 4.0, y: 0.0 },
          },
        },
        {
          id: 's1-conn-grid-load2', unit: 'model', type: 'model.upsert_connection',
          payload: {
            name: 'c-grid-load2', from_device: 'grid1', from_port: 'electricity_import',
            to_device: 'load2', to_port: 'electricity_in',
          },
        },
        {
          id: 's1-finbind', unit: 'assembly', type: 'assembly.set_finance_binding',
          payload: {
            instances: {
              grid1: { type: 'none' },
              load1: { type: 'none' },
              load2: { type: 'none' },
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
        },
      ],
    })

    const submitted = await json(context, 'POST', `/api/projects/${pid}/tasks`, {
      task_type: 'calc',
      config: { config_revision: config.revision },
      evaluation: {
        evaluation_years: 1, construction_period: 0, operation_years: 1, discount_rate: 0.08,
        construction_payments: [{ t: 0, ratio: 1 }], maintenance_expenses: [],
      },
      idempotency_key: `s1-data-${Date.now()}`,
    })
    const taskId: number = submitted.task.id
    expect(taskId).toBeTruthy()

    const inputs = await json(context, 'GET', `/api/projects/${pid}/tasks/${taskId}/inputs`)
    const snapshotIds: number[] = inputs.inputs?.snapshot?.dataset_version_ids ?? []
    expect(snapshotIds).toContain(vid)

    await apiCtx.close()
    await context.close()
  })
})
