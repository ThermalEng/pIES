/**
 * 现有业务 API 的组合入口；各业务族逐步迁入 feature。
 * 通用传输、会话和下载辅助由 shared/http 拥有。
 * 结果/导出直接组合 feature 公开入口和 application 用例；
 * 尚未迁移的业务适配辅助不能作为新 feature 的依赖。
 */

import type {
  ApiErrorBody,
  CalcConfig,
  CalcConfigInput,
  CalculationConfigBody,
  Connection,
  ConnectionInput,
  Currency,
  Dataset,
  DatasetUploadInput,
  DatasetVersion,
  Device,
  ApplySchemeInput,
  ApplySchemeResult,
  DeviceInput,
  DeviceKind,
  DevicePortSpec,
  DeviceTypeSpec,
  Diagnostic,
  ExcelExportInput,
  DraftModelSection,
  ImportConfirmResult,
  ImportProposal,
  PackageExportInput,
  PageResult,
  Project,
  ProjectCreateInput,
  ProjectListParams,
  ProjectRole,
  ProjectVersion,
  ProjectVersionCreateInput,
  VersionRestoreResult,
  Severity,
  Task,
  TaskAttempt,
  TaskBatch,
  TaskCreateInput,
  TaskDetail,
  TaskDraftLoadResult,
  TaskInputs,
  TaskLease,
  TaskListParams,
  TaskOutcome,
  TaskStatus,
  TaskType,
  ValidationResult,
} from '../types'
import { ApiError } from '../types'
import type {
  DatasetDetailResponseDto,
  DatasetsListResponseDto,
  DatasetUploadResponseDto,
} from '../features/datasets/contracts'
import { datasetVersionFromServer } from '../features/datasets/mappers'
import {
  getResult,
  listAssessments,
  assess as assessResult,
  select as selectResult,
  readHourly,
} from '../features/results'
import {
  exportPackage,
  downloadExport,
} from '../features/exports'
import { exportTaskReport } from '../application/exportTaskReport'

import { request, requestBlob } from '../shared/http'

// ---------------------------------------------------------------------------
// 契约适配辅助(后端形状 → 前端类型)
// ---------------------------------------------------------------------------

/** 对象化(非对象输入返回空对象, 避免解构崩溃)。 */
function asRecord(body: unknown): Record<string, unknown> {
  return body !== null && typeof body === 'object' && !Array.isArray(body)
    ? (body as Record<string, unknown>)
    : {}
}

/** 列表信封适配:兼容 {items, next_cursor} 与 {key: [...]} 与裸数组(PageResult 统一)。 */
function asItems<T>(body: unknown, key: string, limit?: number): PageResult<T> {
  if (Array.isArray(body)) return { items: body as T[], next_cursor: null, limit: limit ?? (body as T[]).length }
  const rec = asRecord(body)
  let items: T[] | null = null
  if (Array.isArray(rec.items)) items = rec.items as T[]
  else if (Array.isArray(rec[key])) items = rec[key] as T[]
  if (items === null) items = []
  const rawNext = rec.next_cursor
  const next = rawNext === null || rawNext === undefined ? null : String(rawNext)
  const lmt = typeof rec.limit === 'number' ? (rec.limit as number) : items.length
  return { items, next_cursor: next, limit: limit ?? lmt }
}

/** 列表字段适配:{items} / {key} / 裸数组 → T[]。 */
function asList<T>(body: unknown, key: string): T[] {
  if (Array.isArray(body)) return body as T[]
  const rec = asRecord(body)
  if (Array.isArray(rec.items)) return rec.items as T[]
  if (Array.isArray(rec[key])) return rec[key] as T[]
  return []
}

/** 单资源包裹适配:{key: {...}} → 内部对象(无包裹时原样返回)。 */
function oneOf<T>(body: unknown, key: string): T {
  const rec = asRecord(body)
  if (key in rec) return rec[key] as T
  return body as T
}

/**
 * 后端项目包裹 {project, draft, versions, my_role} → 前端 Project。
 * 草稿摘要只透出修订号与数据集绑定清单(绑定状态以草稿为权威, 数据页真实徽章来源);
 * create/archive 等端点只返回 {project, my_role}, 不虚构 draft。
 */
function projectFromServer(body: unknown): Project {
  const rec = asRecord(body)
  const p = asRecord(rec.project)
  const out = { ...(p as unknown as Project), role: (rec.my_role as ProjectRole) ?? undefined }
  if (rec.draft !== null && typeof rec.draft === 'object') {
    const draft = asRecord(rec.draft)
    const bindings = Array.isArray(draft.dataset_bindings)
      ? (draft.dataset_bindings as NonNullable<Project['draft']>['dataset_bindings'])
      : undefined
    // 任务提交工作输入只读草稿内容文档的 task_working_inputs 单键(纯对象才
    // 携带; 缺席/形态不符为 undefined, 对话框取默认空态, 不猜测)。
    const content =
      draft.content !== null && typeof draft.content === 'object' && !Array.isArray(draft.content)
        ? (draft.content as Record<string, unknown>)
        : null
    const workingRaw = content?.task_working_inputs
    const working =
      workingRaw !== null && typeof workingRaw === 'object' && !Array.isArray(workingRaw)
        ? (workingRaw as Record<string, unknown>)
        : undefined
    out.draft = {
      id: Number(draft.id ?? 0),
      project_id: Number(draft.project_id ?? 0),
      revision: Number(draft.revision ?? 1),
      content_object_id: Number(draft.content_object_id ?? 0),
      is_current: draft.is_current !== false,
      updated_by: Number(draft.updated_by ?? 0),
      updated_at: String(draft.updated_at ?? ''),
      created_at: String(draft.created_at ?? ''),
      dataset_bindings: bindings,
      task_working_inputs: working,
    }
  }
  return out
}

/**
 * 后端版本 dict(version_to_dict) → 前端 ProjectVersion(严格解析)。
 *
 * 必填缺失或类型不符直接抛错, 不补 0/''/null 伪造版本(尤其 version_no /
 * created_by / created_at / source_draft_revision 不可伪造); 可空字段仅
 * 接受 null/undefined → null。详情端点只返回版本元数据, 此处不读取模型内容。
 */
