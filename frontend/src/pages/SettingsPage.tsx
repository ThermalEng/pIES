/**
 * 系统设置页(/settings,默认导出 SettingsPage)。
 *
 * 当前能力(均复用既有 API 与消息键):
 *   - 语言偏好:切换 zh/en,持久化到 localStorage,与全局 I18nProvider 联动;
 *   - 修改密码:调用 POST /api/auth/change-password。后端在改密后轮换凭证版本,
 *     当前会话随即失效——下一次请求由全局 401 处理器(api/client + App)引导重新登录,
 *     因此成功后仅提示(ies.auth.password_changed)并清空表单;
 *   - 安全设置(仅管理员): 自助注册开关
 *   - 服务健康(仅管理员):存储用量/对象数 + 健康状态
 *     (QA-E2E-01 场景 7: 管理员查看存储健康)。
 *   账号管理(用户列表/停用/重置密码/删除)已移至一级入口 /admin/users。
 */

import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'

import { api } from '../api/client'
import { errorMessage, useI18n } from '../i18n'
import { Alert, Badge, Button, Card, FormField, Input } from '../components/ui'
import { formatBytes } from '../lib/format'
import type { HealthStatus } from '../types'

export default function SettingsPage() {
  const { t, locale, setLocale } = useI18n()

  // 修改密码
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

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

  useEffect(() => {
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
      </div>
    </div>
  )
}
