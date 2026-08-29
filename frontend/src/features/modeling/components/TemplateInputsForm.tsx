/**
 * TemplateInputsForm: 递归读取模板 inputs 生成表单(宪法 §9.5 通用交互规则)。
 *
 * 叶子控件映射:
 * - number → 数字输入(文本保留编辑中状态, 单位/范围只读提示);
 * - boolean → 开关(Checkbox);
 * - string → 文本输入;
 * - data_repeat / data_predict → 文件上传占位(临时已上传 ≠ 模型已保存);
 * - object / array → 受控结构编辑器(对象递归分组; 数组逐行编辑, 整体替换)。
 *
 * 前端预检查(即时反馈)不阻断提交; 后端校验始终是权威闸门。
 */

import { useRef } from 'react'
import type { ChangeEvent } from 'react'

import { pt } from '../../../i18n/pageMessages'
import { Badge, Button, Checkbox, FormField, Input } from '../../../components/ui'
import type { FormFieldError, FormFieldValue } from '../form'
import type { InputNode } from '../model'

export interface TemplateInputsFormProps {
  nodes: InputNode[]
  values: Record<string, FormFieldValue>
  /** 已触碰字段的错误(前端预检查)。 */
  errors: FormFieldError[]
  onFieldChange: (path: string, value: FormFieldValue) => void
  onArrayChange: (path: string, items: Array<Record<string, FormFieldValue>>) => void
  /** 上传临时数据文件(父层负责调用 api 并回填 file_ref)。 */
  onUploadFile: (path: string, file: File) => void
  onRemoveFile: (path: string) => void
  /** 当前上传中的叶子路径(显示进度)。 */
  uploadingPath: string | null
  disabled?: boolean
}

function boundText(value: number | null): string {
  return value === null ? '∞' : String(value)
}

/** 数值字段范围提示文本(闭区间, null 表示无界)。 */
function rangeHint(node: InputNode): string | null {
  if (!node.valid_range) return null
  return `${pt('ies.modeling.form.range')}: ${boundText(node.valid_range.minimum)}–${boundText(node.valid_range.maximum)}`
}

function unitSuffix(node: InputNode): string {
  return node.unit && node.unit !== '1' ? ` [${node.unit}]` : ''
}

/** 数组子节点相对路径(去掉 "arr[]" 前缀与点号; 与 mappers.arrayItemRel 一致)。 */
function arrayItemRel(child: InputNode, arrayPath: string): string {
  const prefix = `${arrayPath}[]`
  if (!child.path.startsWith(prefix)) return child.path
  const rest = child.path.slice(prefix.length)
  return rest.startsWith('.') ? rest.slice(1) : rest
}

function arrayItemFields(template: InputNode, arrayPath: string): { rel: string; node: InputNode }[] {
  const leafs: { rel: string; node: InputNode }[] = []
  const push = (child: InputNode) => {
    leafs.push({ rel: arrayItemRel(child, arrayPath), node: child })
  }
  if (template.type === 'object' || template.type === 'array') {
    for (const child of template.children) {
      if (child.type === 'object' || child.type === 'array') {
        // 嵌套结构: 表单编辑器不支持, 明确提示走直接 YAML 编辑(不静默丢弃)
        leafs.push({ rel: child.path, node: child })
      } else {
        push(child)
      }
    }
  } else {
    push(template)
  }
  return leafs
}

export function TemplateInputsForm({
  nodes,
  values,
  errors,
  onFieldChange,
  onArrayChange,
  onUploadFile,
  onRemoveFile,
  uploadingPath,
  disabled = false,
}: TemplateInputsFormProps) {
  const errorByPath = new Map(errors.map((e) => [e.path, e]))
  return (
    <div className="ies-modeling__form">
      {nodes.map((node) => (
        <NodeEditor
          key={node.path}
          node={node}
          values={values}
          errorByPath={errorByPath}
          onFieldChange={onFieldChange}
          onArrayChange={onArrayChange}
          onUploadFile={onUploadFile}
          onRemoveFile={onRemoveFile}
          uploadingPath={uploadingPath}
          disabled={disabled}
        />
      ))}
    </div>
  )
}