/** 导入提案解析(与后端 _proposal_to_dict 一一对应; project_id 可缺席为 null)。 */
function importProposalFromServer(v: unknown): ImportProposal {
  if (v === null || typeof v !== 'object' || Array.isArray(v)) {
    throw new Error('invalid import proposal envelope')
  }
  const r = v as Record<string, unknown>
  if (typeof r.id !== 'number' || !Number.isFinite(r.id)) throw new Error('invalid import proposal field: id')
  if (r.project_id !== null && (typeof r.project_id !== 'number' || !Number.isFinite(r.project_id))) {
    throw new Error('invalid import proposal field: project_id')
  }
  if (typeof r.proposer_id !== 'number' || typeof r.status !== 'string') {
    throw new Error('invalid import proposal field')
  }
  const asNullableRecord = (key: string): Record<string, unknown> | null => {
    const val = r[key]
    if (val === null || val === undefined) return null
    if (typeof val !== 'object' || Array.isArray(val)) throw new Error(`invalid import proposal field: ${key}`)
    return val as Record<string, unknown>
  }
  return {
    id: r.id,
    project_id: r.project_id,
    proposer_id: r.proposer_id,
    status: r.status,
    source_object_id: typeof r.source_object_id === 'number' ? r.source_object_id : null,
    review_summary: asNullableRecord('review_summary'),
    review_errors: asNullableRecord('review_errors'),
    created_at: typeof r.created_at === 'string' ? r.created_at : null,
  }
}

function projectVersionFromServer(v: unknown): ProjectVersion {
  if (v === null || typeof v !== 'object' || Array.isArray(v)) {
    throw new Error('invalid project version envelope')
  }
  const r = v as Record<string, unknown>
  const num = (key: string): number => {
    const val = r[key]
    if (typeof val !== 'number' || !Number.isFinite(val)) throw new Error(`invalid project version field: ${key}`)
    return val
  }
  const nullableNum = (key: string): number | null => {
    const val = r[key]
    if (val === null || val === undefined) return null
    if (typeof val !== 'number' || !Number.isFinite(val)) throw new Error(`invalid project version field: ${key}`)
    return val
  }
  const str = (key: string): string => {
    const val = r[key]
    if (typeof val !== 'string') throw new Error(`invalid project version field: ${key}`)
    return val
  }
  const nullableStr = (key: string): string | null => {
    const val = r[key]
    if (val === null || val === undefined) return null
    if (typeof val !== 'string') throw new Error(`invalid project version field: ${key}`)
    return val
  }
  const baseline = r.project_baseline
  if (baseline === null || typeof baseline !== 'object' || Array.isArray(baseline)) {
    throw new Error('invalid project version field: project_baseline')
  }
  const b = baseline as Record<string, unknown>
  if (typeof b.resolution !== 'string' || typeof b.start_year !== 'number' || typeof b.scenario_mode !== 'string') {
    throw new Error('invalid project version field: project_baseline')
  }
  return {
    id: num('id'),
    project_id: num('project_id'),
    version_no: num('version_no'),
    name: str('name'),
    description: nullableStr('description'),
    created_by: num('created_by'),
    created_at: str('created_at'),
    parent_version_id: nullableNum('parent_version_id'),
    source_draft_id: nullableNum('source_draft_id'),
    source_draft_revision: nullableNum('source_draft_revision'),
    reason: str('reason'),
    project_baseline: {
      resolution: b.resolution,
      start_year: b.start_year,
      scenario_mode: b.scenario_mode,
    },
    currency: r.currency as ProjectVersion['currency'],
    schema_version: num('schema_version'),
    content_object_id: num('content_object_id'),
  }
}

/** 后端任务摘要(task_summary) → 前端 Task。 */
function taskFromSummary(s: Record<string, unknown>, projectId: number): Task {
  return {
    id: Number(s.id),
    project_id: Number(s.project_id ?? projectId),
    type: s.type as TaskType,
    status: s.status as TaskStatus,
    business_outcome: (s.business_outcome as TaskOutcome) ?? null,
    idempotency_key: String(s.idempotency_key ?? ''),
    calc_snapshot_id: s.calc_snapshot_id === null || s.calc_snapshot_id === undefined ? null : Number(s.calc_snapshot_id),
    requested_by: Number(s.requested_by ?? 0),
    requested_at: String(s.requested_at ?? ''),
    priority: Number(s.priority ?? 0),
    deadline: s.deadline === null || s.deadline === undefined ? null : String(s.deadline),
    superseded_by_task_id:
      s.superseded_by_task_id === null || s.superseded_by_task_id === undefined ? null : Number(s.superseded_by_task_id),
    attempt_count: Number(s.attempt_count ?? 0),
    max_attempts: Number(s.max_attempts ?? 1),
    created_at: String(s.created_at ?? ''),
    updated_at: s.updated_at === null || s.updated_at === undefined ? null : String(s.updated_at),
    result_available: s.result_available === true,
    completed_at:
      typeof s.completed_at === 'string' && s.completed_at !== '' ? (s.completed_at as string) : null,
    latest_package_id:
      typeof s.latest_package_id === 'number' && Number.isFinite(s.latest_package_id)
        ? (s.latest_package_id as number)
        : null,
    latest_evidence_status:
      typeof s.latest_evidence_status === 'string' ? (s.latest_evidence_status as string) : null,
    summary: (s.summary as Task['summary']) ?? undefined,
  }
}

/** 后端任务诊断(level/code/message/context) → 前端 Diagnostic(文案键 ies.diag.raw)。 */
function taskDiagFromServer(d: Record<string, unknown>): Diagnostic {
  const level = String(d.level ?? 'error')
  const severity: Severity =
    level === 'blocking' ? 'blocking' : level === 'warning' ? 'warning' : level === 'info' ? 'info' : 'error'
  return {
    code: String(d.code ?? 'TASK-DIAG'),
    message_key: 'ies.diag.raw',
    params: { message: String(d.message ?? '') },
    severity,
    blocking: level === 'blocking' || level === 'error',
    location: null,
    fix_hint_key: null,
    ref_ids: [],
    occurred_at:
      d.created_at === null || d.created_at === undefined ? new Date().toISOString() : String(d.created_at),
    source: 'task',
    trace_id: d.trace_id === null || d.trace_id === undefined ? null : String(d.trace_id),
    project_id: null,
    task_id: null,
    suppressed: false,
  }
}

