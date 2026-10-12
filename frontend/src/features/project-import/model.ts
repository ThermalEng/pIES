/**
 * 项目包导入 feature 前端 model(展示层形态, 与后端 DTO 解耦)。
 *
 * 提案主体直接复用共享 ImportProposal; 本文件只定义展示快照与流程状态,
 * 不复制后端字段。
 */

import type { ImportProposal, Project } from '../../types'

/** 提案展示快照(名称/币种/暂存对象数; 任一缺失即 null, 不伪造)。 */
export interface ProposalView {
  name: string | null
  currency: string | null
  stagedCount: number | null
}

/**
 * 导入流程状态(uploading/confirming 为写请求飞行中, 此时禁用文件更换与
 * 对话框关闭, 完成只由所属飞行推进; 迟到响应按序号丢弃)。
 */
export type ImportFlowState =
  | { status: 'idle' }
  | { status: 'uploading'; fileName: string }
  | { status: 'uploadFailed'; fileName: string; message: string; reasons: string[] }
  | { status: 'proposed'; proposal: ImportProposal; view: ProposalView }
  | { status: 'confirming'; proposal: ImportProposal; view: ProposalView }
  | { status: 'confirmFailed'; proposal: ImportProposal; view: ProposalView; message: string }
  | { status: 'done'; proposal: ImportProposal; view: ProposalView; project: Project }
