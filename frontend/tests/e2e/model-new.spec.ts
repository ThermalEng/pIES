// QA-E2E-01 场景 12(候选模型创建, 非拖放):
//   模板列表(真实后端目录) → 模板表单(number/boolean/string/项目相对 CSV 路径)
//   → 提交为候选
//   → 正式已保存(后端返回的最终 _N ID / 项目 revision)
//   → 直接 YAML 页签(骨架 + 在线编辑 + 提交保存)
//   → 一级菜单「自定义」模板管理页(已发布模板可见)
//
// 被测动作(选择模板、填写表单、提交、状态流转、
// YAML 编辑)全部通过 UI 完成; 不修改 localStorage / React 状态 / 数据库。
//
// 造数(创建工程师用户、通过 API 走真实模板生命周期创建并发布模板)与被测 UI
// 动作严格隔离(13.2): 造数用 setup/api.ts 的 requestJson(API 客户端), 场景
// 动作全部走 UI。测试不 mock 任何后端端点 —— 模板/项目模型后端为真实服务
// (dm2 联调完成, 旧 page.route 草案 mock 已删除)。
//
import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'

// ---------------------------------------------------------------------------
// 测试模板(与后端 test_project_model_save.py 同构; 含 number/boolean/string/
// data_repeat 顶层 inputs; 预定义接口本身即 data_repeat 绑定)
// ---------------------------------------------------------------------------

const TEMPLATE_SLUG_BASE = 'e2e-load'
const DATA_REF = 'e2e_load_data'

const escapeRegExp = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

const makeTemplateYaml = (deviceId: string): string => TEMPLATE_YAML_HEAD.replace('__DEVICE_ID__', deviceId)

const TEMPLATE_YAML_HEAD = `schema: ies.device-model
schema_version: "2.0.0"
device:
  id: __DEVICE_ID__
  names:
    zh-CN: E2E 负荷模板
    en-US: E2E Load Template
inputs:
  properties:
    peak_power_kw:
      value:
        type: number
        unit: kW
        valid_range: {minimum: 0, maximum: 1000}
        default: 100
    is_switchable:
      value: {type: boolean, default: false}
    label:
      value: {type: string, default: ""}
  interfaces:
    electric_demand:
      source:
        data_ref:
          type: data_repeat
          data_ref: data/e2e_load.csv
properties:
  cop:
    value: 3
    unit: "1"
    valid_range: {minimum: 1, maximum: 10}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: 1000}
    source: {mode: data_repeat, data_ref: ${DATA_REF}}
equations:
  variables: {}
  relations: []
`

// ---------------------------------------------------------------------------
// 造数: 通过真实模板生命周期 API 创建并发布模板(与 UI 被测动作隔离)
// ---------------------------------------------------------------------------

async function publishTemplate(
  apiCtx: Parameters<typeof requestJson>[0],
  userToken: string,
): Promise<{ templateId: string; revision: number; deviceId: string }> {
  const slug = `${TEMPLATE_SLUG_BASE}-${Date.now().toString(36)}`
  const payload = (deviceId: string) => ({
    slug,
    model_yaml: makeTemplateYaml(deviceId),
    description: 'E2E 真实后端模板(含 number/boolean/string/data_repeat inputs)',
  })
  // device.id 必须为 user.<用户命名空间>.device.<slug>; 命名空间由服务端分配,
  // 先以占位 id 试探, 从 400 诊断的 expected 取回真实 id 后重试
  let created: any
  let deviceId = `user.tmp.device.${slug}`
  try {
    created = await requestJson(apiCtx, 'POST', '/api/model-templates', payload(deviceId), userToken)
  } catch (err) {
    const expected = /"expected":"([^"]+)"/.exec(String(err))?.[1]
    if (!expected) throw err
    deviceId = expected
    created = await requestJson(apiCtx, 'POST', '/api/model-templates', payload(deviceId), userToken)
  }
  const templateId = String((created.template as { template_id?: unknown }).template_id)
  const pub = await requestJson(apiCtx, 'POST', `/api/model-templates/${encodeURIComponent(templateId)}/publish`, {
    expected_revision: 1,
    idempotency_key: `e2e-pub-${templateId}`,
  }, userToken)
  const rev = pub.revision as { revision: number }
  return { templateId, revision: rev.revision, deviceId }
}

// ---------------------------------------------------------------------------
// 场景 12: 候选模型创建(模板表单 + 直接 YAML, 状态流转与诊断保留)
// ---------------------------------------------------------------------------

