/**
 * 项目包导入流程 hook(上传→提案→确认的状态机; 页面只消费状态与动作)。
 *
 * 竞态策略: 每次写请求递增序号, 迟到响应直接丢弃, 完成只由所属飞行推进。
 * 写请求飞行中(busy)调用方须禁用文件更换与对话框关闭。取消确认仅关闭界面,
 * 不发送请求、不删除后端提案。确认失败保留同一提案供重试, 不自动重传文件。
 */

import { useCallback, useRef, useState } from 'react'

import { errorMessage } from '../../../i18n'
import { confirmImport, proposeImport } from '../api'
import { errorReasons, toProposalView } from '../mappers'
import type { ImportFlowState } from '../model'

export interface ImportProposalActions {
  /** 选择文件并上传创建提案(非忙碌时)。 */
  upload: (file: File) => void
  /** 确认当前提案(仅 proposed/confirmFailed 状态)。 */
  confirm: () => void
  /** 回到初始态(关闭对话框时由调用方重置)。 */
  reset: () => void
}

export function useImportProposal(onConfirmed?: () => void): {
  state: ImportFlowState
  busy: boolean
  actions: ImportProposalActions
} {
  const [state, setState] = useState<ImportFlowState>({ status: 'idle' })
  const stateRef = useRef(state)
  stateRef.current = state
  const seqRef = useRef(0)

  const upload = useCallback((file: File) => {
    const current = stateRef.current
    if (current.status === 'uploading' || current.status === 'confirming') return
    const seq = ++seqRef.current
    setState({ status: 'uploading', fileName: file.name })
    void proposeImport(file).then(
      (proposal) => {
        if (seq !== seqRef.current) return
        setState({ status: 'proposed', proposal, view: toProposalView(proposal) })
      },
      (err: unknown) => {
        if (seq !== seqRef.current) return
        setState({ status: 'uploadFailed', fileName: file.name, message: errorMessage(err), reasons: errorReasons(err) })
      },
    )
  }, [])

  const confirm = useCallback(() => {
    const current = stateRef.current
    if (current.status !== 'proposed' && current.status !== 'confirmFailed') return
    const { proposal, view } = current
    const seq = ++seqRef.current
    setState({ status: 'confirming', proposal, view })
    void confirmImport(proposal.id).then(
      (result) => {
        if (seq !== seqRef.current) return
        setState({ status: 'done', proposal, view, project: result.project })
        onConfirmed?.()
      },
      (err: unknown) => {
        if (seq !== seqRef.current) return
        setState({ status: 'confirmFailed', proposal, view, message: errorMessage(err) })
      },
    )
  }, [onConfirmed])

  const reset = useCallback(() => {
    seqRef.current += 1
    setState({ status: 'idle' })
  }, [])

  const busy = state.status === 'uploading' || state.status === 'confirming'
  return { state, busy, actions: { upload, confirm, reset } }
}
