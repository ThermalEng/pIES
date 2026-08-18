/**
 * 登录与会话页(默认导出 LoginPage)。
 *
 * 视图状态机(均在 /login 路由内切换,不离开公开路由):
 *   login            登录表单(用户名/密码,表单原生 Enter 提交)
 *   change_password  首次登录强制改密(后端 user.force_password_change = true;
 *                    改密成功即凭证轮换,须用新密码重新登录)
 *   takeover         窗口接管确认(后端 needs_takeover_confirm = true:
 *                    旧窗口会话已被降级为 takeover_pending,确认后调用
 *                    confirm-takeover 撤销旧会话并轮换新窗口凭证)
 *
 * 其他能力:
 *   - 会话过期:全局 401 处理器(api/client + App 注册)带 state.reason=expired
 *     跳回本页,此处展示过期提示(ies.auth.session_expired)。
 *   - 登录/改密错误:直接渲染后端诊断信封 message_key 对应文案
 *     (ies.diag.auth.*,params 插值),含登录限速锁定(429 ies.diag.auth.locked)。
 *   - 离线提示:网络层失败(status 0)或浏览器 offline 事件时显示离线徽章,
 *     并提示本机/局域网部署场景的可用性(ies.auth.offline / offline_hint)。
 *
 * 与后端契约(backend/iesplan/api/auth.py):
 *   POST /api/auth/login            → {token, user{force_password_change}, needs_takeover_confirm}
 *   POST /api/auth/change-password  → 改密后凭证版本递增,当前及旧会话全部失效
 *   POST /api/auth/confirm-takeover → 以 Cookie 凭证确认,返回全新 token
 */

import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import { api } from '../api/client'
import { ApiError } from '../types'
import { useI18n } from '../i18n'
import { Alert, Badge, Button, Checkbox, FormField, Input } from '../components/ui'

// ---------------------------------------------------------------------------
// 类型(与后端 AuthResponse 对齐;脚手架 LoginResponse 已过时,此处本地收窄)
// ---------------------------------------------------------------------------

interface AuthUser {
  id: number
  username: string
  display_name: string
  role: string
  status: string
  force_password_change: boolean
  credential_version: number
  last_login_at: string | null
}

interface AuthResponse {
  token: string
  token_type: string
  user: AuthUser
  needs_takeover_confirm: boolean
}

type View = 'login' | 'change_password' | 'takeover'

// ---------------------------------------------------------------------------
// 辅助
// ---------------------------------------------------------------------------

/** 浏览器在线状态(online/offline 事件同步)。 */
function useOnlineStatus(): boolean {
  const [online, setOnline] = useState<boolean>(() =>
    typeof navigator !== 'undefined' ? navigator.onLine : true,
  )
  useEffect(() => {
    const handleOnline = () => setOnline(true)
    const handleOffline = () => setOnline(false)
    window.addEventListener('online', handleOnline)
    window.addEventListener('offline', handleOffline)
    return () => {
      window.removeEventListener('online', handleOnline)
      window.removeEventListener('offline', handleOffline)
    }
  }, [])
  return online
}

/** 是否网络层失败(连接失败/超时,区别于后端业务错误信封)。 */
function isNetworkFailure(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false
  return err.status === 0 && (err.message_key === 'ies.error.network' || err.message_key === 'ies.error.timeout')
}

// ---------------------------------------------------------------------------
// 登录页
// ---------------------------------------------------------------------------

