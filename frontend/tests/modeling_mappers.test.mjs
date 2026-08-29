/**
 * 建模 feature mapper 纯函数单元测试(FE-MD-02)。
 *
 * 覆盖: 数值解析(十进制/NaN/Infinity/边界)、单位与 valid_range、ID 预检查、
 * 空值与默认值、模板 inputs 树递归、表单→提交树、数组整体替换、保存结果与
 * 诊断解析、错误边界(形状不符不静默降级)。
 *
 * 纯 Node 环境(无浏览器/无 React): 用 esbuild 把 mappers.ts 打包为 CJS 后
 * 直接调用(与 tests/help_markdown.test.mjs 同一模式)。
 *
 * 运行(在 frontend 目录, Docker 内):
 *   npx esbuild src/features/modeling/mappers.ts --bundle --format=cjs \
 *     --outfile=/tmp/modeling-mappers.cjs
 *   node tests/modeling_mappers.test.mjs
 */

import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

let failures = 0
function check(name, cond, detail = '') {
  const ok = cond ? 'PASS' : 'FAIL'
  if (!cond) failures++
  console.log(`[${ok}] ${name}${detail ? ` — ${detail}` : ''}`)
}

const md = require('/tmp/modeling-mappers.cjs')

// ---------------------------------------------------------------------------
// 数值解析(宪法 §7.3: 只接受有限十进制数值)
// ---------------------------------------------------------------------------
check('数字: 普通小数', md.parseFiniteNumber('3.2') === 3.2)
check('数字: 前后空白容忍', md.parseFiniteNumber(' 2.5 ') === 2.5)
check('数字: 整数', md.parseFiniteNumber('42') === 42)
check('数字: 负号', md.parseFiniteNumber('-7.5') === -7.5)
check('数字: 科学计数', md.parseFiniteNumber('1e3') === 1000)
check('数字: 纯小数', md.parseFiniteNumber('.5') === 0.5)
check('数字: 空串拒绝', md.parseFiniteNumber('') === null)
check('数字: 全空白拒绝', md.parseFiniteNumber('   ') === null)
check('数字: 非数字拒绝', md.parseFiniteNumber('abc') === null)
check('数字: NaN 拒绝', md.parseFiniteNumber('NaN') === null)
check('数字: Infinity 拒绝', md.parseFiniteNumber('Infinity') === null)
check('数字: -Infinity 拒绝', md.parseFiniteNumber('-Infinity') === null)
check('数字: 十六进制拒绝', md.parseFiniteNumber('0x10') === null)
check('数字: 二进制拒绝', md.parseFiniteNumber('0b101') === null)
check('数字: 千分位拒绝', md.parseFiniteNumber('1,000') === null)
check('数字: 混合字符串拒绝', md.parseFiniteNumber('12abc') === null)
check('数字: 空串格式化', md.formatNumberText(null) === '')
check('数字: 值格式化', md.formatNumberText(3.5) === '3.5')

// ---------------------------------------------------------------------------
// valid_range 与范围校验
// ---------------------------------------------------------------------------
const vr = md.validRangeFromServer({ minimum: 0, maximum: 1000000 })
check('范围: 解析双边界', vr && vr.minimum === 0 && vr.maximum === 1000000)
const vrNull = md.validRangeFromServer({ minimum: 0, maximum: null })
check('范围: 单侧 null 边界', vrNull && vrNull.minimum === 0 && vrNull.maximum === null)
check('范围: null 输入', md.validRangeFromServer(null) === null)
check('范围: 非对象输入', md.validRangeFromServer('0-10') === null)
check('范围: 非数值边界置 null', (() => {
  const v = md.validRangeFromServer({ minimum: '0', maximum: 5 })
  return v && v.minimum === null && v.maximum === 5
})())
check('范围: 值在区间内', md.numberInRange(5, { minimum: 0, maximum: 10 }) === true)
check('范围: 值低于最小值', md.numberInRange(-1, { minimum: 0, maximum: 10 }) === false)
check('范围: 值高于最大值', md.numberInRange(11, { minimum: 0, maximum: 10 }) === false)
check('范围: 无边界恒真', md.numberInRange(1e9, null) === true)

