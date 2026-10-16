/**
 * 工作台共享上下文。
 *
 * 由 WorkbenchPage 挂载,ProjectPage(框架布局)与各子页面(Tasks/Results/Model/
 * Data/Config/Validation/Export)通过 useWorkbench() 读取项目信息、版本、币种、
 * 自动保存状态与离线状态(项目不声明时区; 偏移由数据集版本声明)。
 *
 * 子页面保存草稿时调用 useAutosave().setStatus(...) 通知工作台头部的状态指示;
 * 约定状态机: saving(保存中) -> saved(成功) / dirty(有未保存更改) / error(失败)。
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'

import { api } from '../api/client'
import { getTaskDetail, listTasks, loadTaskDraft } from '../features/tasks'
import { errorMessage, translateError } from '../i18n'
import type { ApiError, Project, ProjectVersion, Task, TaskDetail } from '../types'
import { selectDefaultTaskId, selectEvidencePackage, type SelectedEvidence } from '../lib/taskSelection'

/** 自动保存状态(工作台头部指示)。 */
export type AutosaveStatus = 'saved' | 'saving' | 'dirty' | 'error'

export interface WorkbenchValue {
  /** 当前项目 ID(来自路由 /projects/:id)。 */
  projectId: number
  /** 项目信息(加载中与加载失败均为 null, 以 projectLoading/projectError 区分)。 */
  project: Project | null
  /** 项目主体仍在加载(初始与每次重读期间为 true)。 */
  projectLoading: boolean
  /** 项目主体读取失败的真实错误(成功与加载中为 null)。 */
  projectError: string | null
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
  /** 全局任务选择(项目内各页面共享; null = 无显式选择, 走默认最新完成)。 */
  selectedTaskId: number | null
  /** 更新全局任务选择(仅记录选择, 不创建任务; 持久化到浏览器本地)。 */
  setSelectedTaskId: (taskId: number | null) => void
  /** 正在载入草稿的任务 id(载入期间请选择保持不变, 不并发二次载入)。 */
  draftLoadingTaskId: number | null
  /** 工作区读写协调(草稿保存与任务切换载入共享; 子页保存与本选择同序列)。 */
  workspaceCoord: WorkspaceCoord
  /**
   * 显式选择任务并将其冻结输入载入本项目可编辑草稿(工作区更新)。
   * loaded=true 表示确已载入, draftRevision 为新草稿修订(调用方据此等待
   * 草稿视图刷新落定后再读工作输入); error 非空表示可见失败且选择不变。
   * 取消选择(null)不载入; 同一任务重复选择、载入并发、项目初始化/
   * 重挂载的持久化恢复均不触发载入。不创建任务与项目版本。
   */
  selectTaskWithDraft: (
    taskId: number | null,
  ) => Promise<{ loaded: boolean; error: string | null; draftRevision: number | null }>
  /** 重新加载项目信息与版本列表(即发即忘, 不报告结果)。 */
  refresh: () => void
  /** 重新加载并等待结果: 任一失败在对应位置留空并返回错误文本, 不抛错。 */
  refreshDetailed: () => Promise<WorkbenchRefreshErrors>
}

/** 摘要刷新错误(null 表示该路成功; 恢复等流程用以区分已发生与刷新失败)。 */
export interface WorkbenchRefreshErrors {
  projectError: string | null
  versionsError: string | null
}

const WorkbenchContext = createContext<WorkbenchValue | null>(null)

/**
 * 工作区读写协调(草稿保存与任务切换载入共享, 同页/跨页同一序列)。
 *
 * switchSeq: 切换序列, 每次显式选择任务即递增; 保存执行前比对, 已被
 * 超越的保存(旧任务表单值)在写前丢弃, 不得落地覆盖新载入。
 * saveMutex: 保存互斥链尾; 选择载入前等待在途保存落定(旧值先落, 载入
 * 后覆盖, 顺序确定), 不得让旧值在载入后落地。
 */
export interface WorkspaceCoord {
  switchSeq: { current: number }
  saveMutex: { current: Promise<void> }
}

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

