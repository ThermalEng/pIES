/**
 * 项目版本 feature hook: 列表加载 / 创建提交 / 详情加载。
 *
 * - 列表/详情失败必须可见(展示后端错误, 不回退成“尚无版本”);
 *   仅当请求成功且数组为空时才展示空态。
 * - 创建提交中禁用重复点击; 失败保留用户输入并展示后端错误。
 * - 创建成功后调用 onMutated(页面传入 workbench refresh, 刷新项目
 *   current_version 与列表, 旧版本不消失)。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { errorMessage } from '../../../i18n'
import type { VersionCreateRequestDto } from '../contracts'
import { createVersion, getVersion, listVersions } from '../api'
import type { VersionDetailState, VersionModel } from '../model'

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
}

export function useProjectVersions(projectId: number, onMutated: () => void): UseProjectVersions {
  const [versions, setVersions] = useState<VersionModel[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [reloading, setReloading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [detail, setDetail] = useState<VersionDetailState>({ status: 'idle' })
  const mutatedRef = useRef(onMutated)
  mutatedRef.current = onMutated

  const reload = useCallback(() => {
    setReloading(true)
    setLoadError(null)
    listVersions(projectId).then(
      (list) => {
        setVersions(list)
        setReloading(false)
      },
      (err: unknown) => {
        // 失败必须可见: 保持 versions=null, 不伪装空列表。
        setLoadError(errorMessage(err))
        setReloading(false)
      },
    )
  }, [projectId])

  useEffect(() => {
    reload()
  }, [reload])

  const submitCreate = useCallback(
    async (dto: VersionCreateRequestDto): Promise<VersionModel | null> => {
      if (creating) return null
      setCreating(true)
      setCreateError(null)
      try {
        const created = await createVersion(projectId, dto)
        mutatedRef.current()
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
      setDetail({ status: 'loading', versionId })
      getVersion(projectId, versionId).then(
        (version) => setDetail({ status: 'loaded', version }),
        (err: unknown) =>
          setDetail({
            status: 'failed',
            versionId,
            message: errorMessage(err),
          }),
      )
    },
    [projectId],
  )

  const closeDetail = useCallback(() => setDetail({ status: 'idle' }), [])

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
  }
}