// ---------------------------------------------------------------------------
// 模板 inputs 树递归(buildInputTree)
// ---------------------------------------------------------------------------
const inputsRaw = {
  properties: {
    peak_power_kw: {
      value: { type: 'number', unit: 'kW', valid_range: { minimum: 0, maximum: 10000000 }, default: 100 },
    },
    is_switchable: {
      value: { type: 'boolean', default: false },
    },
    label: {
      value: { type: 'string', default: 'pump' },
    },
  },
  interfaces: {
    electric_demand: {
      source: {
        data_ref: { type: 'data_repeat', data_ref: 'typical_day_load' },
      },
    },
  },
  profile: {
    type: 'array',
    items: { rows: { type: 'number', unit: 'MW' } },
  },
}
const root = md.buildInputTree(inputsRaw)
check('树: 顶层节点数', root.length === 3, `got=${root.length}`)
check('树: 顶层路径', root.map((n) => n.path).join(',') === 'properties,interfaces,profile')
const propNode = root[0]
check('树: properties 隐式对象容器', propNode.type === 'object' && propNode.children.length === 3)
const peakNode = propNode.children[0]
const peakLeaf = peakNode.children[0]
check('树: number 叶子路径', peakLeaf.path === 'properties.peak_power_kw.value')
check('树: number 叶子单位', peakLeaf.unit === 'kW')
check('树: number 叶子默认值', peakLeaf.default === 100)
check('树: number 叶子范围', peakLeaf.valid_range && peakLeaf.valid_range.maximum === 10000000)
check('树: boolean 叶子', propNode.children[1].children[0].type === 'boolean')
check('树: string 叶子默认值', propNode.children[2].children[0].default === 'pump')
const dataNode = root[1].children[0].children[0].children[0]
check('树: data_repeat 叶子与 data_ref', dataNode.type === 'data_repeat' && dataNode.data_ref === 'typical_day_load')
const arrayNode = root[2]
check('树: array 节点与子项路径', arrayNode.type === 'array' && arrayNode.children[0].path === 'profile[]')
check('树: array 子项 object 容器', arrayNode.children[0].type === 'object')
check('树: array 子项叶子路径', arrayNode.children[0].children[0].path === 'profile[].rows')
check('树: 未知类型标记 unsupported', (() => {
  const bad = md.buildInputTree({ weird: { type: 'widget' } })[0]
  return bad.unsupported === true && bad.type === 'widget'
})())
check('树: 无 type 标量节点标记 unsupported', (() => {
  const nodes = md.buildInputTree({ x: { value: 42 } })
  // x 是隐式 object 容器; 其子节点 value(标量 42, 无 type 声明)标记 unsupported
  return nodes[0].type === 'object' && nodes[0].children[0].unsupported === true
})())
check('树: 非对象根抛 MapperError', (() => {
  try {
    md.buildInputTree('not-an-object')
    return false
  } catch (err) {
    return err.name === 'MapperError'
  }
})())

// ---------------------------------------------------------------------------
// 表单初始值(defaultFormValues)
// ---------------------------------------------------------------------------
const defaults = md.defaultFormValues(root)
check('表单: number 默认值转文本', defaults['properties.peak_power_kw.value'].text === '100')
check('表单: boolean 默认值', defaults['properties.is_switchable.value'].checked === false)
check('表单: string 默认值', defaults['properties.label.value'].text === 'pump')
check('表单: data 初始未上传', defaults['interfaces.electric_demand.source.data_ref'].file_ref === null)
check('表单: 无默认 number 初始空', (() => {
  const r = md.buildInputTree({ p: { v: { type: 'number', unit: '1' } } })
  const v = md.defaultFormValues(r)['p.v']
  return v.kind === 'number' && v.text === ''
})())

// ---------------------------------------------------------------------------
// 表单即时校验(validateFormValues / formValuesToInputsOrErrors)
// ---------------------------------------------------------------------------
check('校验: 空 number 必填', (() => {
  const values = md.defaultFormValues(root)
  values['properties.peak_power_kw.value'] = { kind: 'number', text: '' }
  const errors = md.validateFormValues(root, values)
  return errors.some((e) => e.path === 'properties.peak_power_kw.value' && e.message_key === 'ies.modeling.form.err.required')
})())
check('校验: 非数值 number 报错', (() => {
  const values = md.defaultFormValues(root)
  values['properties.peak_power_kw.value'] = { kind: 'number', text: 'NaN' }
  return md.validateFormValues(root, values).some((e) => e.path === 'properties.peak_power_kw.value' && e.message_key === 'ies.modeling.form.err.number')
})())
check('校验: 越界 number 报范围错误', (() => {
  const values = md.defaultFormValues(root)
  values['properties.peak_power_kw.value'] = { kind: 'number', text: '20000000' }
  const errors = md.validateFormValues(root, values)
  return errors.some((e) => e.path === 'properties.peak_power_kw.value' && e.message_key === 'ies.modeling.form.err.range')
})())
check('校验: 合法值无错误', (() => {
  const values = md.defaultFormValues(root)
  values['properties.peak_power_kw.value'] = { kind: 'number', text: '150' }
  return md.validateFormValues(root, values).length === 0
})())
check('校验: 数组元素空 number 定位到 P[0].x', (() => {
  const r = md.buildInputTree({ arr: { type: 'array', items: { val: { type: 'number', unit: '1' } } } })
  const values = md.defaultFormValues(r)
  values.arr = { kind: 'array', items: [{ val: { kind: 'number', text: '' } }] }
  const errors = md.validateFormValues(r, values)
  return errors.some((e) => e.path === 'arr[0].val')
})())

