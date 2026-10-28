/**
 * 建模 feature 纯 mapper(宪法 §9.2): DTO ↔ 前端领域模型 / form ↔ DTO。
 *
 * 全部函数为纯函数: 不访问网络、缓存或 React 状态。数值解析不依赖 locale;
 * ID 不参与算术; 契约形状不符抛 MapperError, 不静默降级为空值/默认值。
 * 前端预检查只做即时反馈, 后端校验始终是权威闸门。
 */

import type {
  CandidateSaveRequestDto,
  CandidateSaveResultDto,
  DeviceParameterSpecDto,
  DeviceTypeDto,
  DeviceTypeListResponseDto,
  DraftConnectionDto,
  DraftDeviceDto,
  ModelDiagnosticDto,
  ProjectModelDto,
  TemplateDetailDto,
  TemplateRevisionDetailDto,
  TemplateRevisionDto,
  TemplateSummaryDto,
  ValidRangeDto,
} from './contracts'
import type { DraftContentDeviceDto, ProjectViewResponseDto } from '../projects'
import type { FormFieldError, FormFieldValue } from './form'
import type {
  CandidateModel,
  InputNode,
  ModelDiagnostic,
  ProjectModelSummary,
  SavedModelInfo,
  TemplateDetail,
  TemplateRevision,
  TemplateSummary,
} from './model'
import { MapperError } from './model'
import { ApiError } from '../../types'
import type {
  Connection,
  Device,
  DevicePortSpec,
  DeviceTypeSpec,
  DraftModelSection,
} from '../../types'

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
const LEAF_TYPES = new Set(['number', 'boolean', 'string', 'data_repeat', 'data_full', 'data_predict'])

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
        children: fields ? buildInputTree(fields, path) : [],
        unsupported: false,
      }
    }
    if (type === 'array') {
      const items = rec.items
      if (items === null || items === undefined) {
        return { path, key, type: 'array', unit: null, valid_range: null, children: [], unsupported: false }
      }
      if (Array.isArray(items)) {
        return {
          path,
          key,
          type: 'array',
          unit: null,
          valid_range: null,
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
          children: [buildInputNode(items, `${path}[]`, '[]')],
          unsupported: false,
        }
      }
      return { path, key, type: 'array', unit: null, valid_range: null, children: [], unsupported: true }
    }
    // 未知类型
    return {
      path,
      key,
      type: type as InputNode['type'],
      unit: null,
      valid_range: null,
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
  return {
    path,
    key,
    type: type as InputNode['type'],
    unit,
    valid_range: vrange,
    children: [],
    unsupported: false,
  }
}

// ---------------------------------------------------------------------------
// 表单初始值与即时校验
// ---------------------------------------------------------------------------

