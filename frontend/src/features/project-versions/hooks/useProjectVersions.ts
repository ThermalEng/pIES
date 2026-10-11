/**
 * 项目版本 feature hook: 列表加载 / 创建提交 / 详情加载。
 *
 * - 列表/详情失败必须可见(展示后端错误, 不回退成“尚无版本”);
 *   仅当请求成功且数组为空时才展示空态。
 * - 创建提交中禁用重复点击; 失败保留用户输入并展示后端错误。
 * - 创建成功后调用 refreshSummary(页面传入 workbench refreshDetailed),
 *   刷新项目 current_version 与列表, 旧版本不消失。
 * - 时序守卫(本地请求标识, 非数据校验): 关闭/新选择/项目切换即作废旧
 *   详情请求, 迟到成功/失败回调不写回; 旧项目列表响应不覆盖新项目。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { errorMessage } from '../../../i18n'
import type { VersionCreateRequestDto } from '../contracts'
import { createVersion, getVersion, listVersions, restoreVersion } from '../api'
import type {
  SummaryRefreshErrors,
  VersionDetailState,
  VersionModel,
  VersionRestoreState,
} from '../model'

export interface UseProjectVersions {
  versions: VersionModel[] | null
  loadError: string | null
  reloading: boolean
  reload: () => void
  creating: boolean
  createError: string | null
  submitCreate: (dto: VersionCreateRequestDto) => Promise<VersionModel | null>
  clearCreateError: () => void
  detail: VersionDetailState
  openDetail: (versionId: number) => void
  closeDetail: () => void
  restore: VersionRestoreState
  /** 打开恢复确认(仅展示影响, 不发送请求)。 */
  requestRestore: (version: VersionModel) => void
  /** 取消恢复确认(不发送请求, 保留选择上下文由调用方持有)。 */
  cancelRestore: () => void
  /** 确认恢复(处理中防重复; 成功后刷新列表与项目摘要)。 */
  confirmRestore: () => Promise<void>
  closeRestore: () => void
}

export function useProjectVersions(
  projectId: number,
  refreshSummary: () => Promise<SummaryRefreshErrors>,
): UseProjectVersions {
  const [versions, setVersions] = useState<VersionModel[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [reloading, setReloading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [detail, setDetail] = useState<VersionDetailState>({ status: 'idle' })
  // 当前项目标识: 列表回调仅当响应仍属于当前项目才写回, 旧项目迟到响应丢弃。
  const projectRef = useRef(projectId)
  projectRef.current = projectId
  // 当前详情请求标识: 关闭或新选择即递增, 旧请求成功/失败回调失效。
  const detailReq = useRef(0)

  const reload = useCallback(() => {
    const pid = projectId
    setReloading(true)
    setLoadError(null)
    listVersions(pid).then(
      (list) => {
        if (projectRef.current !== pid) return
        setVersions(list)
        setReloading(false)
      },
      (err: unknown) => {
        if (projectRef.current !== pid) return
        // 失败必须可见: 保持 versions=null, 不伪装空列表。
        setLoadError(errorMessage(err))
        setReloading(false)
      },
    )
  }, [projectId])

  useEffect(() => {
    reload()
  }, [reload])

  // 项目切换时清理当前详情及旧请求, 不显示其他项目的详情。
  useEffect(() => {
    detailReq.current += 1
    setDetail({ status: 'idle' })
  }, [projectId])

  const submitCreate = useCallback(
    async (dto: VersionCreateRequestDto): Promise<VersionModel | null> => {
      if (creating) return null
      setCreating(true)
      setCreateError(null)
      try {
        const created = await createVersion(projectId, dto)
        void summaryRef.current()
        reload()
        return created
      } catch (err: unknown) {
        setCreateError(errorMessage(err))
        return null
      } finally {
        setCreating(false)
      }
    },
    [creating, projectId, reload],
  )

  const clearCreateError = useCallback(() => setCreateError(null), [])

  const openDetail = useCallback(
    (versionId: number) => {
      const req = detailReq.current + 1
      detailReq.current = req
      setDetail({ status: 'loading', versionId })
      getVersion(projectId, versionId).then(
        (version) => {
          if (detailReq.current !== req) return
          setDetail({ status: 'loaded', version })
        },
        (err: unknown) => {
          if (detailReq.current !== req) return
          setDetail({
            status: 'failed',
            versionId,
            message: errorMessage(err),
          })
        },
      )
    },
    [projectId],
  )

  const closeDetail = useCallback(() => {
    // 关闭即作废当前请求: 迟到成功/失败回调不再重开对话框。
    detailReq.current += 1
    setDetail({ status: 'idle' })
  }, [])

  const summaryRef = useRef(refreshSummary)
  summaryRef.current = refreshSummary

  const [restore, setRestore] = useState<VersionRestoreState>({ status: 'idle' })

  const requestRestore = useCallback((version: VersionModel) => {
    setRestore({ status: 'confirming', version })
  }, [])

  const cancelRestore = useCallback(() => {
    // 取消不发送任何请求。
    setRestore({ status: 'idle' })
  }, [])

  const closeRestore = useCallback(() => {
    setRestore({ status: 'idle' })
  }, [])

  const confirmRestore = useCallback(async () => {
    if (restore.status !== 'confirming' && restore.status !== 'failed') return
    const version = restore.version
    setRestore({ status: 'working', version })
    try {
      const restored = await restoreVersion(projectId, version.id)
      // 恢复已发生; 等待必要摘要刷新, 任一失败明确展示, 不诱导再提交。
      const listPromise = listVersions(projectId).then(
        (list) => ({ ok: true as const, list }),
        (err: unknown) => ({ ok: false as const, message: errorMessage(err) }),
      )
      const [summary, listed] = await Promise.all([summaryRef.current(), listPromise])
      if (projectRef.current !== projectId) return
      const errors: string[] = []
      if (summary.projectError !== null) errors.push(summary.projectError)
      if (summary.versionsError !== null) errors.push(summary.versionsError)
      if (listed.ok) {
        setVersions(listed.list)
        setLoadError(null)
        setReloading(false)
      } else {
        errors.push(listed.message)
      }
      setRestore({
        status: 'done',
        version,
        restored,
        refreshError: errors.length > 0 ? errors.join('\n') : null,
      })
    } catch (err: unknown) {
      // 失败保留所选版本上下文, 可重试; 不丢选择。
      setRestore({ status: 'failed', version, message: errorMessage(err) })
    }
  }, [projectId, restore])

  return {
    versions,
    loadError,
    reloading,
    reload,
    creating,
    createError,
    submitCreate,
    clearCreateError,
    detail,
    openDetail,
    closeDetail,
    restore,
    requestRestore,
    cancelRestore,
    confirmRestore,
    closeRestore,
  }
}
