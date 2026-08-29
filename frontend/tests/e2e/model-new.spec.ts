// QA-E2E-01 场景 12(候选模型创建, 非拖放):
//   模板列表 → 模板表单(number/boolean/string/data 文件上传) → 提交为候选
//   → 校验失败(保留输入 + 字段路径/行列/expected/actual 诊断) → 修正后提交
//   → 正式已保存(后端返回的最终 _N ID / 规范 YAML / 摘要 / 项目 revision)
//   → 直接 YAML 编辑页签(骨架 + 在线编辑 + 提交保存)
//
// 后端候选校验/保存端点由阶段 2 worktree C 开发中(未合并): 本场景通过
// page.route 为草案端点提供契约一致的 mock 响应(端点清单见
// src/features/modeling/README.md「待 C 合并后联调」)。C 合并后删除
// TEMPLATE_MOCK/DIAGNOSTICS_MOCK/SAVED_MOCK 与全部 page.route, 改跑真实后端。
//
// 被测动作(选择模板、填写表单、上传文件、提交、保留输入、查看诊断、状态流转、
// YAML 编辑)全部通过 UI 完成; 不修改 localStorage / React 状态 / 数据库。

import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer } from './setup/api'

// ---------------------------------------------------------------------------
// 草案端点 mock 数据(契约: 见 features/modeling/contracts.ts)
// ---------------------------------------------------------------------------

const TEMPLATE_SUMMARY = {
  template_id: 'ies.test.sample',
  names: { 'zh-CN': '样例负荷模板', 'en-US': 'Sample Load Template' },
  schema_version: '2.0.0',
  description: 'e2e fixture 模板(含 number/boolean/string/data/array inputs)',
  content_sha256: 'ab'.repeat(32),
  revision: 1,
  has_inputs: true,
}

const TEMPLATE_DOCUMENT = {
  schema: 'ies.device-model',
  schema_version: '2.0.0',
  device: { id: 'ies.test.sample', names: { 'zh-CN': '样例负荷', 'en-US': 'Sample Load' } },
  inputs: {
    properties: {
      peak_power_kw: {
        value: { type: 'number', unit: 'kW', valid_range: { minimum: 0, maximum: 1000 }, default: 100 },
      },
      switchable: { value: { type: 'boolean', default: false } },
      label: { value: { type: 'string', default: '' } },
    },
    interfaces: {
      demand: { source: { data_ref: { type: 'data_repeat', data_ref: 'typical_day_load' } } },
    },
    profile: { type: 'array', items: { rows: { type: 'number', unit: 'MW' } } },
  },
  properties: { cop: { value: 3.0, unit: '1', valid_range: { minimum: 1, maximum: 10 } } },
  interfaces: {
    electricity_in: { type: 'in', carrier: 'electricity', unit: 'kW', valid_range: { minimum: 0, maximum: null } },
  },
  equations: { variables: {}, relations: [] },
}

const FAIL_DIAGNOSTICS = [
  {
    code: 'PROJ-VAL-011',
    message_key: 'ies.diag.raw',
    severity: 'error',
    blocking: true,
    params: { file: '<candidate>', detail: 'unit 无法识别: kw', expected: 'kW', actual: 'kw' },
    location: { object_type: 'device-model', field: 'properties.peak_power_kw.unit', line: 9, column: 7 },
    fix_hint_key: '',
    ref_ids: [],
  },
  {
    code: 'PROJ-VAL-014',
    message_key: 'ies.diag.raw',
    severity: 'error',
    blocking: true,
    params: { file: '<candidate>', detail: 'value 超出 valid_range', expected: '0..1000', actual: '1500' },
    location: { object_type: 'device-model', field: 'properties.peak_power_kw.value', line: 8, column: 5 },
    fix_hint_key: '',
    ref_ids: [],
  },
]

const FAIL_ENVELOPE = {
  error: {
    code: 'PROJ-VAL-001',
    message_key: 'ies.error.data_validation_failed',
    severity: 'error',
    blocking: true,
    params: { diagnostics: FAIL_DIAGNOSTICS },
    location: null,
    fix_hint_key: '',
    ref_ids: [],
  },
}

