/**
 * 建模 feature 纯 mapper(宪法 §9.2): DTO ↔ 前端领域模型 / form ↔ DTO。
 *
 * 全部函数为纯函数: 不访问网络、缓存或 React 状态。数值解析不依赖 locale;
 * ID 不参与算术; 契约形状不符抛 MapperError, 不静默降级为空值/默认值。
 * 前端预检查只做即时反馈, 后端校验始终是权威闸门。
 */

import type {
  CandidateSaveRequestDto,
  ValidRangeDto,
} from './contracts'
import type { FormFieldError, FormFieldValue } from './form'
import type {
  CandidateModel,
  InputNode,
  ModelDiagnostic,
  SavedModelInfo,
  TemplateDocument,
  TemplateSummary,
} from './model'
import { MapperError } from './model'

// ---------------------------------------------------------------------------
// 数值(宪法 §7.3: 有限数值; 表单字符串在 mapper 边界解析)
// ---------------------------------------------------------------------------

/** 十进制有限数值文本模式(拒绝 0x/0b/Infinity/NaN/千分位等非十进制形态)。 */
const DECIMAL_NUMBER_PATTERN = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/

/**
 * 解析表单数字文本 → 有限数值。
 * 空串/前后空白/非法/NaN/Infinity/非十进制形态均返回 null; 不做任何隐式换算。
 */
export function parseFiniteNumber(text: string): number | null {
  const trimmed = text.trim()
  if (trimmed === '' || !DECIMAL_NUMBER_PATTERN.test(trimmed)) return null
  const value = Number(trimmed)
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  return value
}

/** 数值 → 表单文本(用于默认值初始化; null → 空串)。 */
export function formatNumberText(value: number | null): string {
  if (value === null || value === undefined) return ''
  return String(value)
}

/** 校验数值是否落在闭区间内(null 边界表示无界)。 */
export function numberInRange(value: number, vr: { minimum: number | null; maximum: number | null } | null): boolean {
  if (!vr) return true
  if (vr.minimum !== null && value < vr.minimum) return false
  if (vr.maximum !== null && value > vr.maximum) return false
  return true
}

/** 后端 valid_range(含 null) → 前端闭区间(null 表示无该侧边界)。 */
export function validRangeFromServer(value: unknown): ValidRangeDto | null {
  if (value === null || value === undefined) return null
  if (typeof value !== 'object' || Array.isArray(value)) return null
  const rec = value as Record<string, unknown>
  const minimum = typeof rec.minimum === 'number' ? rec.minimum : null
  const maximum = typeof rec.maximum === 'number' ? rec.maximum : null
  return { minimum, maximum }
}

// ---------------------------------------------------------------------------
// 模板 inputs 树(与后端 parse_template_inputs 语义一致)
// ---------------------------------------------------------------------------

/** 模板 inputs 叶子类型集合。 */
const LEAF_TYPES = new Set(['number', 'boolean', 'string', 'data_repeat', 'data_predict'])

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

/** 从原始 inputs 树递归构建 InputNode 树(路径语义与后端 TemplateInputSpec 一致)。 */
export function buildInputTree(raw: unknown, basePath = ''): InputNode[] {
  const root = asRecord(raw)
  if (!root) {
    throw new MapperError('模板 inputs 必须是 mapping')
  }
  const nodes: InputNode[] = []
  for (const [key, sub] of Object.entries(root)) {
    nodes.push(buildInputNode(sub, basePath ? `${basePath}.${key}` : key, key))
  }
  return nodes
}

