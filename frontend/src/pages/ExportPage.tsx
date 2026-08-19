/**
 * 导出(ExportPage):Excel 报告导出 + 完整项目包导出。
 *
 * - Excel 报告:语言选择(中文/English,默认中文)+ 导出按钮;
 *   固定引用最近完成任务的证据包与最新评估(适配层 resolveAssessmentId
 *   依赖 证据包→任务 映射缓存,故先取任务详情填充),导出成功经
 *   api.exports.download(report.id) 取 Blob 后由 downloadBlob 触发浏览器下载。
 * - 完整项目包:仅项目所有者可导出(后端 403);非所有者时按钮禁用并提示。
 *   包内容为模型/版本/数据/历史结果,不含账号/权限/会话。
 * - 错误处理:API 错误经 translateError 渲染为诊断文案(Alert)。
 * - 可访问性:原生 button/select 控件,Tab+Enter 可达。
 *
 * 挂载方式:工作台子路由 /projects/:id/export(见 WorkbenchPage)。
 */

import { useCallback, useEffect, useState } from 'react'

import { api, downloadBlob } from '../api/client'
import { Alert, Button, Card, FormField, Select, Spinner } from '../components/ui'
import { translateError, useI18n } from '../i18n'
import { pt } from '../i18n/pageMessages'
import type { ApiError, ExcelExportInput } from '../types'
import { useWorkbench } from './workbench'

/** 报告语言(与后端 ExcelExportRequest.lang 的 zh/en 对齐)。 */
type ExportLang = 'zh' | 'en'

/** 解析最近完成任务的证据包 id(Excel 报告固定引用证据包 + 最新评估)。 */
async function resolveEvidencePackage(projectId: number): Promise<number | null> {
  const page = await api.tasks.list({ project_id: projectId, limit: 100 })
  const done = page.items
    .filter((tk) => tk.status === 'completed')
    .sort((a, b) => b.requested_at.localeCompare(a.requested_at))
  // 最多尝试最近的 10 个已完成任务
  for (const tk of done.slice(0, 10)) {
    try {
      // 先取任务详情,填充适配层的 证据包→任务 映射缓存(Excel 导出反查评估依赖)
      await api.tasks.get(projectId, tk.id)
      const r = await api.results.result(projectId, tk.id)
      if (r.evidence_package_id > 0) return r.evidence_package_id
    } catch {
      // 单任务解析失败继续尝试下一个
    }
  }
  return null
}

export default function ExportPage() {
  useI18n() // 订阅语言切换,pt() 随全局表联动
  const { projectId, project } = useWorkbench()

  // -------------------------------------------------------------------------
  // 数据状态
  // -------------------------------------------------------------------------
  const [lang, setLang] = useState<ExportLang>('zh')
  /** 证据包 id:null = 无可导出结果,undefined = 解析中。 */
  const [evidence, setEvidence] = useState<number | null | undefined>(undefined)
  const [resolving, setResolving] = useState(true)

  const [exportError, setExportError] = useState<string | null>(null)
  /** 当前进行中的导出动作(excel / package / null)。 */
  const [exportBusy, setExportBusy] = useState<'excel' | 'package' | null>(null)
  const [okMsg, setOkMsg] = useState<string | null>(null)

  // -------------------------------------------------------------------------
  // 挂载时解析证据包
  // -------------------------------------------------------------------------
  useEffect(() => {
    let cancelled = false
    const resolve = async () => {
      setResolving(true)
      try {
        const found = await resolveEvidencePackage(projectId)
        if (!cancelled) setEvidence(found)
      } catch {
        if (!cancelled) setEvidence(null)
      } finally {
        if (!cancelled) setResolving(false)
      }
    }
    void resolve()
    return () => {
      cancelled = true
    }
  }, [projectId])

  // -------------------------------------------------------------------------
  // 动作
  // -------------------------------------------------------------------------

  /** Excel 报告导出(固定引用证据包 + 最新评估,报告语言可选)。 */
  const exportExcel = useCallback(async () => {
    if (evidence === null || evidence === undefined) return
    setExportError(null)
    setOkMsg(null)
    setExportBusy('excel')
    try {
      // lang 为前端约定附加参数(适配层透传给后端,缺省 zh)
      const input = {
        project_id: projectId,
        evidence_package_id: evidence,
        include_hourly: true,
        include_diagnostics: true,
        lang,
      } as ExcelExportInput
      const report = await api.exports.excel(input)
      const { blob, filename } = await api.exports.download(report.id)
      downloadBlob(blob, filename)
      setOkMsg(pt('ies.export.download_ok', { filename }))
    } catch (err) {
      setExportError(translateError(err as ApiError))
    } finally {
      setExportBusy(null)
    }
  }, [projectId, evidence, lang])

  /** 完整项目包导出(仅所有者;后端对查看者返回 403)。 */
  const exportPackage = useCallback(async () => {
    if (project?.role !== 'owner') return
    setExportError(null)
    setOkMsg(null)
    setExportBusy('package')
    try {
      const report = await api.exports.package({ project_id: projectId, include_versions: true })
      const { blob, filename } = await api.exports.download(report.id)
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
        {!resolving && !excelReady ? (
          <div style={{ marginTop: 'var(--ies-space-3)' }}>
            <Alert variant="warning">{pt('ies.export.no_result')}</Alert>
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