/** 固定 UTC 偏移(分钟, 由数据集版本声明)格式化为 "UTC+08:00" / "UTC-05:30"。 */
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
  const [projectLoading, setProjectLoading] = useState(true)
  const [projectError, setProjectError] = useState<string | null>(null)
  const [versions, setVersions] = useState<ProjectVersion[]>([])
  // 全局任务选择: 项目内任务/结果/导出页共享; 仅记录选择, 不创建任务。
  // 键沿用任务页既有持久化键, 浏览器关闭后重开恢复同一选择。
  const [selectedTaskId, setSelectedTaskIdState] = useState<number | null>(() => {
    try {
      const raw = window.localStorage.getItem(`iesplan.tasks.selected.${projectId}`)
      const v = raw ? Number(raw) : NaN
      return Number.isFinite(v) && v > 0 ? v : null
    } catch {
      return null
    }
  })
  const setSelectedTaskId = useCallback((taskId: number | null) => {
    setSelectedTaskIdState(taskId)
    try {
      const key = `iesplan.tasks.selected.${projectId}`
      if (taskId) window.localStorage.setItem(key, String(taskId))
      else window.localStorage.removeItem(key)
    } catch {
      // 隐私模式忽略
    }
  }, [projectId])
  // Provider 已按 projectId 键控重挂: 上方初始化器每次挂载按新项目键读取
  // 持久化选择, 前一项目的任务 id 不得泄漏到新项目, 无需第二个同步 effect。
  const [autosave, setAutosave] = useState<AutosaveStatus>('saved')
  const [offline, setOffline] = useState(() =>
    typeof navigator === 'undefined' ? false : !navigator.onLine,
  )

  // 摘要刷新序列(同实例内): 挂载双读/任务页双读/任务载入后刷新等同一项目
  // 的并发重读以后发起者为准, 先发起的迟到回包不得覆盖更新的发布(含
  // loading/error 位); 跨项目由 Provider 键控重挂在结构上隔离。
  const refreshSeqRef = useRef(0)
  const refreshDetailed = useCallback(async (): Promise<WorkbenchRefreshErrors> => {
    // 每次重读都更新三态: 加载中 → 成功/失败, 供消费者区分 null 的含义。
    const mySeq = refreshSeqRef.current + 1
    refreshSeqRef.current = mySeq
    setProjectLoading(true)
    setProjectError(null)
    const [projectSettled, versionsSettled] = await Promise.allSettled([
      api.projects.get(projectId),
      api.projects.versions(projectId),
    ])
    let nextProjectError: string | null = null
    let versionsError: string | null = null
    if (projectSettled.status === 'fulfilled') {
      if (refreshSeqRef.current === mySeq) setProject(projectSettled.value)
    } else {
      nextProjectError = errorMessage(projectSettled.reason)
      if (refreshSeqRef.current === mySeq) setProject(null)
    }
    if (versionsSettled.status === 'fulfilled') {
      if (refreshSeqRef.current === mySeq) {
        setVersions([...versionsSettled.value].sort((a, b) => a.version_no - b.version_no))
      }
    } else {
      versionsError = errorMessage(versionsSettled.reason)
      if (refreshSeqRef.current === mySeq) setVersions([])
    }
    // 被超越的刷新: 真实错误仍如实返回给调用方(调用方自有飞行检查决定),
    // 但不再触碰共享摘要/版本/loading 位, 避免旧回包覆盖新发布。
    if (refreshSeqRef.current !== mySeq) {
      return { projectError: nextProjectError, versionsError }
    }
    setProjectError(nextProjectError)
    setProjectLoading(false)
    return { projectError: nextProjectError, versionsError }
  }, [projectId])

  const refresh = useCallback(() => {
    void refreshDetailed()
  }, [refreshDetailed])

  const selectedRef = useRef<number | null>(null)
  selectedRef.current = selectedTaskId
  const [draftLoadingTaskId, setDraftLoadingTaskId] = useState<number | null>(null)
  const draftLoadingRef = useRef<number | null>(null)
  // 工作区读写协调: 保存互斥与切换序列归工作台持有, 各子页保存与
  // 本选择同序列(同页防抖/落定/卸载保存与跨页选择载入互斥有序)。
  const switchSeqRef = useRef(0)
  const saveMutexRef = useRef<Promise<void>>(Promise.resolve())
  const workspaceCoord = useMemo(
    (): WorkspaceCoord => ({ switchSeq: switchSeqRef, saveMutex: saveMutexRef }),
    [],
  )
  /**
   * 显式选择即载入(仅用户手势调用; 初始化/重挂载/程序跟随不调用,
   * 故重进页面不会用旧快照覆盖用户已做的草稿编辑)。
   */
  const selectTaskWithDraft = useCallback(
    async (
      taskId: number | null,
    ): Promise<{ loaded: boolean; error: string | null; draftRevision: number | null }> => {
      if (taskId === null) {
        // 取消选择不载入。若有载入在途, 同其他新选择一律忽略(由下分支),
        // 在途载入落定后发布其任务, 不复活已取消的意图。
        if (draftLoadingRef.current !== null) {
          return { loaded: false, error: null, draftRevision: null }
        }
        setSelectedTaskId(null)
        return { loaded: false, error: null, draftRevision: null }
      }
      // 单一在途载入: 载入期间的任何新选择(含其他任务)一律忽略——不递增
      // 序列、不发起载入、不报错。调用方(任务行/结果选择器/直达初始化)在
      // 发起前同样按忙态阻止, 此处是共享边界的决定性 enforcement: 响应
      // 顺序检查无法撤销已提交的写, 故不允许第二份载入与第一份交错提交。
      if (draftLoadingRef.current !== null) {
        return { loaded: false, error: null, draftRevision: null }
      }
      if (taskId === selectedRef.current) {
        // 同任务重复选择: 无操作(调用方不得为此清计时器/脏态)。
        return { loaded: false, error: null, draftRevision: null }
      }
      // 与保存同序列: 递增序列作废已排队/在途的旧保存, 等其落定(旧值
      // 先落), 再载入并覆盖, 顺序确定; 旧值不得在载入后落地。
      switchSeqRef.current += 1
      draftLoadingRef.current = taskId
      setDraftLoadingTaskId(taskId)
      await saveMutexRef.current
      try {
        const loaded = await loadTaskDraft(projectId, taskId)
        setSelectedTaskId(taskId)
        // 草稿 revision 已变化, 刷新项目摘要使各编辑页读到最新; 刷新失败
        // 由摘要错误位可见, 不回滚已成功的载入(草稿权威在服务端)。
        await refreshDetailed()
        return { loaded: true, error: null, draftRevision: loaded.draftRevision }
      } catch (err) {
        return { loaded: false, error: errorMessage(err), draftRevision: null }
      } finally {
        draftLoadingRef.current = null
        setDraftLoadingTaskId(null)
      }
    },
    [projectId, refreshDetailed, setSelectedTaskId],
  )

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
      projectLoading,
      projectError,
      versions,
      currentVersion,
      offline,
      autosave,
      setAutosave,
      selectedTaskId,
      setSelectedTaskId,
      draftLoadingTaskId,
      workspaceCoord,
      selectTaskWithDraft,
      refresh,
      refreshDetailed,
    }),
    [projectId, project, projectLoading, projectError, versions, currentVersion, offline, autosave, refresh, refreshDetailed, selectedTaskId, setSelectedTaskId, draftLoadingTaskId, workspaceCoord, selectTaskWithDraft],
  )

  return <WorkbenchContext.Provider value={value}>{children}</WorkbenchContext.Provider>
}

