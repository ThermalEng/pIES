/**
 * 自定义 feature(用户模型模板管理)前后端契约。
 *
 * 端点(与后端 /api 路由一致, prefix /api/model-templates):
 *   POST   /api/model-templates                      创建模板草稿
 *   GET    /api/model-templates                      当前用户模板列表 {templates: [...]}
 *   GET    /api/model-templates/{template_id}        模板详情 {template, document, diagnostics}
 *   PUT    /api/model-templates/{template_id}        保存草稿(expected_revision 乐观锁)
 *   POST   /api/model-templates/{template_id}/validate   模板完整校验 {valid, diagnostics}
 *   POST   /api/model-templates/{template_id}/publish    发布/更新唯一当前发布版
 *   POST   /api/model-templates/{template_id}/disable    停用
 *   POST   /api/model-templates/{template_id}/enable     重新启用
 *   DELETE /api/model-templates/{template_id}        删除未发布草稿
 *   GET    /api/model-templates/{template_id}/published  当前唯一发布版详情
 *
 * 依赖方向: features/customization → ../../types / ../../shared/http。
 */

/** 模板生命周期状态。 */
export type TemplateStatusDto = 'draft' | 'published' | 'disabled'

/** 模板列表项(与后端 _template_to_dict 对应)。 */
export interface TemplateDto {
  id: string
  template_id: string
  slug: string | null
  public_namespace: string | null
  status: TemplateStatusDto
  description: string | null
  draft_revision: number
  draft_has_inputs: boolean | null
  published_revision: number
  published_at: string | null
  created_at: string | null
  updated_at: string | null
}

/** 精确发布 revision。 */
export interface TemplateRevisionDto {
  id: string
  revision: number
  schema_version: string
  input_count: number
  yaml_object_id: string
  receipt_object_id: string
  summary_object_id: string
  published_by: string
  published_at: string | null
}

/** 模板详情({template, document, diagnostics})。 */
export interface TemplateDetailDto {
  template: TemplateDto
  document: Record<string, unknown> | null
  diagnostics: ModelDiagnosticDto[]
}

/** 模板列表响应信封: 精确 {templates: [...]}。 */
export interface TemplateListResponseDto {
  templates: TemplateDto[]
}

/** 模板详情响应信封: 精确 {template, document, diagnostics}。 */
export type TemplateDetailResponseDto = TemplateDetailDto

/** 模板变更响应信封(创建/保存草稿/启停用): 精确 {template: {...}}。 */
export interface TemplateMutationResponseDto {
  template: TemplateDto
}

/** 模板校验响应信封: 精确 {valid: boolean, diagnostics: [...]}。 */
export interface TemplateValidateResponseDto {
  valid: boolean
  diagnostics: ModelDiagnosticDto[]
}

/** 模板发布响应信封: 精确 {revision: {...}, duplicate: boolean}。 */
export interface TemplatePublishResponseDto {
  revision: TemplateRevisionDto
  duplicate: boolean
}

/** 模板删除响应信封: 精确 {ok: boolean, deleted: string}。 */
export interface TemplateDeleteResponseDto {
  ok: boolean
  deleted: string
}

/** 模板精确 revision 详情响应信封。 */
export interface TemplateRevisionDetailResponseDto {
  template: TemplateDto
  revision: TemplateRevisionDto
  document: Record<string, unknown>
  receipt: Record<string, unknown>
  summary: Record<string, unknown>
  diagnostics: ModelDiagnosticDto[]
}

/** 创建/保存草稿请求。 */
export interface TemplateDraftRequestDto {
  slug: string
  model_yaml: string
  description?: string | null
  /** 保存草稿时必填(乐观锁)。 */
  expected_revision?: number
}

/** 发布请求(可重试写操作携带幂等键)。 */
export interface TemplatePublishRequestDto {
  expected_revision: number
  idempotency_key?: string | null
}

/** 校验请求(直接提交候选 YAML)。 */
export interface TemplateValidateRequestDto {
  model_yaml: string
}

/** 后端诊断条目(与 backend/core/diagnostics 对应)。 */
export interface ModelDiagnosticDto {
  code: string
  message_key: string
  severity: 'blocking' | 'error' | 'warning' | 'info'
  blocking: boolean
  params: Record<string, unknown>
  location: { object_type: string; field?: string | null; line?: number | null; column?: number | null } | null
  fix_hint_key: string | null
  ref_ids: string[]
}

// ---------------------------------------------------------------------------
// 开放生态与 pIES-Market 契约(/api/ecosystem)
// ---------------------------------------------------------------------------

export type ArtifactKindDto =
  | 'device_model'
  | 'predictor'
  | 'generator'
  | 'solver'
  | 'evaluator'
  | 'algorithm'

