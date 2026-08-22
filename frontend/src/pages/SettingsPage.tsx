/**
 * 系统设置页(/settings,默认导出 SettingsPage)。
 *
 * 当前能力(均复用既有 API 与消息键):
 *   - 语言偏好:切换 zh/en,持久化到 localStorage,与全局 I18nProvider 联动;
 *   - 修改密码:调用 POST /api/auth/change-password。后端在改密后轮换凭证版本,
 *     当前会话随即失效——下一次请求由全局 401 处理器(api/client + App)引导重新登录,
 *     因此成功后仅提示(ies.auth.password_changed)并清空表单;
 *   - 账号管理(仅管理员):用户列表 + 停用/重新启用 + 删除账号
 *     (删除账号会级联删除该账号拥有的全部项目且不可恢复, 采用「预告→确认」两步:
 *      先 preview 返回将受影响项目清单 + 确认令牌, 确认时携带令牌执行删除);
 *   - 服务健康(仅管理员):存储用量/对象数 + 健康状态
 *     (QA-E2E-01 场景 7: 管理员查看存储健康)。
 */

import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'

import { api } from '../api/client'
import { errorMessage, useI18n } from '../i18n'
import { Alert, Badge, Button, Card, Dialog, FormField, Input, Table, TBody, TD, TH, THead, TR } from '../components/ui'
import { formatBytes } from '../lib/format'
import type { AdminUserRow, HealthStatus, UserDeletePreview } from '../types'

