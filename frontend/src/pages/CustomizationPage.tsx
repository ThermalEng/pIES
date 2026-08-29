/**
 * 自定义页面(/custom, 一级菜单): 用户模型模板管理。
 *
 * 能力:
 * - 创建模板(在线编辑完整 YAML, 模板 ID = YAML 的 device.id);
 * - 保存草稿(expected_revision 乐观锁; 校验失败展示聚合诊断并保留输入);
 * - 发布不可变 revision(相同内容幂等; 幂等键重放);
 * - 停用 / 重新启用(只影响后续选择);
 * - 删除未发布草稿(已发布模板禁止删除);
 * - 查看模板状态、发布 revision、内容摘要与聚合诊断。
 *
 * 与「新建项目模型」页面的关系: 发布成功的模板出现在项目模板选择器中,
 * 用户填写 inputs 生成项目模型(模板溯源固定精确 revision)。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { useI18n, translateDiagnostic } from '../i18n'
import { pt } from '../i18n/pageMessages'
import { Alert, Badge, Button, Spinner, Textarea } from '../components/ui'
import {
  createTemplate,
  deleteTemplate,
  diagnosticsFromApiError,
  getTemplateDetail,
  getTemplateRevision,
  listMyTemplates,
  publishTemplate,
  saveTemplateDraft,
  setTemplateEnabled,
  validateTemplateYaml,
} from '../features/customization/api'
import type { ModelDiagnosticDto, TemplateDto, TemplateRevisionDto } from '../features/customization/contracts'
import { isValidDeviceId } from '../features/modeling/mappers'

/** 标准 ies.device-model 2.0.0 模板骨架(含顶层 inputs 声明)。 */
const TEMPLATE_SKELETON = [
  'schema: ies.device-model',
  'schema_version: "2.0.0"',
  '',
  'device:',
  '  id: your.namespace.template_id',
  '  names:',
  '    zh-CN: 模板名称',
  '    en-US: Template Name',
  '',
  'inputs:',
  '  properties:',
  '    # peak_power_kw:',
  '    #   value:',
  '    #     type: number',
  '    #     unit: kW',
  '    #     valid_range: {minimum: 0, maximum: 1000}',
  '    #     default: 100',
  '',
  'properties:',
  '  # cop:',
  '  #   value: 3.2',
  '  #   unit: "1"',
  '  #   valid_range: {minimum: 1, maximum: 10}',
  '',
  'interfaces:',
  '  # electricity_in:',
  '  #   type: in',
  '  #   carrier: electricity',
  '  #   unit: kW',
  '  #   valid_range: {minimum: 0, maximum: null}',
  '',
  'equations:',
  '  variables: {}',
  '  relations: []',
  '',
].join('\n')

type EditorMode = 'list' | 'edit'