/** 后端任务详情 dict → 前端 TaskDetail。 */
function taskDetailFromServer(d: Record<string, unknown>, projectId: number): TaskDetail {
  const base = taskFromSummary(d, projectId)
  const progress = asRecord(d.progress)
  const attempts: TaskAttempt[] = Array.isArray(d.attempts)
    ? (d.attempts as Record<string, unknown>[]).map((a) => ({
        attempt_no: Number(a.attempt_no ?? 0),
        worker_id: a.worker_id === null || a.worker_id === undefined ? null : String(a.worker_id),
        status: (a.status as TaskAttempt['status']) ?? 'pending',
        stop_reason: a.stop_reason === null || a.stop_reason === undefined ? null : String(a.stop_reason),
        started_at: a.started_at === null || a.started_at === undefined ? null : String(a.started_at),
        finished_at: a.finished_at === null || a.finished_at === undefined ? null : String(a.finished_at),
      }))
    : []
  const batch = d.batch as TaskBatch | null | undefined
  return {
    ...base,
    attempts,
    current_lease: d.current_lease === null || d.current_lease === undefined ? null : (d.current_lease as TaskLease),
    progress:
      d.progress === null || d.progress === undefined
        ? null
        : {
            task_id: base.id,
            status: base.status,
            attempt_no: Number(progress.attempt_no ?? 0),
            percent: typeof progress.percent === 'number' ? (progress.percent as number) : null,
            stage: progress.stage === null || progress.stage === undefined ? null : String(progress.stage),
            detail: asRecord(progress.detail),
            updated_at:
              progress.updated_at === null || progress.updated_at === undefined
                ? ''
                : String(progress.updated_at),
            source: progress.source === 'redis' ? 'redis' : 'pg',
            retry: Boolean(progress.retry ?? false),
          },
    batch: batch ?? null,
    outcome_note: d.outcome_note === null || d.outcome_note === undefined ? null : String(d.outcome_note),
    diagnostics: Array.isArray(d.diagnostics)
      ? (d.diagnostics as Record<string, unknown>[]).map(taskDiagFromServer)
      : [],
    evidence: Array.isArray(d.evidence) ? (d.evidence as TaskDetail['evidence']) : [],
    calc_snapshot: d.calc_snapshot === null || d.calc_snapshot === undefined ? null : (d.calc_snapshot as TaskDetail['calc_snapshot']),
  }
}

/**
 * 后端配置信封 {calculation_config, revision} → 前端 CalcConfig。
 *
 * 可信响应直接类型映射: 后端在用户输入入口已唯一校验全文, 此处不补齐
 * 缺项(不伪造 ''/[]/null/0, 尤其 time_limit_seconds/options), 也不重复
 * 作第二次业务校验; 仅保留 revision 信封判断。缺项原样透出, 由编辑页
 * 显式呈现为空态。
 */
function configFromServer(body: unknown, projectId: number): CalcConfig {
  const env = asRecord(body)
  if (typeof env.revision !== 'number' || !env.calculation_config || typeof env.calculation_config !== 'object') {
    throw new Error('invalid calculation config envelope')
  }
  return {
    project_id: projectId,
    calculation_config: env.calculation_config as CalculationConfigBody,
    revision: env.revision as number,
  }
}

/** 数据集版本/质量纯映射归 features/datasets/mappers 所有, 本文件只作传输层调用。 */

/** 草稿内容设备条目 → 前端 Device(2.0 名键, 无 id/状态/时间字段)。 */
function deviceFromDraft(d: Record<string, unknown>): Device {
  return {
    name: String(d.name ?? ''),
    device_type: String(d.device_type ?? ''),
    kind: (d.kind as DeviceKind) ?? 'new',
    params: asRecord(d.params),
  }
}

/** 草稿内容连接条目 → 前端 Connection(2.0 名键端点, 无类型/容量/损耗)。 */
function connFromDraft(c: Record<string, unknown>): Connection {
  return {
    name: String(c.name ?? ''),
    from_device: String(c.from_device ?? ''),
    from_port: String(c.from_port ?? ''),
    to_device: String(c.to_device ?? ''),
    to_port: String(c.to_port ?? ''),
  }
}

