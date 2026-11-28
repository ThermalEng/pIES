/**
 * 统一单页「模型与算法」(/custom, 一级菜单):
 *
 * 双平级页签（单当前生效版本模型，无多版本切换）:
 * 1. 「我的创作/草稿」(默认打开，保持既有 /custom 与 model-new.spec.ts 选择器兼容):
 *    - 设备模型模板草稿列表/编辑/校验/保存/发布(可再次发布)/启停/删除;
 *    - 常用基础字段表单(device.id / names / properties / interfaces)与 YAML 双向同步(equations 保持 YAML);
 *    - 本地 .device.yaml 导入导出、.algorithm.zip 上传(可选 expected_sha256)与提交状态诊断;
 *    - 联网且配置投稿网关时提供「公共市场投稿向导」(发 OTP → 验 OTP → 上传投稿 → PR 链接)。
 * 2. 「市场与已安装」:
 *    - 分类筛选 + 搜索 + 混合展示本地目录与联网远端制品及三态(远端 / 本地未安装 / 本地已安装);
 *    - 支持远端下载安装、本地安装、启用/停用、个人卸载、导出 .device.yaml;
 *    - 联网读取失败显式报错，绝不伪装为空列表; 本地模式零市场出网请求。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'

import { useI18n, translateDiagnostic, errorMessage } from '../i18n'
import { pt } from '../i18n/pageMessages'
import {
  Alert,
  Badge,
  Button,
  Card,
  Dialog,
  EmptyState,
  FormField,
  Input,
  Select,
  Spinner,
  Table,
  Tabs,
  TBody,
  TD,
  Textarea,
  TH,
  THead,
  TR,
} from '../components/ui'
import {
  applyDeviceFormToText,
  createTemplate,
  deleteTemplate,
  diagnosticsFromApiError,
  exportDeviceModel,
  formatArtifactDiagnostics,
  getEcosystemSettings,
  getMarketIndex,
  getPublishedTemplate,
  getTemplateDetail,
  importDeviceModel,
  installEcosystemArtifact,
  installMarketArtifact,
  listEcosystemArtifacts,
  listMyTemplates,
  mergeCatalogEntries,
  parseDeviceFormFromText,
  publishTemplate,
  requestMarketSubmitOtp,
  resolveArtifactLifecycleStatus,
  saveTemplateDraft,
  setEcosystemArtifactEnabled,
  setTemplateEnabled,
  submitMarketArtifact,
  uninstallMemberArtifact,
  uploadPluginZip,
  validateTemplateYaml,
  verifyMarketSubmitOtp,
} from '../features/customization'
import type {
  ArtifactKindDto,
  DeviceBasicFormState,
  EcosystemSettingsDto,
  LocalArtifactDto,
  MarketIndexItemDto,
  MarketSubmitResponseDto,
  ModelDiagnosticDto,
  TemplateDto,
  TemplateRevisionDto,
  UnifiedCatalogEntry,
} from '../features/customization'
import { isValidDeviceId } from '../features/modeling'

const TEMPLATE_SKELETON = [
  'schema: ies.device-model',
  'schema_version: "1.0.0"',
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

const ARTIFACT_KINDS: Array<'all' | ArtifactKindDto> = [
  'all',
  'device_model',
  'predictor',
  'generator',
  'solver',
  'evaluator',
  'algorithm',
]

type PageTab = 'mine' | 'market'
type EditorMode = 'list' | 'edit'

function downloadTextFile(filename: string, text: string) {
  const blob = new Blob([text], { type: 'text/yaml;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

export default function CustomizationPage() {
  const { locale } = useI18n()
  const [activeTab, setActiveTab] = useState<PageTab>('mine')

  // 生态设置、本地目录与远端市场状态
  const [settings, setSettings] = useState<EcosystemSettingsDto | null>(null)
  const [localArtifacts, setLocalArtifacts] = useState<LocalArtifactDto[]>([])
  const [marketItems, setMarketItems] = useState<MarketIndexItemDto[]>([])
  const [marketError, setMarketError] = useState<string | null>(null)
  const [ecoLoading, setEcoLoading] = useState(true)
  const [ecoActionError, setEcoActionError] = useState<string | null>(null)
  const [ecoActionOk, setEcoActionOk] = useState<string | null>(null)
  const [ecoBusyKey, setEcoBusyKey] = useState<string | null>(null)

  // 「市场与已安装」筛选
  const [kindFilter, setKindFilter] = useState<'all' | ArtifactKindDto>('all')
  const [searchQuery, setSearchQuery] = useState('')

  // 模板草稿状态（与既有 /custom 完全兼容）
  const [templates, setTemplates] = useState<TemplateDto[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [mode, setMode] = useState<EditorMode>('list')
  const [selected, setSelected] = useState<TemplateDto | null>(null)
  const [slug, setSlug] = useState('')
  const [yamlText, setYamlText] = useState(TEMPLATE_SKELETON)
  const [description, setDescription] = useState('')
  const [diagnostics, setDiagnostics] = useState<ModelDiagnosticDto[]>([])
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [revisionDetail, setRevisionDetail] = useState<{
    revision: TemplateRevisionDto
    summary: Record<string, unknown> | null
  } | null>(null)
  const [showBasicForm, setShowBasicForm] = useState(false)

  // 本地 .device.yaml 导入 & .algorithm.zip 上传
  const [importYaml, setImportYaml] = useState('')
  const [importRevision, setImportRevision] = useState('1')
  const [importSha, setImportSha] = useState('')
  const [pluginFile, setPluginFile] = useState<File | null>(null)
  const [pluginSha, setPluginSha] = useState('')
  const [uploadBusy, setUploadBusy] = useState(false)

  // 公共市场投稿向导弹窗（仅 online + submission_gateway_url 可用）
  const [submitOpen, setSubmitOpen] = useState(false)
  const [submitEmail, setSubmitEmail] = useState('')
  const [challengeToken, setChallengeToken] = useState('')
  const [otpCode, setOtpCode] = useState('')
  const [submissionToken, setSubmissionToken] = useState('')
  const [subKind, setSubKind] = useState<ArtifactKindDto>('device_model')
  const [subArtifactId, setSubArtifactId] = useState('')
  const [subVersion, setSubVersion] = useState('1')
  const [subPublisher, setSubPublisher] = useState('')
  const [subLicense, setSubLicense] = useState('MPL-2.0')
  const [subDesc, setSubDesc] = useState('')
  const [subReadme, setSubReadme] = useState('')
  const [subFile, setSubFile] = useState<File | null>(null)
  const [subBusy, setSubBusy] = useState(false)
  const [subError, setSubError] = useState<string | null>(null)
  const [subResult, setSubResult] = useState<MarketSubmitResponseDto | null>(null)

  const reloadTemplates = useCallback(async (): Promise<TemplateDto[] | null> => {
    setLoading(true)
    setListError(null)
    try {
      const items = await listMyTemplates()
      setTemplates(items)
      return items
    } catch (err) {
      setListError(err instanceof Error ? err.message : String(err))
      setTemplates(null)
      return null
    } finally {
      setLoading(false)
    }
  }, [])

  const reloadEcosystem = useCallback(async () => {
    setEcoLoading(true)
    setMarketError(null)
    try {
      const [cfg, artifacts] = await Promise.all([
        getEcosystemSettings(),
        listEcosystemArtifacts(),
      ])
      setSettings(cfg)
      setLocalArtifacts(artifacts)
      if (cfg.market_mode === 'online') {
        try {
          const market = await getMarketIndex()
          setMarketItems(market.items)
        } catch (err) {
          setMarketItems([])
          setMarketError(errorMessage(err))
        }
      } else {
        setMarketItems([])
      }
    } catch (err) {
      setEcoActionError(errorMessage(err))
    } finally {
      setEcoLoading(false)
    }
  }, [])

  useEffect(() => {
    void reloadTemplates()
    void reloadEcosystem()
  }, [reloadTemplates, reloadEcosystem])

  // 设备基础表单双向同步视图（从 YAML 实时派生）
  const deviceForm: DeviceBasicFormState = useMemo(
    () => parseDeviceFormFromText(yamlText),
    [yamlText],
  )

  const updateDeviceForm = useCallback(
    (patch: Partial<Omit<DeviceBasicFormState, 'valid'>>) => {
      const next = {
        deviceId: patch.deviceId ?? deviceForm.deviceId,
        nameZh: patch.nameZh ?? deviceForm.nameZh,
        nameEn: patch.nameEn ?? deviceForm.nameEn,
        properties: patch.properties ?? deviceForm.properties,
        interfaces: patch.interfaces ?? deviceForm.interfaces,
      }
      setYamlText(applyDeviceFormToText(yamlText, next))
    },
    [deviceForm, yamlText],
  )

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
        setSlug(detail.template.slug ?? '')
        setDiagnostics(detail.diagnostics)
      } catch (err) {
        setActionError(err instanceof Error ? err.message : String(err))
        setYamlText(TEMPLATE_SKELETON)
        setDescription('')
        setSlug('')
      }
    } else {
      setYamlText(TEMPLATE_SKELETON)
      setDescription('')
      setSlug('')
    }
    setMode('edit')
  }, [])

  const closeEditor = useCallback(() => {
    setMode('list')
    setSelected(null)
    setDiagnostics([])
    setRevisionDetail(null)
    void reloadTemplates()
  }, [reloadTemplates])

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

  const handleSaveDraft = useCallback(async () => {
    setBusy(true)
    setActionError(null)
    try {
      let createSlug = slug.trim()
      if (!selected && !createSlug) {
        const m = yamlText.match(/^\s{2}id:\s*["']?([^"'\s#]+)/m)
        if (m) {
          const parts = m[1].split('.')
          createSlug = parts[parts.length - 1] || ''
        }
      }
      const result = selected
        ? await saveTemplateDraft(selected.template_id, yamlText, selected.draft_revision, description)
        : await createTemplate(createSlug || 'my-device', yamlText, description || null)
      setSelected(result)
      setDiagnostics([])
      await reloadTemplates()
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
  }, [selected, yamlText, slug, description, reloadTemplates])

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
      const list = await reloadTemplates()
      const updated = list?.find((item) => item.template_id === selected.template_id) ?? null
      if (updated) setSelected(updated)
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
  }, [selected, reloadTemplates])

  const handleToggleEnabled = useCallback(async (tpl: TemplateDto, enabled: boolean) => {
    setBusy(true)
    setActionError(null)
    try {
      await setTemplateEnabled(tpl.template_id, enabled)
      await reloadTemplates()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [reloadTemplates])

  const handleDelete = useCallback(async (tpl: TemplateDto) => {
    setBusy(true)
    setActionError(null)
    try {
      await deleteTemplate(tpl.template_id)
      await reloadTemplates()
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }, [reloadTemplates])

  const showPublished = useCallback(async (tpl: TemplateDto) => {
    setBusy(true)
    setActionError(null)
    try {
      const detail = await getPublishedTemplate(tpl.template_id)
      setSelected(detail.template)
      setRevisionDetail({ revision: detail.revision, summary: detail.summary })
      if (detail.document) {
        setYamlText(JSON.stringify(detail.document, null, 2))
      }
      setMode('edit')
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

  // 本地导入与插件上传
  const handleImportDevice = async () => {
    if (!importYaml.trim()) return
    setUploadBusy(true)
    setEcoActionError(null)
    setEcoActionOk(null)
    try {
      const rev = Math.max(1, Number.parseInt(importRevision, 10) || 1)
      const created = await importDeviceModel({
        yaml_text: importYaml,
        revision: rev,
        expected_sha256: importSha.trim() || null,
        source_origin: 'local_upload',
      })
      setEcoActionOk(pt('ies.eco.import_ok', { id: created.stable_id, status: created.approval_status }))
      setImportYaml('')
      setImportSha('')
      await reloadEcosystem()
    } catch (err) {
      setEcoActionError(errorMessage(err))
    } finally {
      setUploadBusy(false)
    }
  }

  const handleUploadPlugin = async () => {
    if (!pluginFile) return
    setUploadBusy(true)
    setEcoActionError(null)
    setEcoActionOk(null)
    try {
      const created = await uploadPluginZip({
        file: pluginFile,
        expected_sha256: pluginSha.trim() || null,
        source_origin: 'local_upload',
      })
      setEcoActionOk(pt('ies.eco.upload_ok', { id: created.stable_id, status: created.approval_status }))
      setPluginFile(null)
      setPluginSha('')
      await reloadEcosystem()
    } catch (err) {
      setEcoActionError(errorMessage(err))
    } finally {
      setUploadBusy(false)
    }
  }

  const handleExportDevice = async (artifactId: number) => {
    setEcoBusyKey(`export-${artifactId}`)
    setEcoActionError(null)
    try {
      const res = await exportDeviceModel(artifactId)
      downloadTextFile(res.filename, res.yaml_text)
    } catch (err) {
      setEcoActionError(errorMessage(err))
    } finally {
      setEcoBusyKey(null)
    }
  }

  const handleCatalogAction = async (entry: UnifiedCatalogEntry, action: 'remote_install' | 'local_install' | 'enable' | 'disable' | 'uninstall') => {
    setEcoBusyKey(`${action}-${entry.key}`)
    setEcoActionError(null)
    setEcoActionOk(null)
    try {
      if (action === 'remote_install') {
        await installMarketArtifact({
          artifact_id: entry.id,
          kind: entry.kind,
          version_or_revision: entry.kind === 'device_model'
            ? Number((entry.remoteVersionOrRevision ?? '').replace(/^r/, '')) || 1
            : entry.remoteVersionOrRevision,
        })
      } else if (action === 'local_install' && entry.localArtifact) {
        await installEcosystemArtifact(entry.localArtifact.id)
      } else if (action === 'enable' && entry.localArtifact) {
        await setEcosystemArtifactEnabled(entry.localArtifact.id, true)
      } else if (action === 'disable' && entry.localArtifact) {
        await setEcosystemArtifactEnabled(entry.localArtifact.id, false)
      } else if (action === 'uninstall' && entry.localArtifact) {
        await uninstallMemberArtifact(entry.localArtifact.id)
      }
      await reloadEcosystem()
    } catch (err) {
      setEcoActionError(errorMessage(err))
    } finally {
      setEcoBusyKey(null)
    }
  }

  // 投稿向导动作
  const canSubmitToMarket = Boolean(
    settings && settings.market_mode === 'online' && settings.submission_gateway_url,
  )

  const handleRequestOtp = async () => {
    if (!submitEmail.trim()) return
    setSubBusy(true)
    setSubError(null)
    try {
      const res = await requestMarketSubmitOtp(submitEmail.trim())
      setChallengeToken(res.challenge_token)
    } catch (err) {
      setSubError(errorMessage(err))
    } finally {
      setSubBusy(false)
    }
  }

  const handleVerifyOtp = async () => {
    if (!submitEmail.trim() || !otpCode.trim() || !challengeToken) return
    setSubBusy(true)
    setSubError(null)
    try {
      const res = await verifyMarketSubmitOtp({
        email: submitEmail.trim(),
        otp_code: otpCode.trim(),
        challenge_token: challengeToken,
      })
      setSubmissionToken(res.submission_token)
    } catch (err) {
      setSubError(errorMessage(err))
    } finally {
      setSubBusy(false)
    }
  }

  const handleSubmitMarketPr = async () => {
    if (!submissionToken || !subFile) return
    setSubBusy(true)
    setSubError(null)
    setSubResult(null)
    try {
      const res = await submitMarketArtifact({
        submission_token: submissionToken,
        kind: subKind,
        artifact_id: subArtifactId.trim(),
        version_or_revision: subVersion.trim(),
        publisher: subPublisher.trim(),
        license: subLicense.trim(),
        description: subDesc.trim(),
        readme_markdown: subReadme,
        file: subFile,
      })
      setSubResult(res)
    } catch (err) {
      setSubError(errorMessage(err))
    } finally {
      setSubBusy(false)
    }
  }

  // 合并「市场与已安装」列表
  const mergedEntries = useMemo(
    () =>
      mergeCatalogEntries({
        localArtifacts,
        marketItems,
        marketMode: settings?.market_mode ?? 'local',
      }),
    [localArtifacts, marketItems, settings?.market_mode],
  )

  const visibleCatalogEntries = useMemo(() => {
    const q = searchQuery.trim().toLowerCase()
    return mergedEntries.filter((entry) => {
      if (kindFilter !== 'all' && entry.kind !== kindFilter) return false
      if (!q) return true
      return [entry.id, entry.displayNameZh, entry.displayNameEn, entry.description, entry.publisher ?? '']
        .some((v) => v.toLowerCase().includes(q))
    })
  }, [mergedEntries, kindFilter, searchQuery])

  return (
    <div className="ies-custom-page">
      <div className="ies-page-title-row">
        <div>
          <h1 className="ies-page-title">{pt('ies.eco.page_title')}</h1>
          <p className="ies-page-subtitle">{pt('ies.custom.desc')}</p>
        </div>
        {settings ? (
          <Badge
            variant={settings.market_mode === 'online' ? 'info' : 'neutral'}
            label={pt(settings.market_mode === 'online' ? 'ies.eco.mode_online' : 'ies.eco.mode_local')}
          />
        ) : null}
      </div>

      <Tabs<PageTab>
        activeId={activeTab}
        ariaLabel={pt('ies.eco.page_title')}
        onChange={(next) => {
          setActiveTab(next)
          setEcoActionError(null)
          setEcoActionOk(null)
        }}
        items={[
          { id: 'mine', label: pt('ies.eco.tab_mine') },
          { id: 'market', label: pt('ies.eco.tab_market') },
        ]}
      />

      {ecoActionError ? (
        <div className="ies-section">
          <Alert variant="error" title={ecoActionError} closable onClose={() => setEcoActionError(null)} />
        </div>
      ) : null}
      {ecoActionOk ? (
        <div className="ies-section">
          <Alert variant="success" title={ecoActionOk} closable onClose={() => setEcoActionOk(null)} />
        </div>
      ) : null}

      {activeTab === 'market' ? (
        <div className="ies-stack" style={{ marginTop: 'var(--ies-space-4)' }}>
          {settings?.market_mode === 'local' ? (
            <Alert variant="info" title={pt('ies.eco.local_mode_notice')} />
          ) : null}
          {marketError ? (
            <Alert variant="error" title={pt('ies.eco.market_load_error', { reason: marketError })}>
              <Button size="sm" variant="secondary" onClick={() => void reloadEcosystem()}>
                {pt('ies.eco.retry')}
              </Button>
            </Alert>
          ) : null}

          <Card>
            <div className="ies-form-row" style={{ flexWrap: 'wrap', gap: 'var(--ies-space-3)' }}>
              <Input
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                placeholder={pt('ies.eco.search_placeholder')}
                aria-label={pt('ies.eco.search_placeholder')}
              />
              <div className="ies-flex" style={{ flexWrap: 'wrap' }} role="group" aria-label={pt('ies.eco.filter_kind')}>
                {ARTIFACT_KINDS.map((kind) => (
                  <Button
                    key={kind}
                    size="sm"
                    variant={kindFilter === kind ? 'primary' : 'secondary'}
                    onClick={() => setKindFilter(kind)}
                  >
                    {pt(`ies.eco.kind.${kind}`)}
                  </Button>
                ))}
              </div>
              <Button size="sm" variant="ghost" onClick={() => void reloadEcosystem()} loading={ecoLoading}>
                {pt('ies.eco.refresh')}
              </Button>
            </div>
          </Card>

          {ecoLoading ? (
            <div className="ies-page-placeholder"><Spinner size="lg" /></div>
          ) : visibleCatalogEntries.length === 0 && !marketError ? (
            <EmptyState icon="info" title={pt('ies.eco.catalog_empty')} />
          ) : (
            <div className="ies-custom-list">
              {visibleCatalogEntries.map((entry) => {
                const title = locale === 'en' ? entry.displayNameEn : entry.displayNameZh
                const stateLabel =
                  entry.memberState === 'remote'
                    ? pt('ies.eco.state_remote')
                    : entry.memberState === 'local_installed'
                      ? pt('ies.eco.state_local_installed')
                      : pt('ies.eco.state_local_uninstalled')
                const stateVariant =
                  entry.memberState === 'local_installed'
                    ? 'success'
                    : entry.memberState === 'local_uninstalled'
                      ? 'info'
                      : 'neutral'
                return (
                  <div key={entry.key} className="ies-custom-card" data-artifact-key={entry.key}>
                    <div className="ies-custom-card__head">
                      <div>
                        <strong>{title}</strong>{' '}
                        <code className="ies-code">{entry.id}</code>
                      </div>
                      <div className="ies-flex">
                        <Badge variant="neutral" size="sm" label={pt(`ies.eco.kind.${entry.kind}`)} />
                        <Badge variant="neutral" size="sm" label={entry.versionOrRevision} />
                        <Badge variant={stateVariant} size="sm" label={stateLabel} />
                        {entry.localArtifact && entry.memberState === 'local_installed' && !entry.localArtifact.enabled ? (
                          <Badge variant="warning" size="sm" label={pt('ies.custom.status.disabled')} />
                        ) : null}
                      </div>
                    </div>
                    {entry.description ? (
                      <p className="ies-text-secondary" style={{ margin: '6px 0' }}>{entry.description}</p>
                    ) : null}
                    <div className="ies-custom-card__meta">
                      {entry.publisher ? <span>{pt('ies.eco.publisher')}: {entry.publisher}</span> : null}
                      {entry.license ? <span>{pt('ies.eco.license')}: {entry.license}</span> : null}
                      {entry.sha256 ? <span>SHA-256: <code>{entry.sha256.slice(0, 12)}…</code></span> : null}
                    </div>
                    <div className="ies-custom-card__actions">
                      {entry.memberState === 'remote' ? (
                        <Button
                          size="sm"
                          variant="primary"
                          loading={ecoBusyKey === `remote_install-${entry.key}`}
                          disabled={ecoBusyKey !== null}
                          onClick={() => void handleCatalogAction(entry, 'remote_install')}
                        >
                          {pt('ies.eco.download_install')}
                        </Button>
                      ) : null}
                      {entry.memberState === 'local_uninstalled' && entry.localArtifact ? (
                        <Button
                          size="sm"
                          variant="primary"
                          loading={ecoBusyKey === `local_install-${entry.key}`}
                          disabled={ecoBusyKey !== null}
                          onClick={() => void handleCatalogAction(entry, 'local_install')}
                        >
                          {pt('ies.eco.install')}
                        </Button>
                      ) : null}
                      {entry.memberState === 'local_installed' && entry.localArtifact ? (
                        <>
                          <Button
                            size="sm"
                            variant="secondary"
                            loading={ecoBusyKey === `${entry.localArtifact.enabled ? 'disable' : 'enable'}-${entry.key}`}
                            disabled={ecoBusyKey !== null}
                            onClick={() =>
                              void handleCatalogAction(
                                entry,
                                entry.localArtifact?.enabled ? 'disable' : 'enable',
                              )
                            }
                          >
                            {entry.localArtifact.enabled ? pt('ies.custom.disable') : pt('ies.custom.enable')}
                          </Button>
                          <Button
                            size="sm"
                            variant="danger"
                            loading={ecoBusyKey === `uninstall-${entry.key}`}
                            disabled={ecoBusyKey !== null}
                            onClick={() => void handleCatalogAction(entry, 'uninstall')}
                          >
                            {pt('ies.eco.uninstall')}
                          </Button>
                        </>
                      ) : null}
                      {entry.updateAvailable ? (
                        <Button
                          size="sm"
                          variant="secondary"
                          loading={ecoBusyKey === `remote_install-${entry.key}`}
                          disabled={ecoBusyKey !== null}
                          onClick={() => void handleCatalogAction(entry, 'remote_install')}
                        >
                          {pt('ies.eco.upgrade_to', { ver: entry.remoteVersionOrRevision ?? '' })}
                        </Button>
                      ) : null}
                      {entry.kind === 'device_model' && entry.localArtifact ? (
                        <Button
                          size="sm"
                          variant="secondary"
                          loading={ecoBusyKey === `export-${entry.localArtifact.id}`}
                          disabled={ecoBusyKey !== null}
                          onClick={() => void handleExportDevice(entry.localArtifact!.id)}
                        >
                          {pt('ies.eco.export_device')}
                        </Button>
                      ) : null}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      ) : (
        <div className="ies-stack" style={{ marginTop: 'var(--ies-space-4)' }}>
          {/* 1. 模板草稿管理（完全保留 /custom 既有选择器与工作流） */}
          <Card
            title={pt('ies.eco.templates_section')}
            actions={
              <div className="ies-flex">
                {canSubmitToMarket ? (
                  <Button variant="secondary" size="sm" onClick={() => { setSubResult(null); setSubError(null); setSubmitOpen(true) }}>
                    {pt('ies.eco.submit_market_btn')}
                  </Button>
                ) : null}
                <Button variant="primary" size="sm" onClick={() => void openEditor(null)}>
                  {pt('ies.custom.create')}
                </Button>
              </div>
            }
          >
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
                      {tpl.slug ? <span>slug: {tpl.slug}</span> : null}
                      {tpl.public_namespace ? <span>ns: {tpl.public_namespace.slice(0, 6)}…</span> : null}
                      <span>
                        {pt('ies.custom.draft_rev')}: {tpl.draft_revision}
                      </span>
                      <span>
                        {pt('ies.custom.published_rev')}: {tpl.published_revision}
                      </span>
                    </div>
                    <div className="ies-custom-card__actions">
                      {tpl.published_revision > 0 ? (
                        <>
                          <Button variant="secondary" size="sm" disabled={busy} onClick={() => void showPublished(tpl)}>
                            {pt('ies.custom.view_published')}
                          </Button>
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
                  <Button variant="secondary" size="sm" onClick={() => setShowBasicForm((v) => !v)}>
                    {pt('ies.eco.toggle_basic_form')}
                  </Button>
                </div>

                {!selected ? (
                  <>
                    <label className="ies-form-label" htmlFor="ies-custom-slug">
                      slug {pt('ies.custom.slug_hint')}
                    </label>
                    <input
                      id="ies-custom-slug"
                      className="ies-input"
                      value={slug}
                      onChange={(e) => setSlug(e.target.value)}
                      maxLength={64}
                      placeholder="my-heat-pump"
                      pattern="[a-z0-9]+([._-][a-z0-9]+)*"
                      aria-label="slug"
                    />
                  </>
                ) : (
                  <div className="ies-custom-id-display">
                    <span>slug: {selected.slug ?? '—'}</span>
                    {' · '}
                    <span>namespace: {selected.public_namespace ?? '—'}</span>
                    {' · '}
                    <span>完整 ID: {selected.template_id}</span>
                  </div>
                )}

                {showBasicForm && deviceForm.valid ? (
                  <Card title={pt('ies.eco.basic_form_title')}>
                    <div className="ies-form-grid">
                      <FormField label="device.id" htmlFor="ies-device-form-id">
                        <Input
                          id="ies-device-form-id"
                          value={deviceForm.deviceId}
                          onChange={(e) => updateDeviceForm({ deviceId: e.target.value })}
                        />
                      </FormField>
                      <FormField label="names.zh-CN" htmlFor="ies-device-form-zh">
                        <Input
                          id="ies-device-form-zh"
                          value={deviceForm.nameZh}
                          onChange={(e) => updateDeviceForm({ nameZh: e.target.value })}
                        />
                      </FormField>
                      <FormField label="names.en-US" htmlFor="ies-device-form-en">
                        <Input
                          id="ies-device-form-en"
                          value={deviceForm.nameEn}
                          onChange={(e) => updateDeviceForm({ nameEn: e.target.value })}
                        />
                      </FormField>
                    </div>
                    <div style={{ marginTop: 'var(--ies-space-3)' }}>
                      <div className="ies-flex" style={{ justifyContent: 'space-between' }}>
                        <strong>properties ({deviceForm.properties.length})</strong>
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() =>
                            updateDeviceForm({
                              properties: [
                                ...deviceForm.properties,
                                { name: `prop_${deviceForm.properties.length + 1}`, value: '1', unit: '1', min: '0', max: '' },
                              ],
                            })
                          }
                        >
                          + {pt('ies.eco.add_property')}
                        </Button>
                      </div>
                      {deviceForm.properties.map((row, idx) => (
                        <div key={idx} className="ies-form-row" style={{ marginTop: 'var(--ies-space-2)' }}>
                          <Input
                            value={row.name}
                            aria-label={`prop-name-${idx}`}
                            placeholder="name"
                            onChange={(e) => {
                              const next = [...deviceForm.properties]
                              next[idx] = { ...row, name: e.target.value }
                              updateDeviceForm({ properties: next })
                            }}
                          />
                          <Input
                            value={row.value}
                            aria-label={`prop-value-${idx}`}
                            placeholder="value"
                            onChange={(e) => {
                              const next = [...deviceForm.properties]
                              next[idx] = { ...row, value: e.target.value }
                              updateDeviceForm({ properties: next })
                            }}
                          />
                          <Input
                            value={row.unit}
                            aria-label={`prop-unit-${idx}`}
                            placeholder="unit"
                            onChange={(e) => {
                              const next = [...deviceForm.properties]
                              next[idx] = { ...row, unit: e.target.value }
                              updateDeviceForm({ properties: next })
                            }}
                          />
                        </div>
                      ))}
                    </div>
                    <div style={{ marginTop: 'var(--ies-space-3)' }}>
                      <div className="ies-flex" style={{ justifyContent: 'space-between' }}>
                        <strong>interfaces ({deviceForm.interfaces.length})</strong>
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() =>
                            updateDeviceForm({
                              interfaces: [
                                ...deviceForm.interfaces,
                                { name: `port_${deviceForm.interfaces.length + 1}`, type: 'in', carrier: 'electricity', unit: 'kW' },
                              ],
                            })
                          }
                        >
                          + {pt('ies.eco.add_interface')}
                        </Button>
                      </div>
                      {deviceForm.interfaces.map((row, idx) => (
                        <div key={idx} className="ies-form-row" style={{ marginTop: 'var(--ies-space-2)' }}>
                          <Input
                            value={row.name}
                            aria-label={`iface-name-${idx}`}
                            placeholder="name"
                            onChange={(e) => {
                              const next = [...deviceForm.interfaces]
                              next[idx] = { ...row, name: e.target.value }
                              updateDeviceForm({ interfaces: next })
                            }}
                          />
                          <Select
                            value={row.type}
                            aria-label={`iface-type-${idx}`}
                            onChange={(e) => {
                              const next = [...deviceForm.interfaces]
                              next[idx] = { ...row, type: e.target.value }
                              updateDeviceForm({ interfaces: next })
                            }}
                          >
                            <option value="in">in</option>
                            <option value="out">out</option>
                            <option value="bidir">bidir</option>
                            <option value="predefined">predefined</option>
                          </Select>
                          <Input
                            value={row.carrier}
                            aria-label={`iface-carrier-${idx}`}
                            placeholder="carrier"
                            onChange={(e) => {
                              const next = [...deviceForm.interfaces]
                              next[idx] = { ...row, carrier: e.target.value }
                              updateDeviceForm({ interfaces: next })
                            }}
                          />
                        </div>
                      ))}
                    </div>
                  </Card>
                ) : null}

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
                    <Button variant="primary" disabled={busy} loading={busy} onClick={() => void handlePublish()}>
                      {pt('ies.custom.publish')}
                    </Button>
                  ) : null}
                </div>
              </div>
            )}
          </Card>

          {/* 2. 本地 .device.yaml 导入与 .algorithm.zip 上传 */}
          <div className="ies-grid ies-grid--cols-2">
            <Card title={pt('ies.eco.import_device_title')}>
              <FormField label={pt('ies.eco.yaml_file_or_text')} htmlFor="ies-eco-import-yaml">
                <input
                  type="file"
                  accept=".yaml,.yml"
                  aria-label={pt('ies.eco.choose_device_file')}
                  onChange={(e) => {
                    const file = e.target.files?.[0]
                    if (file) void file.text().then((txt) => setImportYaml(txt))
                  }}
                />
                <Textarea
                  id="ies-eco-import-yaml"
                  value={importYaml}
                  onChange={(e) => setImportYaml(e.target.value)}
                  rows={5}
                  placeholder="schema: ies.device-model"
                />
              </FormField>
              <div className="ies-form-grid">
                <FormField label="revision" htmlFor="ies-eco-import-rev">
                  <Input
                    id="ies-eco-import-rev"
                    value={importRevision}
                    onChange={(e) => setImportRevision(e.target.value)}
                    inputMode="numeric"
                  />
                </FormField>
                <FormField label={pt('ies.eco.expected_sha_optional')} htmlFor="ies-eco-import-sha">
                  <Input
                    id="ies-eco-import-sha"
                    value={importSha}
                    onChange={(e) => setImportSha(e.target.value)}
                    placeholder="64-hex sha256"
                  />
                </FormField>
              </div>
              <Button
                variant="primary"
                size="sm"
                loading={uploadBusy}
                disabled={uploadBusy || !importYaml.trim()}
                onClick={() => void handleImportDevice()}
              >
                {pt('ies.eco.import_device_btn')}
              </Button>
            </Card>

            <Card title={pt('ies.eco.upload_plugin_title')}>
              <FormField label={pt('ies.eco.plugin_zip_file')} htmlFor="ies-eco-plugin-file">
                <input
                  id="ies-eco-plugin-file"
                  type="file"
                  accept=".zip"
                  onChange={(e) => setPluginFile(e.target.files?.[0] ?? null)}
                />
              </FormField>
              <FormField label={pt('ies.eco.expected_sha_optional')} htmlFor="ies-eco-plugin-sha">
                <Input
                  id="ies-eco-plugin-sha"
                  value={pluginSha}
                  onChange={(e) => setPluginSha(e.target.value)}
                  placeholder="64-hex sha256"
                />
              </FormField>
              <Button
                variant="primary"
                size="sm"
                loading={uploadBusy}
                disabled={uploadBusy || !pluginFile}
                onClick={() => void handleUploadPlugin()}
              >
                {pt('ies.eco.upload_plugin_btn')}
              </Button>
            </Card>
          </div>

          {/* 3. 本地上传/导入提交状态与诊断 */}
          <Card title={pt('ies.eco.submissions_title')}>
            {localArtifacts.length === 0 ? (
              <p className="ies-text-secondary">{pt('ies.eco.submissions_empty')}</p>
            ) : (
              <Table>
                <THead>
                  <TR>
                    <TH>ID</TH>
                    <TH>{pt('ies.eco.col_kind')}</TH>
                    <TH>{pt('ies.eco.col_version')}</TH>
                    <TH>{pt('ies.eco.col_status')}</TH>
                    <TH>{pt('ies.eco.col_diag')}</TH>
                    <TH>{pt('ies.eco.col_actions')}</TH>
                  </TR>
                </THead>
                <TBody>
                  {localArtifacts.map((art) => {
                    const life = resolveArtifactLifecycleStatus(art)
                    const diagText = formatArtifactDiagnostics(art.diagnostics)
                    const badgeVariant =
                      life === 'ready'
                        ? 'success'
                        : life === 'build_failed' || life === 'rejected'
                          ? 'danger'
                          : life === 'pending_approval' || life === 'disabled'
                            ? 'warning'
                            : 'info'
                    return (
                      <TR key={art.id}>
                        <TD><code>{art.stable_id}</code></TD>
                        <TD>{pt(`ies.eco.kind.${art.kind}`)}</TD>
                        <TD>{art.kind === 'device_model' ? `r${art.version}` : art.version}</TD>
                        <TD>
                          <Badge variant={badgeVariant} size="sm" label={pt(`ies.eco.life.${life}`)} />
                        </TD>
                        <TD>{diagText ?? '—'}</TD>
                        <TD>
                          {art.kind === 'device_model' ? (
                            <Button
                              size="sm"
                              variant="secondary"
                              loading={ecoBusyKey === `export-${art.id}`}
                              onClick={() => void handleExportDevice(art.id)}
                            >
                              {pt('ies.eco.export_device')}
                            </Button>
                          ) : '—'}
                        </TD>
                      </TR>
                    )
                  })}
                </TBody>
              </Table>
            )}
          </Card>
        </div>
      )}

      {/* 公共市场投稿向导弹窗（仅 online + submission_gateway_url） */}
      <Dialog
        open={submitOpen}
        onClose={() => setSubmitOpen(false)}
        title={pt('ies.eco.submit_modal_title')}
        size="lg"
      >
        <div className="ies-stack">
          {subError ? <Alert variant="error" title={subError} /> : null}
          {subResult ? (
            <Alert variant="success" title={pt('ies.eco.submit_pr_ok', { pr: subResult.pr_number })}>
              <div>
                {subResult.duplicate ? <span>{pt('ies.eco.submit_duplicate')} · </span> : null}
                <a href={subResult.pr_url} target="_blank" rel="noopener noreferrer">
                  {subResult.pr_url}
                </a>
              </div>
            </Alert>
          ) : null}

          <Card title={pt('ies.eco.submit_step1')}>
            <div className="ies-form-row">
              <Input
                value={submitEmail}
                onChange={(e) => setSubmitEmail(e.target.value)}
                placeholder="author@example.com"
                aria-label={pt('ies.eco.submit_email')}
              />
              <Button size="sm" variant="secondary" loading={subBusy} onClick={() => void handleRequestOtp()}>
                {pt('ies.eco.send_otp')}
              </Button>
            </div>
          </Card>

          {challengeToken ? (
            <Card title={pt('ies.eco.submit_step2')}>
              <div className="ies-form-row">
                <Input
                  value={otpCode}
                  onChange={(e) => setOtpCode(e.target.value)}
                  placeholder="6-digit OTP"
                  aria-label={pt('ies.eco.otp_code')}
                />
                <Button size="sm" variant="primary" loading={subBusy} onClick={() => void handleVerifyOtp()}>
                  {pt('ies.eco.verify_otp')}
                </Button>
              </div>
            </Card>
          ) : null}

          {submissionToken ? (
            <Card title={pt('ies.eco.submit_step3')}>
              <div className="ies-form-grid">
                <FormField label={pt('ies.eco.col_kind')} htmlFor="sub-kind">
                  <Select id="sub-kind" value={subKind} onChange={(e) => setSubKind(e.target.value as ArtifactKindDto)}>
                    {ARTIFACT_KINDS.filter((k) => k !== 'all').map((k) => (
                      <option key={k} value={k}>{k}</option>
                    ))}
                  </Select>
                </FormField>
                <FormField label="artifact_id" htmlFor="sub-id">
                  <Input id="sub-id" value={subArtifactId} onChange={(e) => setSubArtifactId(e.target.value)} placeholder="acme.device.sample" />
                </FormField>
                <FormField label="version / revision" htmlFor="sub-ver">
                  <Input id="sub-ver" value={subVersion} onChange={(e) => setSubVersion(e.target.value)} />
                </FormField>
                <FormField label="publisher" htmlFor="sub-pub">
                  <Input id="sub-pub" value={subPublisher} onChange={(e) => setSubPublisher(e.target.value)} placeholder="acme" />
                </FormField>
                <FormField label="license" htmlFor="sub-lic">
                  <Input id="sub-lic" value={subLicense} onChange={(e) => setSubLicense(e.target.value)} />
                </FormField>
                <FormField label="description" htmlFor="sub-desc">
                  <Input id="sub-desc" value={subDesc} onChange={(e) => setSubDesc(e.target.value)} />
                </FormField>
              </div>
              <FormField label="README Markdown" htmlFor="sub-readme">
                <Textarea id="sub-readme" value={subReadme} onChange={(e) => setSubReadme(e.target.value)} rows={4} />
              </FormField>
              <FormField label="Package File" htmlFor="sub-file">
                <input id="sub-file" type="file" onChange={(e) => setSubFile(e.target.files?.[0] ?? null)} />
              </FormField>
              <Button variant="primary" size="sm" loading={subBusy} disabled={!subFile} onClick={() => void handleSubmitMarketPr()}>
                {pt('ies.eco.submit_pr_btn')}
              </Button>
            </Card>
          ) : null}
        </div>
      </Dialog>
    </div>
  )
}

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
