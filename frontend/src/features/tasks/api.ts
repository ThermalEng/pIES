/**
 * 任务 feature 请求(传输层实现, 供 api/client 聚合器委托)。
 *
 * 全部请求经 shared/http request 发出, 成功正文按 contracts 具名键直接
 * 消费后交 mappers 映射; 畸形体自然抛错可见, 不做形状并集猜测。
 * 结果可用性补充读取(tasks.get 内 /result)与旧行为一致: 仅当任务元数据
 * result_available 明确为 true 时读取(未完成/无结果不是失败, 不探测 404);
 * 可用时读取失败必须 reject(调用方负责失败可见), 不吞错置空。
 * saveWorkingInputs 仍由 api/client 直接实现(复用跨家族共享的草稿命令
 * helper, 不在本家族重复), 故不在此文件。
 */

import { ApiError } from '../../types'
import type {
  PageResult,
  Task,
  TaskCreateInput,
  TaskDetail,
  TaskDraftLoadResult,
  TaskInputs,
  TaskListParams,
} from '../../types'
import { request } from '../../shared/http'
import type {
  TaskCancelResponseDto,
  TaskCreateRequestDto,
  TaskCreateResponseDto,
  TaskDetailResponseDto,
  TaskInputsResponseDto,
  TaskListResponseDto,
  TaskResultEnvelopeDto,
  TaskRetryResponseDto,
} from './contracts'
import {
  evidenceFromServer,
  loadDraftFromServer,
  taskDetailFromServer,
  taskFromCancelResponse,
  taskFromCreateResponse,
  taskFromRetryResponse,
  taskListFromServer,
  taskInputsFromServer,
} from './mappers'

/** 任务列表(项目 ID 必填; 信封 {items, next_cursor} 直接消费)。 */
export async function listTasks(params?: TaskListParams): Promise<PageResult<Task>> {
  const projectId = params?.project_id
  if (projectId === undefined || projectId === null) {
    throw new ApiError(400, null, 'ies.error.unknown')
  }
  const query = { ...params } as Record<string, unknown>
  delete query.project_id
  const body = await request<TaskListResponseDto>(`/projects/${projectId}/tasks`, { query })
  const items = taskListFromServer(body, projectId)
  return {
    items,
    // 后端游标为任务 id 数字(或 null); 分页查询走字符串, 此处为传输边界转换。
    next_cursor: body.next_cursor === null ? null : String(body.next_cursor),
    // 后端列表无 limit 键: 条数即本页实际条数(与旧回退口径一致)。
    limit: items.length,
  }
}

/** 提交任务(请求体与后端 TaskCreateRequest 同形; 评价确认顶层提交)。 */
export async function createTask(input: TaskCreateInput): Promise<Task> {
  // 数据集版本清单唯一权威是草稿 dataset_bindings(数据页绑定); 提交装配
  // 只读该绑定。对话框不再发送 dataset_ids(旧 config.dataset_ids 后端
  // 从未消费, 为误导性输入, 删除)。
  const config: Record<string, unknown> = {}
  if (input.config_revision !== undefined) config.config_revision = input.config_revision
  if (input.priority !== undefined) config.priority = input.priority
  if (input.deadline !== undefined) config.deadline = input.deadline
  if (input.params !== undefined) config.params = input.params
  const body: TaskCreateRequestDto = {
    task_type: input.type,
    config: Object.keys(config).length > 0 ? config : null,
    // 评价确认是顶层类型化任务输入, 不混入 config。
    ...(input.evaluation !== undefined ? { evaluation: input.evaluation as Record<string, unknown> } : {}),
    idempotency_key: input.idempotency_key ?? null,
    parent_task_id: null,
  }
  const res = await request<TaskCreateResponseDto>(`/projects/${input.project_id}/tasks`, {
    method: 'POST',
    body,
  })
  return taskFromCreateResponse(res, input.project_id)
}

/** 任务详情 + 结果可用时的证据身份补充读取(见文件头)。 */
export async function getTaskDetail(projectId: number, taskId: number): Promise<TaskDetail> {
  const body = await request<TaskDetailResponseDto>(`/projects/${projectId}/tasks/${taskId}`)
  const detail = taskDetailFromServer(body, projectId)
  if (detail.result_available) {
    // 可用性权威说明存在证据: 补充元数据必需字段直接消费。可用性判定与
    // 本读取同查询族, 证据缺席只可能是不一致的后端响应, 按畸形体明确失败
    // (deployment.defaults 同口径), 不伪造 NaN 条目。
    const result = await request<TaskResultEnvelopeDto>(`/projects/${projectId}/tasks/${taskId}/result`)
    const evidence = result.result.evidence
    if (evidence === null) {
      throw new ApiError(500, null, 'ies.error.unknown')
    }
    const pkg = evidenceFromServer(evidence)
    detail.evidence = [
      {
        package_id: pkg.package_id,
        object_id: pkg.object_id,
        status: pkg.status,
      },
    ]
  }
  return detail
}

/** 任务冻结输入上下文(只读; 无快照时 snapshot=null)。 */
export async function getTaskInputs(projectId: number, taskId: number): Promise<TaskInputs> {
  const body = await request<TaskInputsResponseDto>(`/projects/${projectId}/tasks/${taskId}/inputs`)
  return taskInputsFromServer(body)
}

/**
 * 选择任务载入工作草稿(新草稿 revision + 配置追加; 不建任务/版本;
 * 无快照任务 400)。返回新草稿 revision 与追加的配置 revision(缺席为 null)。
 */
export async function loadTaskDraft(projectId: number, taskId: number): Promise<TaskDraftLoadResult> {
  const body = await request<{ draft: { revision: number }; config_revision: number | null }>(
    `/projects/${projectId}/tasks/${taskId}/load-draft`,
    { method: 'POST' },
  )
  return loadDraftFromServer(body)
}

/** 取消任务(返回取消后任务摘要)。 */
export async function cancelTask(projectId: number, taskId: number): Promise<Task> {
  const res = await request<TaskCancelResponseDto>(`/projects/${projectId}/tasks/${taskId}/cancel`, {
    method: 'POST',
    body: { reason: 'user_cancel' },
  })
  return taskFromCancelResponse(res, projectId)
}

/** 手动重试任务(返回重试后任务摘要)。 */
export async function retryTask(projectId: number, taskId: number): Promise<Task> {
  const res = await request<TaskRetryResponseDto>(`/projects/${projectId}/tasks/${taskId}/retry`, {
    method: 'POST',
    body: {},
  })
  return taskFromRetryResponse(res, projectId)
}