/** 从 inputs 树生成初始表单值(data 字段为项目内相对 CSV 路径)。 */
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
      // 2.2.0: 模板不提供默认值, 由用户必填。
      out[node.path] = { kind: 'number', text: '' }
      break
    case 'boolean':
      out[node.path] = { kind: 'boolean', checked: false }
      break
    case 'string':
      out[node.path] = { kind: 'string', text: '' }
      break
    case 'data_repeat':
    case 'data_full':
    case 'data_predict':
      out[node.path] = { kind: 'data', path: '' }
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
  if (node.type === 'data_repeat' || node.type === 'data_full' || node.type === 'data_predict') {
    const path = field?.kind === 'data' ? field.path.trim() : ''
    if (path === '') {
      errors.push({ path: node.path, message_key: 'ies.modeling.form.err.required' })
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
 * - data 叶子直接提交项目内相对 CSV 路径;
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
    case 'data_full':
    case 'data_predict': {
      const data = field?.kind === 'data' ? field : null
      if (data?.path.trim()) out[node.key] = data.path.trim()
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
      } else if (child.type === 'data_repeat' || child.type === 'data_full' || child.type === 'data_predict') {
        if (leaf?.kind === 'data' && leaf.path.trim()) obj[child.key] = leaf.path.trim()
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

/** 后端模板列表项 → 前端 TemplateSummary(模板 ID 即展示名; schema_version 来自权威 revision)。 */
export function templateSummaryFromServer(dto: TemplateSummaryDto, locale?: string): TemplateSummary {
  void locale
  return {
    id: dto.id,
    template_id: dto.template_id,
    status: dto.status,
    description: dto.description,
    draft_revision: dto.draft_revision,
    draft_has_inputs: dto.draft_has_inputs,
    published_revision: dto.published_revision,
    published_at: dto.published_at,
    created_at: dto.created_at,
    updated_at: dto.updated_at,
    revision: dto.revision ? templateRevisionFromServer(dto.revision) : null,
    name: '',
    schema_version: dto.revision?.schema_version ?? null,
    has_inputs: dto.draft_has_inputs === true,
  }
}

/** 精确发布 revision 后端视图 → 前端领域形态(直映射权威事实)。 */
export function templateRevisionFromServer(dto: TemplateRevisionDto): TemplateRevision {
  return {
    id: dto.id,
    revision: dto.revision,
    schema_version: dto.schema_version,
    input_count: dto.input_count,
    yaml_object_id: dto.yaml_object_id,
    receipt_object_id: dto.receipt_object_id,
    summary_object_id: dto.summary_object_id,
    published_by: dto.published_by,
    published_at: dto.published_at,
  }
}

/**
 * 模板草稿详情或精确 revision 详情 → 前端 TemplateDetail。
 * 精确 revision 接口将 revision 与 template 分列返回；这里把它合入 summary，
 * 使项目候选始终携带与 document 完全一致的 revision。
 */
export function templateDetailFromServer(
  body: TemplateRevisionDetailDto | TemplateDetailDto,
  locale?: string,
): TemplateDetail {
  void locale
  const exactRevision = 'revision' in body && body.revision ? body.revision : body.template.revision ?? null
  const summaryDto: TemplateSummaryDto = {
    ...body.template,
    revision: exactRevision,
  }
  const summary = templateSummaryFromServer(summaryDto)
  const document = body.document
  const inputs = document && document.inputs ? buildInputTree(document.inputs) : []
  return {
    summary,
    document,
    inputs,
    diagnostics: body.diagnostics.map(diagFromServer),
    raw: document ?? {},
  }
}

/** 项目模型清单行 → 前端领域形态(编号对用户可见, 严格按后端 project_model_to_dict 映射)。 */
export function projectModelFromServer(dto: ProjectModelDto): ProjectModelSummary {
  return {
    id: dto.id,
    project_id: dto.project_id,
    device_id: dto.device_id,
    base_device_id: dto.base_device_id,
    suffix: dto.suffix,
    revision: dto.revision,
    project_revision: dto.project_revision,
    model_object_id: dto.model_object_id,
    receipt_object_id: dto.receipt_object_id,
    source: dto.source,
    template_id: dto.template_id,
    template_revision: dto.template_revision,
    created_by: dto.created_by,
    created_at: dto.created_at,
  }
}

/** 保存成功响应 {project_model, receipt, project_revision, duplicate} → 前端 SavedModelInfo。 */
export function savedModelFromServer(res: CandidateSaveResultDto): SavedModelInfo {
  const model = res.project_model
  const receipt = res.receipt
  return {
    model_id: model.device_id,
    device_id: model.device_id,
    suffix: model.suffix,
    base_device_id: model.base_device_id,
    summary: {
      property_count: receipt.property_count,
      interface_count: receipt.interface_count,
      relation_count: receipt.relation_count,
    },
    project_revision: res.project_revision,
    source: model.source,
    template_id: model.template_id,
    template_revision: model.template_revision,
    duplicate: res.duplicate === true,
  }
}

/** 后端诊断条目 → 前端 ModelDiagnostic。 */
export function diagFromServer(dto: ModelDiagnosticDto): ModelDiagnostic {
  const loc = dto.location
  const params = dto.params
  return {
    code: dto.code,
    message_key: dto.message_key,
    severity: dto.severity,
    blocking: dto.blocking,
    field: loc?.field ?? null,
    line: loc?.line ?? null,
    column: loc?.column ?? null,
    detail: typeof params?.detail === 'string' ? params.detail : null,
    expected: params?.expected ?? null,
    actual: params?.actual ?? null,
    fix_hint_key: dto.fix_hint_key,
    ref_ids: dto.ref_ids,
  }
}

/** 从错误对象(ApiError 信封 params.diagnostics / 诊断数组)提取诊断列表。 */
export function diagnosticsFromError(error: unknown): ModelDiagnostic[] {
  const params = error instanceof ApiError ? error.params : (error as { params?: { diagnostics?: unknown[] } })?.params
  const list = params && Array.isArray(params.diagnostics)
    ? params.diagnostics
    : Array.isArray((error as { diagnostics?: unknown[] })?.diagnostics)
      ? (error as { diagnostics: unknown[] }).diagnostics
      : null
  if (!list) return []
  return (list as ModelDiagnosticDto[]).map(diagFromServer)
}

/** 候选领域对象 → 保存请求 DTO(判别字段 source)。 */
export function buildCandidateRequest(candidate: CandidateModel): CandidateSaveRequestDto {
  if (candidate.source === 'template') {
    if (!candidate.template_id) throw new MapperError('source=template 必须提供 template_id')
    if (!candidate.template_revision) {
      throw new MapperError('source=template 必须提供 template_revision')
    }
    return {
      source: 'template',
      model_yaml: '',
      template_id: candidate.template_id,
      template_revision: candidate.template_revision,
      template_inputs: candidate.inputs_json ?? {},
      expected_revision: candidate.project_revision,
      idempotency_key: candidate.idempotency_key,
    }
  }
  if (candidate.content_yaml === null || candidate.content_yaml.trim() === '') {
    throw new MapperError('source=yaml 必须提供候选 YAML 内容')
  }
  return {
    source: 'direct_yaml',
    model_yaml: candidate.content_yaml,
    template_id: null,
    template_revision: null,
    template_inputs: null,
    expected_revision: candidate.project_revision,
    idempotency_key: candidate.idempotency_key,
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

/** 标准 ies.device-model 2.2.0 YAML 骨架(直接创建页初始内容)。 */
export function buildYamlSkeleton(): string {
  return [
    'schema: ies.device-model',
    'schema_version: "2.2.0"',
    '',
    'device:',
    '  id: your.namespace.device_id',
    '  names:',
    '    zh-CN: 设备名称',
    '    en-US: Device Name',
    '',
    'properties: {}',
    '  # cop:',
    '  #   value: 3.2',
    '  #   unit: "1"',
    '  #   valid_range: {minimum: 1, maximum: 10}',
    '',
    'interfaces: {}',
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

// ---------------------------------------------------------------------------
// 草稿模型节(项目视图 draft.content.model; 2.0 唯一项目建模事实)
// ---------------------------------------------------------------------------

/** 草稿内容设备条目 → 前端 Device(2.0 名键, 无 id/状态/时间字段)。 */
export function deviceFromDraft(d: DraftDeviceDto | DraftContentDeviceDto): Device {
  return {
    name: d.name,
    ref: d.ref,
    display_name: d.display_name,
    device_type: d.device_type,
    kind: d.kind,
    params: d.params,
  }
}

/** 草稿内容连接条目 → 前端 Connection(2.0 名键端点, 无类型/容量/损耗)。 */
export function connFromDraft(c: DraftConnectionDto): Connection {
  return {
    name: c.name,
    from_device: c.from_device,
    from_port: c.from_port,
    to_device: c.to_device,
    to_port: c.to_port,
  }
}

/** 项目视图 draft.content.model → 前端 DraftModelSection。 */
export function modelSectionFromView(view: ProjectViewResponseDto): { revision: number; model: DraftModelSection } {
  return {
    revision: view.draft.revision,
    model: {
      devices: view.draft.content.model.devices.map(deviceFromDraft),
      connections: view.draft.content.model.connections.map(connFromDraft),
    },
  }
}

/** 2.0 连接默认命名(与后端 upsert_connection 缺省名公式一致)。 */
export function connectionName(
  fromDevice: string,
  fromPort: string,
  toDevice: string,
  toPort: string,
): string {
  return `${fromDevice}.${fromPort}->${toDevice}.${toPort}`
}

// ---------------------------------------------------------------------------
// 设备类型注册表
// ---------------------------------------------------------------------------

/** 参数默认值适配:枚举为字符串, 数值为 number, 结构化对象原样透传数值项。 */
export function paramDefaultValue(v: unknown): number | string | Record<string, number> | null {
  if (typeof v === 'number') return v
  if (typeof v === 'string') return v
  if (typeof v === 'object' && v !== null) {
    const out: Record<string, number> = {}
    for (const [key, val] of Object.entries(v)) {
      if (typeof val === 'number') out[key] = val
    }
    return Object.keys(out).length > 0 ? out : null
  }
  return null
}

/** 后端设备类型注册项 → 前端 DeviceTypeSpec(参数规格字段名适配 + 真实端口透传)。 */
export function deviceTypeFromServer(s: DeviceTypeDto): DeviceTypeSpec {
  const params = s.parameters
  const ports: DevicePortSpec[] = s.ports.map((p) => ({
    name: p.name,
    direction: p.direction,
    energy_carrier: p.energy_carrier,
    unit: p.unit ?? null,
  }))
  // 注册表公开 DTO 用 names {zh-CN,en-US} 字典(无 name_zh/name_en 平铺字段),
  // 载能只存在于 ports[].energy_carrier(无 energy_carriers 顶层数组): 直接按
  // 端口声明顺序去重派生, 与 descriptor 同一身份, 不做形状复核与值过滤。
  const names = s.names
  const carriers = [...new Set(ports.map((p) => p.energy_carrier))]
  return {
    type_id: s.type_id,
    schema_version: s.schema_version,
    name_zh: names['zh-CN'] ?? '',
    name_en: names['en-US'] ?? '',
    energy_carriers: carriers,
    ports,
    parameters: Object.fromEntries(
      Object.entries(params).map(([name, p]: [string, DeviceParameterSpecDto]) => [
        name,
        {
          name: p.name,
          unit: p.unit ?? null,
          min: p.min ?? null,
          max: p.max ?? null,
          default: paramDefaultValue(p.default),
        },
      ]),
    ),
  }
}

/** 注册表响应 {items} → DeviceTypeSpec 列表。 */
export function deviceTypeListFromServer(body: DeviceTypeListResponseDto): DeviceTypeSpec[] {
  return body.items.map(deviceTypeFromServer)
}
