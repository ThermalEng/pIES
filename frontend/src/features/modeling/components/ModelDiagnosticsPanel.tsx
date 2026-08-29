/**
 * ModelDiagnosticsPanel: 候选校验失败聚合诊断展示。
 *
 * - 每条诊断显示: 严重度徽章 + 诊断码 + 文案(message_key 渲染, 缺键时回退
 *   后端 detail) + 字段路径 + YAML 行列 + expected/actual;
 * - 后端消息键缺失时不伪造本地化文案(显示受控 detail/键名);
 * - 校验失败必须保留输入(本面板只展示诊断, 不重置表单)。
 */

import { translate } from '../../../i18n'
import { pt } from '../../../i18n/pageMessages'
import { Alert, SeverityBadge } from '../../../components/ui'
import type { ModelDiagnostic } from '../model'

export interface ModelDiagnosticsPanelProps {
  diagnostics: ModelDiagnostic[]
}

function diagText(d: ModelDiagnostic): string {
  const translated = translate(d.message_key, {
    detail: d.detail ?? '',
    expected: d.expected ?? '',
    actual: d.actual ?? '',
  })
  // 文案键已本地化且无未替换占位 → 用本地化文案; 否则回退后端 detail(受控技术详情)
  if (translated !== d.message_key && !translated.includes('{')) return translated
  if (d.detail) return d.detail
  return d.message_key
}

function expectedActual(d: ModelDiagnostic): string | null {
  if (d.expected === null && d.actual === null) return null
  const parts: string[] = []
  if (d.expected !== null) parts.push(`${pt('ies.modeling.diag.expected')}: ${JSON.stringify(d.expected)}`)
  if (d.actual !== null) parts.push(`${pt('ies.modeling.diag.actual')}: ${JSON.stringify(d.actual)}`)
  return parts.join(' · ')
}

function diagLocation(d: ModelDiagnostic): string | null {
  if (!d.field && d.line === null && d.column === null) return null
  const parts: string[] = []
  if (d.field) parts.push(pt('ies.modeling.diag.loc_field', { field: d.field }))
  if (d.line !== null || d.column !== null) {
    parts.push(pt('ies.modeling.diag.yloc', { line: d.line ?? 0, column: d.column ?? 0 }))
  }
  return parts.length > 0 ? parts.join(' · ') : null
}

export function ModelDiagnosticsPanel({ diagnostics }: ModelDiagnosticsPanelProps) {
  if (diagnostics.length === 0) return null
  return (
    <div className="ies-modeling__diagnostics" role="alert" aria-label={pt('ies.modeling.candidate.diagnostics_title', { count: diagnostics.length })}>
      <Alert variant="error" title={pt('ies.modeling.candidate.diagnostics_title', { count: diagnostics.length })}>
        <p>{pt('ies.modeling.candidate.failed_note')}</p>
      </Alert>
      <ul className="ies-modeling__diag-list">
        {diagnostics.map((d, i) => {
          const loc = diagLocation(d)
          const ea = expectedActual(d)
          return (
            <li key={`${d.code}-${i}`} className="ies-modeling__diag-item">
              <span className="ies-modeling__diag-head">
                <SeverityBadge severity={d.severity} />
                <span className="ies-modeling__diag-code">{d.code}</span>
              </span>
              <span className="ies-modeling__diag-text">{diagText(d)}</span>
              {loc ? <span className="ies-modeling__diag-loc">{loc}</span> : null}
              {ea ? <span className="ies-modeling__diag-ea">{ea}</span> : null}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
