/**
 * 建模 feature 前端领域模型(由 mappers.ts 从 contracts.ts DTO 转换而来)。
 *
 * 保存状态机(与 frontend.md「新建并保存项目模型」一致):
 *   editing ──上传临时数据文件──> temporary_uploaded
 *   editing / temporary_uploaded ──提交──> validating
 *   validating ──后端校验失败(聚合诊断)──> validation_failed(保留输入)
 *   validating ──后端校验通过(返回最终 _N ID/规范 YAML/摘要/revision)──> saved
 *   validation_failed ──再次编辑──> editing
 * 前端不预分配 _N 编号; 只有 saved 才进入项目模型列表并允许进入装配。
 */

import type { CandidateDiagnosticDto, ModelInputNodeType } from './contracts'

// ---------------------------------------------------------------------------
// 保存状态
// ---------------------------------------------------------------------------

/** 明确区分的保存状态: 编辑中 / 临时已上传 / 校验中 / 校验失败 / 正式已保存。 */
export type ModelSavePhase =
  | 'editing'
  | 'temporary_uploaded'
  | 'validating'
  | 'validation_failed'
  | 'saved'

/** 候选来源(与后端 CandidateSource 一致)。 */
export type ModelSource = 'template' | 'yaml'

// ---------------------------------------------------------------------------
// 模板
// ---------------------------------------------------------------------------

/** 模板列表项(前端领域形态; name 已按当前 locale 解析)。 */
export interface TemplateSummary {
  template_id: string
  name: string
  names: Record<string, string>
  schema_version: string
  description: string | null
  content_sha256: string
  revision: number
  has_inputs: boolean
}

/** 模板 inputs 树节点(递归表单生成的结构视图)。 */
export interface InputNode {
  /** 点分路径(数组子项以 "[]" 分段); 表单值以此为键。 */
  path: string
  /** 当前路径段(展示名)。 */
  key: string
  type: ModelInputNodeType
  unit: string | null
  valid_range: { minimum: number | null; maximum: number | null } | null
  default: number | boolean | string | null
  /** data_repeat/data_predict 绑定的数据引用(只读展示)。 */
  data_ref: string | null
  /** object(fields)/array(items) 子节点。 */
  children: InputNode[]
  /** 类型声明无法识别时为 true(不静默丢弃, 表单中明确提示)。 */
  unsupported: boolean
}

/** 模板文档(列表项 + 已解析的 inputs 树)。 */
export interface TemplateDocument {
  summary: TemplateSummary
  /** 顶层 inputs 树根列表(表单生成输入)。 */
  inputs: InputNode[]
  /** 原始 2.0.0 文档(供表单提交/回显)。 */
  raw: Record<string, unknown>
}

// ---------------------------------------------------------------------------
// 候选模型与保存结果
// ---------------------------------------------------------------------------

/** 提交候选所需的前端领域对象(由页面/hook 组装)。 */
export interface CandidateModel {
  source: ModelSource
  /** source=template 时必填。 */
  template_id: string | null
  /** source=template: 表单 JSON inputs 树。 */
  inputs_json: unknown | null
  /** source=yaml: 候选 YAML 文本。 */
  content_yaml: string | null
  /** 项目草稿修订(乐观锁)。 */
  project_revision: number
  /** 幂等键。 */
  idempotency_key: string
  /** 临时数据文件引用(path → temp_file_ref)。 */
  temp_file_refs: { path: string; temp_file_ref: string }[]
}

/** 保存成功后的正式模型信息(以后端返回为权威)。 */
export interface SavedModelInfo {
  /** 后端分配的项目内最终 ID(如 acme.device.heat_pump_1)。 */
  model_id: string
  device_id: string
  schema_version: string
  canonical_yaml: string
  content_sha256: string
  summary: { property_count: number; interface_count: number; relation_count: number }
  /** 保存后的项目草稿修订。 */
  project_revision: number
}

/** 校验失败诊断(前端领域形态: 字段路径 + YAML 行列 + expected/actual)。 */
export interface ModelDiagnostic {
  code: string
  message_key: string
  severity: 'blocking' | 'error' | 'warning' | 'info'
  blocking: boolean
  /** 诊断定位字段路径(如 properties.cop.value)。 */
  field: string | null
  /** YAML 行列(后端可定位时提供)。 */
  line: number | null
  column: number | null
  /** 后端详细描述。 */
  detail: string | null
  expected: unknown
  actual: unknown
  fix_hint_key: string | null
  ref_ids: string[]
}

/** 候选校验失败: 携带聚合诊断(输入必须保留, 不得显示保存成功)。 */
export class CandidateSaveError extends Error {
  readonly diagnostics: ModelDiagnostic[]

  constructor(diagnostics: ModelDiagnostic[]) {
    super(`候选校验失败: ${diagnostics.length} 条诊断`)
    this.name = 'CandidateSaveError'
    this.diagnostics = diagnostics
  }
}

/** 契约解析错误(响应形状与契约不一致时抛出, 不静默降级)。 */
export class MapperError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'MapperError'
  }
}

/** 诊断条目类型收窄辅助(供组件/测试引用)。 */
export type { CandidateDiagnosticDto }