interface NodeEditorProps {
  node: InputNode
  values: Record<string, FormFieldValue>
  errorByPath: Map<string, FormFieldError>
  onFieldChange: (path: string, value: FormFieldValue) => void
  onArrayChange: (path: string, items: Array<Record<string, FormFieldValue>>) => void
  onUploadFile: (path: string, file: File) => void
  onRemoveFile: (path: string) => void
  uploadingPath: string | null
  disabled: boolean
}

function NodeEditor({ node, values, errorByPath, onFieldChange, onArrayChange, onUploadFile, onRemoveFile, uploadingPath, disabled }: NodeEditorProps) {
  if (node.unsupported) {
    return (
      <div className="ies-modeling__unsupported">
        {pt('ies.modeling.form.unsupported', { path: node.path, type: node.type })}
      </div>
    )
  }
  if (node.type === 'object') {
    if (node.children.length === 0) return null
    return (
      <fieldset className="ies-modeling__group">
        <legend>{node.path}</legend>
        <div className="ies-modeling__group-body">
          {node.children.map((child) => (
            <NodeEditor
              key={child.path}
              node={child}
              values={values}
              errorByPath={errorByPath}
              onFieldChange={onFieldChange}
              onArrayChange={onArrayChange}
              onUploadFile={onUploadFile}
              onRemoveFile={onRemoveFile}
              uploadingPath={uploadingPath}
              disabled={disabled}
            />
          ))}
        </div>
      </fieldset>
    )
  }
  if (node.type === 'array') {
    return <ArrayEditor node={node} values={values} errorByPath={errorByPath} onArrayChange={onArrayChange} disabled={disabled} />
  }
  switch (node.type) {
    case 'number':
      return (
        <NumberField node={node} value={values[node.path]} error={errorByPath.get(node.path)} onChange={onFieldChange} disabled={disabled} />
      )
    case 'boolean': {
      const value = values[node.path]
      const checked = value?.kind === 'boolean' ? value.checked : false
      return (
        <div className="ies-modeling__field-row">
          <Checkbox
            id={`ies-modeling-field-${node.path}`}
            checked={checked}
            disabled={disabled}
            onChange={(e) => onFieldChange(node.path, { kind: 'boolean', checked: e.target.checked })}
          />
          <label htmlFor={`ies-modeling-field-${node.path}`}>{node.path}</label>
        </div>
      )
    }
    case 'string': {
      const value = values[node.path]
      const text = value?.kind === 'string' ? value.text : ''
      return (
        <FormField label={node.path} htmlFor={`ies-modeling-field-${node.path}`}>
          <Input
            id={`ies-modeling-field-${node.path}`}
            type="text"
            value={text}
            disabled={disabled}
            onChange={(e) => onFieldChange(node.path, { kind: 'string', text: e.target.value })}
          />
        </FormField>
      )
    }
    case 'data_repeat':
    case 'data_predict':
      return (
        <DataFileField
          node={node}
          value={values[node.path]}
          onUploadFile={onUploadFile}
          onRemoveFile={onRemoveFile}
          uploading={uploadingPath === node.path}
          disabled={disabled}
        />
      )
    default:
      return null
  }
}

function NumberField({
  node,
  value,
  error,
  onChange,
  disabled,
}: {
  node: InputNode
  value: FormFieldValue | undefined
  error: FormFieldError | undefined
  onChange: (path: string, value: FormFieldValue) => void
  disabled: boolean
}) {
  const text = value?.kind === 'number' ? value.text : ''
  const hint = [rangeHint(node)].filter(Boolean).join(' · ')
  return (
    <FormField
      label={`${node.path}${unitSuffix(node)}`}
      htmlFor={`ies-modeling-field-${node.path}`}
      hint={hint || undefined}
      error={error ? pt(error.message_key, error.params) : undefined}
    >
      <Input
        id={`ies-modeling-field-${node.path}`}
        type="text"
        inputMode="decimal"
        value={text}
        invalid={!!error}
        disabled={disabled}
        onChange={(e) => onChange(node.path, { kind: 'number', text: e.target.value })}
      />
    </FormField>
  )
}

