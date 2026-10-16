/**
 * 导出(ExportPage):Excel 报告导出 + 完整项目包导出。
 *
 * - Excel 报告:语言选择(中文/English,默认中文)+ 导出按钮;
 *   固定引用全局所选任务的证据包与最新评估(无显式选择时默认最新完成;
 *   所选任务未完成/无结果时禁用导出且不回退他任务; 评估历史每次真实读取),
 *   导出成功经 api.exports.download(本地下载会话 id) 取 Blob 后由
 *   downloadBlob 触发浏览器下载。
 * - 完整项目包:仅项目所有者可导出(后端 403);非所有者时按钮禁用并提示。
 *   包内容为模型/版本/数据/历史结果,不含账号/权限/会话。
 * - 错误处理:API 错误经 translateError 渲染为诊断文案(Alert)。
 * - 可访问性:原生 button/select 控件,Tab+Enter 可达。
 *
 * 挂载方式:工作台子路由 /projects/:id/export(见 WorkbenchPage)。
 */

import { useCallback, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, downloadBlob, isNoAssessmentError } from '../api/client'
import { Alert, Button, Card, FormField, Select, Spinner } from '../components/ui'
import { translateError, useI18n } from '../i18n'
import { pt } from '../i18n/pageMessages'
import type { ApiError, ExcelExportInput } from '../types'
import { useEffectiveTaskContext, useWorkbench } from './workbench'

/** 报告语言(与后端 ExcelExportRequest.lang 的 zh/en 对齐)。 */
type ExportLang = 'zh' | 'en'

