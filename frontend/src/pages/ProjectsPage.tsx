/**
 * 项目列表页(/):展示当前用户参与的项目(所有者/查看者)及其状态,
 * 支持新建、打开、归档/撤销归档、复制、删除(明确确认)、转移所有权、管理查看者。
 *
 * 权限规则(后端仍会权威校验,此处仅为可用性提示):
 * - 查看者(role === 'viewer')仅可打开项目;其余操作禁用并显示原因
 *   (ies.project.owner_only),并附在按钮 title 上供悬停/读屏获取。
 * - 归档/删除/转移所有权/管理查看者/复制仅所有者可执行。
 *
 * 键盘可达性:
 * - 列表行可 Tab 聚焦,Enter / Space 打开项目(行内按钮独立聚焦)。
 * - 全部操作按钮为原生 button,天然可聚焦;对话框支持 Esc 关闭与焦点圈定。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, MouseEvent as ReactMouseEvent } from 'react'
import { useNavigate } from 'react-router-dom'

import { api } from '../api/client'
import { translateError, useI18n } from '../i18n'
import { formatDateTime, formatRelativeTime } from '../lib/format'
import { formatUtcOffset } from './workbench'
import {
  Alert,
  Badge,
  Button,
  Dialog,
  EmptyState,
  FormField,
  Icon,
  Input,
  ProjectStatusBadge,
  Select,
  Spinner,
  Table,
  TBody,
  TD,
  TH,
  THead,
  TR,
  TaskOutcomeBadge,
  TaskStatusBadge,
} from '../components/ui'
import { ApiError } from '../types'
import type { AdminUserRow, Currency, Project, ProjectMember, ProjectListParams, Task } from '../types'

type StatusFilter = 'all' | 'active' | 'archived'
type RowOp = 'archive' | 'unarchive' | 'duplicate' | 'delete' | 'transfer' | 'viewers'

interface Notice {
  kind: 'success' | 'error'
  text: string
}

const PAGE_SIZE = 50

/** 常用 UTC 偏移(小时,-12:00 ~ +14:00),创建对话框候选值;默认 +08:00(480 分钟)。 */
const UTC_OFFSET_HOURS: number[] = Array.from({ length: 27 }, (_, i) => i - 12)

