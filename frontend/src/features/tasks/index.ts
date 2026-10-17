/**
 * 任务 feature 公开入口(页面组合真正需要的请求、类型与纯映射)。
 *
 * 内部 contract 按需由调用方经子路径导入, 不经此桶文件转发实现。
 */

export {
  cancelTask,
  createTask,
  getTaskDetail,
  getTaskInputs,
  listTasks,
  loadTaskDraft,
  retryTask,
  saveWorkingInputs,
} from './api'
export {
  buildInput,
  buildParams,
  DEFAULT_FORM,
  formFromWorking,
  hasSavedConfig,
  logicalSigOf,
  newSubmitNonce,
  parseEvaluation,
  serializeParams,
  stableHash,
  SUBMITTABLE_TYPES,
  workingOfForm,
} from './form'
export {
  describeFrozenConfig,
  describeFrozenEvaluation,
  describeFrozenFinance,
  evidenceFromServer,
  frozenNumText,
  frozenStr,
  loadDraftFromServer,
  taskDetailFromServer,
  taskDiagFromServer,
  taskFromCancelResponse,
  taskFromCreateResponse,
  taskFromListItem,
  taskFromRetryResponse,
  taskFromSummary,
  taskInputsFromServer,
  taskListFromServer,
} from './mappers'
export type {
  CreateForm,
  DistType,
  ParsedEvaluation,
  SweepRow,
  UncertaintyMode,
} from './form'
export type {
  LoadDraftResponseDto,
  TaskAttemptDto,
  TaskBatchDto,
  TaskCancelResponseDto,
  TaskCreateRequestDto,
  TaskCreateResponseDto,
  TaskDetailDto,
  TaskDetailResponseDto,
  TaskDiagDto,
  TaskEvidenceDto,
  TaskFrozenSnapshotDto,
  TaskInputsBodyDto,
  TaskInputsResponseDto,
  TaskLeaseDto,
  TaskListItemDto,
  TaskListResponseDto,
  TaskProgressDto,
  TaskResultEnvelopeDto,
  TaskRetryResponseDto,
  TaskSummaryBlockDto,
  TaskSummaryDto,
} from './contracts'
