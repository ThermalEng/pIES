/**
 * 版本历史页(VersionsPage): 项目内版本历史入口。
 *
 * 页面只组合 feature(hook + 面板), 不直接拼 DTO:
 * - 当前版本标识来自 workbench 项目摘要(currentVersion), 来源草稿修订来自
 *   列表/详情的 source_draft_revision(后端真实字段)。
 * - 创建成功后经 workbench refresh 刷新项目 current_version 与列表。
 * - 恢复入口在版本详情内(确认影响后执行); 结果应用与包导入见各自页面。
 */

import { VersionHistoryPanel } from '../features/project-versions/components/VersionHistoryPanel'
import { useProjectVersions } from '../features/project-versions/hooks/useProjectVersions'
import { useWorkbench } from './workbench'

/** 版本历史页(默认导出, 供 WorkbenchPage 路由挂载)。 */
export default function VersionsPage() {
  const { projectId, project, currentVersion, refreshDetailed } = useWorkbench()
  const store = useProjectVersions(projectId, refreshDetailed)

  return (
    <VersionHistoryPanel
      store={store}
      currentVersionId={currentVersion?.id ?? project?.current_version_id ?? null}
    />
  )
}
