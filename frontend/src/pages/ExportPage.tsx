/**
 * 导出(ExportPage):Excel 报告导出 + 完整项目包导出。
 *
 * - Excel 报告:语言选择(中文/English,默认中文)+ 导出按钮;
 *   固定引用最近完成任务的证据包与最新评估(适配层 resolveAssessmentId
 *   依赖 证据包→任务 映射缓存,故先取任务详情填充；评估历史每次真实读取),
 *   导出成功经 api.exports.download(本地下载会话 id) 取 Blob 后由
 *   downloadBlob 触发浏览器下载。
 * - 完整项目包:仅项目所有者可导出(后端 403);非所有者时按钮禁用并提示。
 *   包内容为模型/版本/数据/历史结果,不含账号/权限/会话。
 * - 错误处理:API 错误经 translateError 渲染为诊断文案(Alert)。
 * - 可访问性:原生 button/select 控件,Tab+Enter 可达。
 *
 * 挂载方式:工作台子路由 /projects/:id/export(见 WorkbenchPage)。
 */

import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { api, downloadBlob, isNoAssessmentError } from '../api/client'
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
  // 最多读取最近的 10 个已完成任务; 任一必需详情(含明确可用时的 /result
  // 补充读取)失败即整组失败, 不跳过失败任务降到其他包(调用方呈现并可重试)。
  const details = await Promise.all(done.slice(0, 10).map((tk) => api.tasks.get(projectId, tk.id)))
  for (const d of details) {
    if (d.evidence.length > 0) {
      return d.evidence[0].package_id
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
  /** 解析失败(列表/任一详情失败): 明确加载失败并可重试, 不当无结果。 */
  const [resolveError, setResolveError] = useState<string | null>(null)
  /** 解析重试序号: 仅重读列表与详情, 不发任务/评估 POST。 */
  const [resolveNonce, setResolveNonce] = useState(0)

  const [exportError, setExportError] = useState<string | null>(null)
  /** 导出缺评估引导(无既有评估时置 true, 不隐式创建评估)。 */
  const [exportNeedsAssess, setExportNeedsAssess] = useState(false)
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
      setResolveError(null)
      try {
        const found = await resolveEvidencePackage(projectId)
        if (cancelled) return
        setEvidence(found)
      } catch (err) {
        if (cancelled) return
        // 解析失败不是无结果: 清证据选择、显错并可重试, 不降到其他包。
        setEvidence(null)
        setResolveError(translateError(err as ApiError))
      } finally {
        if (!cancelled) setResolving(false)
      }
    }
    void resolve()
    return () => {
      cancelled = true
    }
  }, [projectId, resolveNonce])

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
  }, [projectId, evidence, lang])

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
            <Button onClick={() => setResolveNonce((n) => n + 1)}>{pt('ies.common.retry')}</Button>
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
