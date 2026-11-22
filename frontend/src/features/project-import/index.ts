/**
 * 项目包导入 feature 公开入口(页面组合真正需要的请求、类型与纯映射)。
 */

export { ImportProposalDialog } from './components/ImportProposalDialog'
export type { ImportProposalDialogProps } from './components/ImportProposalDialog'
export { useImportProposal } from './hooks/useImportProposal'
export type { ImportProposalActions } from './hooks/useImportProposal'
export { confirmImport, proposeImport } from './api'
export { confirmFromServer, errorReasons, proposalFromServer, proposalSnapshot } from './mappers'
export type {
  ImportConfirmResponseDto,
  ImportProposalDto,
  ImportProposalResponseDto,
} from './contracts'
export { confirmedRole } from './contracts'
export type { ProposalView } from './model'
