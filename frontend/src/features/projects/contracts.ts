/**
 * 项目 feature 前后端契约(与后端 api/projects.py + application/projects 同形, snake_case)。
 *
 * 端点(与后端 /api 路由一致):
 *   GET    /api/projects                          项目列表 {projects: [...]}
 *   POST   /api/projects                          创建项目 {project, my_role}
 *   GET    /api/projects/{pid}                    项目视图 {project, draft, versions, my_role}
 *   PUT    /api/projects/{pid}/draft              草稿语义命令 {expected_revision, commands} → {revision}
 *   POST   /api/projects/{id}/archive             归档 {project, my_role}
 *   POST   /api/projects/{id}/unarchive           撤销归档 {project, my_role}
 *   DELETE /api/projects/{id}                     删除(须 name 精确匹配)
 *
 * 版本/导入/应用方案端点归 project-versions / project-import feature 所有, 见各自契约。
 * 草稿命令 DTO/ID 构造与 PUT 传输是项目业务协议, 由本 feature 公开;
 * modeling/dataset/task 各自准备业务 payload, 共用同一传输, 不复制。
 */

import type { Currency, DeviceKind, ProjectRole, ProjectStatus } from '../../types'

// ---------------------------------------------------------------------------
// 项目主体(project_to_dict + 行内 my_role)
// ---------------------------------------------------------------------------

/** 项目基线(与 project_to_dict.project_baseline 同形)。 */
export interface ProjectBaselineDto {
  resolution: string
  start_year: number
  scenario_mode: string
}

/** 项目主体(与 project_to_dict 键一一对应; 无 draft 键, 草稿见视图)。 */
export interface ProjectDto {
  id: number
  name: string
  description: string | null
  status: ProjectStatus
  owner_id: number
  currency: Currency
  project_baseline: ProjectBaselineDto
  schema_version: number
  current_draft_id: number | null
  current_version_id: number | null
  created_at: string
  updated_at: string | null
  created_by: number
}

/** 列表项(主体 + 行内 my_role; 映射提升为 role)。 */
export interface ProjectListItemDto extends ProjectDto {
  my_role: unknown
}

/** 列表响应: 精确 {projects: [...]} 信封。 */
export interface ProjectListResponseDto {
  projects: ProjectListItemDto[]
}

/** 创建请求(与后端 ProjectCreateRequest 同形)。 */
export interface ProjectCreateRequestDto {
  name: string
  description: string | null
  currency: string
  baseline_resolution: string
  baseline_start_year: number
  baseline_scenario_mode: string
}

/** 创建/归档响应: {project, my_role}。 */
export interface ProjectMutationResponseDto {
  project: ProjectDto
  my_role: unknown
}

export type DriverTargetDto =
  | { property: string }
  | { interface: string; aggregation: 'integrate_positive' | 'sum_positive' }

export type CostBindingDto =
  | { type: 'none' }
  | { type: 'costed'; finance_type: string; drivers: Record<string, DriverTargetDto> }

export interface TariffBindingDto {
  price: string
  instance: string
  interface: string
  aggregation: 'integrate_positive' | 'sum_positive'
}

/** Explicit natural-month demand meter; independent of energy tariffs. */
export interface DemandBindingDto {
  charge: string
  instance: string
  interface: string
}

// ---------------------------------------------------------------------------
// 项目视图(含草稿摘要与内容)
// ---------------------------------------------------------------------------

/** 草稿数据集绑定行(权威位置: 视图 draft.content.dataset_bindings)。 */
export interface DraftBindingDto {
  dataset_version_id: number
  dataset_id?: number | null
  role?: string | null
  note?: string | null
}

/** 草稿内容模型设备条目。 */
export interface DraftContentDeviceDto {
  name: string
  ref: string
  display_name: string
  device_type: string
  kind: DeviceKind
  params: Record<string, unknown>
}

/** 草稿内容模型连接条目。 */
export interface DraftContentConnectionDto {
  name: string
  from_device: string
  from_port: string
  to_device: string
  to_port: string
}

/** 草稿内容 model 节。 */
export interface DraftContentModelDto {
  devices: DraftContentDeviceDto[]
  connections: DraftContentConnectionDto[]
}

/** 草稿内容文档(初始骨架见 backend project/content.py: schema_version, model, calc_config 等)。 */
export interface DraftContentDto {
  model: DraftContentModelDto
  /** dataset.bind / dataset.unbind 写入的权威绑定列表。 */
  dataset_bindings?: DraftBindingDto[] | null
  /** assembly.set_finance_binding: omitted until explicitly saved. */
  finance_binding?: { instances: Record<string, CostBindingDto> }
  /** assembly.set_tariff_bindings: omitted until explicitly saved. */
  tariff_bindings?: Record<string, TariffBindingDto>
  /** assembly.set_demand_bindings: empty object means no demand meters. */
  demand_bindings?: Record<string, DemandBindingDto>
  task_working_inputs?: Record<string, unknown> | null
  [key: string]: unknown
}

/** 视图草稿(draft_to_dict + 内容文档)。 */
export interface DraftViewDto {
  id: number
  project_id: number
  revision: number
  content_object_id: number
  is_current: boolean
  updated_by: number
  updated_at: string
  created_at: string
  content: DraftContentDto
}

/** 项目视图响应: 精确 {project, draft, versions, my_role}(versions 由版本特性消费, 此处不消费)。 */
export interface ProjectViewResponseDto {
  project: ProjectDto
  draft: DraftViewDto
  my_role: unknown
}

// ---------------------------------------------------------------------------
// 草稿语义命令(项目业务协议, 各家族共用传输)
// ---------------------------------------------------------------------------

/** 草稿命令(与后端 update_draft 命令形态同形: id/project_id/expected_revision/session/unit/type/payload)。 */
export interface DraftCommandDto {
  id: string
  project_id: number
  expected_revision: number
  session: string
  unit: string
  type: string
  payload: Record<string, unknown>
}

/** 草稿命令调用方载荷(业务 type + payload; id/unit 由传输层构造)。 */
export interface DraftCommandInput {
  type: string
  payload: Record<string, unknown>
}

/** PUT draft 请求: 精确 {expected_revision, commands}。 */
export interface DraftUpdateRequestDto {
  expected_revision: number
  commands: DraftCommandDto[]
}

/** PUT draft 响应: 精确 {revision}(后端恒返新修订号, 缺席即抛错, 不回显期望值)。 */
export interface DraftUpdateResponseDto {
  revision: number
}

/** 角色提升(列表/视图行内 my_role → role; 非 owner 字符串原样保留由调用方判定)。 */
export function roleOf(myRole: unknown): ProjectRole | undefined {
  return (myRole as ProjectRole) ?? undefined
}
