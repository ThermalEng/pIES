// QA-E2E-01 场景 12(候选模型创建, 非拖放):
//   模板列表(真实后端目录) → 模板表单(number/boolean/string/data 文件上传)
//   → 提交为候选 → 后端真实校验失败(内容锁: 数据文件摘要不匹配, 保留输入 +
//   字段路径/expected/actual 聚合诊断) → 替换为正确数据文件后重新提交
//   → 正式已保存(后端返回的最终 _N ID / 项目 revision)
//   → 直接 YAML 页签(骨架 + 在线编辑 + 提交保存)
//   → 一级菜单「自定义」模板管理页(已发布模板可见)
//
// 被测动作(选择模板、填写表单、上传文件、提交、保留输入、查看诊断、状态流转、
// YAML 编辑)全部通过 UI 完成; 不修改 localStorage / React 状态 / 数据库。
//
// 造数(创建工程师用户、通过 API 走真实模板生命周期创建并发布模板)与被测 UI
// 动作严格隔离(13.2): 造数用 setup/api.ts 的 requestJson(API 客户端), 场景
// 动作全部走 UI。测试不 mock 任何后端端点 —— 模板/项目模型后端为真实服务
// (dm2 联调完成, 旧 page.route 草案 mock 已删除)。
//
// 配套数据文件摘要: 数据文件头声明的 device_content_sha256 必须等于模板
// 实例化后候选文档(用户 inputs 合并、顶层 inputs 删除)的规范内容摘要 ——
// 该值在前端用与后端 canonical_bytes 相同的算法(键排序 + 紧凑 JSON +
// 非 ASCII 保留)计算, 并在测试中独立验证数据文件内容锁。

import { test, expect } from '@playwright/test'
import type { Page } from '@playwright/test'
import { createHash } from 'node:crypto'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'

// ---------------------------------------------------------------------------
// 测试模板(与后端 test_project_model_save.py 同构; 含 number/boolean/string/
// data_repeat 顶层 inputs; 预定义接口本身即 data_repeat 绑定)
// ---------------------------------------------------------------------------

const TEMPLATE_ID = 'acme.device.e2e_load'
const DATA_REF = 'e2e_load_data'