/** 有效任务上下文(结果页与导出页同一规则、同一来源消费)。 */
export interface EffectiveTaskContext {
  /** 读取中(初始与每次重解期间为 true)。 */
  loading: boolean
  /** 读取失败的真实错误(成功与加载中为 null; 失败即整组失败, 不缩水)。 */
  error: string | null
  /** 重试: 仅重读列表与详情, 不发任务/评估 POST。 */
  retry: () => void
  /** 已读取的任务详情(显式任务精确读取 + 默认候选精确读取各一)。 */
  details: TaskDetail[]
  /** 走完全部 completed 历史分页的任务列表项(默认排序与选择器选项的来源)。 */
  history: Task[]
  /** 生效选择(null = 无显式选择时走默认; 解析见 lib/taskSelection)。 */
  explicitTaskId: number | null
  /** 绑定的任务 + 证据包; 无可用结果为 null(永不回退他任务)。 */
  picked: SelectedEvidence | null
  /** 显式选择存在但无可用结果(未完成/无证据/未知)。 */
  explicitBlocked: boolean
}

/** 默认候选计入的计算类任务类型(与既有默认口径一致)。 */
function isDefaultCandidateType(type: string): boolean {
  return type === 'calc' || type === 'optimization' || type === 'uncertainty'
}