export default function ProjectsPage() {
  const { t } = useI18n()
  const navigate = useNavigate()

  // 列表
  const [projects, setProjects] = useState<Project[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [latestTasks, setLatestTasks] = useState<Record<number, Task | null>>({})
  const [busy, setBusy] = useState<{ id: number; op: RowOp } | null>(null)

  // 新建项目对话框
  const [createOpen, setCreateOpen] = useState(false)
  const [createName, setCreateName] = useState('')
  const [createCurrency, setCreateCurrency] = useState<Currency>('CNY')
  const [createOffset, setCreateOffset] = useState(480)
  const [createNameError, setCreateNameError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  // 归档 / 删除确认对话框
  const [archiveTarget, setArchiveTarget] = useState<Project | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null)

  // 转移所有权对话框
  const [transferTarget, setTransferTarget] = useState<Project | null>(null)
  const [transferUsername, setTransferUsername] = useState('')
  const [transferUsers, setTransferUsers] = useState<AdminUserRow[]>([])
  const [transferError, setTransferError] = useState<string | null>(null)
  const [transferring, setTransferring] = useState(false)

  // 管理查看者对话框
  const [viewersTarget, setViewersTarget] = useState<Project | null>(null)
  const [viewersMembers, setViewersMembers] = useState<ProjectMember[]>([])
  const [viewersLoading, setViewersLoading] = useState(false)
  const [viewersError, setViewersError] = useState<string | null>(null)
  const [newViewerName, setNewViewerName] = useState('')
  const [viewerSubmitting, setViewerSubmitting] = useState<'add' | 'remove' | null>(null)
  const [removeTarget, setRemoveTarget] = useState<ProjectMember | null>(null)

  /** 请求序号:丢弃过期响应(切换筛选/并发刷新时)。 */
  const seqRef = useRef(0)

  // -------------------------------------------------------------------------
  // 列表加载
  // -------------------------------------------------------------------------

  const loadLatestTasks = async (items: Project[]) => {
    if (items.length === 0) return
    const results = await Promise.allSettled(items.map((p) => api.tasks.list({ project_id: p.id, limit: 1 })))
    setLatestTasks((prev) => {
      const next = { ...prev }
      items.forEach((p, i) => {
        const r = results[i]
        if (r.status === 'fulfilled') next[p.id] = r.value.items[0] ?? null
      })
      return next
    })
  }

  const loadProjects = async (append = false, silent = false) => {
    if (!silent) setLoading(true)
    if (append) setLoadingMore(true)
    setLoadError(null)
    const seq = ++seqRef.current
    try {
      const params: ProjectListParams = { limit: PAGE_SIZE }
      if (statusFilter !== 'all') params.status = statusFilter
      if (append && nextCursor) params.cursor = nextCursor
      const page = await api.projects.list(params)
      if (seq !== seqRef.current) return
      setProjects((prev) => {
        const merged = append ? [...prev, ...page.items] : page.items
        const seen = new Map<number, Project>()
        for (const item of merged) seen.set(item.id, item)
        return Array.from(seen.values())
      })
      setNextCursor(page.next_cursor)
      void loadLatestTasks(page.items)
    } catch (err) {
      if (seq !== seqRef.current) return
      setLoadError(translateError(err as ApiError))
    } finally {
      if (seq === seqRef.current) {
        setLoading(false)
        setLoadingMore(false)
      }
    }
  }

  useEffect(() => {
    void loadProjects(false)
  }, [statusFilter])

  // 成功/失败提示自动消失
  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(null), 5000)
    return () => window.clearTimeout(timer)
  }, [notice])

  // -------------------------------------------------------------------------
  // 行操作(每个操作成功后刷新列表)
  // -------------------------------------------------------------------------

  const runOp = async (
    op: RowOp,
    project: Project,
    fn: () => Promise<unknown>,
    okKey: string,
  ): Promise<boolean> => {
    setBusy({ id: project.id, op })
    try {
      await fn()
      setNotice({ kind: 'success', text: t(okKey) })
      await loadProjects(false, true)
      return true
    } catch (err) {
      setNotice({ kind: 'error', text: translateError(err as ApiError) })
      return false
    } finally {
      setBusy(null)
    }
  }

  const openProject = (project: Project) => {
    void navigate(`/projects/${project.id}`)
  }

  const handleArchiveConfirm = async () => {
    const target = archiveTarget
    if (!target) return
    const ok = await runOp('archive', target, () => api.projects.archive(target.id), 'ies.project.archive_ok')
    if (ok) setArchiveTarget(null)
  }

  const handleDeleteConfirm = async () => {
    const target = deleteTarget
    if (!target) return
    const ok = await runOp('delete', target, () => api.projects.delete(target.id), 'ies.project.delete_ok')
    if (ok) setDeleteTarget(null)
  }

  // -------------------------------------------------------------------------
  // 新建项目
  // -------------------------------------------------------------------------

  const handleCreateSubmit = async () => {
    const name = createName.trim()
    if (!name) {
      setCreateNameError(t('ies.project.name_required'))
      return
    }
    setCreateNameError(null)
    setCreating(true)
    try {
      await api.projects.create({
        name,
        currency: createCurrency,
        fixed_utc_offset_minutes: createOffset,
      })
      setCreateOpen(false)
      setCreateName('')
      setCreateCurrency('CNY')
      setCreateOffset(480)
      setNotice({ kind: 'success', text: t('ies.project.create_ok') })
      await loadProjects(false, true)
    } catch (err) {
      setNotice({ kind: 'error', text: translateError(err as ApiError) })
    } finally {
      setCreating(false)
    }
  }

  // -------------------------------------------------------------------------
  // 转移所有权
  // -------------------------------------------------------------------------

  const openTransfer = async (project: Project) => {
    setTransferTarget(project)
    setTransferUsername('')
    setTransferError(null)
    setTransferUsers([])
    try {
      // 有管理员接口权限时列出工程师供选择;无权限(403)时退化为手输用户名
      const page = await api.admin.users({ status: 'active', limit: 200 })
      setTransferUsers(page.items)
    } catch {
      setTransferUsers([])
    }
  }

  const handleTransferSubmit = async () => {
    const target = transferTarget
    if (!target) return
    const username = transferUsername.trim()
    if (!username) {
      setTransferError(t('ies.project.username_required'))
      return
    }
    // 后端按用户 id 转移(target_user_id): 从管理员用户清单解析用户名 → id
    const user = transferUsers.find((u) => u.username === username)
    if (!user) {
      setTransferError(t('ies.project.transfer_unknown_user', { username }))
      return
    }
    setTransferring(true)
    try {
      await api.projects.transfer(target.id, { target_user_id: user.id })
      setTransferTarget(null)
      setNotice({ kind: 'success', text: t('ies.project.transfer_ok') })
      await loadProjects(false, true)
    } catch (err) {
      setTransferError(translateError(err as ApiError))
    } finally {
      setTransferring(false)
    }
  }

  // -------------------------------------------------------------------------
  // 管理查看者
  // -------------------------------------------------------------------------

  const refreshViewers = async (projectId: number) => {
    setViewersLoading(true)
    setViewersError(null)
    try {
      setViewersMembers(await api.projects.viewers(projectId))
    } catch (err) {
      setViewersError(translateError(err as ApiError))
    } finally {
      setViewersLoading(false)
    }
  }

  const openViewers = async (project: Project) => {
    setViewersTarget(project)
    setViewersMembers([])
    setViewersError(null)
    setRemoveTarget(null)
    setNewViewerName('')
    await refreshViewers(project.id)
  }

  const handleAddViewer = async () => {
    const target = viewersTarget
    if (!target) return
    const username = newViewerName.trim()
    if (!username) return
    setViewerSubmitting('add')
    try {
      // 后端查看者接口按用户 id 操作: 先由管理员用户清单解析用户名 → id
      const page = await api.admin.users({ limit: 1000 })
      const user = page.items.find((u) => u.username === username)
      if (!user) throw new ApiError(400, null, 'ies.project.viewer_not_found')
      await api.projects.updateViewer(target.id, { user_id: user.id, action: 'add' })
      setNewViewerName('')
      setNotice({ kind: 'success', text: t('ies.project.viewer_added', { username }) })
      await refreshViewers(target.id)
    } catch (err) {
      setViewersError(translateError(err as ApiError))
    } finally {
      setViewerSubmitting(null)
    }
  }

  const handleRemoveViewer = async (member: ProjectMember) => {
    const target = viewersTarget
    if (!target) return
    setViewerSubmitting('remove')
    try {
      await api.projects.updateViewer(target.id, { user_id: member.user_id, action: 'remove' })
      setRemoveTarget(null)
      setNotice({ kind: 'success', text: t('ies.project.viewer_removed', { username: member.username }) })
      await refreshViewers(target.id)
    } catch (err) {
      setViewersError(translateError(err as ApiError))
    } finally {
      setViewerSubmitting(null)
    }
  }

  // -------------------------------------------------------------------------
  // 键盘可达性
  // -------------------------------------------------------------------------

  /** 行内 Enter / Space 打开项目(忽略行内按钮上冒泡的键盘事件)。 */
  const handleRowKeyDown = (event: ReactKeyboardEvent<HTMLTableRowElement>, project: Project) => {
    if (event.target !== event.currentTarget) return
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      openProject(project)
    }
  }

  /** 阻止行内按钮点击冒泡触发整行打开。 */
  const stopRow = (event: ReactMouseEvent) => {
    event.stopPropagation()
  }

  const sortedProjects = useMemo(() => {
    return [...projects].sort((a, b) => {
      const at = a.updated_at ?? a.created_at
      const bt = b.updated_at ?? b.created_at
      return bt.localeCompare(at)
    })
  }, [projects])

  const canManage = (project: Project) => project.role !== 'viewer'

  // -------------------------------------------------------------------------
  // 渲染
  // -------------------------------------------------------------------------

  const ownerOnly = t('ies.project.owner_only')

  return (
    <div className="ies-content--wide">
      <div className="ies-page-header">
        <h1 className="ies-page-title">{t('ies.nav.projects')}</h1>
        <Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)}>
          {t('ies.project.new')}
        </Button>
      </div>

      {notice ? (
        <div className="ies-projects__notice">
          <Alert variant={notice.kind} closable onClose={() => setNotice(null)}>
            {notice.text}
          </Alert>
        </div>
      ) : null}

      {loadError ? (
        <div className="ies-projects__notice">
          <Alert
            variant="error"
            title={t('ies.common.load_failed', { reason: loadError })}
            closable
            onClose={() => setLoadError(null)}
          >
            <Button variant="secondary" size="sm" onClick={() => void loadProjects(false)}>
              {t('ies.common.retry')}
            </Button>
          </Alert>
        </div>
      ) : null}

      <div className="ies-projects__toolbar" role="group" aria-label={t('ies.common.filter')}>
        {(
          [
            ['all', t('ies.common.all')],
            ['active', t('ies.project.status_active')],
            ['archived', t('ies.project.status_archived')],
          ] as const
        ).map(([value, label]) => (
          <Button
            key={value}
            variant={statusFilter === value ? 'primary' : 'ghost'}
            size="sm"
            aria-pressed={statusFilter === value}
            onClick={() => setStatusFilter(value)}
          >
            {label}
          </Button>
        ))}
      </div>

      {loading ? (
        <div className="ies-projects__loading" role="status">
          <Spinner label={t('ies.common.loading')} />
        </div>
      ) : sortedProjects.length === 0 ? (
        <EmptyState
          icon="info"
          title={t('ies.project.empty')}
          action={
            <Button variant="primary" icon="plus" onClick={() => setCreateOpen(true)}>
              {t('ies.project.new')}
            </Button>
          }
        />
      ) : (
        <Table>
          <THead>
            <TR>
              <TH>{t('ies.common.name')}</TH>
              <TH>{t('ies.project.role')}</TH>
              <TH>{t('ies.common.status')}</TH>
              <TH>{t('ies.common.updated_at')}</TH>
              <TH>{t('ies.project.latest_task')}</TH>
              <TH>{t('ies.common.actions')}</TH>
            </TR>
          </THead>
          <TBody>
            {sortedProjects.map((project) => {
              const viewer = !canManage(project)
              const archived = project.status === 'archived'
              const rowBusy = busy?.id === project.id
              const latest = latestTasks[project.id]
              return (
                <TR
                  key={project.id}
                  className="ies-projects__row"
                  tabIndex={0}
                  onClick={() => openProject(project)}
                  onKeyDown={(event) => handleRowKeyDown(event, project)}
                  aria-label={t('ies.project.open_project', { name: project.name })}
                >
                  <TD>
                    <div className="ies-projects__name">{project.name}</div>
                    {project.description ? (
                      <div className="ies-projects__desc">{project.description}</div>
                    ) : null}
                  </TD>
                  <TD>
                    {viewer ? (
                      <Badge label={t('ies.project.viewer_role')} variant="neutral" icon="info" />
                    ) : (
                      <Badge label={t('ies.project.owner_role')} variant="primary" icon="check" />
                    )}
                  </TD>
                  <TD>
                    <ProjectStatusBadge status={project.status} />
                  </TD>
                  <TD>
                    <span title={formatDateTime(project.updated_at ?? project.created_at)}>
                      {formatRelativeTime(project.updated_at ?? project.created_at)}
                    </span>
                  </TD>
                  <TD>
                    {latest === undefined ? (
                      '—'
                    ) : latest === null ? (
                      <span className="ies-projects__muted">{t('ies.project.no_tasks')}</span>
                    ) : (
                      <div className="ies-projects__task-cell">
                        <TaskStatusBadge status={latest.status} />
                        <TaskOutcomeBadge outcome={latest.business_outcome} />
                      </div>
                    )}
                  </TD>
                  <TD>
                    <div className="ies-projects__actions" onClick={stopRow}>
                      {viewer ? <span className="ies-projects__perm-note">{ownerOnly}</span> : null}
                      <Button size="sm" variant="ghost" onClick={() => openProject(project)}>
                        {t('ies.project.open')}
                      </Button>
                      {archived ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={viewer || rowBusy}
                          title={viewer ? ownerOnly : undefined}
                          loading={rowBusy && busy?.op === 'unarchive'}
                          onClick={() =>
                            void runOp('unarchive', project, () => api.projects.unarchive(project.id), 'ies.project.unarchive_ok')
                          }
                        >
                          {t('ies.project.unarchive')}
                        </Button>
                      ) : (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={viewer || rowBusy}
                          title={viewer ? ownerOnly : undefined}
                          loading={rowBusy && busy?.op === 'archive'}
                          onClick={() => setArchiveTarget(project)}
                        >
                          {t('ies.project.archive')}
                        </Button>
                      )}
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={viewer || rowBusy}
                        title={viewer ? ownerOnly : undefined}
                        loading={rowBusy && busy?.op === 'duplicate'}
                        onClick={() =>
                          void runOp('duplicate', project, () => api.projects.duplicate(project.id), 'ies.project.duplicate_ok')
                        }
                      >
                        {t('ies.project.duplicate')}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={viewer || rowBusy}
                        title={viewer ? ownerOnly : undefined}
                        onClick={() => void openTransfer(project)}
                      >
                        {t('ies.project.transfer')}
                      </Button>
                      <Button
                        size="sm"
                        variant="ghost"
                        disabled={viewer || rowBusy}
                        title={viewer ? ownerOnly : undefined}
                        onClick={() => void openViewers(project)}
                      >
                        {t('ies.project.viewers')}
                      </Button>
                      <Button
                        size="sm"
                        variant="danger"
                        disabled={viewer || rowBusy}
                        title={viewer ? ownerOnly : undefined}
                        loading={rowBusy && busy?.op === 'delete'}
                        onClick={() => setDeleteTarget(project)}
                      >
                        {t('ies.project.delete')}
                      </Button>
                    </div>
                  </TD>
                </TR>
              )
            })}
          </TBody>
        </Table>
      )}

      {nextCursor && !loading ? (
        <div className="ies-projects__loadmore">
          <Button variant="secondary" loading={loadingMore} onClick={() => void loadProjects(true)}>
            {t('ies.common.more')}
          </Button>
        </div>
      ) : null}

      {/* 新建项目 */}
      <Dialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title={t('ies.project.new')}
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setCreateOpen(false)} disabled={creating}>
              {t('ies.common.cancel')}
            </Button>
            <Button variant="primary" loading={creating} onClick={() => void handleCreateSubmit()}>
              {t('ies.common.confirm')}
            </Button>
          </>
        }
      >
        <FormField label={t('ies.common.name')} htmlFor="pp-create-name" required error={createNameError}>
          <Input
            id="pp-create-name"
            value={createName}
            placeholder={t('ies.project.name_placeholder')}
            invalid={!!createNameError}
            onChange={(event) => setCreateName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void handleCreateSubmit()
            }}
          />
        </FormField>
        <FormField label={t('ies.project.currency')} htmlFor="pp-create-currency">
          <Select
            id="pp-create-currency"
            value={createCurrency}
            onChange={(event) => setCreateCurrency(event.target.value as Currency)}
          >
            <option value="CNY">{t('ies.project.currency_cny')}</option>
            <option value="USD">{t('ies.project.currency_usd')}</option>
          </Select>
        </FormField>
        <FormField label={t('ies.project.utc_offset')} htmlFor="pp-create-offset">
          <Select id="pp-create-offset" value={createOffset} onChange={(event) => setCreateOffset(Number(event.target.value))}>
            {UTC_OFFSET_HOURS.map((h) => {
              const minutes = h * 60
              return (
                <option key={minutes} value={minutes}>
                  {formatUtcOffset(minutes)}
                </option>
              )
            })}
          </Select>
        </FormField>
      </Dialog>

      {/* 归档确认 */}
      <Dialog
        open={archiveTarget !== null}
        onClose={() => setArchiveTarget(null)}
        title={t('ies.project.archive')}
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setArchiveTarget(null)} disabled={busy?.op === 'archive'}>
              {t('ies.common.cancel')}
            </Button>
            <Button variant="primary" loading={busy?.op === 'archive'} onClick={() => void handleArchiveConfirm()}>
              {t('ies.common.confirm')}
            </Button>
          </>
        }
      >
        <Alert variant="warning">
          {t('ies.project.archive_confirm', { name: archiveTarget?.name ?? '' })}
        </Alert>
      </Dialog>

      {/* 删除确认(不可恢复) */}
      <Dialog
        open={deleteTarget !== null}
        onClose={() => setDeleteTarget(null)}
        title={t('ies.project.delete')}
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeleteTarget(null)} disabled={busy?.op === 'delete'}>
              {t('ies.common.cancel')}
            </Button>
            <Button variant="danger" loading={busy?.op === 'delete'} onClick={() => void handleDeleteConfirm()}>
              {t('ies.common.delete')}
            </Button>
          </>
        }
      >
        <Alert variant="error">
          {t('ies.project.delete_confirm', { name: deleteTarget?.name ?? '' })}
        </Alert>
      </Dialog>

      {/* 转移所有权 */}
      <Dialog
        open={transferTarget !== null}
        onClose={() => setTransferTarget(null)}
        title={t('ies.project.transfer')}
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={() => setTransferTarget(null)} disabled={transferring}>
              {t('ies.common.cancel')}
            </Button>
            <Button variant="primary" loading={transferring} onClick={() => void handleTransferSubmit()}>
              {t('ies.project.transfer')}
            </Button>
          </>
        }
      >
        <FormField label={t('ies.project.transfer')} htmlFor="pp-transfer-user" error={transferError}>
          {transferUsers.length > 0 ? (
            <Select
              id="pp-transfer-user"
              value={transferUsername}
              invalid={!!transferError}
              onChange={(event) => {
                setTransferUsername(event.target.value)
                setTransferError(null)
              }}
            >
              <option value="">{t('ies.project.username_placeholder')}</option>
              {transferUsers.map((user) => (
                <option key={user.id} value={user.username}>
                  {user.display_name} ({user.username})
                </option>
              ))}
            </Select>
          ) : (
            <Input
              id="pp-transfer-user"
              value={transferUsername}
              placeholder={t('ies.project.username_placeholder')}
              invalid={!!transferError}
              onChange={(event) => {
                setTransferUsername(event.target.value)
                setTransferError(null)
              }}
            />
          )}
        </FormField>
        {transferTarget && transferUsername.trim() ? (
          <p className="ies-projects__muted">
            {t('ies.project.transfer_confirm', {
              name: transferTarget.name,
              username: transferUsername.trim(),
            })}
          </p>
        ) : null}
      </Dialog>

      {/* 管理查看者 */}
      <Dialog
        open={viewersTarget !== null}
        onClose={() => setViewersTarget(null)}
        title={t('ies.project.viewers')}
        size="md"
        footer={
          <Button variant="secondary" onClick={() => setViewersTarget(null)}>
            {t('ies.common.close')}
          </Button>
        }
      >
        {viewersError ? (
          <Alert variant="error" title={viewersError} closable onClose={() => setViewersError(null)}>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                if (viewersTarget) void refreshViewers(viewersTarget.id)
              }}
            >
              {t('ies.common.retry')}
            </Button>
          </Alert>
        ) : null}
        {viewersLoading ? (
          <div className="ies-projects__loading" role="status">
            <Spinner label={t('ies.common.loading')} />
          </div>
        ) : viewersMembers.length === 0 ? (
          <p className="ies-projects__muted">{t('ies.common.no_data')}</p>
        ) : (
          <ul className="ies-projects__members">
            {viewersMembers.map((member) => (
              <li key={member.user_id} className="ies-projects__member">
                <div>
                  <div className="ies-projects__name">{member.display_name}</div>
                  <div className="ies-projects__desc">
                    {member.username} · {formatDateTime(member.granted_at)}
                  </div>
                </div>
                <div className="ies-projects__member-right">
                  <Badge
                    label={member.role === 'owner' ? t('ies.project.owner_role') : t('ies.project.viewer_role')}
                    variant={member.role === 'owner' ? 'primary' : 'neutral'}
                    icon={member.role === 'owner' ? 'check' : 'info'}
                  />
                  {removeTarget?.user_id === member.user_id ? (
                    <span className="ies-projects__inline-confirm">
                      <span className="ies-projects__muted">
                        {t('ies.project.remove_viewer_confirm', { username: member.username })}
                      </span>
                      <Button
                        size="sm"
                        variant="danger"
                        loading={viewerSubmitting === 'remove'}
                        onClick={() => void handleRemoveViewer(member)}
                      >
                        {t('ies.common.confirm')}
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setRemoveTarget(null)}>
                        {t('ies.common.cancel')}
                      </Button>
                    </span>
                  ) : member.role === 'viewer' ? (
                    <Button size="sm" variant="ghost" onClick={() => setRemoveTarget(member)}>
                      <Icon name="trash" size={14} />
                      {t('ies.project.remove_viewer')}
                    </Button>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        )}
        <div className="ies-projects__add-viewer">
          <Input
            value={newViewerName}
            placeholder={t('ies.project.username_placeholder')}
            aria-label={t('ies.project.add_viewer')}
            onChange={(event) => setNewViewerName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void handleAddViewer()
            }}
          />
          <Button
            variant="secondary"
            loading={viewerSubmitting === 'add'}
            onClick={() => void handleAddViewer()}
          >
            {t('ies.project.add_viewer')}
          </Button>
        </div>
      </Dialog>
    </div>
  )
}
