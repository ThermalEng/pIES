/**
 * 应用路由表与外壳布局。
 *
 * 路由:
 *   /login          登录页(已登录则重定向首页)
 *   /               项目列表
 *   /projects/:id   项目工作台
 *   /help/*         帮助中心(独立页,无外壳,静态可读)
 *   /settings       系统设置(管理员)
 *   *               404
 *
 * 页面组件约定(由并行 agent 实现,路径即约定,不得改名):
 *   src/pages/LoginPage.tsx         默认导出登录组件
 *   src/pages/ProjectListPage.tsx   默认导出项目列表组件
 *   src/pages/WorkbenchPage.tsx     默认导出工作台组件(useParams 取 :id)
 *   src/pages/HelpPage.tsx          默认导出帮助中心组件
 *   src/pages/SettingsPage.tsx      默认导出设置组件
 *
 * 页面通过 import.meta.glob 惰性扫描加载:页面文件未就绪时自动回退到内联
 * 占位组件,不阻塞构建;页面就绪后无缝启用(代码分割)。
 */

import { Suspense, lazy, useEffect } from 'react'
import type { ComponentType, ReactNode } from 'react'
import {
  Link,
  Navigate,
  NavLink,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from 'react-router-dom'

import { api, hasSession, setUnauthorizedHandler } from './api/client'
import { useI18n } from './i18n'
import { Button, Icon, Spinner } from './components/ui'

/** 页面模块约定路径(见文件头注释),惰性扫描避免页面未就绪时构建失败。 */
const pageLoaders = import.meta.glob<{ default: ComponentType }>('./pages/*.tsx')

/** 页面占位组件(对应页面尚未实现时展示)。 */
function PagePlaceholder(name: string): ComponentType {
  return function PagePlaceholderView() {
    const { t } = useI18n()
    return (
      <div className="ies-page-placeholder">
        <Spinner size="lg" />
        <p>
          {name} · {t('ies.common.loading')}
        </p>
      </div>
    )
  }
}

/** 页面加载失败页:显示错误并提供重试(替代永久"加载中", 避免旧构建缓存导致卡死)。 */
function PageLoadError(name: string): ComponentType {
  return function PageLoadErrorView() {
    const { t } = useI18n()
    return (
      <div className="ies-page-placeholder" role="alert">
        <Icon name="warning" size={32} />
        <p>
          {name} · {t('ies.common.load_failed')}
        </p>
        <Button variant="primary" onClick={() => window.location.reload()}>
          {t('ies.common.retry')}
        </Button>
        <p className="ies-page-subtitle">{t('ies.common.load_failed_hint')}</p>
      </div>
    )
  }
}

/** 惰性加载页面;模块缺失或加载失败时回退占位,保证应用可启动。 */
function lazyPage(name: string) {
  return lazy(async () => {
    const loader = pageLoaders[`./pages/${name}.tsx`]
    if (!loader) return { default: PagePlaceholder(name) }
    try {
      const mod = await loader()
      if (!mod.default) return { default: PagePlaceholder(name) }
      return mod
    } catch {
      // 构建产物更新后旧 chunk 哈希失效: 显示可重试错误页, 而非永久"加载中"
      return { default: PageLoadError(name) }
    }
  })
}

const LoginPage = lazyPage('LoginPage')
const ProjectListPage = lazyPage('ProjectListPage')
const WorkbenchPage = lazyPage('WorkbenchPage')
const HelpPage = lazyPage('HelpPage')
const SettingsPage = lazyPage('SettingsPage')

/** 整页加载指示(路由切换 Suspense 兜底)。 */
function PageLoading() {
  const { t } = useI18n()
  return (
    <div className="ies-page-placeholder" role="status" aria-label={t('ies.common.loading')}>
      <Spinner size="lg" />
    </div>
  )
}

/** 认证守卫:无会话标记跳登录(权威校验由后端完成,401 由 client 统一处理)。 */
function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation()
  if (!hasSession()) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return <>{children}</>
}

