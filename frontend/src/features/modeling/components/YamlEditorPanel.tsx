/**
 * YamlEditorPanel: 直接 YAML 编辑面板(标准 ies.device-model 2.0.0 骨架 + 在线编辑)。
 *
 * - 光标位置以 行:列 显示(mapper yamlLineColumn);
 * - 可重置为骨架; 编辑内容保留在本地表单状态(失败后不丢失)。
 */

import { pt } from '../../../i18n/pageMessages'
import { Button, Textarea } from '../../../components/ui'
import { yamlLineColumn } from '../mappers'

export interface YamlEditorPanelProps {
  yaml_text: string
  touched: boolean
  onChange: (text: string) => void
  onReset: () => void
  disabled?: boolean
}

export function YamlEditorPanel({ yaml_text, touched, onChange, onReset, disabled = false }: YamlEditorPanelProps) {
  const cursor = yamlLineColumn(yaml_text, yaml_text.length)
  return (
    <div className="ies-modeling__yaml-editor">
      <div className="ies-modeling__yaml-toolbar">
        <span className="ies-modeling__yaml-title">{pt('ies.modeling.yaml.title')}</span>
        <span className="ies-modeling__yaml-cursor">
          {pt('ies.modeling.yaml.line_col', { line: cursor.line, column: cursor.column })}
        </span>
        <Button variant="ghost" size="sm" disabled={disabled} onClick={onReset}>
          {pt('ies.modeling.yaml.reset')}
        </Button>
      </div>
      <Textarea
        id="ies-modeling-yaml-text"
        className="ies-modeling__yaml-textarea"
        value={yaml_text}
        spellCheck={false}
        disabled={disabled}
        aria-label={pt('ies.modeling.yaml.title')}
        onChange={(e) => onChange(e.target.value)}
      />
      {!touched ? <p className="ies-modeling__yaml-hint">{pt('ies.modeling.yaml.hint')}</p> : null}
    </div>
  )
}