export default function SettingsPage() {
  const { t, locale, setLocale } = useI18n()

  // 修改密码
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // 账号管理(管理员)
  const [users, setUsers] = useState<AdminUserRow[] | null>(null)
  const [usersError, setUsersError] = useState<string | null>(null)
  const [usersBusy, setUsersBusy] = useState(false)
  const [usersNotice, setUsersNotice] = useState<string | null>(null)
  // 删除账号预告(0.2.0 B1): 选中目标后先预览影响范围, 确认后携带令牌删除
  const [deletePreview, setDeletePreview] = useState<UserDeletePreview | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<AdminUserRow | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)
  const [deleteBusy, setDeleteBusy] = useState(false)

  // 安全设置(管理员): 自助注册开关
  const [registrationEnabled, setRegistrationEnabled] = useState<boolean | null>(null)
  const [securityError, setSecurityError] = useState<string | null>(null)
  const [securityBusy, setSecurityBusy] = useState(false)

  // 服务健康(管理员): 存储用量 + 健康状态
  const [storage, setStorage] = useState<{
    total_bytes: number
    used_bytes: number
    quota_bytes: number | null
    object_count: number
  } | null>(null)
  const [health, setHealth] = useState<HealthStatus | null>(null)
  const [healthError, setHealthError] = useState<string | null>(null)
  const [healthBusy, setHealthBusy] = useState(false)

  /** 渲染后端诊断文案(共享 errorMessage:ApiError 走 translateError,其余兜底)。 */
  const errorText = (err: unknown): string => errorMessage(err)

  const loadSecuritySettings = () => {
    setSecurityError(null)
    api.admin
      .getSecuritySettings()
      .then((s) => setRegistrationEnabled(s.registration_enabled))
      .catch((err) => setSecurityError(errorText(err)))
  }

  const handleToggleRegistration = async (enabled: boolean) => {
    setSecurityBusy(true)
    setSecurityError(null)
    try {
      const result = await api.admin.setRegistrationEnabled(enabled)
      setRegistrationEnabled(result.registration_enabled)
    } catch (err) {
      setSecurityError(errorText(err))
    } finally {
      setSecurityBusy(false)
    }
  }

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
    loadSecuritySettings()
    loadHealth()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /** 加载存储用量与健康状态(管理员)。 */
  const loadHealth = () => {
    setHealthBusy(true)
    setHealthError(null)
    Promise.all([api.admin.storage(), api.admin.health()])
      .then(([storageData, healthData]) => {
        setStorage(storageData)
        setHealth(healthData)
      })
      .catch((err) => setHealthError(errorText(err)))
      .finally(() => setHealthBusy(false))
  }

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

  const handleToggleUser = async (user: AdminUserRow, enable: boolean) => {
    setUsersBusy(true)
    setUsersError(null)
    setUsersNotice(null)
    try {
      if (enable) await api.admin.reactivateUser(user.id)
      else await api.admin.deactivateUser(user.id)
      setUsersNotice(enable ? t('ies.admin.user_reactivated', { username: user.username }) : t('ies.admin.user_deactivated', { username: user.username }))
      loadUsers()
    } catch (err) {
      setUsersError(errorText(err))
    } finally {
      setUsersBusy(false)
    }
  }

  const handleChangePassword = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setNotice(null)
    if (!oldPassword || !newPassword || !confirmPassword) {
      setError(t('ies.auth.required'))
      return
    }
    if (newPassword !== confirmPassword) {
      setError(t('ies.auth.password_mismatch'))
      return
    }
    if (newPassword === oldPassword) {
      setError(t('ies.auth.same_password'))
      return
    }
    setError(null)
    setBusy(true)
    try {
      await api.auth.changePassword({ old_password: oldPassword, new_password: newPassword })
      setNotice(t('ies.auth.password_changed'))
      setOldPassword('')
      setNewPassword('')
      setConfirmPassword('')
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="ies-page">
      <header className="ies-page-header">
        <h1 className="ies-page-title">{t('ies.nav.settings')}</h1>
        <p className="ies-page-subtitle">{t('ies.admin.title')}</p>
      </header>

      <div className="ies-settings-grid">
        <Card title={t('ies.nav.language')}>
          <div
            className="ies-settings__lang"
            role="group"
            aria-label={t('ies.nav.language')}
          >
            <Button
              type="button"
              variant={locale === 'zh' ? 'primary' : 'secondary'}
              aria-pressed={locale === 'zh'}
              onClick={() => setLocale('zh')}
            >
              {t('ies.nav.lang_zh')}
            </Button>
            <Button
              type="button"
              variant={locale === 'en' ? 'primary' : 'secondary'}
              aria-pressed={locale === 'en'}
              onClick={() => setLocale('en')}
            >
              {t('ies.nav.lang_en')}
            </Button>
          </div>
        </Card>

        <Card title={t('ies.auth.change_password')}>
          <form onSubmit={handleChangePassword} noValidate>
            <FormField label={t('ies.auth.old_password')} htmlFor="settings-old-password" required>
              <Input
                id="settings-old-password"
                name="old_password"
                type="password"
                value={oldPassword}
                onChange={(event) => setOldPassword(event.target.value)}
                autoComplete="current-password"
                disabled={busy}
              />
            </FormField>
            <FormField label={t('ies.auth.new_password')} htmlFor="settings-new-password" required>
              <Input
                id="settings-new-password"
                name="new_password"
                type="password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                autoComplete="new-password"
                disabled={busy}
              />
            </FormField>
            <FormField label={t('ies.auth.confirm_password')} htmlFor="settings-confirm-password" required>
              <Input
                id="settings-confirm-password"
                name="confirm_password"
                type="password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                autoComplete="new-password"
                disabled={busy}
              />
            </FormField>
            {error ? (
              <Alert variant="error" title={error} closable onClose={() => setError(null)} />
            ) : null}
            {notice ? (
              <Alert variant="success" closable onClose={() => setNotice(null)}>
                {notice}
              </Alert>
            ) : null}
            <div className="ies-settings__actions">
              <Button type="submit" variant="primary" loading={busy}>
                {t('ies.auth.change_password')}
              </Button>
            </div>
          </form>
        </Card>

        <Card title={t('ies.admin.security')}>
          {securityError ? (
            <Alert variant="error" title={securityError} closable onClose={() => setSecurityError(null)} />
          ) : null}
          {registrationEnabled === null ? (
            <p>{t('ies.common.loading')}</p>
          ) : (
            <div className="ies-settings__toggle-row">
              <label className="ies-checkbox-label" htmlFor="settings-registration">
                {t('ies.admin.open_registration')}
              </label>
              <input
                id="settings-registration"
                type="checkbox"
                checked={registrationEnabled}
                disabled={securityBusy}
                onChange={(event) => handleToggleRegistration(event.target.checked)}
              />
            </div>
          )}
          <p className="ies-form-message">{t('ies.admin.registration_hint')}</p>
        </Card>

        <Card title={t('ies.admin.health')} actions={healthBusy ? <Badge label={t('ies.common.loading')} variant="neutral" /> : undefined}>
          {healthError ? (
            <Alert variant="error" title={healthError} closable onClose={() => setHealthError(null)}>
              <Button variant="secondary" size="sm" onClick={() => void loadHealth()}>
                {t('ies.common.retry')}
              </Button>
            </Alert>
          ) : storage === null || health === null ? (
            <p>{t('ies.common.loading')}</p>
          ) : (
            <>
              <div className="ies-meta-grid">
                <div>
                  <h4 className="ies-config-section-title">{t('ies.admin.storage')}</h4>
                  <div className="ies-flex" style={{ flexWrap: 'wrap', gap: 'var(--ies-space-2)' }}>
                    <span className="ies-form-message">
                      {t('ies.admin.storage_used')}: {formatBytes(storage.total_bytes)}
                    </span>
                    {storage.quota_bytes !== null ? (
                      <span className="ies-form-message">
                        {t('ies.admin.storage_quota')}: {formatBytes(storage.quota_bytes)}
                      </span>
                    ) : null}
                    <span className="ies-form-message">
                      {t('ies.admin.object_count')}: {storage.object_count}
                    </span>
                  </div>
                </div>
                <div>
                  <h4 className="ies-config-section-title">{t('ies.admin.health')}</h4>
                  <div className="ies-flex" style={{ flexWrap: 'wrap', gap: 'var(--ies-space-2)' }}>
                    <Badge
                      label={t(`ies.admin.health_${health.checks.storage?.status ?? 'down'}`)}
                      variant={health.checks.storage?.status === 'ok' ? 'success' : 'warning'}
                      icon={health.checks.storage?.status === 'ok' ? 'check' : 'warning'}
                    />
                    <span className="ies-form-message">v{health.version}</span>
                  </div>
                </div>
              </div>
            </>
          )}
        </Card>

        <Card title={t('ies.admin.accounts')}>
          {usersError ? (
            <Alert variant="error" title={usersError} closable onClose={() => setUsersError(null)} />
          ) : null}
          {usersNotice ? (
            <Alert variant="success" closable onClose={() => setUsersNotice(null)}>
              {usersNotice}
            </Alert>
          ) : null}
          {users === null && !usersError ? (
            <p>{t('ies.common.loading')}</p>
          ) : (
            <Table>
              <THead>
                <TR>
                  <TH>{t('ies.admin.username')}</TH>
                  <TH>{t('ies.admin.status')}</TH>
                  <TH>{t('ies.admin.roles')}</TH>
                  <TH>{t('ies.admin.actions')}</TH>
                </TR>
              </THead>
              <TBody>
                {(users ?? []).map((u) => (
                  <TR key={u.id}>
                    <TD>
                      {u.display_name}
                      <span className="ies-mono"> ({u.username})</span>
                    </TD>
                    <TD>{u.status}</TD>
                    <TD>{(u.roles ?? []).join(', ')}</TD>
                    <TD>
                      <div className="ies-flex" style={{ gap: 'var(--ies-space-1)' }}>
                        {u.status === 'disabled' ? (
                          <Button variant="secondary" size="sm" disabled={usersBusy} onClick={() => handleToggleUser(u, true)}>
                            {t('ies.admin.reactivate')}
                          </Button>
                        ) : (
                          <Button variant="secondary" size="sm" disabled={usersBusy || u.username === 'admin'} onClick={() => handleToggleUser(u, false)}>
                            {t('ies.admin.deactivate')}
                          </Button>
                        )}
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
          )}
          <p className="ies-form-message">{t('ies.admin.delete_hint')}</p>
        </Card>
      </div>

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