function DataFileField({
  node,
  value,
  onUploadFile,
  onRemoveFile,
  uploading,
  disabled,
}: {
  node: InputNode
  value: FormFieldValue | undefined
  onUploadFile: (path: string, file: File) => void
  onRemoveFile: (path: string) => void
  uploading: boolean
  disabled: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const fileRef = value?.kind === 'data' ? value.file_ref : null
  const fileName = value?.kind === 'data' ? value.file_name : null
  const handleChange = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) onUploadFile(node.path, file)
    e.target.value = '' // 允许重复选择同一文件
  }
  return (
    <div className="ies-modeling__data-field">
      <div className="ies-modeling__data-head">
        <span className="ies-modeling__data-label">{node.path}</span>
        {node.data_ref ? <span className="ies-modeling__data-ref">{node.data_ref}</span> : null}
        <Badge
          variant={fileRef ? 'success' : 'neutral'}
          size="sm"
          label={fileRef ? pt('ies.modeling.form.data_uploaded', { name: fileName ?? '' }) : pt('ies.modeling.form.data_not_uploaded')}
        />
      </div>
      <div className="ies-modeling__data-actions">
        <input ref={inputRef} type="file" className="ies-modeling__file-input" onChange={handleChange} disabled={disabled} aria-label={pt('ies.modeling.form.data_file')} />
        <Button variant="secondary" size="sm" disabled={disabled || uploading} loading={uploading} onClick={() => inputRef.current?.click()}>
          {pt('ies.modeling.form.data_upload')}
        </Button>
        {fileRef ? (
          <Button variant="ghost" size="sm" disabled={disabled} onClick={() => onRemoveFile(node.path)}>
            {pt('ies.modeling.form.data_remove')}
          </Button>
        ) : null}
      </div>
    </div>
  )
}

