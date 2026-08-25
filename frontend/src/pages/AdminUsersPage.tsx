/**
 * 账号管理页(/admin/users,默认导出 AdminUsersPage, 仅管理员)。
 *
 * 能力(复用既有 API 与消息键):
 *   - 用户列表 + 停用/重新启用 + 删除账号
 *     (删除账号会级联删除该账号拥有的全部项目且不可恢复, 采用「预告→确认」两步:
 *      先 preview 返回将受影响项目清单 + 确认令牌, 确认时携带令牌执行删除);
 *   - 重置密码: 管理员为用户签发临时密码(强制改密), 目标用户全部会话失效。
 */

import { useEffect, useState } from 'react'

import { api } from '../api/client'
import { errorMessage, useI18n } from '../i18n'
import { Alert, Button, Card, Dialog, FormField, Input, Table, TBody, TD, TH, THead, TR } from '../components/ui'
import type { AdminUserRow, UserDeletePreview } from '../types'

export default function AdminUsersPage() {
  const { t } = useI18n()

  const [users, setUsers] = useState<AdminUserRow[] | null>(null)
  const [usersError, setUsersError] = useState<string | null>(null)
  const [usersBusy, setUsersBusy] = useState(false)
  const [usersNotice, setUsersNotice] = useState<string | null>(null)

  // 删除账号预告(0.2.0 B1): 选中目标后先预览影响范围, 确认后携带令牌删除
  const [deletePreview, setDeletePreview] = useState<UserDeletePreview | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<AdminUserRow | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)

  // 重置密码对话框
  const [resetTarget, setResetTarget] = useState<AdminUserRow | null>(null)
  const [resetPassword, setResetPassword] = useState('')
  const [resetConfirm, setResetConfirm] = useState('')
  const [resetError, setResetError] = useState<string | null>(null)
  const [resetBusy, setResetBusy] = useState(false)
  // 重置成功后展示密码(要求复制/截图后确认)
  const [resetDone, setResetDone] = useState<{ username: string; password: string } | null>(null)
  const [resetCopied, setResetCopied] = useState(false)

  /** 渲染后端诊断文案(共享 errorMessage:ApiError 走 translateError,其余兜底)。 */
  const errorText = (err: unknown): string => errorMessage(err)

  const loadUsers = () => {
    setUsersBusy(true)
    setUsersError(null)
    api.admin
      .users({ limit: 200 })
      .then((page) => setUsers(page.items))
      .catch((err) => setUsersError(errorText(err)))
      .finally(() => setUsersBusy(false))
  }

  useEffect(() => {
    loadUsers()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /** 请求删除预告(0.2.0 B1): 返回将受影响项目清单 + 确认令牌, 展示在确认对话框。 */
  const handlePreviewDeleteUser = async (user: AdminUserRow) => {
    setDeleteTarget(user)
    setDeleteError(null)
    setDeletePreview(null)
    setDeleteBusy(true)
    try {
      const preview = await api.admin.previewUserDelete(user.id)
      setDeletePreview(preview)
    } catch (err) {
      setDeleteError(errorText(err))
    } finally {
      setDeleteBusy(false)
    }
  }

  const handleDeleteUser = async () => {
    if (!deleteTarget || !deletePreview) return
    setDeleteBusy(true)
    setDeleteError(null)
    setUsersError(null)
    setUsersNotice(null)
    try {
      const result = await api.admin.deleteUser(deleteTarget.id, deletePreview.confirm_token)
      setUsersNotice(t('ies.admin.user_deleted', { username: deleteTarget.username, count: result.deleted_projects }))
      setDeleteTarget(null)
      setDeletePreview(null)
      loadUsers()
    } catch (err) {
      setDeleteError(errorText(err))
    } finally {
      setDeleteBusy(false)
    }
  }

  const handleCancelDelete = () => {
    setDeleteTarget(null)
    setDeletePreview(null)
    setDeleteError(null)
  }

  const handleResetSubmit = async () => {
    const target = resetTarget
    if (!target) return
    if (!resetPassword || !resetConfirm) {
      setResetError(t('ies.auth.required'))
      return
    }
    if (resetPassword !== resetConfirm) {
      setResetError(t('ies.auth.password_mismatch'))
      return
    }
    setResetBusy(true)
    setResetError(null)
    try {
      await api.admin.resetPassword(target.id, resetPassword)
      const donePassword = resetPassword
      setResetTarget(null)
      setResetPassword('')
      setResetConfirm('')
      setResetDone({ username: target.username, password: donePassword })
      setResetCopied(false)
      loadUsers()
    } catch (err) {
      setResetError(errorText(err))
    } finally {
      setResetBusy(false)
    }
  }

  const handleCancelReset = () => {
    setResetTarget(null)
    setResetPassword('')
    setResetConfirm('')
    setResetError(null)
  }
  const handleCloseResetDone = () => {
    setResetDone(null)
    setResetCopied(false)
  }
  const handleCopyResetPassword = async () => {
    if (!resetDone) return
    try {
      await navigator.clipboard.writeText(resetDone.password)
      setResetCopied(true)
    } catch {
      // 降级: 选中文本由用户手动复制
      setResetCopied(false)
    }
  }

  return (
    <div className="ies-page">
      <header className="ies-page-header">
        <h1 className="ies-page-title">{t('ies.admin.accounts')}</h1>
        <p className="ies-page-subtitle">{t('ies.admin.title')}</p>
      </header>

      {usersNotice ? (
        <Alert variant="success" title={usersNotice} closable onClose={() => setUsersNotice(null)} />
      ) : null}
      {usersError ? (
        <Alert variant="error" title={usersError} closable onClose={() => setUsersError(null)}>
          <Button variant="secondary" size="sm" onClick={loadUsers}>
            {t('ies.common.retry')}
          </Button>
        </Alert>
      ) : null}

      <Card title={t('ies.admin.users')}>
        {users === null && !usersError ? (
          <p role="status">{t('ies.common.loading')}</p>
        ) : users !== null ? (
          <Table>
            <THead>
              <TR>
                <TH>{t('ies.admin.username')}</TH>
                <TH>{t('ies.admin.project_count')}</TH>
                <TH>{t('ies.admin.roles')}</TH>
                <TH>{t('ies.admin.actions')}</TH>
              </TR>
            </THead>
            <TBody>
              {users.map((u) => (
                <TR key={u.id}>
                  <TD>
                    {u.display_name}
                    <span className="ies-mono"> ({u.username})</span>
                  </TD>
                  <TD>{u.project_count}</TD>
                  <TD>{(u.roles ?? []).join(', ')}</TD>
                  <TD>
                    <div className="ies-flex" style={{ gap: 'var(--ies-space-1)' }}>
                      <Button
                        variant="secondary"
                        size="sm"
                        disabled={usersBusy || u.username === 'admin'}
                        onClick={() => {
                          setResetTarget(u)
                          setResetPassword('')
                          setResetConfirm('')
                          setResetError(null)
                        }}
                      >
                        {t('ies.admin.reset_password')}
                      </Button>
                      <Button
                        variant="danger"
                        size="sm"
                        disabled={usersBusy || u.username === 'admin'}
                        onClick={() => void handlePreviewDeleteUser(u)}
                      >
                        {t('ies.admin.delete_user')}
                      </Button>
                    </div>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        ) : null}
        <p className="ies-form-message">{t('ies.admin.delete_hint')}</p>
      </Card>

      {/* 重置密码(管理员): 签发临时密码, 目标用户全部会话失效, 下次登录须改密 */}
      <Dialog
        open={resetTarget !== null}
        onClose={handleCancelReset}
        title={t('ies.admin.reset_password')}
        size="sm"
        footer={
          <>
            <Button variant="secondary" onClick={handleCancelReset} disabled={resetBusy}>
              {t('ies.common.cancel')}
            </Button>
            <Button variant="primary" loading={resetBusy} onClick={() => void handleResetSubmit()}>
              {t('ies.admin.reset_password')}
            </Button>
          </>
        }
      >
        <Alert variant="warning">{t('ies.admin.reset_scope', { username: resetTarget?.username ?? '' })}</Alert>
        <FormField label={t('ies.auth.new_password')} htmlFor="au-reset-new" required>
          <Input
            id="au-reset-new"
            type="password"
            value={resetPassword}
            autoComplete="new-password"
            invalid={!!resetError}
            onChange={(event) => {
              setResetPassword(event.target.value)
              setResetError(null)
            }}
          />
        </FormField>
        <FormField label={t('ies.auth.confirm_password')} htmlFor="au-reset-confirm" required>
          <Input
            id="au-reset-confirm"
            type="password"
            value={resetConfirm}
            autoComplete="new-password"
            invalid={!!resetError}
            onChange={(event) => {
              setResetConfirm(event.target.value)
              setResetError(null)
            }}
          />
        </FormField>
        {resetError ? <Alert variant="error" title={resetError} closable onClose={() => setResetError(null)} /> : null}
      </Dialog>

      {/* 重置成功后展示新密码(要求复制/截图后确认, 仅显示一次) */}
      <Dialog
        open={resetDone !== null}
        onClose={handleCloseResetDone}
        title={t('ies.admin.reset_done_title')}
        size="sm"
        footer={
          <Button variant="primary" onClick={handleCloseResetDone}>
            {t('ies.admin.reset_done_confirm')}
          </Button>
        }
      >
        {resetDone ? (
          <>
            <Alert variant="success">{t('ies.admin.reset_done_desc', { username: resetDone.username })}</Alert>
            <div className="ies-flex" style={{ gap: 'var(--ies-space-2)', alignItems: 'center', margin: 'var(--ies-space-3) 0' }}>
              <code className="ies-mono" style={{ fontSize: '1.1em', padding: 'var(--ies-space-2)', background: 'var(--ies-color-bg-muted)', borderRadius: 'var(--ies-radius-sm)', flex: 1, wordBreak: 'break-all' }}>
                {resetDone.password}
              </code>
              <Button variant="secondary" size="sm" onClick={() => void handleCopyResetPassword()}>
                {resetCopied ? t('ies.admin.reset_copied') : t('ies.admin.reset_copy')}
              </Button>
            </div>
            <p className="ies-form-message">{t('ies.admin.reset_done_hint')}</p>
          </>
        ) : null}
      </Dialog>

      {/* 删除账号确认(0.2.0 B1 误操作防护): 先预告影响范围, 再携带确认令牌删除 */}
      <Dialog
        open={deleteTarget !== null}
        onClose={handleCancelDelete}
        title={t('ies.admin.delete_user')}
        size="md"
        footer={
          <>
            <Button variant="secondary" onClick={handleCancelDelete} disabled={deleteBusy}>
              {t('ies.common.cancel')}
            </Button>
            <Button
              variant="danger"
              loading={deleteBusy}
              disabled={!deletePreview}
              onClick={() => void handleDeleteUser()}
            >
              {t('ies.admin.delete_confirm')}
            </Button>
          </>
        }
      >
        <Alert variant="error">
          {t('ies.admin.delete_scope', { username: deleteTarget?.username ?? '' })}
        </Alert>
        {deleteError ? (
          <Alert variant="error" title={deleteError} closable onClose={() => setDeleteError(null)} />
        ) : null}
        {deletePreview === null && !deleteError ? (
          <p>{t('ies.common.loading')}</p>
        ) : deletePreview !== null ? (
          <div className="ies-config-section">
            <h4 className="ies-config-section-title">
              {t('ies.admin.delete_preview', { count: deletePreview.project_count })}
            </h4>
            {deletePreview.projects.length === 0 ? (
              <p className="ies-form-message">{t('ies.admin.delete_preview_none')}</p>
            ) : (
              <Table>
                <THead>
                  <TR>
                    <TH>{t('ies.admin.project_name')}</TH>
                    <TH>{t('ies.admin.project_id')}</TH>
                  </TR>
                </THead>
                <TBody>
                  {deletePreview.projects.map((p) => (
                    <TR key={p.id}>
                      <TD>{p.name}</TD>
                      <TD>
                        <span className="ies-mono">{p.id}</span>
                      </TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            )}
          </div>
        ) : null}
      </Dialog>
    </div>
  )
}
