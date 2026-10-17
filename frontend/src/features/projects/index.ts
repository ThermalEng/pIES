/**
 * 项目 feature 公开入口(页面组合真正需要的请求、类型与纯映射)。
 *
 * 草稿语义命令传输(sendDraftCommand/putDraftCommands)是项目业务协议,
 * modeling/dataset/task 各自准备业务 payload 后共用, 不在各家族复制。
 */

export {
  archiveProject,
  createProject,
  deleteProject,
  getProject,
  listProjects,
  putDraftCommands,
  sendDraftCommand,
  unarchiveProject,
} from './api'
export {
  createRequestFromInput,
  draftFromView,
  projectFromDto,
  projectFromMutation,
  projectFromView,
  projectListFromServer,
} from './mappers'
export type {
  DraftBindingDto,
  DraftCommandDto,
  DraftCommandInput,
  DraftContentConnectionDto,
  DraftContentDeviceDto,
  DraftContentDto,
  DraftContentModelDto,
  DraftUpdateRequestDto,
  DraftUpdateResponseDto,
  DraftViewDto,
  ProjectBaselineDto,
  ProjectCreateRequestDto,
  ProjectDto,
  ProjectListItemDto,
  ProjectListResponseDto,
  ProjectMutationResponseDto,
  ProjectViewResponseDto,
} from './contracts'
export { roleOf } from './contracts'
