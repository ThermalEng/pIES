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

/** 模板列表项(前端领域形态; 后端不返回 names, 以模板 ID 为展示名)。 */
export interface TemplateSummary {
  /** 主表行 id(不透明十进制字符串)。 */
  id: string
  template_id: string
  /** 生命周期状态: draft(未发布) / published(已发布且启用) / disabled(已停用)。 */
  status: 'draft' | 'published' | 'disabled'
  description: string | null
  draft_revision: number
  draft_sha256: string | null
  draft_has_inputs: boolean | null
  published_revision: number
  published_at: string | null
  created_at: string | null
  updated_at: string | null
  /** 目录接口附加: 最新发布 revision 精确视图。 */
  revision: TemplateRevision | null
  /** 展示名(模板 ID 派生; 后端无独立 names 字段)。 */
  name: string
  /** 兼容字段(模板详情文档含 names; 目录项为空)。 */
  names: Record<string, string>
  schema_version: string
  /** 内容摘要(有 revision 时 = revision 摘要, 否则 = 草稿摘要)。 */
  content_sha256: string
  has_inputs: boolean
}

/** 精确发布 revision(不可变; 模板 ID + revision + schema_version + 摘要固定内容)。 */
export interface TemplateRevision {
  id: string
  revision: number
  schema_version: string
  content_sha256: string
  inputs_sha256: string | null
  input_count: number
  yaml_object_id: string
  receipt_object_id: string
  summary_object_id: string
  published_by: string
  published_at: string | null
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

/** 模板文档(列表项 + 已解析的 inputs 树 + 原始文档 + 聚合诊断)。 */
export interface TemplateDocument {
  summary: TemplateSummary
  /** 顶层 inputs 树根列表(表单生成输入)。 */
  inputs: InputNode[]
  /** 原始 2.0.0 文档(供表单提交/回显)。 */
  raw: Record<string, unknown>
}

/** 模板详情(与后端 {template, document, diagnostics} 对应)。 */
export interface TemplateDetail {
  summary: TemplateSummary
  /** 完整 2.0.0 文档(含顶层 inputs 树); 无草稿时为 null。 */
  document: Record<string, unknown> | null
  /** 草稿最近一次校验的聚合诊断。 */
  diagnostics: ModelDiagnostic[]
  inputs: InputNode[]
  raw: Record<string, unknown>
}

// ---------------------------------------------------------------------------
// 候选模型与保存结果
// ---------------------------------------------------------------------------

/** 配套数据文件引用(data_ref → 临时隔离区文件; 由后端在完整校验阶段绑定)。 */
export interface DataFileRef {
  data_ref: string
  upload_id: string
  object_id: string
  sha256: string
}

/** 提交候选所需的前端领域对象(由页面/hook 组装)。 */
export interface CandidateModel {
  source: ModelSource
  /** source=template 时必填。 */
  template_id: string | null
  /** source=template: 精确发布 revision(固定不可变)。 */
  template_revision: number | null
  /** source=template: 精确 revision 的内容摘要。 */
  template_sha256: string | null
  /** source=template: 表单 JSON inputs 树。 */
  inputs_json: unknown | null
  /** source=yaml: 候选 YAML 文本。 */
  content_yaml: string | null
  /** 项目草稿修订(乐观锁)。 */
  project_revision: number
  /** 幂等键。 */
  idempotency_key: string
  /** 配套数据文件引用(data_ref → 临时对象 + 摘要)。 */
  data_files: DataFileRef[]
}

/** 保存成功后的正式模型信息(以后端返回为权威)。 */
export interface SavedModelInfo {
  /** 后端分配的项目内最终 ID(如 acme.device.heat_pump_1)。 */
  model_id: string
  device_id: string
  /** 项目内编号(_N)。 */
  suffix: number
  base_device_id: string
  schema_version: string
  /** 规范 YAML(正式保存响应不直接携带, 由模型对象读取; 保持空)。 */
  canonical_yaml: string
  content_sha256: string
  summary: { property_count: number; interface_count: number; relation_count: number }
  /** 保存后的项目草稿修订。 */
  project_revision: number
  /** 后端来源值(direct_yaml | template)。 */
  source: 'direct_yaml' | 'template'
  /** 模板溯源(模板来源时非空)。 */
  template_id: string | null
  template_revision: number | null
  /** 幂等重放标志(重试返回同一逻辑结果)。 */
  duplicate: boolean
}

/** 项目模型清单行(编号对用户可见)。 */
export interface ProjectModelSummary {
  id: string
  project_id: string
  device_id: string
  base_device_id: string
  suffix: number
  revision: number
  project_revision: number
  content_sha256: string
  model_object_id: string
  receipt_object_id: string
  /** 后端来源值(direct_yaml | template)。 */
  source: 'direct_yaml' | 'template'
  template_id: string | null
  template_revision: number | null
  template_sha256: string | null
  inputs_sha256: string | null
  created_by: string
  created_at: string | null
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