// ---------------------------------------------------------------------------
// form → 提交 JSON inputs 树
// ---------------------------------------------------------------------------
check('提交: 合法表单产出 inputs 树', (() => {
  const values = md.defaultFormValues(root)
  values['properties.peak_power_kw.value'] = { kind: 'number', text: '150' }
  values['properties.is_switchable.value'] = { kind: 'boolean', checked: true }
  values['interfaces.electric_demand.source.data_ref'] = { kind: 'data', file_ref: 'temp:1', file_name: 'load.csv' }
  const result = md.formValuesToInputsOrErrors(root, values)
  if (!result.ok) return false
  const inputs = result.inputs
  return (
    inputs.properties.peak_power_kw.value === 150 &&
    inputs.properties.is_switchable.value === true &&
    inputs.interfaces.electric_demand.source.data_ref === 'typical_day_load'
  )
})())
check('提交: 空 number 必填错误阻断提交', (() => {
  const values = md.defaultFormValues(root)
  values['properties.peak_power_kw.value'] = { kind: 'number', text: '' }
  const result = md.formValuesToInputsOrErrors(root, values)
  return !result.ok && result.errors.some((e) => e.path === 'properties.peak_power_kw.value' && e.message_key === 'ies.modeling.form.err.required')
})())
check('提交: 未上传 data 叶子不提交', (() => {
  const result = md.formValuesToInputsOrErrors(root, md.defaultFormValues(root))
  // data 叶子未上传 → interfaces 子树整体不提交(模板保持原样)
  return result.ok && !('interfaces' in result.inputs)
})())
check('提交: 非法表单返回错误不产出树', (() => {
  const values = md.defaultFormValues(root)
  values['properties.peak_power_kw.value'] = { kind: 'number', text: 'abc' }
  const result = md.formValuesToInputsOrErrors(root, values)
  return !result.ok && result.errors.length > 0
})())
check('提交: 数组整体替换为元素数组', (() => {
  const r = md.buildInputTree({ arr: { type: 'array', items: { val: { type: 'number', unit: 'MW' } } } })
  const values = md.defaultFormValues(r)
  values.arr = { kind: 'array', items: [{ val: { kind: 'number', text: '1.5' } }, { val: { kind: 'number', text: '2.5' } }] }
  const result = md.formValuesToInputsOrErrors(r, values)
  if (!result.ok) return false
  const arr = result.inputs.arr
  return Array.isArray(arr) && arr.length === 2 && arr[0].val === 1.5 && arr[1].val === 2.5
})())
check('提交: 未添加数组元素则不提交数组(模板保持原样)', (() => {
  const r = md.buildInputTree({ arr: { type: 'array', items: { val: { type: 'number', unit: 'MW' } } } })
  const values = md.defaultFormValues(r)
  const result = md.formValuesToInputsOrErrors(r, values)
  return result.ok && !('arr' in result.inputs)
})())
check('提交: 已添加但全空的数组元素报必填错误', (() => {
  const r = md.buildInputTree({ arr: { type: 'array', items: { val: { type: 'number', unit: 'MW' } } } })
  const values = md.defaultFormValues(r)
  values.arr = { kind: 'array', items: [{ val: { kind: 'number', text: '' } }] }
  const result = md.formValuesToInputsOrErrors(r, values)
  return !result.ok && result.errors.some((e) => e.path === 'arr[0].val' && e.message_key === 'ies.modeling.form.err.required')
})())
check('提交: 单叶子数组模板元素为标量', (() => {
  const r = md.buildInputTree({ arr: { type: 'array', items: { type: 'number', unit: 'kW' } } })
  const values = md.defaultFormValues(r)
  values.arr = { kind: 'array', items: [{ '': { kind: 'number', text: '3' } }] }
  const result = md.formValuesToInputsOrErrors(r, values)
  return result.ok && Array.isArray(result.inputs.arr) && result.inputs.arr[0] === 3
})())