/** 公开页守卫:已登录访问 /login 时重定向首页。 */
function PublicOnly({ children }: { children: ReactNode }) {
  if (hasSession()) {
    return <Navigate to="/" replace />
  }
  return <>{children}</>
}

/** 应用外壳:顶栏(品牌/导航/语言/退出)+ 内容出口。 */
function AppShell() {
  const { t, locale, setLocale } = useI18n()
  const navigate = useNavigate()
  const location = useLocation()

  // 会话失效(401)统一处理:清除会话并跳转登录页
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (location.pathname !== '/login') {
        navigate('/login', { replace: true, state: { reason: 'expired' } })
      }
    })
    return () => setUnauthorizedHandler(null)
  }, [navigate, location.pathname])

  const handleLogout = () => {
    void api.auth.logout().finally(() => {
      navigate('/login', { replace: true })
    })
  }

  const toggleLocale = () => {
    setLocale(locale === 'zh' ? 'en' : 'zh')
  }

  return (
    <div className="ies-app">
      <header className="ies-topbar">
        <Link to="/" className="ies-topbar__brand" aria-label={t('ies.nav.app_title')}>
          <span className="ies-topbar__brand-mark" aria-hidden="true">
            IES
          </span>
          <span>{t('ies.nav.app_title')}</span>
        </Link>
        <nav className="ies-topbar__nav" aria-label={t('ies.nav.projects')}>
          <NavLink to="/" end className="ies-topbar__link">
            {t('ies.nav.projects')}
          </NavLink>
          <NavLink to="/help" className="ies-topbar__link">
            {t('ies.nav.help')}
          </NavLink>
          <NavLink to="/settings" className="ies-topbar__link">
            {t('ies.nav.settings')}
          </NavLink>
        </nav>
        <div className="ies-topbar__spacer" />
        <div className="ies-topbar__actions">
          <Button variant="ghost" size="sm" onClick={toggleLocale} aria-label={t('ies.nav.language')}>
            {locale === 'zh' ? 'EN' : '中文'}
          </Button>
          <Button variant="secondary" size="sm" onClick={handleLogout}>
            <Icon name="cross" size={13} />
            {t('ies.nav.logout')}
          </Button>
        </div>
      </header>
      <main className="ies-app__main">
        <div className="ies-content">
          <Outlet />
        </div>
      </main>
    </div>
  )
}

/** 404 页面。 */
function NotFoundPage() {
  const { t } = useI18n()
  return (
    <div className="ies-page-placeholder">
      <h1 className="ies-page-title">{t('ies.common.page_not_found')}</h1>
      <Link to="/" className="ies-btn ies-btn--primary">
        {t('ies.common.back_home')}
      </Link>
    </div>
  )
}

/** 路由表。 */
export default function App() {
  return (
    <Suspense fallback={<PageLoading />}>
      <Routes>
        <Route
          path="/login"
          element={
            <PublicOnly>
              <LoginPage />
            </PublicOnly>
          }
        />
        {/* 帮助中心(独立页,无外壳,静态可读)。
            RR-P2-10: 显式 lang/pageId 段路由, useParams 才能解析深链接
            (/help/zh-CN/getting-started); "/help/*" 通配下 useParams 拿不到段参数。 */}
        <Route path="/help" element={<HelpPage />} />
        <Route path="/help/:lang" element={<HelpPage />} />
        <Route path="/help/:lang/:pageId" element={<HelpPage />} />
        {/* 应用外壳(需登录) */}
        <Route
          element={
            <RequireAuth>
              <AppShell />
            </RequireAuth>
          }
        >
          <Route path="/" element={<ProjectListPage />} />
          <Route path="/projects/:id/*" element={<WorkbenchPage />} />
          <Route path="/settings" element={<SettingsPage />} />
        </Route>
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </Suspense>
  )
}