export default function LoginPage() {
  const { t } = useI18n()
  const navigate = useNavigate()
  const location = useLocation()
  const browserOnline = useOnlineStatus()

  // 路由状态:会话过期回跳(带 reason=expired)与登录后目标地址(RequireAuth 传入 from)
  const routeState = location.state as { reason?: string; from?: string } | null
  const sessionExpired = routeState?.reason === 'expired'
  const redirectTarget = routeState?.from ?? '/'

  const [view, setView] = useState<View>('login')
  const [loginResult, setLoginResult] = useState<AuthResponse | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // 登录表单
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(false)
  const [loginError, setLoginError] = useState<string | null>(null)
  const [loginBusy, setLoginBusy] = useState(false)
  const [loginOffline, setLoginOffline] = useState(false)

  // 强制改密
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [changeError, setChangeError] = useState<string | null>(null)
  const [changeBusy, setChangeBusy] = useState(false)

  // 接管确认
  const [takeoverError, setTakeoverError] = useState<string | null>(null)
  const [takeoverBusy, setTakeoverBusy] = useState(false)

  /** 渲染后端诊断文案(ApiError.message_key + params 插值)。 */
  const errorText = (err: unknown): string =>
    err instanceof ApiError ? t(err.message_key, err.params) : t('ies.error.unknown')

  const handleLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setLoginOffline(false)
    setNotice(null)
    if (!username.trim() || !password) {
      setLoginError(t('ies.auth.required'))
      return
    }
    setLoginError(null)
    setLoginBusy(true)
    try {
      const res = (await api.auth.login({
        username: username.trim(),
        password,
        remember,
      })) as unknown as AuthResponse
      setLoginResult(res)
      if (res.user.force_password_change) {
        // 首次登录(或密码被重置):强制改密;改密成功后当前会话随之失效
        setOldPassword(password)
        setNewPassword('')
        setConfirmPassword('')
        setChangeError(null)
        setView('change_password')
      } else if (res.needs_takeover_confirm) {
        // 旧窗口已被降级为待接管:展示确认页,确认后轮换凭证
        setTakeoverError(null)
        setView('takeover')
      } else {
        navigate(redirectTarget, { replace: true })
      }
    } catch (err) {
      setLoginOffline(isNetworkFailure(err))
      setLoginError(errorText(err))
    } finally {
      setLoginBusy(false)
    }
  }

  const handleChangePassword = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!oldPassword || !newPassword || !confirmPassword) {
      setChangeError(t('ies.auth.required'))
      return
    }
    if (newPassword !== confirmPassword) {
      setChangeError(t('ies.auth.password_mismatch'))
      return
    }
    if (newPassword === oldPassword) {
      setChangeError(t('ies.auth.same_password'))
      return
    }
    setChangeError(null)
    setChangeBusy(true)
    try {
      await api.auth.changePassword({ old_password: oldPassword, new_password: newPassword })
      // 凭证版本已轮换,当前及全部旧会话失效:回登录表单,用新密码重新登录
      setNotice(t('ies.auth.password_changed'))
      setLoginResult(null)
      setPassword('')
      setOldPassword('')
      setNewPassword('')
      setConfirmPassword('')
      setView('login')
    } catch (err) {
      setChangeError(errorText(err))
    } finally {
      setChangeBusy(false)
    }
  }

  const handleConfirmTakeover = async () => {
    setTakeoverError(null)
    setTakeoverBusy(true)
    try {
      // 后端以 Cookie 会话确认接管并轮换凭证(body 仅供客户端语义传递,服务端忽略)
      await api.auth.confirmTakeover({ token: loginResult?.token ?? '' })
      navigate(redirectTarget, { replace: true })
    } catch (err) {
      setTakeoverError(errorText(err))
    } finally {
      setTakeoverBusy(false)
    }
  }

  const handleCancelTakeover = () => {
    // 放弃接管:撤销当前新会话并回登录表单(旧窗口保持待接管,下次登录时自动清理)
    void api.auth.logout().catch(() => {
      /* 会话失效由 client 的 401 处理器兜底清理 */
    })
    setLoginResult(null)
    setView('login')
  }

  const showOffline = !browserOnline || loginOffline

  return (
    <main className="ies-login">
      <section className="ies-login__card" aria-label={t('ies.auth.login_title')}>
        <header className="ies-login__header">
          <span className="ies-login__mark" aria-hidden="true">
            IES
          </span>
          <h1 className="ies-login__title">{t('ies.auth.login_title')}</h1>
        </header>

        {showOffline ? (
          <div className="ies-login__offline" role="status">
            <Badge label={t('ies.auth.offline')} variant="warning" icon="warning" shape="triangle" />
            <p className="ies-login__offline-hint">{t('ies.auth.offline_hint')}</p>
          </div>
        ) : null}

        {sessionExpired ? <Alert variant="warning" title={t('ies.auth.session_expired')} /> : null}
        {notice ? (
          <Alert variant="success" closable onClose={() => setNotice(null)}>
            {notice}
          </Alert>
        ) : null}

        {view === 'login' ? (
          <form className="ies-login__form" onSubmit={handleLogin} noValidate>
            <FormField label={t('ies.auth.username')} htmlFor="login-username" required>
              <Input
                id="login-username"
                name="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="username"
                autoFocus
                disabled={loginBusy}
              />
            </FormField>
            <FormField label={t('ies.auth.password')} htmlFor="login-password" required>
              <Input
                id="login-password"
                name="password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                disabled={loginBusy}
              />
            </FormField>
            <Checkbox
              id="login-remember"
              label={t('ies.auth.remember')}
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
              disabled={loginBusy}
            />
            {loginError ? (
              <Alert variant="error" title={loginError} closable onClose={() => setLoginError(null)} />
            ) : null}
            <Button type="submit" variant="primary" size="lg" fullWidth loading={loginBusy}>
              {t('ies.auth.login_submit')}
            </Button>
          </form>
        ) : null}

        {view === 'change_password' ? (
          <form className="ies-login__form" onSubmit={handleChangePassword} noValidate>
            <Alert variant="info">{t('ies.auth.force_change_password')}</Alert>
            <FormField label={t('ies.auth.old_password')} htmlFor="pwd-old" required>
              <Input
                id="pwd-old"
                name="old_password"
                type="password"
                value={oldPassword}
                onChange={(event) => setOldPassword(event.target.value)}
                autoComplete="current-password"
                disabled={changeBusy}
              />
            </FormField>
            <FormField label={t('ies.auth.new_password')} htmlFor="pwd-new" required>
              <Input
                id="pwd-new"
                name="new_password"
                type="password"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                autoComplete="new-password"
                disabled={changeBusy}
              />
            </FormField>
            <FormField label={t('ies.auth.confirm_password')} htmlFor="pwd-confirm" required>
              <Input
                id="pwd-confirm"
                name="confirm_password"
                type="password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                autoComplete="new-password"
                disabled={changeBusy}
              />
            </FormField>
            {changeError ? (
              <Alert variant="error" title={changeError} closable onClose={() => setChangeError(null)} />
            ) : null}
            <Button type="submit" variant="primary" size="lg" fullWidth loading={changeBusy}>
              {t('ies.auth.change_password')}
            </Button>
          </form>
        ) : null}

        {view === 'takeover' ? (
          <div className="ies-login__takeover">
            <Alert variant="warning" title={t('ies.auth.window_takeover_title')}>
              <p>{t('ies.auth.window_takeover_desc')}</p>
            </Alert>
            {takeoverError ? <Alert variant="error" title={takeoverError} /> : null}
            <div className="ies-login__actions">
              <Button variant="primary" size="lg" loading={takeoverBusy} onClick={handleConfirmTakeover}>
                {t('ies.auth.window_takeover_confirm')}
              </Button>
              <Button variant="secondary" size="lg" disabled={takeoverBusy} onClick={handleCancelTakeover}>
                {t('ies.auth.window_takeover_cancel')}
              </Button>
            </div>
          </div>
        ) : null}
      </section>
    </main>
  )
}