export default function ExportPage() {
  useI18n() // 订阅语言切换,pt() 随全局表联动
  const { projectId, project } = useWorkbench()
  // 有效任务上下文(与结果页同一规则、同一来源; 只读列表与详情,
  // 不发任务/评估 POST)。Excel 固定引用所选任务的证据包 + 其最新评估;
  // 项目包导出与任务选择无关。
  const taskCtx = useEffectiveTaskContext()

  // -------------------------------------------------------------------------
  // 数据状态
  // -------------------------------------------------------------------------
  const [lang, setLang] = useState<ExportLang>('zh')
  /** 证据包 id:null = 无可导出结果,undefined = 解析中。 */
  const evidence = taskCtx.loading ? undefined : taskCtx.picked ? taskCtx.picked.packageId : null
  /** 所选任务 id(与证据同源; 导出评估按此任务读取)。 */
  const pickedTaskId = taskCtx.picked ? taskCtx.picked.taskId : 0
  const resolving = taskCtx.loading
  /** 显式选择无可用结果(未完成/无证据): Excel 禁用, 不回退他任务。 */
  const explicitBlocked = !taskCtx.loading && taskCtx.explicitBlocked
  /** 解析失败(列表/任一详情失败): 明确加载失败并可重试, 不当无结果。 */
  const resolveError = taskCtx.error

  const [exportError, setExportError] = useState<string | null>(null)
  /** 导出缺评估引导(无既有评估时置 true, 不隐式创建评估)。 */
  const [exportNeedsAssess, setExportNeedsAssess] = useState(false)
  /** 当前进行中的导出动作(excel / package / null)。 */
  const [exportBusy, setExportBusy] = useState<'excel' | 'package' | null>(null)
  const [okMsg, setOkMsg] = useState<string | null>(null)

  // -------------------------------------------------------------------------
  // 动作
  // -------------------------------------------------------------------------

  /** Excel 报告导出(固定引用证据包 + 最新评估,报告语言可选)。 */
  const exportExcel = useCallback(async () => {
    if (evidence === null || evidence === undefined) return
    setExportError(null)
    setOkMsg(null)
    setExportNeedsAssess(false)
    setExportBusy('excel')
    try {
      // lang 为契约字段(缺省 zh 由适配层按后端约定补齐)。
      // 若无既有评估, excel 直接抛缺评估业务错并由本页引导用户先完成
      // 系统评估, 不隐式创建评估。
      const input: ExcelExportInput = {
        project_id: projectId,
        task_id: pickedTaskId,
        evidence_package_id: evidence,
        include_hourly: true,
        include_diagnostics: true,
        lang,
      }
      const downloadSessionId = await api.exports.excel(input)
      const { blob, filename } = await api.exports.download(downloadSessionId)
      downloadBlob(blob, filename)
      setOkMsg(pt('ies.export.download_ok', { filename }))
    } catch (err) {
      if (isNoAssessmentError(err)) {
        setExportNeedsAssess(true)
      } else {
        setExportError(translateError(err as ApiError))
      }
    } finally {
      setExportBusy(null)
    }
  }, [projectId, evidence, pickedTaskId, lang])

  /** 完整项目包导出(仅所有者;后端对查看者返回 403)。 */
  const exportPackage = useCallback(async () => {
    if (project?.role !== 'owner') return
    setExportError(null)
    setOkMsg(null)
    setExportBusy('package')
    try {
      const downloadSessionId = await api.exports.package({ project_id: projectId, include_versions: true })
      const { blob, filename } = await api.exports.download(downloadSessionId)
      downloadBlob(blob, filename)
      setOkMsg(pt('ies.export.package_ok', { filename }))
    } catch (err) {
      setExportError(translateError(err as ApiError))
    } finally {
      setExportBusy(null)
    }
  }, [projectId, project?.role])

  const isOwner = project?.role === 'owner'
  const excelReady = evidence !== undefined && evidence !== null
  const excelDisabled = resolving || !excelReady || exportBusy !== null

  // -------------------------------------------------------------------------
  // 渲染
  // -------------------------------------------------------------------------
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--ies-space-4)' }}>
      <div className="ies-page-header">
        <div>
          <h1 className="ies-page-title">{pt('ies.export.title')}</h1>
          <p className="ies-page-subtitle">{pt('ies.export.subtitle')}</p>
        </div>
      </div>

      {okMsg ? (
        <Alert variant="success" closable onClose={() => setOkMsg(null)}>
          {okMsg}
        </Alert>
      ) : null}
      {exportError ? (
        <Alert
          variant="error"
          title={pt('ies.export.export_failed', { reason: '' })}
          closable
          onClose={() => setExportError(null)}
        >
          {exportError}
        </Alert>
      ) : null}
      {resolveError ? (
        <div className="ies-section">
          <Alert variant="error">{resolveError}</Alert>
          <div style={{ marginTop: 'var(--ies-space-2)' }}>
            <Button onClick={() => taskCtx.retry()}>{pt('ies.common.retry')}</Button>
          </div>
        </div>
      ) : null}
      {exportNeedsAssess ? (
        <div className="ies-section">
          <Alert variant="warning">{pt('ies.export.need_assessment')}</Alert>
          <div style={{ marginTop: 'var(--ies-space-2)' }}>
            <Link
              className="ies-btn ies-btn--secondary ies-btn--md"
              to={evidence == null ? `/projects/${projectId}/results` : `/projects/${projectId}/results?package=${evidence}`}
            >
              {pt('ies.export.goto_results')}
            </Link>
          </div>
        </div>
      ) : null}

      {/* Excel 报告导出 */}
      <Card title={pt('ies.export.excel')}>
        <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
          {pt('ies.export.excel_desc')}
        </p>
        <div className="ies-flex" style={{ marginTop: 'var(--ies-space-3)', flexWrap: 'wrap', gap: 'var(--ies-space-3)', alignItems: 'flex-end' }}>
          <FormField label={pt('ies.export.lang')} htmlFor="export-lang">
            <Select id="export-lang" value={lang} onChange={(e) => setLang(e.target.value as ExportLang)}>
              <option value="zh">{pt('ies.export.lang_zh')}</option>
              <option value="en">{pt('ies.export.lang_en')}</option>
            </Select>
          </FormField>
          <Button
            icon="download"
            onClick={() => void exportExcel()}
            loading={exportBusy === 'excel'}
            disabled={excelDisabled}
          >
            {pt('ies.export.excel_btn')}
          </Button>
          {resolving ? <Spinner size="sm" /> : null}
        </div>
        {!resolving && !excelReady && !resolveError ? (
          <div style={{ marginTop: 'var(--ies-space-3)' }}>
            <Alert variant="warning">
              {explicitBlocked ? pt('ies.export.selected_unfinished') : pt('ies.export.no_result')}
            </Alert>
          </div>
        ) : null}
      </Card>

      {/* 完整项目包导出 */}
      <Card title={pt('ies.export.package')}>
        <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
          {pt('ies.export.package_desc')}
        </p>
        {!isOwner ? (
          <div style={{ marginTop: 'var(--ies-space-3)' }}>
            <Alert variant="warning">{pt('ies.export.package_owner_only')}</Alert>
          </div>
        ) : null}
        <div className="ies-flex" style={{ marginTop: 'var(--ies-space-3)' }}>
          <Button
            icon="download"
            variant="secondary"
            onClick={() => void exportPackage()}
            loading={exportBusy === 'package'}
            disabled={!isOwner || exportBusy !== null}
          >
            {pt('ies.export.package_btn')}
          </Button>
        </div>
      </Card>
    </div>
  )
}