/** 数组受控结构编辑器(整体替换; 元素模板单一声明时支持逐行编辑)。 */
function ArrayEditor({
  node,
  values,
  errorByPath,
  onArrayChange,
  disabled,
}: {
  node: InputNode
  values: Record<string, FormFieldValue>
  errorByPath: Map<string, FormFieldError>
  onArrayChange: (path: string, items: Array<Record<string, FormFieldValue>>) => void
  disabled: boolean
}) {
  const field = values[node.path]
  const items = field?.kind === 'array' ? field.items : []
  const template = node.children.length === 1 ? node.children[0] : null
  const fields = template ? arrayItemFields(template, node.path) : []

  const updateRow = (index: number, rel: string, value: FormFieldValue) => {
    const next = items.map((row, i) => (i === index ? { ...row, [rel]: value } : row))
    onArrayChange(node.path, next)
  }
  const addRow = () => {
    const row: Record<string, FormFieldValue> = {}
    for (const f of fields) {
      const child = f.node
      if (child.type === 'number') row[f.rel] = { kind: 'number', text: '' }
      else if (child.type === 'boolean') row[f.rel] = { kind: 'boolean', checked: false }
      else if (child.type === 'string') row[f.rel] = { kind: 'string', text: '' }
      else if (child.type === 'data_repeat' || child.type === 'data_predict') row[f.rel] = { kind: 'data', file_ref: null, file_name: null }
    }
    onArrayChange(node.path, [...items, row])
  }
  const removeRow = (index: number) => {
    onArrayChange(node.path, items.filter((_, i) => i !== index))
  }
  const addRowDisabled = fields.length === 0 || !template || template.type === 'object' || template.type === 'array'

  return (
    <fieldset className="ies-modeling__array">
      <legend>
        {node.path} <span className="ies-modeling__array-note">{pt('ies.modeling.form.array_note')}</span>
      </legend>
      {template && template.type === 'object' && template.children.some((c) => c.type === 'object' || c.type === 'array') ? (
        <div className="ies-modeling__unsupported">{pt('ies.modeling.form.nested_array_unsupported')}</div>
      ) : null}
      {items.length === 0 ? <div className="ies-modeling__array-empty">{pt('ies.modeling.form.array_empty')}</div> : null}
      {items.map((row, i) => (
        <div className="ies-modeling__array-row" key={i}>
          <span className="ies-modeling__array-index">[{i}]</span>
          <div className="ies-modeling__array-fields">
            {fields.map((f) => {
              const itemPath = `${node.path}[${i}]${f.rel ? `.${f.rel}` : ''}`
              const error = errorByPath.get(itemPath)
              const rowValue = row[f.rel]
              if (f.node.type === 'number') {
                return (
                  <FormField
                    key={f.rel}
                    label={`${f.rel || node.path}${unitSuffix(f.node)}`}
                    htmlFor={`ies-modeling-field-${itemPath}`}
                    hint={rangeHint(f.node) ?? undefined}
                    error={error ? pt(error.message_key, error.params) : undefined}
                  >
                    <Input
                      id={`ies-modeling-field-${itemPath}`}
                      type="text"
                      inputMode="decimal"
                      value={rowValue?.kind === 'number' ? rowValue.text : ''}
                      invalid={!!error}
                      disabled={disabled}
                      onChange={(e) => updateRow(i, f.rel, { kind: 'number', text: e.target.value })}
                    />
                  </FormField>
                )
              }
              if (f.node.type === 'boolean') {
                return (
                  <div className="ies-modeling__field-row" key={f.rel}>
                    <Checkbox
                      id={`ies-modeling-field-${itemPath}`}
                      checked={rowValue?.kind === 'boolean' ? rowValue.checked : false}
                      disabled={disabled}
                      onChange={(e) => updateRow(i, f.rel, { kind: 'boolean', checked: e.target.checked })}
                    />
                    <label htmlFor={`ies-modeling-field-${itemPath}`}>{f.rel || node.path}</label>
                  </div>
                )
              }
              if (f.node.type === 'string') {
                return (
                  <FormField key={f.rel} label={f.rel || node.path} htmlFor={`ies-modeling-field-${itemPath}`}>
                    <Input
                      id={`ies-modeling-field-${itemPath}`}
                      type="text"
                      value={rowValue?.kind === 'string' ? rowValue.text : ''}
                      disabled={disabled}
                      onChange={(e) => updateRow(i, f.rel, { kind: 'string', text: e.target.value })}
                    />
                  </FormField>
                )
              }
              if (f.node.type === 'data_repeat' || f.node.type === 'data_predict') {
                const fileRef = rowValue?.kind === 'data' ? rowValue.file_ref : null
                return (
                  <span key={f.rel} className="ies-modeling__data-ref">
                    {f.node.data_ref ? `${f.rel}: ${f.node.data_ref}` : f.rel}
                    {fileRef ? ` · ${pt('ies.modeling.form.data_uploaded', { name: '' })}` : ''}
                  </span>
                )
              }
              if (f.node.type === 'object' || f.node.type === 'array') {
                return (
                  <span key={f.rel} className="ies-modeling__unsupported">
                    {pt('ies.modeling.form.unsupported', { path: f.rel, type: f.node.type })}
                  </span>
                )
              }
              return null
            })}
          </div>
          <Button variant="ghost" size="sm" disabled={disabled} onClick={() => removeRow(i)} aria-label={pt('ies.modeling.form.array_remove_row', { index: i })}>
            {pt('ies.common.delete')}
          </Button>
        </div>
      ))}
      <Button variant="secondary" size="sm" disabled={disabled || addRowDisabled} onClick={addRow}>
        {pt('ies.modeling.form.array_add_row')}
      </Button>
    </fieldset>
  )
}