const TEMPLATE_YAML = `schema: ies.device-model
schema_version: "2.0.0"
device:
  id: ${TEMPLATE_ID}
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
          data_ref: ${DATA_REF}
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

// 与后端 canonical_bytes 相同的规范序列化: 键排序 + 紧凑 JSON + 非 ASCII 保留。
// (Python json.dumps(ensure_ascii=False, sort_keys=True, separators=(",",":")) 与
// 本实现输出逐字节一致: 字符串转义规则相同, 数字均按 JSON 字面量输出。)
// 注意: valid_range 边界在后端解析为 float, 规范字节输出 "1000.0"(整值浮点带
// .0 后缀); 标量值按 YAML 解析类型输出(int 3 / int 150)。用 floatB() 标记
// float 类型值, 由本序列化器按 Python 规则输出。
function floatB(n: number): { f: number } {
  return { f: n }
}

function canonicalJson(value: unknown): string {
  if (value !== null && typeof value === 'object' && !Array.isArray(value) && 'f' in (value as Record<string, unknown>) && Object.keys(value as Record<string, unknown>).length === 1) {
    const n = (value as { f: number }).f
    return Number.isInteger(n) ? `${n}.0` : String(n)
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>
    const keys = Object.keys(record).sort()
    return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalJson(record[k])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

function sha256Hex(text: string): string {
  return createHash('sha256').update(text, 'utf8').digest('hex')
}

// 模板实例化后的候选文档(用户 inputs 合并、顶层 inputs 删除) —— 与后端
// template2.instantiate_template 的输出一致: 用户提交的标量写入叶子路径,
// 新增 property 由叶子声明补全 unit/valid_range(boolean/string 用 "-",
// 无范围时规范输出 valid_range: null)。
function instantiatedDoc(peakKw: number, switchable: boolean, label: string) {
  return {
    schema: 'ies.device-model',
    schema_version: '2.0.0',
    device: { id: TEMPLATE_ID, names: { 'en-US': 'E2E Load Template', 'zh-CN': 'E2E 负荷模板' } },
    properties: {
      cop: { value: 3, unit: '1', valid_range: { minimum: floatB(1), maximum: floatB(10) } },
      peak_power_kw: { value: peakKw, unit: 'kW', valid_range: { minimum: floatB(0), maximum: floatB(1000) } },
      is_switchable: { value: switchable, unit: '-', valid_range: null },
      label: { value: label, unit: '-', valid_range: null },
    },
    interfaces: {
      electric_demand: {
        type: 'predefined',
        carrier: 'electricity',
        unit: 'kW',
        valid_range: { minimum: floatB(0), maximum: floatB(1000) },
        source: { mode: 'data_repeat', data_ref: DATA_REF },
      },
    },
    equations: { variables: {}, relations: [] },
  }
}

/** 实例化后候选文档的规范内容摘要(数据文件头 device_content_sha256 必须等于它)。 */
function candidateContentSha256(peakKw: number, switchable: boolean, label: string): string {
  return sha256Hex(canonicalJson(instantiatedDoc(peakKw, switchable, label)))
}

// 配套数据文件: 元数据头按候选基础模型声明(device_id / device_content_sha256),
// source_mode=data_repeat, resolution=1h + period=day → 24 行 step 0..23。
// 值 10+step 在接口 valid_range [0,1000] 内。
function dataCsv(sha256: string): Buffer {
  const lines = [
    '# schema: ies.device-data',
    '# schema_version: 2.0.0',
    '# dataset_id: e2e.load.profile',
    `# device_id: ${TEMPLATE_ID}`,
    `# device_content_sha256: ${sha256}`,
    '# source_mode: data_repeat',
    '# resolution: 1h',
    '# period: day',
    `# unit.electric_demand: kW`,
    'step,electric_demand',
    ...Array.from({ length: 24 }, (_, step) => `${step},${10 + step}`),
  ]
  return Buffer.from(lines.join('\n') + '\n', 'utf-8')
}

// ---------------------------------------------------------------------------
// 造数: 通过真实模板生命周期 API 创建并发布模板(与 UI 被测动作隔离)
// ---------------------------------------------------------------------------

