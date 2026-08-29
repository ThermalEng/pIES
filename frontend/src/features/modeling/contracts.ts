/**
 * 建模 feature 前后端契约(与后端 HTTP JSON 字段一一对应, snake_case)。
 *
 * 模板与模型统一使用 `ies.device-model` 2.0.0(见
 * manual/developer-guide/zh-CN/formats/device-model-yaml.md);模板额外含顶层
 * `inputs`(同构树形结构, 叶子节点是
 * {type: number|boolean|string|data_repeat|data_predict, unit?, valid_range?, default?})。
 *
 * 候选校验/保存端点由阶段 2 worktree C 开发中(尚未合并), 本文件与 api.ts 按
 * C 的契约草案编写, 联调说明见本目录 README.md「待 C 合并后联调」节。C 合并后
 * 如有字段差异, 只允许调整本文件与 api.ts, 页面与 mapper 保持依赖前端领域模型。
 *
 * 依赖方向: features/modeling → ../../types(共享错误类型)/../../api/client(HTTP 层)。
 */

// ---------------------------------------------------------------------------
// 模板 inputs(与后端 TemplateInputSpec / device-model-2.0.0.schema.json 对应)
// ---------------------------------------------------------------------------

/** 模板 inputs 叶子类型: 标量 + 预定义数据。 */
export type ModelInputLeafType = 'number' | 'boolean' | 'string' | 'data_repeat' | 'data_predict'

/** 模板 inputs 节点类型(叶子 + 结构容器)。 */
export type ModelInputNodeType = ModelInputLeafType | 'object' | 'array'

/** 闭区间有效范围; null 表示该侧无有限边界(宪法 §7.4 / 格式标准)。 */
export interface ValidRangeDto {
  minimum: number | null
  maximum: number | null
}

/** 模板 inputs 叶子声明(与后端 TemplateInputSpec 字段一一对应)。 */
export interface TemplateInputSpecDto {
  /** 点分路径, 如 "properties.peak_power_kw.value"; array 子项以 "[]" 分段。 */
  path: string
  type: ModelInputNodeType
  unit?: string | null
  valid_range?: ValidRangeDto | null
  default?: number | boolean | string | null
  /** data_repeat/data_predict 绑定的数据引用(由模板声明, 用户上传临时文件为其提供内容)。 */
  data_ref?: string | null
  /** object(fields)/array(items) 子声明。 */
  children?: TemplateInputSpecDto[]
}

// ---------------------------------------------------------------------------
// 模板(模型库)
// ---------------------------------------------------------------------------

/** 模板列表项(列表信封 {items: [...]})。 */
export interface TemplateSummaryDto {
  /** 稳定模板 ID(命名空间字符串, 非项目内编号)。 */
  template_id: string
  /** 本地化显示名: {locale: 名称}。 */
  names: Record<string, string>
  schema_version: string
  description?: string | null
  /** 模板规范字节 SHA-256(小写 64 位十六进制)。 */
  content_sha256: string
  /** 模板发布修订。 */
  revision: number
  /** 是否声明了顶层 inputs(决定是否生成输入表单)。 */
  has_inputs: boolean
}

/** 模板文档原始 JSON(ies.device-model 2.0.0 文档, 含顶层 inputs 树)。 */
export type DeviceModelRawDto = Record<string, unknown>

/** 模板详情(单资源信封 {template, document})。 */
export interface TemplateDetailDto {
  template: TemplateSummaryDto
  /** 完整 2.0.0 文档(含顶层 inputs 树, 供递归表单生成)。 */
  document: DeviceModelRawDto
}

// ---------------------------------------------------------------------------
// 候选模型(校验 + 保存; 契约草案, 待 C 合并后联调)
// ---------------------------------------------------------------------------

/** 候选来源: 模板实例化 / 直接 YAML 编辑(两者汇合为同一保存用例)。 */
export type CandidateSource = 'template' | 'yaml'

/**
 * 候选保存请求(判别字段 source):
 * - source=template: template_id + inputs(表单 JSON 树, 与模板 inputs 声明同构);
 *   由后端模板实例化器合并并重新完整校验;
 * - source=yaml: content(候选 YAML 文本)由后端直接解析校验。
 * 两条路径汇合为同一个后端用例: 候选校验 → 失败聚合诊断 / 成功分配 _N 编号并保存。
 */
export interface CandidateSaveRequestDto {
  source: CandidateSource
  /** source=template 必填。 */
  template_id: string | null
  /** source=template: 表单 JSON inputs 树(只含模板已声明路径)。 */
  inputs: unknown | null
  /** source=yaml: 候选 YAML 文本。 */
  content: string | null
  /** 项目草稿修订(乐观锁, 并发编辑不得静默覆盖)。 */
  project_revision: number
  /** 幂等键(可重试写操作, 宪法 §8.4)。 */
  idempotency_key: string
  /** 临时数据文件引用: [{path, temp_file_ref}], path 为 data 叶子路径。 */
  temp_file_refs: TempFileRefDto[]
}

/** 临时数据文件引用(path → 后端临时隔离区文件引用)。 */
export interface TempFileRefDto {
  path: string
  temp_file_ref: string
}

/** 临时数据文件上传结果(临时隔离区, 不等于模型已保存)。 */
export interface TempFileUploadResultDto {
  temp_file_ref: string
  file_name: string
}

/** 模型摘要计数(回执结构摘要)。 */
export interface ModelSummaryCountsDto {
  property_count: number
  interface_count: number
  relation_count: number
}

/** 保存成功返回的最终模型(权威: 最终 _N ID、规范 YAML、摘要、项目 revision)。 */
export interface ModelSummaryDto {
  /** 最终项目内 ID(如 acme.device.heat_pump_1; 由后端分配, 前端不预分配)。 */
  model_id: string
  device_id: string
  schema_version: string
  /** 规范 YAML 文本。 */
  canonical_yaml: string
  /** 规范内容 SHA-256(小写 64 位十六进制)。 */
  content_sha256: string
  summary: ModelSummaryCountsDto
  /** 项目草稿修订(保存后更新)。 */
  project_revision: number
}

/** 候选保存成功响应(单资源信封 {model, project_revision})。 */
export interface CandidateSaveResultDto {
  model: ModelSummaryDto
  project_revision: number
}

// ---------------------------------------------------------------------------
// 模型诊断(parser2 聚合诊断: message_key/字段路径/YAML 行列/expected/actual)
// ---------------------------------------------------------------------------

/** 后端设备模型诊断定位(parser2 location: {object_type, field, line, column})。 */
export interface ModelDiagnosticLocationDto {
  object_type: string
  field?: string | null
  line?: number | null
  column?: number | null
}

/** 设备模型诊断(与后端 Diagnostic + params{file, detail, expected, actual} 对应)。 */
export interface ModelDiagnosticDto {
  code: string
  message_key: string
  severity: 'blocking' | 'error' | 'warning' | 'info'
  blocking: boolean
  params: {
    file?: string
    detail?: string
    expected?: unknown
    actual?: unknown
    [key: string]: unknown
  }
  location: ModelDiagnosticLocationDto | null
  fix_hint_key: string | null
  ref_ids: string[]
}

/** 候选保存失败: 400 错误信封 params.diagnostics 中的聚合诊断条目。 */
export type CandidateDiagnosticDto = ModelDiagnosticDto
