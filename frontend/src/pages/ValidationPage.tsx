/**
 * 项目校验(ValidationPage):完整预检。
 *
 * - 运行校验:调 features/validation.runValidation(projectId) → {valid, diagnostics};
 *   由诊断严重度推导展示状态(ok / warnings / blocked),阻断时给出
 *   "禁止提交"提示;诊断列表复用 DiagnosticsList(严重度分组、代码、
 *   定位、修复建议),与数据/配置页展示一致。
 * - 财务基准确认入口已随旧形态经济参数移除而暂停(0.12-A1): 确认假设须由
 *   后续独立评价输入流程显式提供, 不再由计算配置派生默认值; 后端
 *   baseline-confirm 接口保留, 待评价输入切片恢复显式确认 UI。
 * - 错误处理:API 错误经 translateError 渲染为诊断文案(Alert)。
 * - 可访问性:所有操作均为原生 button/select,Tab+Enter 可达,
 *   状态徽章同时以文字 + 图标 + 形状编码。
 *
 * 挂载方式:工作台子路由 /projects/:id/validation(见 WorkbenchPage)。
 */

import { useCallback, useState } from 'react'

import { runValidation as runValidationRequest } from '../features/validation'
import { DiagnosticsList } from '../components/DiagnosticsList'
import { Alert, Badge, Button, Card, EmptyState } from '../components/ui'
import type { BadgeVariant, IconName } from '../components/ui'
import { translateError, useI18n } from '../i18n'
import { pt } from '../i18n/pageMessages'
import type { ApiError, Diagnostic, ValidationResult } from '../types'
import { useWorkbench } from './workbench'

/** 校验状态(由诊断严重度推导,与后端 report.status 的 ok/warnings/blocked 对齐)。 */
type ValidationStatus = 'ok' | 'warnings' | 'blocked'

/** 从诊断列表推导校验状态:存在阻断/错误 → blocked;仅警告 → warnings;否则 ok。 */
function deriveStatus(diagnostics: Diagnostic[]): ValidationStatus {
  if (diagnostics.some((d) => d.blocking || d.severity === 'blocking')) return 'blocked'
  if (diagnostics.some((d) => d.severity === 'warning')) return 'warnings'
  return 'ok'
}

/** 校验状态徽章(文字 + 图标双重编码)。 */
function ValidationStatusBadge({ status }: { status: ValidationStatus }) {
  useI18n() // 订阅语言切换,pt() 随全局表联动
  const map: Record<ValidationStatus, { variant: BadgeVariant; icon: IconName }> = {
    ok: { variant: 'success', icon: 'check' },
    warnings: { variant: 'warning', icon: 'warning' },
    blocked: { variant: 'danger', icon: 'stop' },
  }
  const cfg = map[status]
  return <Badge label={pt(`ies.validation.status_${status}`)} variant={cfg.variant} icon={cfg.icon} />
}

export default function ValidationPage() {
  useI18n() // 订阅语言切换,pt() 随全局表联动
  const { projectId } = useWorkbench()

  // -------------------------------------------------------------------------
  // 数据状态
  // -------------------------------------------------------------------------
  const [result, setResult] = useState<ValidationResult | null>(null)
  const [status, setStatus] = useState<ValidationStatus | null>(null)
  const [running, setRunning] = useState(false)
  const [runError, setRunError] = useState<string | null>(null)

  // -------------------------------------------------------------------------
  // 动作
  // -------------------------------------------------------------------------

  /** 运行完整预检(模型/数据/配置/就绪)。 */
  const runValidation = useCallback(async () => {
    setRunning(true)
    setRunError(null)
    try {
      const res = await runValidationRequest(projectId)
      setResult(res)
      setStatus(deriveStatus(res.diagnostics))
    } catch (err) {
      setRunError(translateError(err as ApiError))
    } finally {
      setRunning(false)
    }
  }, [projectId])

  const blocked = status === 'blocked'
  const blockingCount = result ? result.diagnostics.filter((d) => d.blocking || d.severity === 'blocking').length : 0

  // -------------------------------------------------------------------------
  // 渲染
  // -------------------------------------------------------------------------
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--ies-space-4)' }}>
      <div className="ies-page-header">
        <div>
          <h1 className="ies-page-title">{pt('ies.validation.title')}</h1>
          <p className="ies-page-subtitle">{pt('ies.validation.subtitle')}</p>
        </div>
        <div className="ies-flex">
          <Button icon="check" onClick={() => void runValidation()} loading={running}>
            {running ? pt('ies.validation.running') : pt('ies.validation.run')}
          </Button>
        </div>
      </div>

      {/* 校验结果 */}
      <Card title={pt('ies.validation.result')}>
        {runError ? (
          <Alert variant="error" title={pt('ies.validation.run_failed', { reason: '' })}>
            {runError}
          </Alert>
        ) : result === null || status === null ? (
          <EmptyState
            icon="info"
            title={pt('ies.validation.not_run')}
            description={pt('ies.validation.not_run_hint')}
            action={
              <Button icon="check" onClick={() => void runValidation()} loading={running}>
                {running ? pt('ies.validation.running') : pt('ies.validation.run')}
              </Button>
            }
          />
        ) : (
          <>
            <div className="ies-flex" style={{ flexWrap: 'wrap', gap: 'var(--ies-space-2)', alignItems: 'center' }}>
              <ValidationStatusBadge status={status} />
              <span className="ies-badge ies-badge--neutral ies-badge--shape-square">
                <span className="ies-badge__label">
                  {result.diagnostics.length} {pt('ies.validation.diag_count_suffix')}
                </span>
              </span>
            </div>

            {blocked ? (
              <div style={{ marginTop: 'var(--ies-space-3)' }}>
                <Alert variant="error" title={pt('ies.validation.blocked_title', { count: blockingCount })}>
                  {pt('ies.validation.blocked_note')}
                </Alert>
              </div>
            ) : null}
            {status === 'warnings' ? (
              <div style={{ marginTop: 'var(--ies-space-3)' }}>
                <Alert variant="warning">{pt('ies.validation.warnings_note')}</Alert>
              </div>
            ) : null}

            <div style={{ marginTop: 'var(--ies-space-3)' }}>
              <DiagnosticsList diagnostics={result.diagnostics} />
            </div>
          </>
        )}
      </Card>

    </div>
  )
}