async function publishTemplate(
  apiCtx: Parameters<typeof requestJson>[0],
  userToken: string,
): Promise<{ templateId: string; revision: number }> {
  const created = await requestJson(apiCtx, 'POST', '/api/model-templates', {
    model_yaml: TEMPLATE_YAML,
    description: 'E2E 真实后端模板(含 number/boolean/string/data_repeat inputs)',
  }, userToken)
  const templateId = String((created.template as { template_id?: unknown }).template_id)
  const pub = await requestJson(apiCtx, 'POST', `/api/model-templates/${encodeURIComponent(templateId)}/publish`, {
    expected_revision: 1,
    idempotency_key: `e2e-pub-${templateId}`,
  }, userToken)
  const rev = pub.revision as { revision: number }
  return { templateId, revision: rev.revision }
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
    const { templateId, revision } = await publishTemplate(apiCtx.request, userToken)
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
    await expect(page.getByText(TEMPLATE_ID)).toBeVisible()
    await page.getByRole('button', { name: new RegExp(TEMPLATE_ID) }).click()

    // 12c. 模板表单按 inputs 递归生成(number/boolean/string/data_repeat)
    await expect(page.getByLabel('properties.peak_power_kw.value [kW]')).toBeVisible()
    await expect(page.getByText('interfaces.electric_demand.source.data_ref')).toBeVisible()
    await expect(page.getByText(DATA_REF)).toBeVisible()
    // 初始状态: 编辑中(非已保存)
    await expect(page.getByRole('status', { name: /编辑中|Editing/i })).toBeVisible()

    // 12d. 填写表单 + 上传临时数据文件 → 临时已上传
    // 表单提交值决定实例化后候选文档摘要(数据文件必须与之一致); 先计算
    // 正确摘要, 首次故意上传错误摘要文件验证内容锁失败诊断
    const expectedSha = candidateContentSha256(150, true, 'e2e-label')
    const numberInput = page.getByLabel('properties.peak_power_kw.value [kW]')
    await numberInput.fill('150')
    await page.getByRole('checkbox').nth(0).check()
    await page.getByLabel(/^properties\.label\.value$/).fill('e2e-label')
    // 故意使用错误摘要(内容锁: 数据文件必须与候选模型绑定): 首次提交会
    // 被真实后端拒绝(DATA-META-010), 验证失败保留输入与诊断展示
    await page.locator('input[type="file"]').setInputFiles({
      name: 'load.csv',
      mimeType: 'text/csv',
      buffer: dataCsv('12'.repeat(32)),
    })
    await expect(page.getByText(/临时已上传.*load\.csv|Temporarily uploaded: load\.csv/i)).toBeVisible()
    await expect(page.getByRole('status', { name: /临时已上传|Temporarily uploaded/i })).toBeVisible()

    // 12e. 提交为候选 → 真实后端校验失败(内容锁): 保留输入 + 诊断展示
    await page.getByRole('button', { name: /提交为候选|Submit as candidate/i }).click()
    await expect(page.getByRole('status', { name: /校验中|Validating/i })).toBeVisible()
    await expect(page.getByRole('status', { name: /校验失败|Validation failed/i })).toBeVisible()
    await expect(page.getByText(/校验失败\(|Validation failed \(/i).first()).toBeVisible()
    // 诊断: 诊断码 + 字段路径 + expected(限定在诊断面板内, 避免与表单标签歧义)
    const diagPanel = page.locator('.ies-modeling__diagnostics')
    await expect(diagPanel.getByText('DATA-META-010')).toBeVisible()
    await expect(diagPanel.getByText(/device_content_sha256/)).toBeVisible()
    await expect(diagPanel.getByText(expectedSha)).toBeVisible() // expected=正确摘要(参数渲染)
    // 输入保留(数值与临时文件引用均未丢失)
    await expect(numberInput).toHaveValue('150')
    await expect(page.getByText(/临时已上传.*load\.csv|Temporarily uploaded: load\.csv/i)).toBeVisible()
    // 未显示保存成功、无最终编号
    await expect(page.getByText(/正式已保存|Model saved/i)).not.toBeVisible()
    await expect(page.getByText(new RegExp(`${TEMPLATE_ID}_1`))).not.toBeVisible()

    // 12f. 移除错误数据文件, 上传与候选模型匹配的正确文件 → 重新提交 → 正式已保存
    await page.getByRole('button', { name: /移除|Remove/i }).click()
    const correctSha = candidateContentSha256(150, true, 'e2e-label')
    await page.locator('input[type="file"]').setInputFiles({
      name: 'load.csv',
      mimeType: 'text/csv',
      buffer: dataCsv(correctSha),
    })
    await expect(page.getByText(/临时已上传.*load\.csv|Temporarily uploaded: load\.csv/i)).toBeVisible()
    await page.getByRole('button', { name: /提交为候选|Submit as candidate/i }).click()
    await expect(page.getByRole('status', { name: /正式已保存|Saved/i })).toBeVisible()
    const savedPanel = page.locator('.ies-modeling__saved')
    // 最终 _N ID(后端分配; 面板同时展示"最终编号"与"设备 ID"两处, 限定面板内取首处)
    await expect(savedPanel.getByText(new RegExp(`${TEMPLATE_ID}_1`)).first()).toBeVisible()
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
    await expect(page.getByText(TEMPLATE_ID)).toBeVisible()
    await expect(page.getByText(/已发布|Published/i).first()).toBeVisible()

    // 除有意触发的 400 校验失败外, 不应有 console error / pageerror / 失败网络请求
    expect(
      [...consoleErrors, ...pageErrors],
      `console error / pageerror 应不存在, 实际:\n${[...consoleErrors, ...pageErrors].join('\n')}`,
    ).toEqual([])
    expect(failedRequests, `失败网络请求应不存在, 实际:\n${failedRequests.join('\n')}`).toEqual([])
    await context.close()
  })
})
