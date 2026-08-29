/**
 * 新建项目模型页面(/projects/:id/model/new)。
 *
 * 模板实例化与直接 YAML 编辑汇合为同一个候选校验/保存用例(frontend.md
 * 「新建并保存项目模型」): 编辑中/临时已上传/校验中/校验失败/正式已保存;
 * 校验失败保留输入并按字段路径/YAML 行列展示诊断; 成功后以后端返回的
 * 最终 _N ID、规范 YAML、内容摘要与项目 revision 替换编辑状态。
 *
 * 本页只做路由级组合: 数据与状态来自 features/modeling 的 hooks/组件,
 * 不直接拼请求 JSON(宪法 §9)。
 */

import { useCallback, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { useI18n } from '../../i18n'
import { errorMessage } from '../../i18n'
import { pt } from '../../i18n/pageMessages'
import { Alert, Button } from '../../components/ui'
import { useWorkbench } from '../workbench'
import {
  CandidateStatusBar,
  ModelDiagnosticsPanel,
  SavedModelPanel,
  TemplateInputsForm,
  TemplateListPanel,
  YamlEditorPanel,
} from '../../features/modeling/components'
import {
  useCandidateSave,
  useTemplateDocument,
  useTemplateForm,
  useTemplates,
  useYamlForm,
} from '../../features/modeling/hooks'
import { buildYamlSkeleton, collectDataFileRefs, formValuesToInputsOrErrors, isValidDeviceId } from '../../features/modeling/mappers'
import type { FormFieldValue } from '../../features/modeling/form'
import type { CandidateModel } from '../../features/modeling/model'
import '../../features/modeling/modeling.css'

type CreateTab = 'template' | 'yaml'

/** 直接 YAML 骨架(模块级常量, 每次进入页面使用同一内容)。 */
const YAML_SKELETON = buildYamlSkeleton()

export default function NewModelPage() {
  const { projectId } = useWorkbench()
  const { t } = useI18n()
  const [tab, setTab] = useState<CreateTab>('template')
  const [selectedTemplateId, setSelectedTemplateId] = useState<string | null>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)

  const templates = useTemplates()
  const detail = useTemplateDocument(selectedTemplateId)
  const form = useTemplateForm(detail.document)
  const yaml = useYamlForm(YAML_SKELETON)
  const save = useCandidateSave(projectId)

  const backToModelUrl = useMemo(() => `/projects/${projectId}/model`, [projectId])

  /** 字段编辑: 更新表单并让保存状态回到编辑中(保留输入)。 */
  const handleFieldChange = useCallback(
    (path: string, value: FormFieldValue) => {
      form.setField(path, value)
      save.backToEditing()
    },
    [form, save],
  )

  const handleArrayChange = useCallback(
    (path: string, items: Array<Record<string, FormFieldValue>>) => {
      form.setArray(path, items)
      save.backToEditing()
    },
    [form, save],
  )

  const handleYamlChange = useCallback(
    (text: string) => {
      yaml.setYaml(text)
      save.backToEditing()
    },
    [yaml, save],
  )

  /** 切换页签 = 开始一次新的候选创建(清空保存结果/诊断, 保留各页签编辑内容)。 */
  const handleTabChange = useCallback(
    (next: CreateTab) => {
      setTab(next)
      save.reset()
    },
    [save],
  )

  /** 临时数据文件上传(临时隔离区; 上传完成 ≠ 模型已保存)。 */
  const handleUploadFile = useCallback(
    async (path: string, dataRef: string, file: File) => {
      setUploadError(null)
      try {
        const ref = await save.uploadTempFile(file, dataRef)
        form.setField(path, {
          kind: 'data',
          file_ref: ref.object_id,
          file_name: file.name,
          data_ref: dataRef,
          upload: { upload_id: ref.upload_id, object_id: ref.object_id, sha256: ref.sha256 },
        })
        save.markUploaded()
      } catch (err) {
        setUploadError(errorMessage(err))
      }
    },
    [form, save],
  )

  const handleRemoveFile = useCallback(
    (path: string) => {
      form.setField(path, { kind: 'data', file_ref: null, file_name: null, data_ref: null, upload: null })
      save.backToEditing()
    },
    [form, save],
  )

  /** 模板表单提交: 表单 → inputs JSON → 候选保存(携带精确模板引用)。 */
  const handleSubmitTemplate = useCallback(async () => {
    if (!detail.document) return
    const result = formValuesToInputsOrErrors(detail.document.inputs, form.values)
    if (!result.ok) {
      form.markAllTouched()
      return
    }
    const summary = detail.document.summary
    const revision = summary.revision
    const candidate: CandidateModel = {
      source: 'template',
      template_id: summary.template_id,
      template_revision: revision ? revision.revision : null,
      template_sha256: revision ? revision.content_sha256 : null,
      inputs_json: result.inputs,
      content_yaml: null,
      project_revision: 0, // 由 useCandidateSave 在提交时读取项目草稿修订
      idempotency_key: '', // 由 useCandidateSave 生成
      data_files: collectDataFileRefs(form.values),
    }
    await save.submit(candidate)
  }, [detail.document, form, save])

  /** 直接 YAML 提交。 */
  const handleSubmitYaml = useCallback(async () => {
    if (yaml.yaml_text.trim() === '') return
    const candidate: CandidateModel = {
      source: 'yaml',
      template_id: null,
      template_revision: null,
      template_sha256: null,
      inputs_json: null,
      content_yaml: yaml.yaml_text,
      project_revision: 0,
      idempotency_key: '',
      data_files: [],
    }
    await save.submit(candidate)
  }, [yaml.yaml_text, save])

  const submitDisabled = save.phase === 'saved' || save.phase === 'validating' || (tab === 'template' && !detail.document)
  const yamlIdHint = yaml.touched && !isValidDeviceId(extractDeviceId(yaml.yaml_text))

  return (
    <div className="ies-modeling__page">
      <div className="ies-modeling__header">
        <h2>{pt('ies.modeling.new_model')}</h2>
        <p className="ies-modeling__subtitle">{pt('ies.modeling.new_model_desc')}</p>
      </div>

      <nav className="ies-modeling__tabs" role="tablist" aria-label={pt('ies.modeling.new_model')}>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'template'}
          className={`ies-modeling__tab${tab === 'template' ? ' is-active' : ''}`}
          onClick={() => handleTabChange('template')}
        >
          {pt('ies.modeling.template_tab')}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'yaml'}
          className={`ies-modeling__tab${tab === 'yaml' ? ' is-active' : ''}`}
          onClick={() => handleTabChange('yaml')}
        >
          {pt('ies.modeling.yaml_tab')}
        </button>
      </nav>

      <CandidateStatusBar phase={save.phase} />

      {save.lastError ? (
        <Alert variant="error" title={pt('ies.modeling.candidate.submit_error', { reason: errorMessage(save.lastError) })} closable onClose={save.reset}>
          {pt('ies.modeling.candidate.retry_note')}
        </Alert>
      ) : null}

      {tab === 'template' ? (
        <div className="ies-modeling__template-layout">
          <div className="ies-modeling__template-side">
            <TemplateListPanel
              templates={templates.templates}
              loading={templates.loading}
              error={templates.error}
              selectedId={selectedTemplateId}
              onSelect={setSelectedTemplateId}
              onRetry={() => void templates.reload()}
            />
          </div>
          <div className="ies-modeling__template-main">
            {detail.loading ? <p className="ies-modeling__hint">{t('ies.common.loading')}</p> : null}
            {detail.error && !detail.document ? (
              <Alert variant="error" title={pt('ies.modeling.template_load_error', { reason: detail.error })} />
            ) : null}
            {!detail.loading && !detail.error && !detail.document ? (
              <p className="ies-modeling__hint">{pt('ies.modeling.template_select_hint')}</p>
            ) : null}
            {detail.document ? (
              <div className="ies-modeling__form-panel">
                <TemplateInputsForm
                  nodes={detail.document.inputs}
                  values={form.values}
                  errors={form.visibleErrors}
                  onFieldChange={handleFieldChange}
                  onArrayChange={handleArrayChange}
                  onUploadFile={handleUploadFile}
                  onRemoveFile={handleRemoveFile}
                  uploadingPath={null}
                  disabled={save.phase === 'saved' || save.phase === 'validating'}
                />
              </div>
            ) : null}
            {uploadError ? <Alert variant="error" title={uploadError} closable onClose={() => setUploadError(null)} /> : null}
          </div>
        </div>
      ) : (
        <div className="ies-modeling__yaml-layout">
          <YamlEditorPanel
            yaml_text={yaml.yaml_text}
            touched={yaml.touched}
            onChange={handleYamlChange}
            onReset={() => {
              yaml.reset()
              save.backToEditing()
            }}
            disabled={save.phase === 'saved' || save.phase === 'validating'}
          />
          {yamlIdHint ? <p className="ies-modeling__yaml-warn">{pt('ies.modeling.yaml.id_invalid')}</p> : null}
        </div>
      )}

      <ModelDiagnosticsPanel diagnostics={save.diagnostics} />

      {save.saved ? <SavedModelPanel saved={save.saved} /> : null}

      <div className="ies-modeling__footer">
        <Button
          variant="primary"
          disabled={submitDisabled}
          loading={save.phase === 'validating'}
          onClick={() => void (tab === 'template' ? handleSubmitTemplate() : handleSubmitYaml())}
        >
          {pt('ies.modeling.candidate.submit')}
        </Button>
        <Link className="ies-button ies-button--secondary ies-button--sm" to={backToModelUrl}>
          {pt('ies.modeling.back_to_model')}
        </Link>
      </div>
    </div>
  )
}

/** 从 YAML 文本提取 device.id(即时提示用, 非权威校验)。 */
function extractDeviceId(yamlText: string): string {
  const lines = yamlText.split('\n')
  const deviceIdx = lines.findIndex((l) => /^\s*device:\s*$/.test(l))
  if (deviceIdx < 0) return ''
  for (let i = deviceIdx + 1; i < Math.min(deviceIdx + 6, lines.length); i += 1) {
    const m = lines[i].match(/^\s{2,}id:\s*["']?([^"'\s#]+)/)
    if (m) return m[1]
  }
  return ''
}
