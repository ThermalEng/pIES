/**
 * 系统设置页(/settings,默认导出 SettingsPage)。
 *
 * 当前能力(均复用既有 API 与消息键):
 *   - 语言偏好:切换 zh/en,持久化到 localStorage,与全局 I18nProvider 联动;
 *   - 修改密码:调用 POST /api/auth/change-password。后端在改密后轮换凭证版本,
 *     当前会话随即失效——下一次请求由全局 401 处理器(api/client + App)引导重新登录,
 *     因此成功后仅提示(ies.auth.password_changed)并清空表单。
 */

import { useState } from 'react'
import type { FormEvent } from 'react'

import { api } from '../api/client'
import { ApiError } from '../types'
import { useI18n } from '../i18n'
import { Alert, Button, Card, FormField, Input } from '../components/ui'

export default function SettingsPage() {
  const { t, locale, setLocale } = useI18n()

  // 修改密码
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  /** 渲染后端诊断文案(ApiError.message_key + params 插值)。 */
  const errorText = (err: unknown): string =>
    err instanceof ApiError ? t(err.message_key, err.params) : t('ies.error.unknown')

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
      </div>
    </div>
  )
}
