/**
 * 应用路由表与外壳布局。
 *
 * 路由:
 *   /login          登录页(已登录则重定向首页)
 *   /               项目列表
 *   /projects/:id   项目工作台
 *   /admin/users    账号管理(管理员)
 *   /admin/audit    审计日志(管理员,只读)
 *   /settings       系统设置(管理员)
 *   /help/*         帮助中心(一級菜单最后一项, 在应用外壳内)
 *   *               404
 *
 * 页面组件约定(路径即约定,不得改名):
 *   src/pages/LoginPage.tsx         默认导出登录组件
 *   src/pages/ProjectListPage.tsx   默认导出项目列表组件
 *   src/pages/WorkbenchPage.tsx     默认导出工作台组件(useParams 取 :id)
 *   src/pages/HelpPage.tsx          默认导出帮助中心组件
 *   src/pages/SettingsPage.tsx      默认导出设置组件
 *
 * 已发版页面按约定路径惰性加载(代码分割); 模块缺失或分块加载失败均显示
 * 可见错误与重试, 不再假设未来占位页。
 */

import { Suspense, lazy, useEffect, useState } from 'react'
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
  useOutletContext,
} from 'react-router-dom'

import { getMe, logout } from './features/auth'
import { hasSession, setUnauthorizedHandler } from './shared/http'
import { BrandMark } from './components/BrandMark'
import { useI18n } from './i18n'
import { Button, Icon, PageLoadError, Spinner } from './components/ui'
import type { User } from './types'

/** 已发版页面模块(见文件头注释),惰性加载做代码分割。 */
const pageLoaders = import.meta.glob<{ default: ComponentType }>('./pages/*.tsx')

/** 惰性加载已发版页面; 模块缺失或分块加载失败均显示可见错误与重试。 */
function lazyPage(name: string) {
  return lazy(async () => {
    const loader = pageLoaders[`./pages/${name}.tsx`]
    if (!loader) return { default: PageLoadError }
    try {
      return await loader()
    } catch {
      // 构建产物更新后旧 chunk 哈希失效等: 显示可重试错误页, 而非永久"加载中"
      return { default: PageLoadError }
    }
  })
}

const LoginPage = lazyPage('LoginPage')
const ProjectListPage = lazyPage('ProjectListPage')
const WorkbenchPage = lazyPage('WorkbenchPage')
const HelpPage = lazyPage('HelpPage')
const SettingsPage = lazyPage('SettingsPage')
const AdminUsersPage = lazyPage('AdminUsersPage')
const AdminAuditPage = lazyPage('AdminAuditPage')
const CustomizationPage = lazyPage('CustomizationPage')

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

/** 管理员页守卫: 依据外壳已加载的当前用户角色判定(权威校验仍由后端完成)。 */
function RequireAdmin({ children }: { children: ReactNode }) {
  const { isAdmin } = useOutletContext<{ isAdmin: boolean | null }>()
  if (isAdmin === null) return <PageLoading />
  if (!isAdmin) return <Navigate to="/" replace />
  return <>{children}</>
}

/** 应用外壳:顶栏(品牌/导航/语言/退出)+ 内容出口。 */
function AppShell() {
  const { t, locale, setLocale } = useI18n()
  const navigate = useNavigate()
  const location = useLocation()
  const [currentUser, setCurrentUser] = useState<User | null>(null)
  const [userLoaded, setUserLoaded] = useState(false)

  // 会话失效(401)统一处理:清除会话并跳转登录页
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (location.pathname !== '/login') {
        navigate('/login', { replace: true, state: { reason: 'expired' } })
      }
    })
    return () => setUnauthorizedHandler(null)
  }, [navigate, location.pathname])

  // 加载当前用户角色, 用于管理员导航/页面守卫(后端 403 为权威闸门)
  useEffect(() => {
    let cancelled = false
    getMe()
      .then((u) => {
        if (!cancelled) {
          setCurrentUser(u)
          setUserLoaded(true)
        }
      })
      .catch(() => {
        if (!cancelled) setUserLoaded(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const isAdmin = userLoaded ? currentUser?.role === 'admin' : null

  const handleLogout = () => {
    void logout().finally(() => {
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
          <BrandMark className="ies-topbar__brand-mark" />
          <span className="ies-topbar__brand-name">{t('ies.nav.app_title')}</span>
        </Link>
        <nav className="ies-topbar__nav" aria-label={t('ies.nav.projects')}>
          <NavLink to="/" end className="ies-topbar__link">
            {t('ies.nav.projects')}
          </NavLink>
          <NavLink to="/custom" className="ies-topbar__link">
            {t('ies.nav.custom')}
          </NavLink>
          {isAdmin === true ? (
            <NavLink to="/admin/users" className="ies-topbar__link">
              {t('ies.nav.accounts')}
            </NavLink>
          ) : null}
          {isAdmin === true ? (
            <NavLink to="/admin/audit" className="ies-topbar__link">
              {t('ies.nav.audit')}
            </NavLink>
          ) : null}
          <NavLink to="/settings" className="ies-topbar__link">
            {t('ies.nav.settings')}
          </NavLink>
          <NavLink to="/help" className="ies-topbar__link">
            {t('ies.nav.help')}
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
          <Outlet context={{ isAdmin }} />
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
        {/* 应用外壳(需登录) */}
        <Route
          element={
            <RequireAuth>
              <AppShell />
            </RequireAuth>
          }
        >
          <Route path="/" element={<ProjectListPage />} />
          {/* 自定义: 用户模型模板管理(一级菜单) */}
          <Route path="/custom" element={<CustomizationPage />} />
          <Route path="/projects/:id/*" element={<WorkbenchPage />} />
          <Route
            path="/admin/users"
            element={
              <RequireAdmin>
                <AdminUsersPage />
              </RequireAdmin>
            }
          />
          <Route
            path="/admin/audit"
            element={
              <RequireAdmin>
                <AdminAuditPage />
              </RequireAdmin>
            }
          />
          <Route path="/settings" element={<SettingsPage />} />
          {/* 帮助中心(一級菜单最后一项, 在应用外壳内, 无需返回应用按钮)。
              RR-P2-10: 显式 lang/pageId 段路由 */}
          <Route path="/help" element={<HelpPage />} />
          <Route path="/help/:lang" element={<HelpPage />} />
          <Route path="/help/:lang/:pageId" element={<HelpPage />} />
        </Route>
        <Route path="*" element={<NotFoundPage />} />
      </Routes>
    </Suspense>
  )
}
