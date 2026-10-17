/**
 * 项目版本 feature 前后端契约(与后端 HTTP JSON 字段一一对应, snake_case)。
 *
 * 端点(与后端 /api 路由一致):
 *   POST /api/projects/{pid}/versions          从当前草稿创建版本 → {version: {...}}
 *   GET  /api/projects/{pid}/versions          版本列表(新版本在前) → {versions: [...]}
 *   GET  /api/projects/{pid}/versions/{vid}    版本详情(仅版本元数据) → {version: {...}}
 *   POST /api/projects/{pid}/versions/{vid}/restore 恢复版本(新版本+新草稿) → {version, draft}
 *   POST /api/projects/{pid}/apply-result      应用方案(来源版本派生新版本) → {version, draft}
 *
 * 创建原因固定 `manual_save`: 本切片不提供可伪装 restore/apply_result 的
 * 任意原因选择; 快照自动固化(snapshot_freeze)由后端任务链写入, 前端不发送。
 * 详情端点只返回版本元数据, 不读取完整模型内容, 详情页不得声称展示完整模型。
 * 应用方案只发送任务引用、解标识与确认时草稿修订; 参数与来源基线由服务端
 * 权威解析, 不发送补丁。修订不符服务端 409, 不创建版本。
 *
 * 版本主体类型以共享 ProjectVersion 为唯一实际类型; 响应信封在此声明,
 * 经 mappers 严格映射, 不经聚合器转发。
 */

/** 创建版本请求(与后端 VersionCreateRequest 对应)。 */
export interface VersionCreateRequestDto {
  /** 版本名称(后端 min_length=1, max_length=200; 空串由后端拒收)。 */
  name: string
  /** 版本描述(可空, 后端 max_length=2000)。 */
  description: string | null
  /** 创建原因: 本切片恒为 manual_save。 */
  reason: 'manual_save'
}

/** 版本基线(与 version_to_dict.project_baseline 同形)。 */
export interface VersionBaselineDto {
  resolution: string
  start_year: number
  scenario_mode: string
}

/** 版本主体(与 version_to_dict 键一一对应)。 */
export interface ProjectVersionDto {
  id: number
  project_id: number
  version_no: number
  name: string
  description: string | null
  created_by: number
  created_at: string
  parent_version_id: number | null
  source_draft_id: number | null
  source_draft_revision: number | null
  reason: string
  project_baseline: VersionBaselineDto
  currency: string
  schema_version: number
  content_object_id: number
}

/** 版本列表响应: 精确 {versions: [...]}。 */
export interface VersionListResponseDto {
  versions: ProjectVersionDto[]
}

/** 单版本响应: 精确 {version: {...}}(创建/详情共用)。 */
export interface VersionResponseDto {
  version: ProjectVersionDto
}

/** 恢复/应用方案响应的草稿指针(仅消费刷新所需的 id/revision)。 */
export interface VersionDraftPointerDto {
  id: number
  revision: number
}

/** 恢复/应用方案响应: 精确 {version, draft}。 */
export interface VersionLineageResponseDto {
  version: ProjectVersionDto
  draft: VersionDraftPointerDto
}

/** 应用方案请求(只发送引用与确认时修订, 与后端 ApplyResultRequest 子集同形)。 */
export interface ApplySchemeRequestDto {
  task_id: number
  solution_id: number
  expected_revision: number
}
