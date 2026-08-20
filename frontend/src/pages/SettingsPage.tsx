/**
 * 系统设置页(/settings,默认导出 SettingsPage)。
 *
 * 当前能力(均复用既有 API 与消息键):
 *   - 语言偏好:切换 zh/en,持久化到 localStorage,与全局 I18nProvider 联动;
 *   - 修改密码:调用 POST /api/auth/change-password。后端在改密后轮换凭证版本,
 *     当前会话随即失效——下一次请求由全局 401 处理器(api/client + App)引导重新登录,
 *     因此成功后仅提示(ies.auth.password_changed)并清空表单;
 *   - 账号管理(仅管理员):用户列表 + 停用/重新启用 + 删除账号
 *     (删除账号时该账号拥有的项目一并删除)。
 */

import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'

import { api } from '../api/client'
import { errorMessage, useI18n } from '../i18n'
import { Alert, Button, Card, FormField, Input, Table, TBody, TD, TH, THead, TR } from '../components/ui'
import type { AdminUserRow } from '../types'

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
  const [deleteTarget, setDeleteTarget] = useState<AdminUserRow | null>(null)

  // 安全设置(管理员): 自助注册开关
  const [registrationEnabled, setRegistrationEnabled] = useState<boolean | null>(null)
  const [securityError, setSecurityError] = useState<string | null>(null)
  const [securityBusy, setSecurityBusy] = useState(false)

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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const handleDeleteUser = async (user: AdminUserRow) => {
    setUsersBusy(true)
    setUsersError(null)
    setUsersNotice(null)
    try {
      const result = await api.admin.deleteUser(user.id)
      setUsersNotice(t('ies.admin.user_deleted', { username: user.username, count: result.deleted_projects }))
      setDeleteTarget(null)
      loadUsers()
    } catch (err) {
      setUsersError(errorText(err))
    } finally {
      setUsersBusy(false)
    }
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
                        {deleteTarget?.id === u.id ? (
                          <>
                            <Button
                              variant="danger"
                              size="sm"
                              disabled={usersBusy}
                              onClick={() => handleDeleteUser(u)}
                            >
                              {t('ies.admin.delete_confirm')}
                            </Button>
                            <Button variant="ghost" size="sm" onClick={() => setDeleteTarget(null)}>
                              {t('ies.common.cancel')}
                            </Button>
                          </>
                        ) : (
                          <Button
                            variant="danger"
                            size="sm"
                            disabled={usersBusy || u.username === 'admin'}
                            onClick={() => setDeleteTarget(u)}
                          >
                            {t('ies.admin.delete_user')}
                          </Button>
                        )}
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
    </div>
  )
}
