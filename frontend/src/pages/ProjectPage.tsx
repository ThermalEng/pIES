/**
 * 工作台框架(ProjectPage):侧边导航 + 项目信息头 + 内容出口。
 *
 * - 侧边导航:模型 / 数据 / 配置 / 校验 / 任务 / 结果 / 导出。
 * - 项目信息头:项目名称、版本、币种、UTC 偏移、自动保存状态、离线徽章。
 *
 * 子页面通过 useWorkbench()/useAutosave()(见 workbench.tsx)读取上下文;
 * 保存草稿时调用 useAutosave().setStatus(...) 更新头部状态指示。
 * 路由挂载见 WorkbenchPage.tsx(子页面均为其嵌套路由)。
 */

import type { ReactNode } from 'react'
import { NavLink, Outlet } from 'react-router-dom'

import { Icon, Spinner } from '../components/ui'
import { useI18n } from '../i18n'
import { formatUtcOffset, useWorkbench } from './workbench'
import type { AutosaveStatus } from './workbench'
import './workbench.css'

type NavIconName = 'model' | 'data' | 'config' | 'validation' | 'tasks' | 'results' | 'export'

interface NavItem {
  path: string
  labelKey: string
  icon: NavIconName
}

/** 侧边导航项(路径即子页面路由名,与 WorkbenchPage 路由一致)。 */
const NAV_ITEMS: NavItem[] = [
  { path: 'model', labelKey: 'ies.nav.model', icon: 'model' },
  { path: 'data', labelKey: 'ies.nav.data', icon: 'data' },
  { path: 'config', labelKey: 'ies.nav.config', icon: 'config' },
  { path: 'validation', labelKey: 'ies.nav.validation', icon: 'validation' },
  { path: 'tasks', labelKey: 'ies.nav.tasks', icon: 'tasks' },
  { path: 'results', labelKey: 'ies.nav.results', icon: 'results' },
  { path: 'export', labelKey: 'ies.nav.exports', icon: 'export' },
]

const NAV_ICON_PATHS: Record<NavIconName, ReactNode> = {
  model: (
    <>
      <circle cx="6" cy="6" r="2.2" />
      <circle cx="18" cy="6" r="2.2" />
      <circle cx="12" cy="18" r="2.2" />
      <path d="M8.2 6h7.6M7.4 7.6l4.6 8.4M16.6 7.6l-4.6 8.4" />
    </>
  ),
  data: (
    <>
      <ellipse cx="12" cy="5" rx="8" ry="2.6" />
      <path d="M4 5v14c0 1.4 3.6 2.6 8 2.6s8-1.2 8-2.6V5" />
      <path d="M4 12c0 1.4 3.6 2.6 8 2.6s8-1.2 8-2.6" />
    </>
  ),
  config: (
    <>
      <path d="M4 8h10M18 8h2M4 16h2M10 16h10" />
      <circle cx="16" cy="8" r="2" />
      <circle cx="8" cy="16" r="2" />
    </>
  ),
  validation: (
    <>
      <path d="M12 3l7 3v5c0 4.6-3 8-7 10-4-2-7-5.4-7-10V6z" />
      <path d="M9 12l2 2 4-4" />
    </>
  ),
  tasks: (
    <>
      <path d="M9 6h11M9 12h11M9 18h11" />
      <circle cx="4.5" cy="6" r="1" />
      <circle cx="4.5" cy="12" r="1" />
      <circle cx="4.5" cy="18" r="1" />
    </>
  ),
  results: (
    <>
      <path d="M4 20v-9M10 20V4M16 20v-7M21 20H3" />
    </>
  ),
  export: (
    <>
      <path d="M4 17v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" />
      <path d="M12 3v11M7 9l5 5 5-5" />
    </>
  ),
}

function NavIcon({ name }: { name: NavIconName }) {
  return (
    <svg
      className="wb-nav-icon"
      viewBox="0 0 24 24"
      width="16"
      height="16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {NAV_ICON_PATHS[name]}
    </svg>
  )
}

/** 自动保存状态指示(saved/saving/dirty/error 四态)。 */
function AutosaveIndicator({ status }: { status: AutosaveStatus }) {
  const { t } = useI18n()
  if (status === 'saving') {
    return (
      <span className="wb-autosave wb-autosave--saving" role="status" aria-live="polite">
        <Spinner size="sm" />
        {t('ies.workbench.autosave_saving')}
      </span>
    )
  }
  if (status === 'dirty') {
    return (
      <span className="wb-autosave wb-autosave--dirty" role="status" aria-live="polite">
        <span className="wb-autosave__dot" aria-hidden="true" />
        {t('ies.workbench.autosave_dirty')}
      </span>
    )
  }
  if (status === 'error') {
    return (
      <span className="wb-autosave wb-autosave--error" role="alert">
        <Icon name="cross" size={13} />
        {t('ies.workbench.autosave_error')}
      </span>
    )
  }
  return (
    <span className="wb-autosave wb-autosave--saved" role="status" aria-live="polite">
      <Icon name="check" size={13} />
      {t('ies.workbench.autosave_saved')}
    </span>
  )
}

/**
 * 工作台框架布局(默认导出)。
 * 渲染侧边导航与项目信息头,并通过 <Outlet /> 渲染嵌套子页面。
 */
export default function ProjectPage() {
  const { t } = useI18n()
  const { projectId, project, currentVersion, offline, autosave } = useWorkbench()

  const projectName = project?.name ?? t('ies.nav.workbench')

  return (
    <div className="wb">
      <aside className="wb-sidebar">
        <div className="wb-sidebar__head">
          <span className="wb-sidebar__mark" aria-hidden="true">
            IES
          </span>
          <span className="wb-sidebar__project-name" title={projectName}>
            {projectName}
          </span>
        </div>
        <nav className="wb-sidebar__nav" aria-label={t('ies.nav.workbench')}>
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.path}
              to={`/projects/${projectId}/${item.path}`}
              className={({ isActive }) =>
                `wb-nav-item ${isActive ? 'wb-nav-item--active' : ''}`.trim()
              }
            >
              <NavIcon name={item.icon} />
              <span>{t(item.labelKey)}</span>
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="wb-main">
        <header className="wb-header">
          <div className="wb-header__info">
            <h1 className="wb-header__title">{projectName}</h1>
            {project ? (
              <div className="wb-header__meta">
                <span>
                  {t('ies.workbench.version')}: v{currentVersion?.version_no ?? '—'}
                </span>
                <span>
                  {t('ies.workbench.currency')}:{' '}
                  {project.currency === 'CNY' ? t('ies.unit.cny') : t('ies.unit.usd')}(
                  {project.currency})
                </span>
                <span>
                  {t('ies.workbench.utc')}: {formatUtcOffset(project.fixed_utc_offset_minutes)}
                </span>
              </div>
            ) : null}
          </div>
          <div className="wb-header__status">
            <AutosaveIndicator status={autosave} />
            {offline ? (
              <span
                className="ies-badge ies-badge--warning ies-badge--shape-triangle"
                title={t('ies.workbench.offline_hint')}
                role="status"
              >
                <span className="ies-badge__icon" aria-hidden="true">
                  <Icon name="warning" size={12} />
                </span>
                <span className="ies-badge__label">{t('ies.workbench.offline')}</span>
              </span>
            ) : null}
          </div>
        </header>
        <main className="wb-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