export default function CustomizationPage() {
  const [templates, setTemplates] = useState<TemplateDto[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [mode, setMode] = useState<EditorMode>('list')
  const [selected, setSelected] = useState<TemplateDto | null>(null)
  const [yamlText, setYamlText] = useState(TEMPLATE_SKELETON)
  const [description, setDescription] = useState('')
  const [diagnostics, setDiagnostics] = useState<ModelDiagnosticDto[]>([])
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [revisionDetail, setRevisionDetail] = useState<{ revision: TemplateRevisionDto; summary: Record<string, unknown> } | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    setListError(null)
    try {
      setTemplates(await listMyTemplates())
    } catch (err) {
      setListError(err instanceof Error ? err.message : String(err))
      setTemplates(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  /** 打开编辑器: 已有模板 → 载入草稿内容; 新建 → 标准骨架。 */
  const openEditor = useCallback(async (tpl: TemplateDto | null) => {
    setActionError(null)
    setDiagnostics([])
    setRevisionDetail(null)
    setSelected(tpl)
    if (tpl) {
      try {
        const detail = await getTemplateDetail(tpl.template_id)
        setYamlText(detail.document ? JSON.stringify(detail.document, null, 2) : TEMPLATE_SKELETON)
        setDescription(detail.template.description ?? '')
        setDiagnostics(detail.diagnostics)
      } catch (err) {
        setActionError(err instanceof Error ? err.message : String(err))
        setYamlText(TEMPLATE_SKELETON)
        setDescription('')
      }
    } else {
      setYamlText(TEMPLATE_SKELETON)
      setDescription('')
    }
    setMode('edit')
  }, [])

  const closeEditor = useCallback(() => {
    setMode('list')
    setSelected(null)
    setDiagnostics([])
    setRevisionDetail(null)
    void reload()
  }, [reload])

  const handleValidate = useCallback(async () => {
    setBusy(true)
    setActionError(null)
    try {
      const result = await validateTemplateYaml(yamlText)
      setDiagnostics(result.diagnostics)
      if (!result.valid) {
        setActionError(pt('ies.custom.validation_failed'))
      }
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [yamlText])

  /** 保存草稿: 新建走 POST; 已有走 PUT(expected_revision 乐观锁)。 */
  const handleSaveDraft = useCallback(async () => {
    setBusy(true)
    setActionError(null)
    try {
      const result = selected
        ? await saveTemplateDraft(selected.template_id, yamlText, selected.draft_revision, description)
        : await createTemplate(yamlText, description || null)
      setSelected(result)
      setDiagnostics([])
      await reload()
    } catch (err) {
      const diags = diagnosticsFromApiError(err)
      if (diags.length > 0) {
        setDiagnostics(diags)
        setActionError(pt('ies.custom.validation_failed'))
      } else {
        setActionError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setBusy(false)
    }
  }, [selected, yamlText, description, reload])

  /** 发布草稿为不可变 revision(幂等键避免重复发布)。 */
  const handlePublish = useCallback(async () => {
    if (!selected) return
    setBusy(true)
    setActionError(null)
    try {
      const result = await publishTemplate(
        selected.template_id,
        selected.draft_revision,
        `pub-${selected.template_id}-${Date.now()}`,
      )
      setRevisionDetail({ revision: result.revision, summary: {} })
      setDiagnostics([])
      await reload()
    } catch (err) {
      const diags = diagnosticsFromApiError(err)
      if (diags.length > 0) {
        setDiagnostics(diags)
        setActionError(pt('ies.custom.validation_failed'))
      } else {
        setActionError(err instanceof Error ? err.message : String(err))
      }
    } finally {
      setBusy(false)
    }
  }, [selected, reload])

  const handleToggleEnabled = useCallback(async (tpl: TemplateDto, enabled: boolean) => {
    setBusy(true)
    setActionError(null)
    try {
      await setTemplateEnabled(tpl.template_id, enabled)
      await reload()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [reload])

  const handleDelete = useCallback(async (tpl: TemplateDto) => {
    setBusy(true)
    setActionError(null)
    try {
      await deleteTemplate(tpl.template_id)
      await reload()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [reload])

  const showRevision = useCallback(async (tpl: TemplateDto, revision: number) => {
    setBusy(true)
    setActionError(null)
    try {
      const detail = await getTemplateRevision(tpl.template_id, revision)
      setRevisionDetail({ revision: detail.revision, summary: detail.summary })
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [])

  const yamlIdValid = useMemo(() => {
    const m = yamlText.match(/^\s{2}id:\s*["']?([^"'\s#]+)/m)
    return !m || isValidDeviceId(m[1])
  }, [yamlText])

  return (
    <div className="ies-custom-page">
      <div className="ies-page-title-row">
        <h1 className="ies-page-title">{pt('ies.custom.title')}</h1>
        <Button variant="primary" size="sm" onClick={() => void openEditor(null)}>
          {pt('ies.custom.create')}
        </Button>
      </div>
      <p className="ies-page-subtitle">{pt('ies.custom.desc')}</p>

      {listError ? <Alert variant="error" title={listError} closable onClose={() => setListError(null)} /> : null}

      {mode === 'list' ? (
        <div className="ies-custom-list">
          {loading ? <Spinner size="lg" /> : null}
          {templates !== null && templates.length === 0 ? (
            <p className="ies-custom-empty">{pt('ies.custom.empty')}</p>
          ) : null}
          {templates?.map((tpl) => (
            <div key={tpl.id} className="ies-custom-card">
              <div className="ies-custom-card__head">
                <button type="button" className="ies-custom-card__name" onClick={() => void openEditor(tpl)}>
                  {tpl.template_id}
                </button>
                <Badge
                  variant={tpl.status === 'published' ? 'success' : tpl.status === 'disabled' ? 'warning' : 'neutral'}
                  size="sm"
                  label={pt(`ies.custom.status.${tpl.status}`)}
                />
              </div>
              <div className="ies-custom-card__meta">
                {tpl.description ? <span className="ies-custom-card__desc">{tpl.description}</span> : null}
                <span>
                  {pt('ies.custom.draft_rev')}: {tpl.draft_revision}
                </span>
                <span>
                  {pt('ies.custom.published_rev')}: {tpl.published_revision}
                </span>
                {tpl.draft_sha256 ? <span className="ies-custom-card__sha">{tpl.draft_sha256.slice(0, 12)}…</span> : null}
              </div>
              {tpl.published_revision > 0 ? (
                <div className="ies-custom-card__revisions">
                  {pt('ies.custom.revisions')}:{' '}
                  {Array.from({ length: tpl.published_revision }, (_, i) => i + 1).map((rev) => (
                    <button key={rev} type="button" className="ies-custom-card__rev" onClick={() => void showRevision(tpl, rev)}>
                      v{rev}
                    </button>
                  ))}
                </div>
              ) : null}
              <div className="ies-custom-card__actions">
                {tpl.published_revision > 0 ? (
                  <>
                    <Button variant="secondary" size="sm" disabled={busy} onClick={() => void handleToggleEnabled(tpl, tpl.status === 'disabled')}>
                      {tpl.status === 'disabled' ? pt('ies.custom.enable') : pt('ies.custom.disable')}
                    </Button>
                  </>
                ) : (
                  <Button variant="danger" size="sm" disabled={busy} onClick={() => void handleDelete(tpl)}>
                    {pt('ies.custom.delete')}
                  </Button>
                )}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="ies-custom-editor">
          <div className="ies-custom-editor__head">
            <Button variant="ghost" size="sm" onClick={closeEditor}>
              ← {pt('ies.custom.back')}
            </Button>
            <h2>{selected ? selected.template_id : pt('ies.custom.new_template')}</h2>
            {selected ? (
              <Badge variant={selected.status === 'published' ? 'success' : 'neutral'} size="sm" label={pt(`ies.custom.status.${selected.status}`)} />
            ) : null}
          </div>

          <label className="ies-form-label" htmlFor="ies-custom-yaml">
            {pt('ies.custom.yaml_label')}
          </label>
          <Textarea
            id="ies-custom-yaml"
            className="ies-custom-editor__yaml"
            value={yamlText}
            onChange={(e) => setYamlText(e.target.value)}
            rows={22}
            aria-label={pt('ies.custom.yaml_label')}
            spellCheck={false}
          />
          {!yamlIdValid ? <p className="ies-custom-warn">{pt('ies.custom.id_invalid')}</p> : null}

          <label className="ies-form-label" htmlFor="ies-custom-desc">
            {pt('ies.custom.desc_label')}
          </label>
          <input
            id="ies-custom-desc"
            className="ies-input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            maxLength={500}
            placeholder={pt('ies.custom.desc_placeholder')}
          />

          {actionError ? (
            <Alert variant="error" title={actionError} closable onClose={() => setActionError(null)}>
              {diagnostics.length > 0 ? <DiagnosticList diagnostics={diagnostics} /> : null}
            </Alert>
          ) : null}
          {diagnostics.length > 0 && !actionError ? <DiagnosticList diagnostics={diagnostics} /> : null}

          {revisionDetail ? (
            <div className="ies-custom-revision">
              <h3>{pt('ies.custom.published_ok')}</h3>
              <p>
                {pt('ies.custom.rev_label')}: v{revisionDetail.revision.revision} ·{' '}
                {pt('ies.custom.sha_label')}: {revisionDetail.revision.content_sha256.slice(0, 16)}… ·{' '}
                {pt('ies.custom.schema_version')}: {revisionDetail.revision.schema_version} ·{' '}
                {pt('ies.custom.inputs')}: {revisionDetail.revision.input_count}
              </p>
            </div>
          ) : null}

          <div className="ies-custom-editor__actions">
            <Button variant="secondary" disabled={busy} loading={busy} onClick={() => void handleValidate()}>
              {pt('ies.custom.validate')}
            </Button>
            <Button variant="primary" disabled={busy} loading={busy} onClick={() => void handleSaveDraft()}>
              {selected ? pt('ies.custom.save_draft') : pt('ies.custom.create')}
            </Button>
            {selected ? (
              <Button variant="primary" disabled={busy || selected.published_revision > 0} loading={busy} onClick={() => void handlePublish()}>
                {pt('ies.custom.publish')}
              </Button>
            ) : null}
          </div>
        </div>
      )}
    </div>
  )
}

/** 聚合诊断列表(字段路径 / YAML 行列 / expected / actual)。 */
function DiagnosticList({ diagnostics }: { diagnostics: ModelDiagnosticDto[] }) {
  const { t } = useI18n()
  return (
    <ul className="ies-custom-diagnostics">
      {diagnostics.map((d, i) => {
        const loc = d.location
        const field = loc?.field ? `${loc.field}` : ''
        const line = typeof loc?.line === 'number' ? ` L${loc.line}` : ''
        return (
          <li key={`${d.code}-${i}`} className="ies-custom-diagnostics__item">
            <span className="ies-custom-diagnostics__code">{d.code}</span>
            <span className="ies-custom-diagnostics__msg">
              {translateDiagnostic(d as unknown as import('../types').Diagnostic)}
            </span>
            {field ? <span className="ies-custom-diagnostics__field">{field}{line}</span> : null}
            {typeof d.params?.expected !== 'undefined' ? (
              <span className="ies-custom-diagnostics__expected">
                {t('ies.diag.expected')}: {String(d.params.expected)} / {t('ies.diag.actual')}: {String(d.params.actual)}
              </span>
            ) : null}
          </li>
        )
      })}
    </ul>
  )
}
