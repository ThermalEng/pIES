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
 * 已发版子页面按约定路径惰性加载(代码分割); 模块缺失或分块加载失败均
 * 显示可见错误与重试, 与顶层路由同一失败组件。
 */

import { lazy } from 'react'
import type { ComponentType } from 'react'
import { Navigate, Route, Routes, useParams } from 'react-router-dom'

import { PageLoadError } from '../components/ui'
import ProjectPage from './ProjectPage'
import ResultsPage from './ResultsPage'
import TasksPage from './TasksPage'
import VersionsPage from './VersionsPage'
import { WorkbenchProvider } from './workbench'

/** 已发版子页面模块(见文件头注释),惰性加载做代码分割。 */
const subPageLoaders = import.meta.glob<{ default: ComponentType }>([
  './ModelPage.tsx',
  './model/NewModelPage.tsx',
  './DataPage.tsx',
  './ConfigPage.tsx',
  './FinancePage.tsx',
  './ValidationPage.tsx',
  './ExportPage.tsx',
])

/** 惰性加载已发版子页面; 模块缺失或分块加载失败均显示可见错误与重试。 */
function lazySubPage(file: string): ComponentType {
  return lazy(async () => {
    const loader = subPageLoaders[file]
    if (!loader) return { default: PageLoadError }
    try {
      return await loader()
    } catch {
      return { default: PageLoadError }
    }
  })
}

const ModelPage = lazySubPage('./ModelPage.tsx')
const NewModelPage = lazySubPage('./model/NewModelPage.tsx')
const DataPage = lazySubPage('./DataPage.tsx')
const ConfigPage = lazySubPage('./ConfigPage.tsx')
const FinancePage = lazySubPage('./FinancePage.tsx')
const ValidationPage = lazySubPage('./ValidationPage.tsx')
const ExportPage = lazySubPage('./ExportPage.tsx')

/** 工作台路由入口:解析 :id 并渲染 Provider + 嵌套路由。 */
export default function WorkbenchPage() {
  const { id } = useParams()
  const projectId = Number(id)

  if (!Number.isFinite(projectId) || projectId <= 0) {
    return <Navigate to="/" replace />
  }

  // 项目归属即实例归属: :id 切换即全新 Provider(摘要/版本/选择均重读,
  // 旧实例在途读回包无法发布到新项目, 与画布 projectId 键控同一所有权)。
  return (
    <WorkbenchProvider key={projectId} projectId={projectId}>
      <Routes>
        <Route element={<ProjectPage />}>
          <Route index element={<Navigate to="tasks" replace />} />
          <Route path="model" element={<ModelPage />} />
          <Route path="model/new" element={<NewModelPage />} />
          <Route path="data" element={<DataPage />} />
          <Route path="config" element={<ConfigPage />} />
          <Route path="finance" element={<FinancePage />} />
          <Route path="validation" element={<ValidationPage />} />
          <Route path="tasks" element={<TasksPage />} />
          <Route path="versions" element={<VersionsPage />} />
          <Route path="results" element={<ResultsPage />} />
          <Route path="export" element={<ExportPage />} />
        </Route>
      </Routes>
    </WorkbenchProvider>
  )
}