const SAVED_MODEL = {
  model: {
    model_id: 'ies.test.sample_1',
    device_id: 'ies.test.sample',
    schema_version: '2.0.0',
    canonical_yaml: [
      'schema: ies.device-model',
      'schema_version: "2.0.0"',
      'device:',
      '  id: ies.test.sample_1',
      'properties:',
      '  cop:',
      '    value: 3.0',
      '    unit: "1"',
      '',
    ].join('\n'),
    content_sha256: 'cd'.repeat(32),
    summary: { property_count: 2, interface_count: 1, relation_count: 0 },
    project_revision: 4,
  },
  project_revision: 4,
}

// ---------------------------------------------------------------------------
// 场景 12: 候选模型创建(模板表单 + 直接 YAML, 状态流转与诊断保留)
// ---------------------------------------------------------------------------

test.describe('场景 12: 新建项目模型(模板与 YAML)', () => {
  test('模板表单提交、失败保留输入与诊断、保存成功与 YAML 编辑', async ({ browser }) => {
    const username = uniqueName('es-model')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context = await browser.newContext()
    const page: Page = await context.newPage()

    // ---- 草案端点 mock(待 C 合并后联调: 删除本段改跑真实后端) ----
    let saveCalls = 0
    await page.route('**/api/model-templates', (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ items: [TEMPLATE_SUMMARY] }),
      })
    })
    await page.route('**/api/model-templates/ies.test.sample', (route) => {
      if (route.request().method() !== 'GET') return route.fallback()
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ template: TEMPLATE_SUMMARY, document: TEMPLATE_DOCUMENT }),
      })
    })
    await page.route('**/api/projects/*/temp-files', (route) => {
      if (route.request().method() !== 'POST') return route.fallback()
      return route.fulfill({
        status: 201,
        contentType: 'application/json',
        body: JSON.stringify({ temp_file_ref: 'temp:mock-1', file_name: 'load.csv' }),
      })
    })
    await page.route('**/api/projects/*/model-candidates', (route) => {
      if (route.request().method() !== 'POST') return route.fallback()
      saveCalls += 1
      if (saveCalls === 1) {
        // 首次提交: 校验失败(聚合诊断, 400 错误信封 params.diagnostics)
        return route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify(FAIL_ENVELOPE) })
      }
      // 后续提交: 校验成功(后端权威: 最终 _N ID / 规范 YAML / 摘要 / revision)
      return route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(SAVED_MODEL) })
    })

    // 监控: 场景含一次有意触发的 400 校验失败(mock), 浏览器会为该响应产生
    // "Failed to load resource: ... 400" console 噪音; 过滤该已知噪音后断言
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
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText(/系统建模|Modeling/i).click()
    await expect(page).toHaveURL(/\/model$/)

    // 12b. 点击「新建模型」进入模板页签; 模板列表渲染并可选中
    await page.getByRole('button', { name: /新建模型|New model/i }).click()
    await expect(page).toHaveURL(/\/model\/new$/)
    await expect(page.getByText('样例负荷模板')).toBeVisible()
    await page.getByRole('button', { name: /样例负荷模板|Sample Load Template/i }).click()

    // 12c. 模板表单按 inputs 递归生成(number/boolean/string/data/array)
    await expect(page.getByLabel('properties.peak_power_kw.value [kW]')).toBeVisible()
    await expect(page.getByText('properties.peak_power_kw.value')).toBeVisible()
    await expect(page.getByText('interfaces.demand.source.data_ref')).toBeVisible()
    await expect(page.getByText('profile')).toBeVisible()
    // 初始状态: 编辑中(非已保存)
    await expect(page.getByRole('status', { name: /编辑中|Editing/i })).toBeVisible()

    // 12d. 填写表单 + 上传临时数据文件 → 临时已上传
    const numberInput = page.getByLabel('properties.peak_power_kw.value [kW]')
    await numberInput.fill('150')
    await page.locator('input[type="file"]').setInputFiles({
      name: 'load.csv',
      mimeType: 'text/csv',
      buffer: Buffer.from('t,val\n2026-01-01T00:00:00Z,10\n'),
    })
    await expect(page.getByText(/临时已上传.*load\.csv|Temporarily uploaded.*load\.csv/i)).toBeVisible()
    await expect(page.getByRole('status', { name: /临时已上传|Temporarily uploaded/i })).toBeVisible()

    // 12e. 提交为候选 → 校验失败: 保留输入 + 诊断展示(字段路径/行列/expected/actual)
    await page.getByRole('button', { name: /提交为候选|Submit as candidate/i }).click()
    await expect(page.getByRole('status', { name: /校验中|Validating/i })).toBeVisible()
    await expect(page.getByRole('status', { name: /校验失败|Validation failed/i })).toBeVisible()
    await expect(page.getByText(/校验失败\(2 条诊断\)|Validation failed \(2 diagnostics\)/i)).toBeVisible()
    // 诊断: 字段路径 + YAML 行列 + expected/actual(限定在诊断面板内, 避免与表单标签歧义)
    const diagPanel = page.locator('.ies-modeling__diagnostics')
    await expect(diagPanel.getByText(/properties\.peak_power_kw\.unit/)).toBeVisible()
    await expect(diagPanel.getByText(/YAML 第 9 行第 7 列|YAML line 9, column 7/i)).toBeVisible()
    await expect(diagPanel.getByText(/期望.*kW|Expected.*kW/i)).toBeVisible()
    await expect(diagPanel.getByText(/实际.*kw|Actual.*kw/i)).toBeVisible()
    await expect(diagPanel.getByText(/properties\.peak_power_kw\.value/)).toBeVisible()
    // 输入保留(数值与临时文件引用均未丢失)
    await expect(numberInput).toHaveValue('150')
    await expect(page.getByText(/临时已上传.*load\.csv/i)).toBeVisible()
    // 未显示保存成功、无最终编号
    await expect(page.getByText(/正式已保存|Model saved/i)).not.toBeVisible()
    await expect(page.getByText('ies.test.sample_1')).not.toBeVisible()

    // 12f. 修正后重新提交 → 正式已保存(后端权威结果)
    await numberInput.fill('160')
    await expect(page.getByRole('status', { name: /编辑中|Editing/i })).toBeVisible()
    await page.getByRole('button', { name: /提交为候选|Submit as candidate/i }).click()
    await expect(page.getByRole('status', { name: /正式已保存|Saved/i })).toBeVisible()
    await expect(page.getByText('ies.test.sample_1')).toBeVisible() // 最终 _N ID(后端分配)
    await expect(page.locator('.ies-modeling__saved').getByText('4')).toBeVisible() // 项目修订(后端返回)
    await expect(page.getByText(/模型已进入项目模型列表|ready for assembly/i)).toBeVisible()
    // 规范 YAML 可展开查看
    await page.getByRole('button', { name: /展开规范 YAML|Show canonical YAML/i }).click()
    await expect(page.getByText('schema: ies.device-model')).toBeVisible()
    // 保存成功后提交按钮禁用(不允许再次覆盖)
    await expect(page.getByRole('button', { name: /提交为候选|Submit as candidate/i })).toBeDisabled()

    // 12g. 直接 YAML 页签: 标准骨架 + 在线编辑 + 提交保存
    await page.getByRole('tab', { name: /直接编辑 YAML|Edit YAML directly/i }).click()
    const yamlEditor = page.getByLabel(/设备模型 YAML|Device model YAML/i)
    await expect(yamlEditor).toBeVisible()
    const skeleton = await yamlEditor.inputValue()
    expect(skeleton).toContain('schema: ies.device-model')
    expect(skeleton).toContain('schema_version: "2.0.0"')
    // 编辑 YAML(替换占位设备 ID)后提交
    await yamlEditor.fill(
      skeleton
        .replace('your.namespace.device_id', 'acme.device.e2e_load')
        .replace('设备名称', 'E2E 负荷')
        .replace('Device Name', 'E2E Load'),
    )
    await page.getByRole('button', { name: /提交为候选|Submit as candidate/i }).click()
    await expect(page.getByRole('status', { name: /正式已保存|Saved/i })).toBeVisible()
    await expect(page.getByText('ies.test.sample_1')).toBeVisible()

    // 除有意触发的 400 校验失败外, 不应有 console error / pageerror / 失败网络请求
    expect(
      [...consoleErrors, ...pageErrors],
      `console error / pageerror 应不存在, 实际:\n${[...consoleErrors, ...pageErrors].join('\n')}`,
    ).toEqual([])
    expect(failedRequests, `失败网络请求应不存在, 实际:\n${failedRequests.join('\n')}`).toEqual([])
    await context.close()
  })
})
