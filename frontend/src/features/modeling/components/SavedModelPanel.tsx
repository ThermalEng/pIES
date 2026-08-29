/**
 * SavedModelPanel: 正式保存成功面板。
 *
 * 以后端返回为权威: 最终 _N ID、规范 YAML、内容摘要(SHA-256)、摘要计数、
 * 项目 revision。前端不预分配编号; 只有此时模型才进入项目模型列表并允许进入装配。
 */

import { useState } from 'react'

import { pt } from '../../../i18n/pageMessages'
import { Badge, Card } from '../../../components/ui'
import type { SavedModelInfo } from '../model'

export interface SavedModelPanelProps {
  saved: SavedModelInfo
}

export function SavedModelPanel({ saved }: SavedModelPanelProps) {
  const [showYaml, setShowYaml] = useState(false)
  return (
    <Card className="ies-modeling__saved" title={pt('ies.modeling.saved.title')}>
      <div className="ies-modeling__saved-grid">
        <div className="ies-modeling__saved-field">
          <span className="ies-modeling__saved-label">{pt('ies.modeling.saved.id')}</span>
          <Badge variant="success" label={saved.model_id} />
        </div>
        <div className="ies-modeling__saved-field">
          <span className="ies-modeling__saved-label">{pt('ies.modeling.saved.device_id')}</span>
          <span>{saved.device_id}</span>
        </div>
        <div className="ies-modeling__saved-field">
          <span className="ies-modeling__saved-label">{pt('ies.modeling.saved.revision')}</span>
          <span>{saved.project_revision}</span>
        </div>
        <div className="ies-modeling__saved-field">
          <span className="ies-modeling__saved-label">{pt('ies.modeling.saved.sha256')}</span>
          <code className="ies-modeling__saved-hash">{saved.content_sha256}</code>
        </div>
        <div className="ies-modeling__saved-field">
          <span className="ies-modeling__saved-label">{pt('ies.modeling.saved.summary')}</span>
          <span>
            {pt('ies.modeling.saved.counts', {
              p: saved.summary.property_count,
              i: saved.summary.interface_count,
              r: saved.summary.relation_count,
            })}
          </span>
        </div>
      </div>
      <div className="ies-modeling__saved-yaml">
        <button type="button" className="ies-modeling__saved-toggle" onClick={() => setShowYaml((v) => !v)} aria-expanded={showYaml}>
          {pt(showYaml ? 'ies.modeling.saved.hide' : 'ies.modeling.saved.show')}
        </button>
        {showYaml ? <pre className="ies-modeling__saved-pre">{saved.canonical_yaml}</pre> : null}
      </div>
      <p className="ies-modeling__saved-note">{pt('ies.modeling.saved.enter_list_note')}</p>
    </Card>
  )
}
