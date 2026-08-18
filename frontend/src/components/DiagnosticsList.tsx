/**
 * 诊断列表组件(数据上传质量诊断 / 配置校验诊断共用)。
 *
 * - 按严重度分组展示:阻断/错误(红色)优先,随后警告,最后信息;
 * - 每条显示:严重度徽章 + 诊断代码 + 文案(message_key 渲染) + 位置
 *   (对象类型/字段/行,设计输入 §8.3"给出字段、位置、错误代码和修复建议")
 *   + 修复建议(fix_hint_key);
 * - 位置渲染可定位到配置项(ConfigPage)或数据集字段/行(DataPage)。
 */

import type { ReactNode } from 'react'

import { translate, translateDiagnostic, useI18n } from '../i18n'
import { pt } from '../i18n/pageMessages'
import type { Diagnostic, DiagnosticLocation, Severity } from '../types'
import { SeverityBadge } from './ui'

const SEVERITY_ORDER: readonly Severity[] = ['blocking', 'error', 'warning', 'info']

export interface DiagnosticsListProps {
  diagnostics: Diagnostic[]
  /** 空列表时的占位文案(缺省 ies.diag.no_diagnostics)。 */
  emptyText?: string
  /** 列表前附加说明(如来源标注)。 */
  note?: ReactNode
}

/** 对象类型文案:优先本地化键,缺失回退原始键名。 */
function objectTypeLabel(objectType: string): string {
  const key = `ies.diag.loc.${objectType}`
  const label = pt(key)
  return label === key ? objectType : label
}

/** 位置文案:对象类型 · 字段 · 行(支持 number 或 number[])。 */
export function diagnosticLocationText(loc: DiagnosticLocation | null): string | null {
  if (!loc) return null
  const parts: string[] = []
  if (loc.object_type) parts.push(objectTypeLabel(loc.object_type))
  if (loc.object_id && loc.object_type !== 'config') parts.push(`#${loc.object_id}`)
  if (loc.field) parts.push(`${pt('ies.diag.field')}: ${loc.field}`)
  if (loc.row !== null && loc.row !== undefined) {
    const rows = Array.isArray(loc.row) ? loc.row.join(', ') : String(loc.row)
    parts.push(`${pt('ies.diag.rows')}: ${rows}`)
  }
  return parts.length > 0 ? `${pt('ies.diag.location')}: ${parts.join(' · ')}` : null
}

function DiagnosticItem({ diag }: { diag: Diagnostic }) {
  const loc = diagnosticLocationText(diag.location)
  return (
    <li className="ies-diagnostics__item">
      <div className="ies-diagnostics__head">
        <SeverityBadge severity={diag.severity} />
        <span className="ies-diagnostics__code">{diag.code}</span>
        {diag.source ? <span className="ies-diagnostics__source">{diag.source}</span> : null}
      </div>
      <p className="ies-diagnostics__message">{translateDiagnostic(diag)}</p>
      {loc ? <p className="ies-diagnostics__loc">{loc}</p> : null}
      {diag.fix_hint_key ? (
        <p className="ies-diagnostics__fix">
          {pt('ies.diag.fix_hint')}: {translate(diag.fix_hint_key, diag.params)}
        </p>
      ) : null}
    </li>
  )
}

/** 按严重度分组的诊断列表。 */
export function DiagnosticsList({ diagnostics, emptyText, note }: DiagnosticsListProps) {
  const { t } = useI18n()
  const active = diagnostics.filter((d) => !d.suppressed)
  if (active.length === 0) {
    return (
      <div className="ies-diagnostics">
        {note}
        <p className="ies-diagnostics__empty">{emptyText ?? pt('ies.diag.no_diagnostics')}</p>
      </div>
    )
  }
  const groups = SEVERITY_ORDER.map((severity) => ({
    severity,
    items: active.filter((d) => d.severity === severity),
  })).filter((g) => g.items.length > 0)
  return (
    <div className="ies-diagnostics">
      {note}
      {groups.map((group) => (
        <section
          key={group.severity}
          className={`ies-diagnostics__group ies-diagnostics__group--${group.severity}`}
          aria-label={`${t(`ies.severity.${group.severity}`)} (${group.items.length})`}
        >
          <h4 className="ies-diagnostics__group-title">
            <SeverityBadge severity={group.severity} />
            <span className="ies-diagnostics__count">{group.items.length}</span>
          </h4>
          <ul className="ies-diagnostics__list">
            {group.items.map((diag, idx) => (
              <DiagnosticItem
                key={diag.trace_id ?? `${diag.code}-${diag.occurred_at}-${idx}`}
                diag={diag}
              />
            ))}
          </ul>
        </section>
      ))}
    </div>
  )
}