test.describe('场景 12: 新建项目模型(真实后端模板与 YAML)', () => {
  test('模板表单提交、失败保留输入与诊断、保存成功与 YAML 编辑', async ({ browser }) => {
    const username = uniqueName('es-model')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    const userToken = await loginToken(apiCtx.request, username, password)
    const { deviceId: templateDeviceId } = await publishTemplate(apiCtx.request, userToken)
    await apiCtx.close()

    const context = await browser.newContext()
    const page: Page = await context.newPage()

    // 监控: 本场景含一次有意触发的 400 校验失败(真实后端), 浏览器会为该响应
    // 产生 "Failed to load resource: ... 400" console 噪音; 过滤该已知噪音后断言
    // 无其他 console error / pageerror / 失败网络请求(13.2)。
    const consoleErrors: string[] = []
    const pageErrors: string[] = []
    const failedRequests: string[] = []
    page.on('console', (msg) => {
      if (msg.type() === 'error' && !/Failed to load resource.*400/.test(msg.text())) consoleErrors.push(msg.text())
    })
    page.on('pageerror', (err) => pageErrors.push(err.message))
    page.on('requestfailed', (req) => {
      const errText = req.failure()?.errorText ?? 'unknown'
      if (errText === 'net::ERR_ABORTED') return
      failedRequests.push(`${req.method()} ${req.url()} :: ${errText}`)
    })

    // UI 登录(createSession 同时安装会话上下文)
    await createSession(page, username, password)

    // 12a. 通过 UI 新建项目并进入模型页
    const projectName = uniqueName('QA 模型模板')
    await page.getByRole('button', { name: /新建项目|New project/i }).first().click()
    await page.getByLabel(/名称|Name/i).fill(projectName)
    await page.getByRole('button', { name: /确认|Confirm/i }).click()
    await expect(page.getByText(projectName)).toBeVisible()
    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+$/)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText(/系统建模|System Modeling/i).click()
    await expect(page).toHaveURL(/\/model$/)

    // 12b. 点击「新建模型」进入模板页签; 真实目录渲染发布后的模板并可选中
    await page.getByRole('button', { name: /新建模型|New model/i }).click()
    await expect(page).toHaveURL(/\/model\/new$/)
    await expect(page.getByText(templateDeviceId)).toBeVisible()
    await page.getByRole('button', { name: new RegExp(escapeRegExp(templateDeviceId)) }).click()

    // 12c. 模板表单按 inputs 递归生成(number/boolean/string/data_repeat)
    await expect(page.getByLabel('properties.peak_power_kw.value [kW]')).toBeVisible()
    await expect(page.getByText('interfaces.electric_demand.source.data_ref')).toBeVisible()
    // 模板 data_repeat 默认路径以后缀提示(placeholder)呈现, 需用户填写项目内相对路径
    await expect(page.getByPlaceholder('data/e2e_load.csv')).toBeVisible()
    // 初始状态: 编辑中(非已保存)
    await expect(page.getByRole('status', { name: /编辑中|Editing/i })).toBeVisible()

    // 12d. 填写表单与项目内相对 CSV 路径。
    const numberInput = page.getByLabel('properties.peak_power_kw.value [kW]')
    await numberInput.fill('150')
    await page.getByRole('checkbox').nth(0).check()
    await page.getByLabel(/^properties\.label\.value$/).fill('e2e-label')
    await page.getByPlaceholder('data/e2e_load.csv').fill('data/e2e_load.csv')

    // 12e. 提交候选并正式保存。
    await page.getByRole('button', { name: /提交为候选|Submit as candidate/i }).click()
    await expect(page.getByRole('status', { name: /正式已保存|Saved/i })).toBeVisible()
    const savedPanel = page.locator('.ies-modeling__saved')
    // 最终 _N ID(后端分配; 面板同时展示"最终编号"与"设备 ID"两处, 限定面板内取首处)
    await expect(savedPanel.getByText(new RegExp(`${escapeRegExp(templateDeviceId)}_1`)).first()).toBeVisible()
    await expect(savedPanel.getByText('2', { exact: true })).toBeVisible() // 项目修订(后端返回)
    await expect(page.getByText(/模型已进入项目模型列表|ready for assembly/i)).toBeVisible()
    // 保存成功后提交按钮禁用(不允许再次覆盖)
    await expect(page.getByRole('button', { name: /提交为候选|Submit as candidate/i })).toBeDisabled()

    // 12g. 直接 YAML 页签: 标准骨架 + 在线编辑 + 提交保存(编号 _2, 与模板共享递增域)
    await page.getByRole('tab', { name: /直接编辑 YAML|Edit YAML directly/i }).click()
    const yamlEditor = page.getByLabel(/设备模型 YAML|Device model YAML/i)
    await expect(yamlEditor).toBeVisible()
    const skeleton = await yamlEditor.inputValue()
    expect(skeleton).toContain('schema: ies.device-model')
    expect(skeleton).toContain('schema_version: "2.0.0"')
    // 编辑 YAML(替换占位设备 ID)后提交
    await yamlEditor.fill(
      skeleton
        .replace('your.namespace.device_id', 'acme.device.e2e_direct')
        .replace('设备名称', 'E2E 直接负荷')
        .replace('Device Name', 'E2E Direct Load'),
    )
    await page.getByRole('button', { name: /提交为候选|Submit as candidate/i }).click()
    await expect(page.getByRole('status', { name: /正式已保存|Saved/i })).toBeVisible()
    await expect(page.locator('.ies-modeling__saved').getByText('acme.device.e2e_direct_2').first()).toBeVisible() // 项目内编号递增

    // 12h. 一级菜单「自定义」: 模板管理页显示已发布模板(真实列表)
    await page.getByRole('navigation', { name: /项目|Projects/i }).getByText(/自定义|Custom/i).click()
    await expect(page).toHaveURL(/\/custom$/)
    await expect(page.getByText(templateDeviceId)).toBeVisible()
    await expect(page.getByText(/已发布|Published/i).first()).toBeVisible()

    // 不应有 console error / pageerror / 失败网络请求
    expect(
      [...consoleErrors, ...pageErrors],
      `console error / pageerror 应不存在, 实际:\n${[...consoleErrors, ...pageErrors].join('\n')}`,
    ).toEqual([])
    expect(failedRequests, `失败网络请求应不存在, 实际:\n${failedRequests.join('\n')}`).toEqual([])
    await context.close()
  })
})