function buildInputNode(value: unknown, path: string, key: string): InputNode {
  const rec = asRecord(value)
  if (!rec) {
    // 类型声明无法解释: 不静默丢弃, 标记 unsupported 由表单明确提示
    return {
      path,
      key,
      type: 'object',
      unit: null,
      valid_range: null,
      default: null,
      data_ref: null,
      children: [],
      unsupported: true,
    }
  }
  const type = typeof rec.type === 'string' ? rec.type : null
  if (type === null) {
    // 隐式 object 容器(与模型同构树形结构)
    return {
      path,
      key,
      type: 'object',
      unit: null,
      valid_range: null,
      default: null,
      data_ref: null,
      children: buildInputTree(rec, path),
      unsupported: false,
    }
  }
  if (!LEAF_TYPES.has(type)) {
    if (type === 'object') {
      const fields = asRecord(rec.fields)
      return {
        path,
        key,
        type: 'object',
        unit: null,
        valid_range: null,
        default: null,
        data_ref: null,
        children: fields ? buildInputTree(fields, path) : [],
        unsupported: false,
      }
    }
    if (type === 'array') {
      const items = rec.items
      if (items === null || items === undefined) {
        return { path, key, type: 'array', unit: null, valid_range: null, default: null, data_ref: null, children: [], unsupported: false }
      }
      if (Array.isArray(items)) {
        return {
          path,
          key,
          type: 'array',
          unit: null,
          valid_range: null,
          default: null,
          data_ref: null,
          children: items.map((item, i) => buildInputNode(item, `${path}[]`, `[]${i}`)),
          unsupported: false,
        }
      }
      if (asRecord(items)) {
        return {
          path,
          key,
          type: 'array',
          unit: null,
          valid_range: null,
          default: null,
          data_ref: null,
          children: [buildInputNode(items, `${path}[]`, '[]')],
          unsupported: false,
        }
      }
      return { path, key, type: 'array', unit: null, valid_range: null, default: null, data_ref: null, children: [], unsupported: true }
    }
    // 未知类型
    return {
      path,
      key,
      type: type as InputNode['type'],
      unit: null,
      valid_range: null,
      default: null,
      data_ref: null,
      children: [],
      unsupported: true,
    }
  }
  // 标量 / data 叶子
  let unit: string | null = null
  let vrange: ValidRangeDto | null = null
  if (type === 'number') {
    unit = typeof rec.unit === 'string' ? rec.unit : null
    vrange = validRangeFromServer(rec.valid_range)
  }
  const dataRef = type === 'data_repeat' || type === 'data_predict' ? (typeof rec.data_ref === 'string' ? rec.data_ref : null) : null
  const def = rec.default === null || rec.default === undefined ? null : (rec.default as number | boolean | string)
  return {
    path,
    key,
    type: type as InputNode['type'],
    unit,
    valid_range: vrange,
    default: def,
    data_ref: dataRef,
    children: [],
    unsupported: false,
  }
}

// ---------------------------------------------------------------------------
// 表单初始值与即时校验
// ---------------------------------------------------------------------------

/** 从 inputs 树生成初始表单值(叶子默认值; data 字段初始未上传)。 */
export function defaultFormValues(nodes: InputNode[]): Record<string, FormFieldValue> {
  const out: Record<string, FormFieldValue> = {}
  for (const node of nodes) {
    collectDefaults(node, out)
  }
  return out
}

function collectDefaults(node: InputNode, out: Record<string, FormFieldValue>): void {
  if (node.type === 'object' || node.type === 'array') {
    for (const child of node.children) collectDefaults(child, out)
    return
  }
  if (node.unsupported) return
  switch (node.type) {
    case 'number':
      out[node.path] = { kind: 'number', text: formatNumberText(typeof node.default === 'number' ? node.default : null) }
      break
    case 'boolean':
      out[node.path] = { kind: 'boolean', checked: node.default === true }
      break
    case 'string':
      out[node.path] = { kind: 'string', text: typeof node.default === 'string' ? node.default : '' }
      break
    case 'data_repeat':
    case 'data_predict':
      out[node.path] = { kind: 'data', file_ref: null, file_name: null }
      break
  }
}

/** 表单字段即时校验(前端预检查; 后端校验始终是权威闸门)。 */
export function validateFormValues(nodes: InputNode[], values: Record<string, FormFieldValue>): FormFieldError[] {
  const errors: FormFieldError[] = []
  for (const node of nodes) {
    validateNode(node, values, errors)
  }
  return errors
}

