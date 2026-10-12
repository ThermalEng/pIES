/**
 * 项目包导入 feature 前后端契约(与后端 HTTP JSON 字段一一对应, snake_case)。
 *
 * 端点(与后端 /api 路由一致):
 *   POST /api/projects/import                    上传项目包 → {proposal: {...}}
 *   POST /api/projects/import/{proposal_id}/confirm  确认导入 → {project, my_role}
 *
 * 确认前 proposal.project_id 为 null(提案阶段不创建普通项目); 只有确认
 * 成功返回 project 才表示已导入。失败原因经标准错误信封 params.reasons
 * 逐项返回(未知错误沿既有错误展示路径)。没有提案列表/取消 API, 关闭确认
 * 界面仅关闭本地界面, 不删除后端提案。
 *
 * 依赖方向: features/project-import → ../../types(共享类型)/../../api/client(HTTP 层)。
 */

/** 导入提案展示所需快照(源自 review_summary.project_snapshot, 缺失即 null)。 */
export interface ProposalSnapshotDto {
  name: string | null
  currency: string | null
  stagedCount: number | null
}
