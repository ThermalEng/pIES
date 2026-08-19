/**
 * 项目校验(ValidationPage):完整预检 + 财务基准确认。
 *
 * - 运行校验:调 api.validation.run(projectId) → {valid, diagnostics};
 *   由诊断严重度推导展示状态(ok / warnings / blocked),阻断时给出
 *   "禁止提交"提示;诊断列表复用 DiagnosticsList(严重度分组、代码、
 *   定位、修复建议),与数据/配置页展示一致。
 * - 财务基准确认:基准方案 = 不新增建设、仅用已有设备运行,作为方案收益
 *   的对比参照;确认调 api.validation.baselineConfirm(projectId),成功后
 *   显示已确认状态(后端追加式审计记录,不可覆盖)。
 * - 错误处理:API 错误经 translateError 渲染为诊断文案(Alert)。
 * - 可访问性:所有操作均为原生 button/select,Tab+Enter 可达,
 *   状态徽章同时以文字 + 图标 + 形状编码。
 *
 * 挂载方式:工作台子路由 /projects/:id/validation(见 WorkbenchPage)。
 */

import { useCallback, useState } from 'react'

import { api } from '../api/client'
import { DiagnosticsList } from '../components/DiagnosticsList'
import { Alert, Badge, Button, Card, EmptyState, Spinner } from '../components/ui'
import type { BadgeShape, BadgeVariant, IconName } from '../components/ui'
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

/** 校验状态徽章(文字 + 图标 + 形状三重编码)。 */
function ValidationStatusBadge({ status }: { status: ValidationStatus }) {
  useI18n() // 订阅语言切换,pt() 随全局表联动
  const map: Record<ValidationStatus, { variant: BadgeVariant; icon: IconName; shape: BadgeShape }> = {
    ok: { variant: 'success', icon: 'check', shape: 'circle' },
    warnings: { variant: 'warning', icon: 'warning', shape: 'triangle' },
    blocked: { variant: 'danger', icon: 'stop', shape: 'square' },
  }
  const cfg = map[status]
  return <Badge label={pt(`ies.validation.status_${status}`)} variant={cfg.variant} icon={cfg.icon}  />
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

  // 财务基准确认
  const [baselineConfirmed, setBaselineConfirmed] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [confirmError, setConfirmError] = useState<string | null>(null)
  const [confirmOk, setConfirmOk] = useState(false)

  // -------------------------------------------------------------------------
  // 动作
  // -------------------------------------------------------------------------

  /** 运行完整预检(模型/数据/配置/基准确认/就绪)。 */
  const runValidation = useCallback(async () => {
    setRunning(true)
    setRunError(null)
    setConfirmOk(false)
    try {
      const res = await api.validation.run(projectId)
      setResult(res)
      setStatus(deriveStatus(res.diagnostics))
    } catch (err) {
      setRunError(translateError(err as ApiError))
    } finally {
      setRunning(false)
    }
  }, [projectId])

  /** 确认财务基准(基准方案 = 不新增建设仅用已有设备,作为收益参照)。 */
  const confirmBaseline = useCallback(async () => {
    setConfirming(true)
    setConfirmError(null)
    setConfirmOk(false)
    try {
      const res = await api.validation.baselineConfirm(projectId)
      if (res.confirmed) {
        setBaselineConfirmed(true)
        setConfirmOk(true)
      } else {
        // 适配层仅折叠 confirmed 字段,无诊断返回时给出通用失败文案
        setConfirmError(pt('ies.validation.baseline_failed', { reason: pt('ies.common.unknown') }))
      }
    } catch (err) {
      setConfirmError(translateError(err as ApiError))
    } finally {
      setConfirming(false)
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
          <Button icon="check" onClick={() => void runValidation()} loading={running} disabled={confirming}>
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
              <span className="ies-badge ies-badge--neutral ies-badge--shape-circle">
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

      {/* 财务基准确认 */}
      <Card title={pt('ies.validation.baseline_title')}>
        <p style={{ fontSize: 'var(--ies-fs-sm)', color: 'var(--ies-color-text-secondary)' }}>
          {pt('ies.validation.baseline_desc')}
        </p>
        <div className="ies-flex" style={{ marginTop: 'var(--ies-space-3)', flexWrap: 'wrap', gap: 'var(--ies-space-2)' }}>
          {baselineConfirmed ? (
            <Badge label={pt('ies.validation.baseline_confirmed')} variant="success" icon="check" />
          ) : (
            <Button icon="check" onClick={() => void confirmBaseline()} loading={confirming} disabled={running}>
              {pt('ies.validation.baseline_confirm')}
            </Button>
          )}
          {confirming ? <Spinner size="sm" /> : null}
        </div>
        {confirmOk ? (
          <div style={{ marginTop: 'var(--ies-space-3)' }}>
            <Alert variant="success">{pt('ies.validation.baseline_ok')}</Alert>
          </div>
        ) : null}
        {confirmError ? (
          <div style={{ marginTop: 'var(--ies-space-3)' }}>
            <Alert variant="error" title={pt('ies.validation.baseline_failed', { reason: '' })}>
              {confirmError}
            </Alert>
          </div>
        ) : null}
      </Card>
    </div>
  )
}
