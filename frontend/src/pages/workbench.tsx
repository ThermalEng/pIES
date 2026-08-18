/**
 * 工作台共享上下文。
 *
 * 由 WorkbenchPage 挂载,ProjectPage(框架布局)与各子页面(Tasks/Results/Model/
 * Data/Config/Validation/Export)通过 useWorkbench() 读取项目信息、版本、币种、
 * UTC 偏移、自动保存状态与离线状态。
 *
 * 子页面保存草稿时调用 useAutosave().setStatus(...) 通知工作台头部的状态指示;
 * 约定状态机: saving(保存中) -> saved(成功) / dirty(有未保存更改) / error(失败)。
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

import { api } from '../api/client'
import type { Project, ProjectVersion } from '../types'

/** 自动保存状态(工作台头部指示)。 */
export type AutosaveStatus = 'saved' | 'saving' | 'dirty' | 'error'

export interface WorkbenchValue {
  /** 当前项目 ID(来自路由 /projects/:id)。 */
  projectId: number
  /** 项目信息(加载失败为 null)。 */
  project: Project | null
  /** 版本列表(按 version_no 升序)。 */
  versions: ProjectVersion[]
  /** 当前版本(最新版本号)。 */
  currentVersion: ProjectVersion | null
  /** 是否离线(navigator.onLine)。 */
  offline: boolean
  /** 自动保存状态。 */
  autosave: AutosaveStatus
  /** 更新自动保存状态(由子页面保存草稿时调用)。 */
  setAutosave: (status: AutosaveStatus) => void
  /** 重新加载项目信息与版本列表。 */
  refresh: () => void
}

const WorkbenchContext = createContext<WorkbenchValue | null>(null)

/** 在 WorkbenchProvider 内获取工作台上下文。 */
export function useWorkbench(): WorkbenchValue {
  const value = useContext(WorkbenchContext)
  if (!value) {
    throw new Error('useWorkbench 必须在 WorkbenchProvider 内使用')
  }
  return value
}

/** 自动保存状态便捷钩子(子页面保存草稿时使用)。 */
export function useAutosave(): { status: AutosaveStatus; setStatus: (status: AutosaveStatus) => void } {
  const { autosave, setAutosave } = useWorkbench()
  return { status: autosave, setStatus: setAutosave }
}

/** 项目固定 UTC 偏移(分钟)格式化为 "UTC+08:00" / "UTC-05:30"。 */
export function formatUtcOffset(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || Number.isNaN(minutes)) return 'UTC'
  const sign = minutes < 0 ? '-' : '+'
  const abs = Math.abs(Math.round(minutes))
  const h = Math.floor(abs / 60)
  const m = abs % 60
  return `UTC${sign}${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
}

/**
 * 工作台上下文 Provider:加载项目信息与版本列表,跟踪在线/离线状态,
 * 持有自动保存状态(由子页面通过 setAutosave 更新)。
 */
export function WorkbenchProvider({ projectId, children }: { projectId: number; children: ReactNode }) {
  const [project, setProject] = useState<Project | null>(null)
  const [versions, setVersions] = useState<ProjectVersion[]>([])
  const [autosave, setAutosave] = useState<AutosaveStatus>('saved')
  const [offline, setOffline] = useState(() =>
    typeof navigator === 'undefined' ? false : !navigator.onLine,
  )

  const refresh = useCallback(() => {
    api.projects
      .get(projectId)
      .then(setProject)
      .catch(() => setProject(null))
    api.projects
      .versions(projectId)
      .then((list) => setVersions([...list].sort((a, b) => a.version_no - b.version_no)))
      .catch(() => setVersions([]))
  }, [projectId])

  useEffect(() => {
    refresh()
  }, [refresh])

  useEffect(() => {
    const goOnline = () => setOffline(false)
    const goOffline = () => setOffline(true)
    window.addEventListener('online', goOnline)
    window.addEventListener('offline', goOffline)
    return () => {
      window.removeEventListener('online', goOnline)
      window.removeEventListener('offline', goOffline)
    }
  }, [])

  const currentVersion = versions.length > 0 ? versions[versions.length - 1] : null

  const value = useMemo<WorkbenchValue>(
    () => ({
      projectId,
      project,
      versions,
      currentVersion,
      offline,
      autosave,
      setAutosave,
      refresh,
    }),
    [projectId, project, versions, currentVersion, offline, autosave, refresh],
  )

  return <WorkbenchContext.Provider value={value}>{children}</WorkbenchContext.Provider>
}
