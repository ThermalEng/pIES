/**
 * 项目包导入流程 hook(上传→提案→确认的状态机; 页面只消费状态与动作)。
 *
 * 竞态策略: 每次写请求递增序号, 迟到响应直接丢弃, 完成只由所属飞行推进。
 * 写请求飞行中(busy)调用方须禁用文件更换与对话框关闭。取消确认仅关闭界面,
 * 不发送请求、不删除后端提案。确认失败保留同一提案供重试, 不自动重传文件。
 *
 * 确认成功即导入成功(不可逆), 其后列表刷新是独立展示状态: onImported 的
 * 布尔结果只决定 done.refresh(pending/ok/failed), 刷新失败不改变导入成功
 * 事实, 仅允许 refreshList 单独重试列表读取, 永不重发确认、不重传文件。
 */

import { useCallback, useRef, useState } from 'react'

import { errorMessage } from '../../../i18n'
import type { ImportProposal, Project } from '../../../types'
import { confirmImport, proposeImport } from '../api'
import { errorReasons, proposalSnapshot } from '../mappers'
import type { ImportFlowState, ProposalView } from '../model'

export interface ImportProposalActions {
  /** 选择文件并上传创建提案(非忙碌时)。 */
  upload: (file: File) => void
  /** 确认当前提案(仅 proposed/confirmFailed 状态)。 */
  confirm: () => void
  /** 仅重试导入成功后的列表刷新(仅 done+refresh=failed 状态; 不重发确认)。 */
  refreshList: () => void
  /** 回到初始态(关闭对话框/打开新项目时由调用方重置; 忙碌中不得调用)。 */
  reset: () => void
}

interface DoneBase {
  proposal: ImportProposal
  view: ProposalView
  project: Project
}

export function useImportProposal(onImported: () => Promise<boolean>): {
  state: ImportFlowState
  busy: boolean
  actions: ImportProposalActions
} {
  const [state, setState] = useState<ImportFlowState>({ status: 'idle' })
  const stateRef = useRef(state)
  stateRef.current = state
  const seqRef = useRef(0)
  const onImportedRef = useRef(onImported)
  onImportedRef.current = onImported

  /** 导入成功后跟踪列表刷新, 结果只写入所属 done 状态的 refresh。 */
  const runRefresh = useCallback((base: DoneBase, seq: number) => {
    setState({ status: 'done', ...base, refresh: 'pending' })
    void (async () => {
      let ok = false
      try {
        ok = await onImportedRef.current()
      } catch {
        ok = false
      }
      if (seq !== seqRef.current) return
      const current = stateRef.current
      if (current.status !== 'done' || current.project.id !== base.project.id) return
      setState({ ...current, refresh: ok ? 'ok' : 'failed' })
    })()
  }, [])

  const upload = useCallback((file: File) => {
    const current = stateRef.current
    if (current.status === 'uploading' || current.status === 'confirming') return
    const seq = ++seqRef.current
    setState({ status: 'uploading', fileName: file.name })
    void proposeImport(file).then(
      (proposal) => {
        if (seq !== seqRef.current) return
        setState({ status: 'proposed', proposal, view: proposalSnapshot(proposal) })
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
        runRefresh({ proposal, view, project: result.project }, seq)
      },
      (err: unknown) => {
        if (seq !== seqRef.current) return
        setState({ status: 'confirmFailed', proposal, view, message: errorMessage(err) })
      },
    )
  }, [runRefresh])

  const refreshList = useCallback(() => {
    const current = stateRef.current
    if (current.status !== 'done' || current.refresh !== 'failed') return
    const seq = ++seqRef.current
    runRefresh({ proposal: current.proposal, view: current.view, project: current.project }, seq)
  }, [runRefresh])

  const reset = useCallback(() => {
    seqRef.current += 1
    setState({ status: 'idle' })
  }, [])

  const busy = state.status === 'uploading' || state.status === 'confirming'
  return { state, busy, actions: { upload, confirm, refreshList, reset } }
}