export type MarketModeDto = 'local' | 'online'
export type ApprovalStatusDto = 'pending' | 'approved' | 'rejected'
export type InstallStatusDto =
  | 'pending'
  | 'building'
  | 'testing'
  | 'ready'
  | 'build_failed'
  | 'disabled'
export type MemberArtifactStateDto = 'remote' | 'local_uninstalled' | 'local_installed'

/** 实例级正交生态设置(GET/PUT /api/ecosystem/settings)。 */
export interface EcosystemSettingsDto {
  market_mode: MarketModeDto
  local_upload_requires_approval: boolean
  pip_index_url: string | null
  market_index_url: string | null
  submission_gateway_url: string | null
  updated_by?: number | null
  updated_at?: string | null
}

/** 管理员更新生态设置请求体(PUT /api/ecosystem/settings)。 */
export interface EcosystemSettingsUpdateRequestDto {
  market_mode: MarketModeDto
  local_upload_requires_approval: boolean
  pip_index_url?: string | null
  market_index_url?: string | null
  submission_gateway_url?: string | null
}

/** 本地生态制品条目(对应 backend serialize_artifact)。 */
export interface LocalArtifactDto {
  id: number
  kind: ArtifactKindDto
  stable_id: string
  version: string
  ref: string
  source_origin: string
  approval_status: ApprovalStatusDto
  install_status: InstallStatusDto
  uploaded_by: number | null
  approved_by: number | null
  approved_at: string | null
  object_id: string
  metadata: Record<string, unknown>
  image_ref: string | null
  diagnostics: Record<string, unknown> | null
  installed: boolean
  enabled: boolean
  created_at: string | null
  updated_at: string | null
  idempotent?: boolean
  duplicate?: boolean
  upgraded_from?: string
}

/** 本地生态列表响应(GET /api/ecosystem/artifacts)。 */
export interface ArtifactListResponseDto {
  items: LocalArtifactDto[]
}

/** 本地设备模型导入请求(POST /api/ecosystem/device-models/import)。 */
export interface DeviceModelImportRequestDto {
  yaml_text: string
  expected_sha256?: string | null
  revision?: number
  source_origin?: 'local_upload' | 'offline_import'
}

/** 本地设备模型导出响应(GET /api/ecosystem/device-models/{id}/export)。 */
export interface DeviceModelExportResponseDto {
  artifact_id: number
  kind: 'device_model'
  stable_id: string
  revision: number
  filename: string
  yaml_text: string
  metadata: Record<string, unknown>
}

/** 管理员拒绝制品请求(POST /api/ecosystem/artifacts/{id}/reject)。 */
export interface RejectArtifactRequestDto {
  reason: string
}

/** 管理员卸载镜像响应(POST /api/ecosystem/admin/artifacts/{id}/uninstall-image)。 */
export interface AdminUninstallImageResponseDto {
  artifact: LocalArtifactDto
  image_deleted: boolean
  image_status: string
}

/** 远端市场目录条目(GET /api/ecosystem/market/index)。 */
export interface MarketIndexItemDto {
  id: string
  kind: ArtifactKindDto
  version?: string | null
  revision?: number | null
  name: { zh_cn?: string; en_us?: string }
  description: string
  publisher: string
  license: string
  compatibility: Record<string, string>
  consumes?: string[]
  produces?: string[]
  sha256: string
  size_bytes: number
  readme_url?: string | null
  member_state: MemberArtifactStateDto
  local_artifact_id: number | null
  local_version: string | null
  update_available: boolean
}

/** 远端市场目录响应(GET /api/ecosystem/market/index)。 */
export interface MarketIndexResponseDto {
  items: MarketIndexItemDto[]
  generated_at: string
  market_index_url: string
}

/** 从远端市场安装请求(POST /api/ecosystem/market/install)。 */
export interface MarketInstallRequestDto {
  artifact_id: string
  kind: ArtifactKindDto
  version_or_revision?: string | number | null
}

/** 从远端市场安装响应(POST /api/ecosystem/market/install)。 */
export interface MarketInstallResponseDto {
  artifact: LocalArtifactDto
  installed: boolean
}

/** 成员个人卸载响应(POST /api/ecosystem/artifacts/{id}/uninstall)。 */
export interface MemberUninstallResponseDto {
  artifact_id: number
  uninstalled: boolean
}

/** 投稿网关 OTP 请求/校验/投稿响应。 */
export interface MarketOtpRequestResponseDto {
  challenge_token: string
  expires_in: number
}

export interface MarketOtpVerifyResponseDto {
  submission_token: string
  expires_in: number
}

export interface MarketSubmitResponseDto {
  duplicate: boolean
  pr_number: number
  pr_url: string
  branch: string
  artifact_id: string
  kind: ArtifactKindDto
}
