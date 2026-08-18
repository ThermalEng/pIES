/**
 * 工作台路由入口(/projects/:id)。
 *
 * 挂载 WorkbenchProvider 并渲染 ProjectPage(框架布局)+ 嵌套子页面路由:
 *
 *   模型      model       -> src/pages/ModelPage.tsx      (并行 agent 实现)
 *   数据      data        -> src/pages/DataPage.tsx       (并行 agent 实现)
 *   配置      config      -> src/pages/ConfigPage.tsx     (并行 agent 实现)
 *   校验      validation  -> src/pages/ValidationPage.tsx (并行 agent 实现)
 *   任务      tasks       -> src/pages/TasksPage.tsx      (本单元)
 *   结果      results     -> src/pages/ResultsPage.tsx    (本单元)
 *   导出      export      -> src/pages/ExportPage.tsx     (并行 agent 实现)
 *
 * 未就绪的页面按 App.tsx 的约定惰性加载,回退到占位组件,不阻塞构建;
 * 页面文件就绪后(导入路径匹配)自动无缝启用。
 */

import { lazy } from 'react'
import type { ComponentType } from 'react'
import { Navigate, Route, Routes, useParams } from 'react-router-dom'

import { Spinner } from '../components/ui'
import { useI18n } from '../i18n'
import ProjectPage from './ProjectPage'
import ResultsPage from './ResultsPage'
import TasksPage from './TasksPage'
import { WorkbenchProvider } from './workbench'

/** 并行 agent 实现的子页面(惰性扫描,缺失时回退占位)。 */
const subPageLoaders = import.meta.glob<{ default: ComponentType }>([
  './ModelPage.tsx',
  './DataPage.tsx',
  './ConfigPage.tsx',
  './ValidationPage.tsx',
  './ExportPage.tsx',
])

/** 页面占位组件(对应页面尚未实现时展示)。 */
function PagePlaceholder(name: string): ComponentType {
  return function PagePlaceholderView() {
    const { t } = useI18n()
    return (
      <div className="ies-page-placeholder" role="status">
        <Spinner size="lg" />
        <p>
          {name} · {t('ies.common.loading')}
        </p>
      </div>
    )
  }
}

/** 惰性加载子页面;模块缺失或加载失败时回退占位,保证工作台可启动。 */
function lazySubPage(file: string): ComponentType {
  return lazy(async () => {
    const loader = subPageLoaders[file]
    if (!loader) return { default: PagePlaceholder(file) }
    try {
      const mod = await loader()
      if (!mod.default) return { default: PagePlaceholder(file) }
      return mod
    } catch {
      return { default: PagePlaceholder(file) }
    }
  })
}

const ModelPage = lazySubPage('./ModelPage.tsx')
const DataPage = lazySubPage('./DataPage.tsx')
const ConfigPage = lazySubPage('./ConfigPage.tsx')
const ValidationPage = lazySubPage('./ValidationPage.tsx')
const ExportPage = lazySubPage('./ExportPage.tsx')

/** 工作台路由入口:解析 :id 并渲染 Provider + 嵌套路由。 */
export default function WorkbenchPage() {
  const { id } = useParams()
  const projectId = Number(id)

  if (!Number.isFinite(projectId) || projectId <= 0) {
    return <Navigate to="/" replace />
  }

  return (
    <WorkbenchProvider projectId={projectId}>
      <Routes>
        <Route element={<ProjectPage />}>
          <Route index element={<Navigate to="tasks" replace />} />
          <Route path="model" element={<ModelPage />} />
          <Route path="data" element={<DataPage />} />
          <Route path="config" element={<ConfigPage />} />
          <Route path="validation" element={<ValidationPage />} />
          <Route path="tasks" element={<TasksPage />} />
          <Route path="results" element={<ResultsPage />} />
          <Route path="export" element={<ExportPage />} />
        </Route>
      </Routes>
    </WorkbenchProvider>
  )
}