function validateNode(node: InputNode, values: Record<string, FormFieldValue>, errors: FormFieldError[]): void {
  if (node.type === 'object') {
    for (const child of node.children) validateNode(child, values, errors)
    return
  }
  if (node.type === 'array') {
    validateArrayNode(node, values, errors)
    return
  }
  if (node.unsupported) return
  const field = values[node.path]
  if (node.type === 'number') {
    const text = field?.kind === 'number' ? field.text : ''
    if (text.trim() === '') {
      errors.push({ path: node.path, message_key: 'ies.modeling.form.err.required' })
      return
    }
    const num = parseFiniteNumber(text)
    if (num === null) {
      errors.push({ path: node.path, message_key: 'ies.modeling.form.err.number' })
      return
    }
    if (!numberInRange(num, node.valid_range)) {
      errors.push({
        path: node.path,
        message_key: 'ies.modeling.form.err.range',
        params: {
          min: node.valid_range?.minimum ?? null,
          max: node.valid_range?.maximum ?? null,
        },
      })
    }
  }
}

/** 数组子节点的相对路径: 去掉 "arr[]" 前缀与其后的点号(单叶子模板 rel 为 "")。 */
function arrayItemRel(child: InputNode, arrayPath: string): string {
  const prefix = `${arrayPath}[]`
  if (!child.path.startsWith(prefix)) return child.path
  const rest = child.path.slice(prefix.length)
  return rest.startsWith('.') ? rest.slice(1) : rest
}

/** 数组: 逐元素校验(元素路径 P[i].x; 空值按元素位置报告)。 */
function validateArrayNode(node: InputNode, values: Record<string, FormFieldValue>, errors: FormFieldError[]): void {
  const field = values[node.path]
  if (field?.kind !== 'array') return
  const itemTemplate = node.children.length === 1 ? node.children[0] : null
  if (!itemTemplate) return // 多模板/未知数组: 后端权威
  field.items.forEach((row, i) => {
    for (const child of itemTemplate.children.length > 0 ? itemTemplate.children : [itemTemplate]) {
      const rel = arrayItemRel(child, node.path)
      const itemPath = `${node.path}[${i}]${rel ? `.${rel}` : ''}`
      const leafValue = row[rel] as FormFieldValue | undefined
      if (child.type === 'number') {
        const text = leafValue?.kind === 'number' ? leafValue.text : ''
        if (text.trim() === '') {
          errors.push({ path: itemPath, message_key: 'ies.modeling.form.err.required' })
          continue
        }
        const num = parseFiniteNumber(text)
        if (num === null) {
          errors.push({ path: itemPath, message_key: 'ies.modeling.form.err.number' })
          continue
        }
        if (!numberInRange(num, child.valid_range)) {
          errors.push({
            path: itemPath,
            message_key: 'ies.modeling.form.err.range',
            params: { min: child.valid_range?.minimum ?? null, max: child.valid_range?.maximum ?? null },
          })
        }
      }
    }
  })
}

// ---------------------------------------------------------------------------
// form → 提交 JSON inputs 树(只含模板已声明路径; 未填写字段不提交)
// ---------------------------------------------------------------------------

/**
 * 表单值 → 提交 inputs JSON 树。
 * - number 为空的叶子在预检查阶段报必填错误(不进入提交树);
 * - 未上传临时文件的 data 叶子不提交(模板合并保持原样);
 * - boolean / string 整体替换;
 * - array 整体替换为元素数组(元素模板单一声明时; 未添加元素则不提交)。
 * 返回 {ok:false} 时携带字段错误(不抛出, 供页面展示)。
 */
export function formValuesToInputsOrErrors(
  nodes: InputNode[],
  values: Record<string, FormFieldValue>,
): { ok: true; inputs: Record<string, unknown> } | { ok: false; errors: FormFieldError[] } {
  const errors: FormFieldError[] = validateFormValues(nodes, values)
  if (errors.length > 0) return { ok: false, errors }
  const inputs: Record<string, unknown> = {}
  for (const node of nodes) {
    buildSubmitted(node, values, inputs)
  }
  return { ok: true, inputs }
}

