/**
 * TemplateListPanel: 模板列表(模板管理页左栏)。
 *
 * 状态可区分: 加载中 / 失败(重试入口) / 空列表(后端明确返回空) / 已选中。
 * 模板列表是服务器事实(feature query), 选择项是瞬时 UI 状态。
 */

import { pt } from '../../../i18n/pageMessages'
import type { TemplateSummary } from '../model'
import { Alert, Badge, Button, Card, EmptyState, Spinner } from '../../../components/ui'

export interface TemplateListPanelProps {
  templates: TemplateSummary[] | null
  loading: boolean
  error: string | null
  selectedId: string | null
  onSelect: (templateId: string) => void
  onRetry: () => void
}

export function TemplateListPanel({ templates, loading, error, selectedId, onSelect, onRetry }: TemplateListPanelProps) {
  if (loading && templates === null) {
    return (
      <Card title={pt('ies.modeling.template_list')}>
        <div className="ies-modeling__loading">
          <Spinner size="md" label={pt('ies.common.loading')} />
        </div>
      </Card>
    )
  }
  if (error && templates === null) {
    return (
      <Card title={pt('ies.modeling.template_list')}>
        <Alert variant="error" title={pt('ies.modeling.template_list_error', { reason: error })}>
          <Button variant="secondary" size="sm" onClick={onRetry}>
            {pt('ies.common.retry')}
          </Button>
        </Alert>
      </Card>
    )
  }
  return (
    <Card title={pt('ies.modeling.template_list')}>
      {templates && templates.length === 0 ? (
        <EmptyState icon="info" title={pt('ies.modeling.template_list_empty')} description={pt('ies.modeling.template_select_hint')} />
      ) : (
        <ul className="ies-modeling__template-list" role="listbox" aria-label={pt('ies.modeling.template_list')}>
          {(templates ?? []).map((t) => (
            <li key={t.template_id} role="option" aria-selected={selectedId === t.template_id}>
              <button
                type="button"
                className={`ies-modeling__template-item${selectedId === t.template_id ? ' is-selected' : ''}`}
                onClick={() => onSelect(t.template_id)}
              >
                <span className="ies-modeling__template-name">{t.name}</span>
                <span className="ies-modeling__template-id">{t.template_id}</span>
                <span className="ies-modeling__template-meta">
                  <Badge variant={t.has_inputs ? 'primary' : 'neutral'} size="sm" label={pt('ies.modeling.template_has_inputs')} />
                  <span>{t.schema_version}</span>
                </span>
                {t.description ? <span className="ies-modeling__template-desc">{t.description}</span> : null}
              </button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}
