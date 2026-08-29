/**
 * 建模 feature 前后端契约(与后端 HTTP JSON 字段一一对应, snake_case)。
 *
 * 模板与模型统一使用 `ies.device-model` 2.0.0(见
 * manual/developer-guide/zh-CN/formats/device-model-yaml.md);模板额外含顶层
 * `inputs`(同构树形结构, 叶子节点是
 * {type: number|boolean|string|data_repeat|data_predict, unit?, valid_range?, default?})。
 *
 * 端点(与后端 /api 路由一致):
 *   GET  /api/model-templates/catalog                    可用模板目录 {items: [...]}
 *   GET  /api/model-templates/{template_id}              模板详情 {template, document, diagnostics}
 *   GET  /api/projects/{pid}/models                      项目模型清单 {project_models: [...]}
 *   POST /api/projects/{pid}/models/validate             候选模型门禁 {valid, diagnostics}
 *   POST /api/projects/{pid}/models/temp-files           配套数据文件临时上传(multipart)
 *   POST /api/projects/{pid}/models                      正式保存 {project_model, receipt, project_revision}
 *
 * 候选保存判别字段 source:
 *   - source=template: template_id + template_revision + template_sha256 + inputs;
 *     后端读取权威模板内容并实例化(不信任客户端自带的模板字节);
 *   - source=yaml: content(候选 YAML 文本)由后端直接解析校验。
 * 两条路径汇合为同一个后端用例。
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

/** 模板生命周期状态(draft 未发布 / published 已发布且启用 / disabled 已停用)。 */
export type TemplateStatusDto = 'draft' | 'published' | 'disabled'

/** 模板列表项(列表信封 {templates: [...]} / 目录信封 {items: [...]})。 */
export interface TemplateSummaryDto {
  /** 主表行 id(不透明十进制字符串)。 */
  id: string
  /** 稳定模板 ID(命名空间字符串, 非项目内编号)。 */
  template_id: string
  /** 生命周期状态。 */
  status: TemplateStatusDto
  description: string | null
  /** 草稿乐观锁修订。 */
  draft_revision: number
  /** 草稿规范字节 SHA-256(未发布草稿时为空)。 */
  draft_sha256: string | null
  draft_has_inputs: boolean | null
  /** 最新已发布 revision(0 = 尚未发布)。 */
  published_revision: number
  published_at: string | null
  created_at: string | null
  updated_at: string | null
  /** 目录接口(已发布且启用)附加: 最新发布 revision 精确视图。 */
  revision?: TemplateRevisionDto | null
}

/** 精确发布 revision(不可变; 模板 ID + revision + schema_version + 摘要固定内容)。 */
export interface TemplateRevisionDto {
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

/** 模板文档原始 JSON(ies.device-model 2.0.0 文档, 含顶层 inputs 树)。 */
export type DeviceModelRawDto = Record<string, unknown>

/** 模板详情(单资源信封 {template, document, diagnostics})。 */
export interface TemplateDetailDto {
  template: TemplateSummaryDto
  /** 完整 2.0.0 文档(含顶层 inputs 树, 供递归表单生成)。 */
  document: DeviceModelRawDto | null
  /** 草稿最近一次校验的聚合诊断。 */
  diagnostics: ModelDiagnosticDto[]
}

/** 精确 revision 详情({template, revision, document, receipt, summary, diagnostics})。 */
export interface TemplateRevisionDetailDto {
  template: TemplateSummaryDto
  revision: TemplateRevisionDto
  document: DeviceModelRawDto
  receipt: Record<string, unknown>
  summary: { property_count: number; interface_count: number; equation_count: number }
  diagnostics: ModelDiagnosticDto[]
}

// ---------------------------------------------------------------------------
// 候选模型(校验 + 保存)
// ---------------------------------------------------------------------------

/** 候选来源: 模板实例化 / 直接 YAML 编辑(两者汇合为同一保存用例)。 */
export type CandidateSource = 'template' | 'yaml'

/** 配套数据文件引用(已上传的临时对象 + 声明摘要)。 */
export interface DataFileRefDto {
  data_ref: string
  upload_id: string
  object_id: string
  sha256: string
}

/**
 * 候选校验/保存请求(判别字段 source):
 * - source=template: template_id + template_revision + template_sha256 + inputs;
 *   后端读取权威模板内容并实例化;
 * - source=yaml: content(候选 YAML 文本)由后端直接解析校验。
 */
export interface CandidateSaveRequestDto {
  source: CandidateSource
  /** source=yaml: 候选 YAML 文本。 */
  content: string | null
  /** source=template: 稳定模板 ID(不透明字符串)。 */
  template_id: string | null
  /** source=template: 精确发布 revision(固定不可变)。 */
  template_revision: number | null
  /** source=template: 精确 revision 的内容摘要(与后端权威内容二次确认)。 */
  template_sha256: string | null
  /** source=template: 表单 JSON inputs 树(只含模板已声明路径)。 */
  inputs: unknown | null
  /** 项目草稿修订(乐观锁, 并发编辑不得静默覆盖)。 */
  project_revision: number
  /** 幂等键(可重试写操作, 宪法 §8.4)。 */
  idempotency_key: string
  /** 配套数据文件引用(data_ref → 临时隔离区文件)。 */
  data_files: DataFileRefDto[]
}

/** 临时数据文件上传结果(临时隔离区, 不等于模型已保存)。 */
export interface TempFileUploadResultDto {
  temp_file: {
    object_id: string
    oid: string
    sha256: string
    size_bytes: number
    media_type: string
    status: string
  }
  upload_id: string
}

/** 项目模型清单行(正式保存后返回; 编号对用户可见)。 */
export interface ProjectModelDto {
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
  source: 'direct_yaml' | 'template'
  template_id: string | null
  template_revision: number | null
  template_sha256: string | null
  inputs_sha256: string | null
  created_by: string
  created_at: string | null
}

/** 保存成功返回的最终模型(权威: 最终 _N ID、规范 YAML、摘要、项目 revision)。 */
export interface ModelSummaryDto {
  model_id: string
  device_id: string
  schema_version: string
  /** 规范 YAML 文本。 */
  canonical_yaml: string
  /** 规范内容 SHA-256(小写 64 位十六进制)。 */
  content_sha256: string
  summary: { property_count: number; interface_count: number; relation_count: number }
  /** 项目草稿修订(保存后更新)。 */
  project_revision: number
}

/** 候选保存成功响应(单资源信封 {project_model, receipt, project_revision})。 */
export interface CandidateSaveResultDto {
  project_model: ProjectModelDto
  receipt: Record<string, unknown>
  project_revision: number
  /** 幂等重放标志(重试返回同一逻辑结果)。 */
  duplicate: boolean
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