function buildSubmitted(node: InputNode, values: Record<string, FormFieldValue>, out: Record<string, unknown>): void {
  if (node.unsupported) return
  if (node.type === 'object') {
    const childOut: Record<string, unknown> = {}
    for (const child of node.children) buildSubmitted(child, values, childOut)
    if (Object.keys(childOut).length > 0) out[node.key] = childOut
    return
  }
  if (node.type === 'array') {
    const field = values[node.path]
    if (field?.kind === 'array' && field.items.length > 0) {
      const itemTemplate = node.children.length === 1 ? node.children[0] : null
      if (itemTemplate) {
        const items: unknown[] = []
        for (const row of field.items) {
          items.push(buildArrayItem(itemTemplate, row, node.path))
        }
        out[node.key] = items
      }
    }
    return
  }
  const field = values[node.path]
  switch (node.type) {
    case 'number': {
      const text = field?.kind === 'number' ? field.text : ''
      const num = parseFiniteNumber(text)
      if (num !== null) out[node.key] = num
      break
    }
    case 'boolean':
      out[node.key] = field?.kind === 'boolean' ? field.checked : false
      break
    case 'string':
      out[node.key] = field?.kind === 'string' ? field.text : ''
      break
    case 'data_repeat':
    case 'data_predict': {
      // 已上传临时文件时提交模板声明的 data_ref(临时文件内容由 temp_file_refs 绑定)
      const data = field?.kind === 'data' ? field : null
      if (data?.file_ref && node.data_ref !== null) out[node.key] = node.data_ref
      break
    }
  }
}

/** 数组元素值: 单叶子模板 → 标量; 对象容器模板 → 按相对键组装。 */
function buildArrayItem(
  itemTemplate: InputNode,
  row: Record<string, FormFieldValue>,
  arrayPath: string,
): unknown {
  if (itemTemplate.type === 'object' || itemTemplate.type === 'array') {
    const obj: Record<string, unknown> = {}
    for (const child of itemTemplate.children) {
      if (child.type === 'object' || child.type === 'array') continue // 嵌套结构: 后端权威/直接 YAML 编辑
      const rel = arrayItemRel(child, arrayPath)
      const leaf = row[rel]
      if (child.type === 'number') {
        const num = leaf?.kind === 'number' ? parseFiniteNumber(leaf.text) : null
        if (num !== null) obj[child.key] = num
      } else if (child.type === 'boolean') {
        obj[child.key] = leaf?.kind === 'boolean' ? leaf.checked : false
      } else if (child.type === 'string') {
        obj[child.key] = leaf?.kind === 'string' ? leaf.text : ''
      } else if (child.type === 'data_repeat' || child.type === 'data_predict') {
        if (leaf?.kind === 'data' && leaf.file_ref && child.data_ref !== null) obj[child.key] = child.data_ref
      }
    }
    return obj
  }
  // 单叶子数组模板(row 以 '' 为键)
  const leaf = row['']
  if (itemTemplate.type === 'number') return parseFiniteNumber(leaf?.kind === 'number' ? leaf.text : '') ?? null
  if (itemTemplate.type === 'boolean') return leaf?.kind === 'boolean' ? leaf.checked : false
  if (itemTemplate.type === 'string') return leaf?.kind === 'string' ? leaf.text : ''
  return null
}

/** 收集临时数据文件引用(已上传的 data 叶子 → [{path, temp_file_ref}])。 */
export function collectTempFileRefs(values: Record<string, FormFieldValue>): { path: string; temp_file_ref: string }[] {
  const refs: { path: string; temp_file_ref: string }[] = []
  for (const [path, value] of Object.entries(values)) {
    if (value.kind === 'data' && value.file_ref) {
      refs.push({ path, temp_file_ref: value.file_ref })
    }
  }
  return refs
}

// ---------------------------------------------------------------------------
// 模板 / 保存结果 / 诊断解析(严格形状校验, 不符抛 MapperError)
// ---------------------------------------------------------------------------