// ---------------------------------------------------------------------------
// 临时文件引用收集
// ---------------------------------------------------------------------------
check('文件引用: 只收集已上传 data 叶子', (() => {
  const values = md.defaultFormValues(root)
  values['interfaces.electric_demand.source.data_ref'] = { kind: 'data', file_ref: 'temp:1', file_name: 'a.csv' }
  values['properties.label.value'] = { kind: 'string', text: 'x' }
  const refs = md.collectTempFileRefs(values)
  return refs.length === 1 && refs[0].path === 'interfaces.electric_demand.source.data_ref' && refs[0].temp_file_ref === 'temp:1'
})())

// ---------------------------------------------------------------------------
// 模板解析(名称本地化 / 严格形状)
// ---------------------------------------------------------------------------
const tmplRaw = {
  template_id: 'ies.test.sample',
  names: { 'zh-CN': '样例模板', 'en-US': 'Sample Template' },
  schema_version: '2.0.0',
  content_sha256: 'ab'.repeat(32),
  revision: 3,
  has_inputs: true,
}
const summary = md.templateSummaryFromServer(tmplRaw, 'zh')
check('模板: zh 名称解析', summary.name === '样例模板' && summary.template_id === 'ies.test.sample')
check('模板: en 名称解析', md.templateSummaryFromServer(tmplRaw, 'en').name === 'Sample Template')
check('模板: 未知 locale 回退 zh', md.templateSummaryFromServer(tmplRaw, 'fr').name === '样例模板')
check('模板: 空 names 回退 template_id', md.templateSummaryFromServer({ ...tmplRaw, names: {} }, 'zh').name === 'ies.test.sample')
check('模板: 摘要与修订透传', summary.content_sha256.length === 64 && summary.revision === 3 && summary.has_inputs === true)
check('模板: 缺 template_id 抛 MapperError', (() => {
  try {
    md.templateSummaryFromServer({ names: {} }, 'zh')
    return false
  } catch (err) {
    return err.name === 'MapperError'
  }
})())

const docBody = {
  template: tmplRaw,
  document: { schema: 'ies.device-model', schema_version: '2.0.0', inputs: inputsRaw },
}
const doc = md.templateDocumentFromServer(docBody, 'zh')
check('模板详情: 文档解析出 inputs 树', doc.inputs.length === 3 && doc.summary.template_id === 'ies.test.sample')
check('模板详情: 缺 document 抛 MapperError', (() => {
  try {
    md.templateDocumentFromServer({ template: tmplRaw }, 'zh')
    return false
  } catch (err) {
    return err.name === 'MapperError'
  }
})())

// ---------------------------------------------------------------------------
// 保存结果解析(权威: 最终 _N ID / 规范 YAML / 摘要 / revision)
// ---------------------------------------------------------------------------
const saveBody = {
  model: {
    model_id: 'ies.test.sample_1',
    device_id: 'ies.test.sample',
    schema_version: '2.0.0',
    canonical_yaml: 'schema: ies.device-model\n',
    content_sha256: 'cd'.repeat(32),
    summary: { property_count: 2, interface_count: 1, relation_count: 0 },
    project_revision: 4,
  },
  project_revision: 4,
}
const saved = md.savedModelFromServer(saveBody)
check('保存: 最终 _N ID', saved.model_id === 'ies.test.sample_1')
check('保存: 规范 YAML 与摘要', saved.canonical_yaml.includes('ies.device-model') && saved.content_sha256.length === 64)
check('保存: 摘要计数', saved.summary.property_count === 2 && saved.summary.interface_count === 1)
check('保存: 项目修订', saved.project_revision === 4)
check('保存: 响应缺 model 抛 MapperError', (() => {
  try {
    md.savedModelFromServer({ project_revision: 4 })
    return false
  } catch (err) {
    return err.name === 'MapperError'
  }
})())