/** 项目视图 draft.content.model → 前端 DraftModelSection(2.0 唯一项目建模事实)。 */
function modelSectionFromView(body: unknown): { revision: number; model: DraftModelSection } {
  const view = asRecord(body)
  const draft = asRecord(view.draft)
  const content = asRecord(draft.content)
  const model = asRecord(content.model)
  return {
    revision: Number(draft.revision ?? 1),
    model: {
      devices: Array.isArray(model.devices)
        ? (model.devices as Record<string, unknown>[]).map(deviceFromDraft)
        : [],
      connections: Array.isArray(model.connections)
        ? (model.connections as Record<string, unknown>[]).map(connFromDraft)
        : [],
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

/** 参数默认值适配:后端枚举参数(如 mode='both')默认值为字符串,数值参数为 number,
 *  结构化参数(如 import_tariff)为 {peak,flat,valley} 对象,原样透传。 */
function paramDefaultValue(v: unknown): number | string | Record<string, number> | null {
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
function deviceTypeFromServer(s: Record<string, unknown>): DeviceTypeSpec {
  const params = asRecord(s.parameters)
  const ports = Array.isArray(s.ports) ? (s.ports as DevicePortSpec[]) : []
  // 注册表公开 DTO 用 names {zh-CN,en-US} 字典(无 name_zh/name_en 平铺字段),
  // 载能只存在于 ports[].energy_carrier(无 energy_carriers 顶层数组): 直接按
  // 端口声明顺序去重派生, 与 descriptor 同一身份, 不做形状复核与值过滤。
  const names = asRecord(s.names)
  const carriers = [...new Set(ports.map((p) => p.energy_carrier))]
  return {
    type_id: String(s.type_id ?? ''),
    version: String(s.version ?? ''),
    name_zh: String(names['zh-CN'] ?? ''),
    name_en: String(names['en-US'] ?? ''),
    energy_carriers: carriers,
    is_load: Boolean(s.is_load ?? false),
    capabilities: Array.isArray(s.capabilities) ? (s.capabilities as string[]) : [],
    model_method: (s.model_method as DeviceTypeSpec['model_method']) ?? 'mechanism',
    stateful: Boolean(s.stateful ?? false),
    ports,
    parameters: Object.fromEntries(
      Object.entries(params).map(([name, p]) => {
        const pr = asRecord(p)
        return [
          name,
          {
            unit: pr.unit === null || pr.unit === undefined ? null : String(pr.unit),
            min: typeof pr.min === 'number' ? (pr.min as number) : null,
            max: typeof pr.max === 'number' ? (pr.max as number) : null,
            default: paramDefaultValue(pr.default),
            is_optimizable: Boolean(pr.is_optimizable ?? false),
            // 后端同时返回 existing_default(存量默认值)与 stock_or_addition(存量/新增标签);
            // 取值一律走 existing_default(旧实现误从 stock_or_addition 取值得到 NaN)。
            existing_default: paramDefaultValue(pr.existing_default),
            help_key: String(pr.help_key ?? ''),
            // 透出枚举约束(heat_pump.mode=heating/cooling/both 等),前端按 enum 渲染下拉。
            enum: Array.isArray(pr.enum)
              ? (pr.enum as Array<string | number | boolean>)
              : null,
          },
        ]
      }),
    ),
  }
}

/** 语义命令批次的命令 id 计数器(保证批次内唯一)。 */
let commandSeq = 0

/** 构造后端草稿命令(project.py update_draft: id/project_id/expected_revision/session/unit/type/payload)。 */
function makeCommand(
  projectId: number,
  expectedRevision: number,
  type: string,
  payload: Record<string, unknown>,
  unit = 'model',
): Record<string, unknown> {
  commandSeq += 1
  return {
    id: `fe-${Date.now().toString(36)}-${commandSeq.toString(36)}`,
    project_id: projectId,
    expected_revision: expectedRevision,
    session: 'browser',
    unit,
    type,
    payload,
  }
}

/**
 * 发送单条草稿语义命令(数据集绑定等非模型命令), 返回新修订号。
 * 先取项目视图的当前草稿修订作乐观锁, 再 PUT /projects/{id}/draft。
 */
async function sendDraftCommand(
  projectId: number,
  type: string,
  payload: Record<string, unknown>,
): Promise<{ revision: number }> {
  const view = asRecord(await request<unknown>(`/projects/${projectId}`))
  const expectedRevision = Number(asRecord(view.draft).revision ?? 1)
  const cmd = makeCommand(projectId, expectedRevision, type, payload, type.split('.')[0] ?? 'model')
  const body = asRecord(
    await request<unknown>(`/projects/${projectId}/draft`, {
      method: 'PUT',
      body: { expected_revision: expectedRevision, commands: [cmd] },
    }),
  )
  return { revision: Number(body.revision ?? expectedRevision) }
}

// ---------------------------------------------------------------------------
// 类型化方法集
// ---------------------------------------------------------------------------

export interface ProjectsApi {
  list(params?: ProjectListParams): Promise<PageResult<Project>>
  create(input: ProjectCreateInput): Promise<Project>
  get(id: number): Promise<Project>
  /** 版本列表: 严格消费唯一 {versions: [...]} 契约, 形状不符抛错(不猜测、不回退空列表)。 */
  versions(id: number): Promise<ProjectVersion[]>
  /** 从当前草稿创建版本: 请求/响应与后端 VersionCreateRequest/{version} 一一对应。 */
  createVersion(id: number, input: ProjectVersionCreateInput): Promise<ProjectVersion>
  /**
   * 恢复历史版本: 与后端 POST .../versions/{vid}/restore 对应。
   * 请求体沿自动命名(不发送 name/description); 响应唯一 {version, draft}。
   * draft 仅取 id/revision(刷新摘要所需), 其余字段忽略不猜测。
   */
  restoreVersion(projectId: number, versionId: number): Promise<VersionRestoreResult>
  /**
   * 应用方案: 与后端 POST .../apply-result 对应。
   * 只发送任务引用、解标识与确认时草稿修订; 参数与来源基线由服务端权威解析,
   * 不发送补丁。修订不符服务端 409, 不创建版本。
   * 响应唯一 {version, draft}, draft 仅取 id/revision。
   */
  applyScheme(projectId: number, input: ApplySchemeInput): Promise<ApplySchemeResult>
  /** 版本详情: 严格消费唯一 {version: {...}} 契约(只返回版本元数据)。 */
  versionDetail(projectId: number, versionId: number): Promise<ProjectVersion>
  /**
   * 上传项目包创建导入提案: 与后端 POST /projects/import(multipart file) 对应。
   * 响应唯一 {proposal: {...}}; 确认前 proposal.project_id 为 null, 不得据此导航。
   */
  proposeImport(file: File): Promise<ImportProposal>
  /**
   * 确认导入提案: 与后端 POST /projects/import/{proposal_id}/confirm 对应。
   * 响应唯一 {project, my_role}; 只有此时才展示已导入并刷新列表。
   */
  confirmImport(proposalId: number): Promise<ImportConfirmResult>
  archive(id: number): Promise<Project>
  unarchive(id: number): Promise<Project>
  /** 删除项目(0.2.0 B4: 须携带与项目名精确匹配的 name 确认)。 */
  delete(id: number, name: string): Promise<void>
}

export interface ModelApi {
  /** 读项目视图草稿内容 model 节(2.0 名键设备与端点连接, 附带草稿修订号)。 */
  getModel(projectId: number): Promise<{ revision: number; model: DraftModelSection }>
  /** 写单个设备(upsert 语义; 改名经 remove+upsert, 由调用方组批)。
   * 调用方传入已加载的草稿修订号, 成功后以后端返回修订号为准, 409 自行处理。 */
  upsertDevice(projectId: number, input: DeviceInput, expectedRevision: number): Promise<{ revision: number }>
  removeDevice(projectId: number, name: string, expectedRevision: number): Promise<{ revision: number }>
  /** 写单条连接(名键端点; 未显式命名时按默认公式命名并返回)。 */
  upsertConnection(
    projectId: number,
    input: ConnectionInput,
    expectedRevision: number,
  ): Promise<{ revision: number; name: string }>
  removeConnection(projectId: number, name: string, expectedRevision: number): Promise<{ revision: number }>
  /** 批量草稿语义命令(改名等需多命令原子推进的场景), 返回新修订号。 */
  sendCommands(
    projectId: number,
    commands: Array<{ type: string; payload: Record<string, unknown> }>,
    expectedRevision: number,
  ): Promise<{ revision: number }>
  validate(projectId: number): Promise<ValidationResult>
  deviceTypes(): Promise<DeviceTypeSpec[]>
}

/** 详细上传输入:在基础上传之上额外携带分辨率/固定 UTC 偏移/字段定义/许可证(设计输入 §8)。 */
export interface DatasetUploadDetailedInput extends DatasetUploadInput {
  /** 分辨率说明(如 '1h'、'15min'),对齐 dataset_versions.resolution。 */
  resolution?: string
  /** 数据时间戳的固定 UTC 偏移(分钟),对齐 dataset_versions.fixed_utc_offset_minutes。 */
  fixed_utc_offset_minutes?: number
  /** 字段定义(名称 -> 单位/说明)。 */
  fields?: Record<string, { unit?: string | null; description?: string }>
  /** 本版本许可证或授权信息。 */
  license?: string
}

export interface DatasetsApi {
  list(params: { project_id: number; status?: string; cursor?: string; limit?: number }): Promise<PageResult<Dataset>>
  template(): Promise<Blob>
  upload(input: DatasetUploadInput): Promise<DatasetVersion>
  /**
   * 详细上传(设计输入 §8.2/§8.3):后端校验后返回数据集版本,
   * 并附带质量报告(version.quality_report)与诊断列表(阻断/警告分级,
   * 经 location 定位到字段/行);阻断性错误未修复时版本不可绑定计算。
   */
  uploadDetailed(input: DatasetUploadDetailedInput): Promise<DatasetVersion & { diagnostics?: Diagnostic[] }>
  versions(projectId: number, id: number): Promise<DatasetVersion[]>
  /** 最新版本字段名(行级预览无能力, 只返元数据, 不带行载体)。 */
  versionFields(projectId: number, id: number): Promise<string[]>
  /**
   * 绑定数据集版本到项目草稿(dataset.bind 语义命令, 写入 draft.content.dataset_bindings)。
   * 绑定是校验 VALID-DATA-001 与任务数据输入的权威来源。
   */
  bind(projectId: number, datasetVersionId: number, datasetId: number): Promise<{ revision: number }>
  /** 解除数据集版本绑定(dataset.unbind 语义命令)。 */
  unbind(projectId: number, datasetVersionId: number): Promise<{ revision: number }>
}

export interface ConfigApi {
  get(projectId: number): Promise<CalcConfig>
  save(projectId: number, input: CalcConfigInput): Promise<CalcConfig>
}

/** 部署公开初值(统一部署配置中允许公开的非秘密默认值)。 */
export interface DeploymentDefaults {
  calculation_defaults: { time_limit_seconds: number }
}

export interface DeploymentApi {
  defaults(): Promise<DeploymentDefaults>
}

export interface ValidationApi {
  run(projectId: number): Promise<ValidationResult>
  baselineConfirm(
    projectId: number,
    assumptions: Record<string, unknown>,
  ): Promise<{ confirmed: boolean; diagnostics: Diagnostic[] }>
}

export interface TasksApi {
  list(params?: TaskListParams): Promise<PageResult<Task>>
  create(input: TaskCreateInput): Promise<Task>
  get(projectId: number, taskId: number): Promise<TaskDetail>
  /** 任务冻结输入上下文(只读; 无快照时 snapshot=null)。 */
  inputs(projectId: number, taskId: number): Promise<TaskInputs>
  /**
   * 选择任务载入工作草稿(新草稿 revision + 配置追加; 不建任务/版本;
   * 无快照任务 400)。返回新草稿 revision 与追加的配置 revision(缺席为 null)。
   */
  loadDraft(projectId: number, taskId: number): Promise<TaskDraftLoadResult>
  /**
   * 暂存任务提交工作输入(task.set_working_inputs 单命令)。
   * 只做存放, 不做领域校验(提交边界仍为权威)。返回新草稿 revision。
   * expectedRevision 缺省时先读当前修订(常规防抖保存); 卸载兜底刷新传入
   * 调度时修订(冲突即 409 放弃, 不覆盖切换后的新选择)。
   */
  saveWorkingInputs(
    projectId: number,
    working: Record<string, unknown>,
    expectedRevision?: number,
  ): Promise<{ revision: number }>
  cancel(projectId: number, taskId: number): Promise<Task>
  retry(projectId: number, taskId: number): Promise<Task>
}

export interface ExportsApi {
  /**
   * Excel 结果报告导出: 返回本地下载会话 id(一次下载引用, 非持久化报告)。
   * 项目包是完整项目现场导出, 不是报告, 同样只返回本地下载会话 id。
   */
  excel(input: ExcelExportInput): Promise<number>
  package(input: PackageExportInput): Promise<number>
  /** 按本地下载会话 id 取 Blob 与建议文件名并下载。 */
  download(downloadSessionId: number): Promise<{ blob: Blob; filename: string }>
}

export const api = {
  projects: {
    list(params?: ProjectListParams): Promise<PageResult<Project>> {
      return request<unknown>('/projects', { query: params }).then((body) => {
        // 后端返回 {projects: [...]}; items 行内 my_role 提升为 role
        const raw = asItems<Record<string, unknown>>(body, 'projects')
        return {
          items: raw.items.map((p) => ({
            ...(p as unknown as Project),
            role: (p.my_role as ProjectRole) ?? undefined,
          })),
          next_cursor: null,
          limit: raw.items.length,
        }
      })
    },
    create(input: ProjectCreateInput): Promise<Project> {
      return request<unknown>('/projects', {
        method: 'POST',
        body: {
          name: input.name,
          description: input.description ?? null,
          currency: input.currency,
          baseline_resolution: input.baseline_resolution,
          baseline_start_year: input.baseline_start_year,
          baseline_scenario_mode: input.baseline_scenario_mode,
        },
      }).then((body) => projectFromServer(body))
    },
    get(id: number): Promise<Project> {
      // 后端返回 {project, draft, versions, my_role}: 解包 project + role
      return request<unknown>(`/projects/${id}`).then((body) => projectFromServer(body))
    },
    versions(id: number): Promise<ProjectVersion[]> {
      // 本流程只消费唯一 {versions: [...]} 契约; 其他形状直接抛错, 不回退空列表。
      return request<unknown>(`/projects/${id}/versions`).then((body) => {
        const rec = asRecord(body)
        if (!Array.isArray(rec.versions)) throw new Error('invalid project versions envelope')
        return (rec.versions as unknown[]).map(projectVersionFromServer)
      })
    },
    createVersion(id: number, input: ProjectVersionCreateInput): Promise<ProjectVersion> {
      const payload: Record<string, unknown> = { name: input.name }
      if (input.description !== undefined) payload.description = input.description
      if (input.reason !== undefined) payload.reason = input.reason
      return request<unknown>(`/projects/${id}/versions`, { method: 'POST', body: payload }).then((body) => {
        const rec = asRecord(body)
        return projectVersionFromServer(rec.version)
      })
    },
    versionDetail(projectId: number, versionId: number): Promise<ProjectVersion> {
      return request<unknown>(`/projects/${projectId}/versions/${versionId}`).then((body) => {
        const rec = asRecord(body)
        return projectVersionFromServer(rec.version)
      })
    },
    proposeImport(file: File): Promise<ImportProposal> {
      const formData = new FormData()
      formData.append('file', file)
      return request<unknown>('/projects/import', { method: 'POST', formData, timeoutMs: 0 }).then((body) => {
        const rec = asRecord(body)
        return importProposalFromServer(rec.proposal)
      })
    },
    confirmImport(proposalId: number): Promise<ImportConfirmResult> {
      return request<unknown>(`/projects/import/${proposalId}/confirm`, { method: 'POST' }).then((body) => {
        const rec = asRecord(body)
        const project = projectFromServer({ project: rec.project, my_role: rec.my_role })
        return { project, myRole: typeof rec.my_role === 'string' ? rec.my_role : null }
      })
    },
    restoreVersion(projectId: number, versionId: number): Promise<VersionRestoreResult> {
      // 唯一 {version, draft} 契约; draft 仅取刷新所需的 id/revision。
      return request<unknown>(`/projects/${projectId}/versions/${versionId}/restore`, {
        method: 'POST',
        body: {},
      }).then((body) => {
        const rec = asRecord(body)
        const version = projectVersionFromServer(rec.version)
        const draft = rec.draft
        if (draft === null || typeof draft !== 'object' || Array.isArray(draft)) {
          throw new Error('invalid version restore envelope')
        }
        const d = draft as Record<string, unknown>
        if (typeof d.id !== 'number' || typeof d.revision !== 'number') {
          throw new Error('invalid version restore envelope')
        }
        return { version, draftId: d.id, draftRevision: d.revision }
      })
    },
    applyScheme(projectId: number, input: ApplySchemeInput): Promise<ApplySchemeResult> {
      // 唯一 {version, draft} 契约; 只发送引用与确认时修订, draft 仅取刷新所需的 id/revision。可信交换直接映射。
      return request<unknown>(`/projects/${projectId}/apply-result`, {
        method: 'POST',
        body: { task_id: input.task_id, solution_id: input.solution_id, expected_revision: input.expected_revision },
      }).then((body) => {
        const rec = asRecord(body)
        const d = rec.draft as { id: number; revision: number }
        return { version: projectVersionFromServer(rec.version), draftId: d.id, draftRevision: d.revision }
      })
    },
    archive(id: number): Promise<Project> {
      return request<unknown>(`/projects/${id}/archive`, { method: 'POST' }).then((body) => projectFromServer(body))
    },
    unarchive(id: number): Promise<Project> {
      return request<unknown>(`/projects/${id}/unarchive`, { method: 'POST' }).then((body) => projectFromServer(body))
    },
    delete(id: number, name: string): Promise<void> {
      // 0.2.0 B4 误操作防护: 须携带与待删除项目名精确匹配的 name(替代空布尔 confirm)
      return request<void>(`/projects/${id}`, { method: 'DELETE', body: { confirm: true, name } })
    },
  } satisfies ProjectsApi,

  model: {
    async getModel(projectId: number): Promise<{ revision: number; model: DraftModelSection }> {
      const body = await request<unknown>(`/projects/${projectId}`)
      return modelSectionFromView(body)
    },
    async sendCommands(
      projectId: number,
      commands: Array<{ type: string; payload: Record<string, unknown> }>,
      expectedRevision: number,
    ): Promise<{ revision: number }> {
      // 修订号由调用方(ModelPage 已加载值)传入, 写前不 GET; 成功以后端返回为准。
      const body = asRecord(
        await request<unknown>(`/projects/${projectId}/draft`, {
          method: 'PUT',
          body: {
            expected_revision: expectedRevision,
            commands: commands.map((c) =>
              makeCommand(projectId, expectedRevision, c.type, c.payload, c.type.split('.')[0] ?? 'model'),
            ),
          },
        }),
      )
      return { revision: Number(body.revision ?? expectedRevision) }
    },
    async upsertDevice(
      projectId: number,
      input: DeviceInput,
      expectedRevision: number,
    ): Promise<{ revision: number }> {
      return api.model.sendCommands(
        projectId,
        [
          {
            type: 'model.upsert_device',
            payload: {
              name: input.name,
              device_type: input.device_type,
              kind: input.kind,
              ...(input.params !== undefined ? { params: input.params } : {}),
            },
          },
        ],
        expectedRevision,
      )
    },
    async removeDevice(projectId: number, name: string, expectedRevision: number): Promise<{ revision: number }> {
      return api.model.sendCommands(projectId, [{ type: 'model.remove_device', payload: { name } }], expectedRevision)
    },
    async upsertConnection(
      projectId: number,
      input: ConnectionInput,
      expectedRevision: number,
    ): Promise<{ revision: number; name: string }> {
      const name =
        input.name ?? connectionName(input.from_device, input.from_port, input.to_device, input.to_port)
      const { revision } = await api.model.sendCommands(
        projectId,
        [
          {
            type: 'model.upsert_connection',
            payload: {
              name,
              from_device: input.from_device,
              from_port: input.from_port,
              to_device: input.to_device,
              to_port: input.to_port,
            },
          },
        ],
        expectedRevision,
      )
      return { revision, name }
    },
    async removeConnection(projectId: number, name: string, expectedRevision: number): Promise<{ revision: number }> {
      return api.model.sendCommands(projectId, [{ type: 'model.remove_connection', payload: { name } }], expectedRevision)
    },
    validate(projectId: number): Promise<ValidationResult> {
      return request<unknown>(`/projects/${projectId}/validation/run`, { method: 'POST' }).then((body) => {
        const report = asRecord(oneOf<Record<string, unknown>>(body, 'report'))
        const diags = asList<Diagnostic>(report, 'diagnostics')
        return { valid: report.blocks_submit !== true, diagnostics: diags }
      })
    },
    deviceTypes(): Promise<DeviceTypeSpec[]> {
      return request<unknown>('/registry/device-types').then((body) =>
        asList<Record<string, unknown>>(body, 'items').map(deviceTypeFromServer),
      )
    },
  } satisfies ModelApi,

  datasets: {
    list(params: { project_id: number; status?: string; cursor?: string; limit?: number }): Promise<PageResult<Dataset>> {
      // 数据集按项目挂载(/api/projects/{pid}/datasets); 项目 ID 必填,
      // 直接调用公开接口, 不做无项目空页伪成功, 不复核 ID。
      const projectId = params.project_id
      return request<unknown>(`/projects/${projectId}/datasets`).then((body) => {
        // 后端精确信封 {datasets: [{dataset, latest_version}]}: 直接消费具名键。
        // 畸形体自然抛错可见, 不以空列表伪装成功; 空数组与 latest_version=null
        // 均为合法(分别表示无数据集 / 尚无版本), 原样保留。
        const entries = (body as DatasetsListResponseDto).datasets
        return {
          items: entries.map((e) => e.dataset),
          next_cursor: null,
          limit: entries.length,
        }
      })
    },
    template(): Promise<Blob> {
      return requestBlob('/datasets/template')
    },
    upload(input: DatasetUploadInput): Promise<DatasetVersion> {
      return uploadDataset(input as DatasetUploadDetailedInput)
    },
    uploadDetailed(input: DatasetUploadDetailedInput): Promise<DatasetVersion & { diagnostics?: Diagnostic[] }> {
      return uploadDataset(input)
    },
    versions(projectId: number, id: number): Promise<DatasetVersion[]> {
      // 后端精确信封 {dataset, versions}: 直接消费具名键, 畸形体自然抛错可见。
      return request<unknown>(`/projects/${projectId}/datasets/${id}`).then((body) =>
        (body as DatasetDetailResponseDto).versions.map(datasetVersionFromServer),
      )
    },
    versionFields(projectId: number, id: number): Promise<string[]> {
      // 后端无数据行预览端点: 只返回最新版本真实字段名,
      // 不设行/行数载体, 不得伪造空行冒充真实空数据集。
      return request<unknown>(`/projects/${projectId}/datasets/${id}`).then((body) => {
        const versions = (body as DatasetDetailResponseDto).versions
        const latest = versions[0]
        if (latest === undefined) return []
        return Object.keys(latest.fields)
      })
    },
    bind(projectId: number, datasetVersionId: number, datasetId: number): Promise<{ revision: number }> {
      return sendDraftCommand(projectId, 'dataset.bind', {
        dataset_version_id: datasetVersionId,
        dataset_id: datasetId,
        role: 'annual',
      })
    },
    unbind(projectId: number, datasetVersionId: number): Promise<{ revision: number }> {
      return sendDraftCommand(projectId, 'dataset.unbind', { dataset_version_id: datasetVersionId })
    },
  } satisfies DatasetsApi,

  config: {
    get(projectId: number): Promise<CalcConfig> {
      // 后端返回 {calculation_config, revision}: 映射为前端配置对象
      return request<unknown>(`/projects/${projectId}/config`).then((body) => configFromServer(body, projectId))
    },
    save(projectId: number, input: CalcConfigInput): Promise<CalcConfig> {
      // 后端 PUT 需要 {calculation_config, expected_revision}(自有链乐观锁):
      // expected_revision 由调用方传入已加载 revision, 首次保存为 null。
      return request<unknown>(`/projects/${projectId}/config`, {
        method: 'PUT',
        body: { calculation_config: input.calculation_config, expected_revision: input.expected_revision },
      }).then((body) => configFromServer(body, projectId))
    },
  } satisfies ConfigApi,

  deployment: {
    defaults(): Promise<DeploymentDefaults> {
      // 后端返回 {calculation_defaults: {time_limit_seconds}}: 形态断言
      return request<unknown>('/deployment/defaults').then((body) => {
        const root = asRecord(body)
        const calc = asRecord(root.calculation_defaults)
        const timeLimit = calc.time_limit_seconds
        if (typeof timeLimit !== 'number' || !Number.isFinite(timeLimit)) {
          throw new ApiError(500, null, 'ies.error.unknown')
        }
        return { calculation_defaults: { time_limit_seconds: timeLimit } }
      })
    },
  } satisfies DeploymentApi,

  validation: {
    run(projectId: number): Promise<ValidationResult> {
      return request<unknown>(`/projects/${projectId}/validation/run`, { method: 'POST' }).then((body) => {
        // 后端返回 {report, stored}: 报告含 status/blocks_submit/diagnostics
        const report = asRecord(asRecord(body).report)
        return {
          valid: report.blocks_submit !== true,
          diagnostics: asList<Diagnostic>(report, 'diagnostics'),
        }
      })
    },
    baselineConfirm(
      projectId: number,
      assumptions: Record<string, unknown>,
    ): Promise<{ confirmed: boolean; diagnostics: Diagnostic[] }> {
      // 假设内容必须由调用方显式提供(用户确认的评价输入); 本层不编默认值、
      // 不从计算配置派生(0.12-A1 起计算配置不再承载经济参数)。
      return request<unknown>(`/projects/${projectId}/validation/baseline-confirm`, {
        method: 'POST',
        body: { assumptions },
      }).then((body) => ({ confirmed: asRecord(body).confirmed === true, diagnostics: [] }))
    },
  } satisfies ValidationApi,

  tasks: {
    list(params?: TaskListParams): Promise<PageResult<Task>> {
      const projectId = params?.project_id
      if (projectId === undefined || projectId === null) {
        throw new ApiError(400, null, 'ies.error.unknown')
      }
      const query = { ...params } as Record<string, unknown>
      delete query.project_id
      return request<unknown>(`/projects/${projectId}/tasks`, { query }).then((body) => {
        const page = asItems<Record<string, unknown>>(body, 'tasks')
        return {
          items: page.items.map((t) => taskFromSummary(t, projectId)),
          next_cursor: page.next_cursor,
          limit: page.limit,
        }
      })
    },
    create(input: TaskCreateInput): Promise<Task> {
      // 数据集版本清单唯一权威是草稿 dataset_bindings(数据页绑定; 提交装配
      // 只读该绑定)。对话框不再发送 dataset_ids(旧 config.dataset_ids 后端
      // 从未消费, 为误导性输入, 删除)。
      const config: Record<string, unknown> = {}
      if (input.config_revision !== undefined) config.config_revision = input.config_revision
      if (input.priority !== undefined) config.priority = input.priority
      if (input.deadline !== undefined) config.deadline = input.deadline
      if (input.params !== undefined) config.params = input.params
      return request<unknown>(`/projects/${input.project_id}/tasks`, {
        method: 'POST',
        body: {
          task_type: input.type,
          config: Object.keys(config).length > 0 ? config : null,
          // 评价确认是顶层类型化任务输入, 不混入 config(与后端 TaskCreateRequest 同形)。
          ...(input.evaluation !== undefined ? { evaluation: input.evaluation } : {}),
          idempotency_key: input.idempotency_key ?? null,
          parent_task_id: null,
        },
      }).then((res) => taskFromSummary(asRecord(oneOf<Record<string, unknown>>(res, 'task')), input.project_id))
    },
    get(projectId: number, taskId: number): Promise<TaskDetail> {
      return request<unknown>(`/projects/${projectId}/tasks/${taskId}`).then(async (body) => {
        const detail = taskDetailFromServer(asRecord(oneOf<Record<string, unknown>>(body, 'task')), projectId)
        // RR-P1-05: 结果可用性是任务元数据(result_available), 只有明确可用时
        // 才读取证据包; 未完成/无结果不是页面级失败, 不靠探测 /result 的 404 猜测。
        // 可用性明确为 true 时 /result 读取失败必须 reject, 不吞错置空 evidence
        // 伪装详情正常无结果(调用方负责失败可见)。
        if (detail.result_available) {
          // 可用性权威说明存在证据: 补充元数据必需字段直接消费,
          // 不做 ??0/默认 complete/pkgId>0 过滤掩盖, 不新增字段校验器。
          const result = await request<unknown>(`/projects/${projectId}/tasks/${taskId}/result`)
          const r = asRecord(oneOf<Record<string, unknown>>(result, 'result'))
          const evidence = asRecord(r.evidence)
          const pkgId = Number(evidence.id)
          detail.evidence = [
            {
              package_id: pkgId,
              object_id: Number(evidence.object_id),
              status: evidence.status as TaskDetail['evidence'][number]['status'],
            },
          ]
        }
        return detail
      })
    },
    cancel(projectId: number, taskId: number): Promise<Task> {
      return request<unknown>(`/projects/${projectId}/tasks/${taskId}/cancel`, {
        method: 'POST',
        body: { reason: 'user_cancel' },
      }).then((res) => taskFromSummary(asRecord(oneOf<Record<string, unknown>>(res, 'task')), projectId))
    },
    retry(projectId: number, taskId: number): Promise<Task> {
      return request<unknown>(`/projects/${projectId}/tasks/${taskId}/retry`, { method: 'POST', body: {} }).then(
        (res) => taskFromSummary(asRecord(oneOf<Record<string, unknown>>(res, 'task')), projectId),
      )
    },
    async saveWorkingInputs(
      projectId: number,
      working: Record<string, unknown>,
      expectedRevision?: number,
    ): Promise<{ revision: number }> {
      if (expectedRevision === undefined) {
        return sendDraftCommand(projectId, 'task.set_working_inputs', { working })
      }
      const cmd = makeCommand(projectId, expectedRevision, 'task.set_working_inputs', { working }, 'task')
      const body = asRecord(
        await request<unknown>(`/projects/${projectId}/draft`, {
          method: 'PUT',
          body: { expected_revision: expectedRevision, commands: [cmd] },
        }),
      )
      return { revision: Number(body.revision ?? expectedRevision) }
    },
    loadDraft(projectId: number, taskId: number): Promise<TaskDraftLoadResult> {
      return request<unknown>(`/projects/${projectId}/tasks/${taskId}/load-draft`, {
        method: 'POST',
      }).then((body) => {
        const v = asRecord(body)
        const draft = asRecord(v.draft)
        const configRevision = v.config_revision
        return {
          draftRevision: Number(draft.revision),
          configRevision:
            configRevision === null || configRevision === undefined
              ? null
              : Number(configRevision),
        }
      })
    },
    inputs(projectId: number, taskId: number): Promise<TaskInputs> {
      return request<unknown>(`/projects/${projectId}/tasks/${taskId}/inputs`).then((body) => {
        const v = asRecord(oneOf<Record<string, unknown>>(body, 'inputs'))
        const task = asRecord(v.task)
        const snap = v.snapshot === null || v.snapshot === undefined ? null : asRecord(v.snapshot)
        const refs = asRecord(v.frozen_refs)
        return {
          task: {
            id: Number(task.id),
            type: task.type as TaskInputs['task']['type'],
            status: task.status as TaskInputs['task']['status'],
            calc_snapshot_id:
              task.calc_snapshot_id === null || task.calc_snapshot_id === undefined
                ? null
                : Number(task.calc_snapshot_id),
          },
          snapshot:
            snap === null
              ? null
              : {
                  id: Number(snap.id),
                  project_version_id: Number(snap.project_version_id),
                  dataset_version_ids: (snap.dataset_version_ids as unknown[]).map(Number),
                  calc_config_snapshot: asRecord(snap.calc_config_snapshot),
                  device_models: asRecord(snap.device_models),
                  evaluation: asRecord(snap.evaluation),
                  schema_version: String(snap.schema_version),
                  random_seed:
                    snap.random_seed === null || snap.random_seed === undefined ? null : Number(snap.random_seed),
                },
          frozen_refs: {
            planning_revision:
              refs.planning_revision === null || refs.planning_revision === undefined
                ? null
                : Number(refs.planning_revision),
            finance_profile_id:
              refs.finance_profile_id === null || refs.finance_profile_id === undefined
                ? null
                : String(refs.finance_profile_id),
            finance_revision:
              refs.finance_revision === null || refs.finance_revision === undefined
                ? null
                : Number(refs.finance_revision),
          },
        }
      })
    },
  } satisfies TasksApi,

  results: {
    result: getResult,
    assessments: listAssessments,
    assess: assessResult,
    select: selectResult,
    hourly: readHourly,
  },

  exports: {
    excel: exportTaskReport,
    package: exportPackage,
    download: downloadExport,
  } satisfies ExportsApi,
}

// ---------------------------------------------------------------------------
// 数据集上传(两步: 创建元数据 → 上传版本)
// ---------------------------------------------------------------------------

/** 数据集上传: 后端拆分为「创建数据集元数据」+「上传版本(multipart)」两步。 */
async function uploadDataset(
  input: DatasetUploadDetailedInput,
): Promise<DatasetVersion & { diagnostics?: Diagnostic[] }> {
  const projectId = input.project_id
  if (projectId === undefined || projectId === null) {
    throw new ApiError(400, null, 'ies.error.unknown')
  }
  // 1) 创建数据集元数据
  const created = asRecord(
    await request<unknown>(`/projects/${projectId}/datasets`, {
      method: 'POST',
      body: {
        name: input.name,
        description: input.description ?? null,
        source_category: 'user_upload',
        license: input.license ?? null,
      },
    }),
  )
  const datasetId = Number(asRecord(created.dataset).id)
  // 2) 上传版本(multipart: file/resolution/utc_offset_minutes/fields/meta)
  const formData = new FormData()
  formData.append('file', input.file)
  formData.append('resolution', input.resolution ?? '1h')
  if (input.fixed_utc_offset_minutes !== undefined && input.fixed_utc_offset_minutes !== null) {
    formData.append('utc_offset_minutes', String(input.fixed_utc_offset_minutes))
  }
  if (input.fields && Object.keys(input.fields).length > 0) {
    formData.append(
      'fields',
      JSON.stringify(
        Object.fromEntries(
          Object.entries(input.fields).map(([k, v]) => [k, { unit: v.unit ?? '' }]),
        ),
      ),
    )
  }
  formData.append(
    'meta',
    JSON.stringify({
      source_category: 'user_upload',
      // 空许可证省略(后端 meta 白名单要求 license 为字符串, 显式 null 会阻断)
      ...(input.license ? { license: input.license } : {}),
      created_reason: 'upload',
    }),
  )
  const uploaded = (await request<unknown>(`/projects/${projectId}/datasets/${datasetId}/versions`, {
    method: 'POST',
    formData,
    timeoutMs: 0,
  })) as DatasetUploadResponseDto
  const version = datasetVersionFromServer(uploaded.dataset_version)
  const diagnostics = uploaded.diagnostics ?? []
  return { ...version, diagnostics }
}

export type { ApiErrorBody }

/** 任务类型/状态等枚举导出,供页面组件使用。 */
export type { TaskStatus, TaskType, Currency }