/** 显示名解析: 精确 locale → locale 前缀(如 en → en-US) → zh-CN → en-US → 首个名称 → 空串。 */
export function localeName(names: Record<string, string> | null | undefined, locale: string): string {
  if (!names) return ''
  const exact = names[locale]
  if (typeof exact === 'string' && exact) return exact
  for (const [key, value] of Object.entries(names)) {
    if (key.startsWith(`${locale}-`) && typeof value === 'string' && value) return value
  }
  const zh = names['zh-CN']
  if (typeof zh === 'string' && zh) return zh
  const en = names['en-US']
  if (typeof en === 'string' && en) return en
  for (const value of Object.values(names)) {
    if (typeof value === 'string' && value) return value
  }
  return ''
}

/** 后端模板列表项 → 前端 TemplateSummary。 */
export function templateSummaryFromServer(raw: unknown, locale: string): TemplateSummary {
  const rec = asRecord(raw)
  if (!rec || typeof rec.template_id !== 'string') {
    throw new MapperError('模板列表项缺少 template_id')
  }
  const names: Record<string, string> = {}
  const namesRaw = asRecord(rec.names)
  if (namesRaw) {
    for (const [k, v] of Object.entries(namesRaw)) {
      if (typeof v === 'string') names[k] = v
    }
  }
  return {
    template_id: rec.template_id,
    names,
    name: localeName(names, locale) || rec.template_id,
    schema_version: typeof rec.schema_version === 'string' ? rec.schema_version : '2.0.0',
    description: typeof rec.description === 'string' ? rec.description : null,
    content_sha256: typeof rec.content_sha256 === 'string' ? rec.content_sha256 : '',
    revision: typeof rec.revision === 'number' ? rec.revision : 0,
    has_inputs: rec.has_inputs === true,
  }
}

/** 模板详情响应 {template, document} → 前端 TemplateDocument(含 inputs 树)。 */
export function templateDocumentFromServer(body: unknown, locale: string): TemplateDocument {
  const rec = asRecord(body)
  const summaryRaw = asRecord(rec?.template)
  if (!summaryRaw) throw new MapperError('模板详情缺少 template')
  const document = asRecord(rec?.document)
  if (!document) throw new MapperError('模板详情缺少 document')
  const summary = templateSummaryFromServer(summaryRaw, locale)
  const inputs = buildInputTree(document.inputs)
  return { summary, inputs, raw: document }
}

/** 保存成功响应 {model, project_revision} → 前端 SavedModelInfo。 */
export function savedModelFromServer(body: unknown): SavedModelInfo {
  const rec = asRecord(body)
  const model = asRecord(rec?.model)
  if (!model || typeof model.model_id !== 'string' || typeof model.canonical_yaml !== 'string') {
    throw new MapperError('候选保存响应缺少 model.model_id / model.canonical_yaml')
  }
  const summary = asRecord(model.summary)
  return {
    model_id: model.model_id,
    device_id: typeof model.device_id === 'string' ? model.device_id : model.model_id,
    schema_version: typeof model.schema_version === 'string' ? model.schema_version : '2.0.0',
    canonical_yaml: model.canonical_yaml,
    content_sha256: typeof model.content_sha256 === 'string' ? model.content_sha256 : '',
    summary: {
      property_count: typeof summary?.property_count === 'number' ? summary.property_count : 0,
      interface_count: typeof summary?.interface_count === 'number' ? summary.interface_count : 0,
      relation_count: typeof summary?.relation_count === 'number' ? summary.relation_count : 0,
    },
    project_revision: typeof rec?.project_revision === 'number' ? rec.project_revision : 0,
  }
}

/** 后端诊断条目 → 前端 ModelDiagnostic(形状不符返回 null, 由调用方过滤)。 */
export function diagFromServer(raw: unknown): ModelDiagnostic | null {
  const rec = asRecord(raw)
  if (!rec || typeof rec.message_key !== 'string') return null
  const params = asRecord(rec.params)
  const loc = asRecord(rec.location)
  return {
    code: typeof rec.code === 'string' ? rec.code : 'MODEL-VAL-001',
    message_key: rec.message_key,
    severity: rec.severity === 'blocking' || rec.severity === 'warning' || rec.severity === 'info' ? rec.severity : 'error',
    blocking: rec.blocking === true,
    field: loc && typeof loc.field === 'string' ? loc.field : null,
    line: loc && typeof loc.line === 'number' ? loc.line : null,
    column: loc && typeof loc.column === 'number' ? loc.column : null,
    detail: params && typeof params.detail === 'string' ? params.detail : null,
    expected: params?.expected ?? null,
    actual: params?.actual ?? null,
    fix_hint_key: typeof rec.fix_hint_key === 'string' ? rec.fix_hint_key : null,
    ref_ids: Array.isArray(rec.ref_ids) ? (rec.ref_ids as unknown[]).filter((r) => typeof r === 'string') : [],
  }
}