// ---------------------------------------------------------------------------
// 诊断解析(字段路径 / YAML 行列 / expected/actual)
// ---------------------------------------------------------------------------
const diagRaw = {
  code: 'PROJ-VAL-014',
  message_key: 'ies.diag.model.invalid',
  severity: 'error',
  blocking: true,
  params: { file: '<candidate>', detail: 'unit 无法识别', expected: 'kW', actual: 'kw' },
  location: { object_type: 'device-model', field: 'properties.rated_heat_kw.unit', line: 12, column: 9 },
  fix_hint_key: '',
  ref_ids: [],
}
const diag = md.diagFromServer(diagRaw)
check('诊断: 字段路径', diag.field === 'properties.rated_heat_kw.unit')
check('诊断: YAML 行列', diag.line === 12 && diag.column === 9)
check('诊断: expected/actual', diag.expected === 'kW' && diag.actual === 'kw')
check('诊断: 严重度与阻断', diag.severity === 'error' && diag.blocking === true)
check('诊断: 空 location 容错', (() => {
  const d = md.diagFromServer({ ...diagRaw, location: null })
  return d.field === null && d.line === null
})())
check('诊断: 形状不符返回 null', md.diagFromServer({ code: 'X' }) === null)
check('诊断: 从错误 params.diagnostics 提取', (() => {
  const err = { params: { diagnostics: [diagRaw] } }
  const list = md.diagnosticsFromError(err)
  return list.length === 1 && list[0].field === 'properties.rated_heat_kw.unit'
})())
check('诊断: 无诊断返回空数组', md.diagnosticsFromError({ params: {} }).length === 0)
check('诊断: 裸 diagnostics 数组容错', (() => {
  const list = md.diagnosticsFromError({ diagnostics: [diagRaw] })
  return list.length === 1 && list[0].code === 'PROJ-VAL-014'
})())

// ---------------------------------------------------------------------------
// 候选请求构建(source 判别)
// ---------------------------------------------------------------------------
check('请求: template 来源', (() => {
  const req = md.buildCandidateRequest({
    source: 'template',
    template_id: 'ies.test.sample',
    inputs_json: { properties: { x: { value: 1 } } },
    content_yaml: null,
    project_revision: 2,
    idempotency_key: 'k1',
    temp_file_refs: [],
  })
  return req.source === 'template' && req.template_id === 'ies.test.sample' && req.content === null
})())
check('请求: template 缺 template_id 抛错', (() => {
  try {
    md.buildCandidateRequest({ source: 'template', template_id: null, inputs_json: {}, content_yaml: null, project_revision: 1, idempotency_key: 'k', temp_file_refs: [] })
    return false
  } catch (err) {
    return err.name === 'MapperError'
  }
})())
check('请求: yaml 来源', (() => {
  const req = md.buildCandidateRequest({
    source: 'yaml',
    template_id: null,
    inputs_json: null,
    content_yaml: 'schema: ies.device-model\n',
    project_revision: 2,
    idempotency_key: 'k2',
    temp_file_refs: [],
  })
  return req.source === 'yaml' && req.content.includes('ies.device-model') && req.template_id === null
})())
check('请求: yaml 空内容抛错', (() => {
  try {
    md.buildCandidateRequest({ source: 'yaml', template_id: null, inputs_json: null, content_yaml: '   ', project_revision: 1, idempotency_key: 'k', temp_file_refs: [] })
    return false
  } catch (err) {
    return err.name === 'MapperError'
  }
})())

// ---------------------------------------------------------------------------
// 设备 ID 预检查与 YAML 骨架
// ---------------------------------------------------------------------------
check('ID: 合法命名空间 ID', md.isValidDeviceId('acme.device.heat_pump') === true)
check('ID: 下划线/连字符合法', md.isValidDeviceId('vendor_device-01') === true)
check('ID: 大写拒绝', md.isValidDeviceId('Acme.Device') === false)
check('ID: 连续分隔符拒绝', md.isValidDeviceId('a..b') === false)
check('ID: 空串拒绝', md.isValidDeviceId('') === false)

const skeleton = md.buildYamlSkeleton()
check('骨架: 顶层 schema', skeleton.includes('schema: ies.device-model'))
check('骨架: schema_version 2.0.0', skeleton.includes('schema_version: "2.0.0"'))
check('骨架: 五顶层字段齐全', ['device:', 'properties:', 'interfaces:', 'equations:'].every((k) => skeleton.includes(k)))
check('骨架: 不含经济字段', !skeleton.includes('price') && !skeleton.includes('cost'))

check('行列: 首行', (() => { const p = md.yamlLineColumn('abc', 0); return p.line === 1 && p.column === 1 })())
check('行列: 多行偏移', (() => { const p = md.yamlLineColumn('a\nbcd', 4); return p.line === 2 && p.column === 3 })())
check('行列: 越界钳制', (() => { const p = md.yamlLineColumn('ab', 99); return p.line === 1 && p.column === 3 })())

// ---------------------------------------------------------------------------
console.log(`\nmodeling mapper 测试: ${failures === 0 ? '全部通过' : `${failures} 个失败`}`)
process.exit(failures === 0 ? 0 : 1)
