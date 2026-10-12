/**
 * 管理员审计列表 hook(只读查询的状态机; 页面只消费状态与动作)。
 *
 * 竞态策略: 每次请求递增序号, 迟到响应直接丢弃。筛选变化使旧分页和旧
 * 请求失效(entries 置空重查); 翻页追加, 失败保留已加载行与筛选, 可重试。
 * 读取失败展示错误, 永不解释为空记录。
 */

import { useCallback, useRef, useState } from 'react'

import { errorMessage } from '../../../i18n'
import { queryAudit } from '../api'
import type { AppliedAuditQuery, AuditListState } from '../model'

export interface AdminAuditActions {
  /** 应用筛选(新查询; 旧分页与迟到请求失效)。 */
  search: (query: AppliedAuditQuery) => void
  /** 加载下一页(仅有 nextCursor 且非加载中时)。 */
  loadMore: () => void
  /** 重试当前查询(失败后; 保留筛选与已加载行)。 */
  retry: () => void
}

const EMPTY: AppliedAuditQuery = {}

export function useAdminAudit(): {
  state: AuditListState
  actions: AdminAuditActions
} {
  const [state, setState] = useState<AuditListState>({
    entries: null,
    nextCursor: null,
    loading: true,
    loadingMore: false,
    error: null,
    applied: null,
  })
  const stateRef = useRef(state)
  stateRef.current = state
  const seqRef = useRef(0)

  const runQuery = useCallback((query: AppliedAuditQuery, cursor: string | null, append: boolean) => {
    const seq = ++seqRef.current
    setState((prev) => ({
      ...prev,
      entries: append ? prev.entries : null,
      loading: !append,
      loadingMore: append,
      error: null,
      applied: query,
    }))
    void queryAudit(query, cursor).then(
      (page) => {
        if (seq !== seqRef.current) return
        setState((prev) => ({
          entries: append ? [...(prev.entries ?? []), ...page.items] : page.items,
          nextCursor: page.next_cursor,
          loading: false,
          loadingMore: false,
          error: null,
          applied: query,
        }))
      },
      (err: unknown) => {
        if (seq !== seqRef.current) return
        setState((prev) => ({
          ...prev,
          loading: false,
          loadingMore: false,
          error: errorMessage(err),
        }))
      },
    )
  }, [])

  const search = useCallback(
    (query: AppliedAuditQuery) => {
      runQuery(query, null, false)
    },
    [runQuery],
  )

  const loadMore = useCallback(() => {
    const current = stateRef.current
    // 失败后 nextCursor 可能属于旧查询, 必须先重试成功才能翻页。
    if (current.loading || current.loadingMore || current.error !== null) return
    if (current.nextCursor === null || current.applied === null) return
    runQuery(current.applied, current.nextCursor, true)
  }, [runQuery])

  const retry = useCallback(() => {
    const current = stateRef.current
    if (current.loading || current.loadingMore) return
    // 重试保留筛选; 已加载行在追加失败时保留, 首屏失败时 entries 仍为
    // 上次成功值或 null, 由调用方按 error/entries 展示。
    const keep = current.entries !== null && current.nextCursor !== null
    runQuery(current.applied ?? EMPTY, keep ? current.nextCursor : null, keep)
  }, [runQuery])

  return { state, actions: { search, loadMore, retry } }
}