/** 从任意错误对象提取诊断列表(ApiError 信封 params.diagnostics / 裸数组)。 */
export function diagnosticsFromError(error: unknown): ModelDiagnostic[] {
  const rec = asRecord(error)
  const params = asRecord(rec?.params)
  const list = params ? (Array.isArray(params.diagnostics) ? params.diagnostics : null) : null
  if (list === null) {
    if (Array.isArray((error as { diagnostics?: unknown })?.diagnostics)) {
      return (error as { diagnostics: unknown[] }).diagnostics
        .map(diagFromServer)
        .filter((d): d is ModelDiagnostic => d !== null)
    }
    return []
  }
  return list.map(diagFromServer).filter((d): d is ModelDiagnostic => d !== null)
}

/** 候选领域对象 → 保存请求 DTO(判别字段 source)。 */
export function buildCandidateRequest(candidate: CandidateModel): CandidateSaveRequestDto {
  if (candidate.source === 'template') {
    if (!candidate.template_id) throw new MapperError('source=template 必须提供 template_id')
    return {
      source: 'template',
      template_id: candidate.template_id,
      inputs: candidate.inputs_json ?? {},
      content: null,
      project_revision: candidate.project_revision,
      idempotency_key: candidate.idempotency_key,
      temp_file_refs: candidate.temp_file_refs,
    }
  }
  if (candidate.content_yaml === null || candidate.content_yaml.trim() === '') {
    throw new MapperError('source=yaml 必须提供候选 YAML 内容')
  }
  return {
    source: 'yaml',
    template_id: null,
    inputs: null,
    content: candidate.content_yaml,
    project_revision: candidate.project_revision,
    idempotency_key: candidate.idempotency_key,
    temp_file_refs: candidate.temp_file_refs,
  }
}

// ---------------------------------------------------------------------------
// 直接 YAML 编辑辅助
// ---------------------------------------------------------------------------

/** 稳定设备类型 ID 预检查(前端即时提示; 后端校验始终是权威闸门)。 */
const DEVICE_ID_PATTERN = /^[a-z0-9]+([._-][a-z0-9]+)*$/

export function isValidDeviceId(id: string): boolean {
  return DEVICE_ID_PATTERN.test(id)
}

/** 标准 ies.device-model 2.0.0 YAML 骨架(直接创建页初始内容)。 */
export function buildYamlSkeleton(): string {
  return [
    'schema: ies.device-model',
    'schema_version: "2.0.0"',
    '',
    'device:',
    '  id: your.namespace.device_id',
    '  names:',
    '    zh-CN: 设备名称',
    '    en-US: Device Name',
    '',
    'properties:',
    '  # cop:',
    '  #   value: 3.2',
    '  #   unit: "1"',
    '  #   valid_range: {minimum: 1, maximum: 10}',
    '',
    'interfaces:',
    '  # electricity_in:',
    '  #   type: in',
    '  #   carrier: electricity',
    '  #   unit: kW',
    '  #   valid_range: {minimum: 0, maximum: null}',
    '',
    'equations:',
    '  variables: {}',
    '  relations: []',
    '',
  ].join('\n')
}

/** YAML 文本内偏移 → {line, column}(1 基, 供编辑器光标定位诊断)。 */
export function yamlLineColumn(text: string, offset: number): { line: number; column: number } {
  const clamped = Math.max(0, Math.min(offset, text.length))
  const before = text.slice(0, clamped)
  const line = before.split('\n').length
  const lastNewline = before.lastIndexOf('\n')
  const column = lastNewline >= 0 ? clamped - lastNewline : clamped + 1
  return { line, column }
}