/**
 * 有效任务上下文钩子(结果/导出页共享)。
 *
 * - 只读任务列表与详情, 不创建任务、不发评估 POST;
 * - 显式任务按精确 id 独立读取, 不依赖候选窗口(404 即未知选择, 只阻塞
 *   不整组失败; 其他读取失败整组失败并可重试);
 * - 默认取走完全部 completed 历史分页后完成顺序最晚的已完成计算, 其无
 *   结果即无结果, 不截断窗口、不回退;
 * - 显式选择只绑定该任务。
 *
 * @param explicitOverride 显式任务 id(省略即用全局选择; -1 等未知 id 视为
 *   不可解析的显式选择, 不读详情、不回退默认)。
 */
export function useEffectiveTaskContext(explicitOverride?: number | null): EffectiveTaskContext {
  const { projectId, selectedTaskId } = useWorkbench()
  const explicitTaskId = explicitOverride === undefined ? selectedTaskId : explicitOverride
  const [details, setDetails] = useState<TaskDetail[]>([])
  const [history, setHistory] = useState<Task[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      setLoading(true)
      setError(null)
      setDetails([])
      setHistory([])
      try {
        // 显式任务精确读取(未知 id 不读)。
        let explicit: TaskDetail | null = null
        if (explicitTaskId !== null && explicitTaskId >= 0) {
          try {
            explicit = await getTaskDetail(projectId, explicitTaskId)
          } catch (err) {
            // 404 = 选择指向未知任务(已删/跨项目): 只阻塞该选择,
            // 不当整组读取失败; 其余错误整组失败。
            if (!(err instanceof Error && 'status' in err && (err as ApiError).status === 404)) {
              throw err
            }
            explicit = null
          }
          if (cancelled) return
        }
        // 默认候选走完全部 completed 历史分页(游标跟随到底, 不截断)。
        const walked: Task[] = []
        let cursor: string | undefined
        for (;;) {
          const page = await listTasks({
            project_id: projectId,
            status: 'completed',
            limit: 100,
            cursor,
          })
          if (cancelled) return
          walked.push(...page.items)
          if (page.next_cursor === null) break
          cursor = page.next_cursor
        }
        if (cancelled) return
        setHistory(walked)
        // 默认详情只读胜出者(无显式选择时); 有显式选择不取默认详情。
        let winner: TaskDetail | null = null
        if (explicitTaskId === null) {
          const winnerId = selectDefaultTaskId(
            walked.filter((tk) => isDefaultCandidateType(tk.type)),
          )
          if (winnerId !== null) {
            winner = await getTaskDetail(projectId, winnerId)
            if (cancelled) return
          }
        }
        const next: TaskDetail[] = []
        if (explicit) next.push(explicit)
        if (winner && winner.id !== explicit?.id) next.push(winner)
        setDetails(next)
      } catch (err) {
        if (cancelled) return
        setDetails([])
        setHistory([])
        setError(translateError(err as ApiError))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [projectId, nonce, explicitTaskId])

  const picked = selectEvidencePackage(details, explicitTaskId)
  return {
    loading,
    error,
    retry: () => setNonce((n) => n + 1),
    details,
    history,
    explicitTaskId,
    picked,
    explicitBlocked: explicitTaskId !== null && picked === null,
  }
}
