/**
 * 自定义 feature(用户模型模板管理)API 客户端。
 *
 * 端点清单(prefix /api/model-templates)见 contracts.ts。本层只做路径拼接、
 * 信封解包与类型化响应直读; 不组织跨端点工作流。
 */

import { request } from '../../shared/http'
import { ApiError } from '../../types'
import type {
  AdminUninstallImageResponseDto,
  ArtifactKindDto,
  ArtifactListResponseDto,
  DeviceModelExportResponseDto,
  DeviceModelImportRequestDto,
  EcosystemSettingsDto,
  EcosystemSettingsUpdateRequestDto,
  LocalArtifactDto,
  MarketIndexResponseDto,
  MarketInstallRequestDto,
  MarketInstallResponseDto,
  MarketOtpRequestResponseDto,
  MarketOtpVerifyResponseDto,
  MarketSubmitResponseDto,
  MemberUninstallResponseDto,
  ModelDiagnosticDto,
  TemplateDeleteResponseDto,
  TemplateDetailDto,
  TemplateDetailResponseDto,
  TemplateDto,
  TemplateListResponseDto,
  TemplateMutationResponseDto,
  TemplatePublishResponseDto,
  TemplateRevisionDetailResponseDto,
  TemplateRevisionDto,
  TemplateValidateResponseDto,
} from './contracts'

/** 当前用户模板列表(全部状态, 最新在前)。 */
export async function listMyTemplates(): Promise<TemplateDto[]> {
  const res = await request<TemplateListResponseDto>('/model-templates')
  return res.templates
}

/** 模板详情(草稿内容 + 聚合诊断)。 */
export async function getTemplateDetail(templateId: string): Promise<TemplateDetailDto> {
  return request<TemplateDetailResponseDto>(`/model-templates/${encodeURIComponent(templateId)}`)
}

/** 创建模板草稿(模板 ID = YAML 的 device.id)。 */
export async function createTemplate(
  slug: string,
  modelYaml: string,
  description: string | null,
): Promise<TemplateDto> {
  const res = await request<TemplateMutationResponseDto>('/model-templates', {
    method: 'POST',
    body: { slug, model_yaml: modelYaml, description },
  })
  return res.template
}

/** 保存模板草稿(完整替换; expected_revision 乐观锁)。 */
export async function saveTemplateDraft(
  templateId: string,
  modelYaml: string,
  expectedRevision: number,
  description: string | null,
): Promise<TemplateDto> {
  const res = await request<TemplateMutationResponseDto>(
    `/model-templates/${encodeURIComponent(templateId)}`,
    {
      method: 'PUT',
      body: { model_yaml: modelYaml, expected_revision: expectedRevision, description },
    },
  )
  return res.template
}

/** 模板完整校验(不落盘): 返回 {valid, diagnostics}。 */
export async function validateTemplateYaml(
  modelYaml: string,
): Promise<{ valid: boolean; diagnostics: ModelDiagnosticDto[] }> {
  return request<TemplateValidateResponseDto>(
    '/model-templates/validate',
    {
      method: 'POST',
      body: { model_yaml: modelYaml },
    },
  )
}

/** 发布模板草稿为不可变 revision(相同内容幂等; 幂等键重放返回同一结果)。 */
export async function publishTemplate(
  templateId: string,
  expectedRevision: number,
  idempotencyKey: string,
): Promise<{ revision: TemplateRevisionDto; duplicate: boolean }> {
  return request<TemplatePublishResponseDto>(
    `/model-templates/${encodeURIComponent(templateId)}/publish`,
    {
      method: 'POST',
      body: { expected_revision: expectedRevision, idempotency_key: idempotencyKey },
    },
  )
}

/** 停用 / 重新启用模板(只影响后续选择)。 */
export async function setTemplateEnabled(templateId: string, enabled: boolean): Promise<TemplateDto> {
  const res = await request<TemplateMutationResponseDto>(
    `/model-templates/${encodeURIComponent(templateId)}/${enabled ? 'enable' : 'disable'}`,
    { method: 'POST' },
  )
  return res.template
}

/** 删除尚未发布的草稿。 */
export async function deleteTemplate(templateId: string): Promise<void> {
  await request<TemplateDeleteResponseDto>(
    `/model-templates/${encodeURIComponent(templateId)}`,
    { method: 'DELETE' },
  )
}

/** 当前唯一发布版详情(规范 YAML + 回执 + 摘要)。 */
export async function getPublishedTemplate(
  templateId: string,
): Promise<TemplateRevisionDetailResponseDto> {
  return request<TemplateRevisionDetailResponseDto>(
    `/model-templates/${encodeURIComponent(templateId)}/published`,
  )
}

/** 400 校验失败: 抛携带诊断的错误(输入必须保留)。 */
export class TemplateSaveError extends Error {
  readonly diagnostics: ModelDiagnosticDto[]

  constructor(diagnostics: ModelDiagnosticDto[], message = '模板校验失败') {
    super(message)
    this.name = 'TemplateSaveError'
    this.diagnostics = diagnostics
  }
}

/** 从 ApiError 信封 params.diagnostics 提取诊断列表。 */
export function diagnosticsFromApiError(err: unknown): ModelDiagnosticDto[] {
  if (err instanceof ApiError) {
    const list = err.params?.diagnostics
    if (Array.isArray(list)) {
      return list as ModelDiagnosticDto[]
    }
  }
  return []
}

// ---------------------------------------------------------------------------
// 开放生态与 pIES-Market API(/api/ecosystem)
// ---------------------------------------------------------------------------

/** 读取实例级正交生态设置。 */
export async function getEcosystemSettings(): Promise<EcosystemSettingsDto> {
  return request<EcosystemSettingsDto>('/ecosystem/settings')
}

/** 管理员更新正交生态开关与市场/依赖源地址。 */
export async function updateEcosystemSettings(
  body: EcosystemSettingsUpdateRequestDto,
): Promise<EcosystemSettingsDto> {
  return request<EcosystemSettingsDto>('/ecosystem/settings', {
    method: 'PUT',
    body,
  })
}

/** 列出本地生态目录条目（含成员安装态）。 */
export async function listEcosystemArtifacts(params?: {
  kind?: ArtifactKindDto
  approval_status?: string
}): Promise<LocalArtifactDto[]> {
  const res = await request<ArtifactListResponseDto>('/ecosystem/artifacts', {
    query: params,
  })
  return res.items
}

/** 导入或上传本地 ies.device-model@1.0.0 YAML。 */
export async function importDeviceModel(
  body: DeviceModelImportRequestDto,
): Promise<LocalArtifactDto> {
  return request<LocalArtifactDto>('/ecosystem/device-models/import', {
    method: 'POST',
    body,
  })
}

/** 导出纯技术设备模型 YAML。 */
export async function exportDeviceModel(
  artifactId: number,
): Promise<DeviceModelExportResponseDto> {
  return request<DeviceModelExportResponseDto>(`/ecosystem/device-models/${artifactId}/export`)
}

/** 上传算法插件 ZIP。 */
export async function uploadPluginZip(input: {
  file: File
  expected_sha256?: string | null
  source_origin?: 'local_upload' | 'offline_import'
}): Promise<LocalArtifactDto> {
  const formData = new FormData()
  formData.append('file', input.file)
  if (input.expected_sha256?.trim()) {
    formData.append('expected_sha256', input.expected_sha256.trim())
  }
  formData.append('source_origin', input.source_origin ?? 'local_upload')
  return request<LocalArtifactDto>('/ecosystem/plugins/upload', {
    method: 'POST',
    formData,
    timeoutMs: 120_000,
  })
}

/** 管理员审批通过生态制品（插件将触发构建自检）。 */
export async function approveEcosystemArtifact(artifactId: number): Promise<LocalArtifactDto> {
  return request<LocalArtifactDto>(`/ecosystem/artifacts/${artifactId}/approve`, {
    method: 'POST',
    timeoutMs: 120_000,
  })
}

/** 管理员拒绝生态制品。 */
export async function rejectEcosystemArtifact(
  artifactId: number,
  reason = '',
): Promise<LocalArtifactDto> {
  return request<LocalArtifactDto>(`/ecosystem/artifacts/${artifactId}/reject`, {
    method: 'POST',
    body: { reason },
  })
}

/** 当前成员安装并启用本地 ready 制品。 */
export async function installEcosystemArtifact(artifactId: number): Promise<LocalArtifactDto> {
  return request<LocalArtifactDto>(`/ecosystem/artifacts/${artifactId}/install`, {
    method: 'POST',
  })
}

/** 当前成员启用/停用已安装制品。 */
export async function setEcosystemArtifactEnabled(
  artifactId: number,
  enabled: boolean,
): Promise<LocalArtifactDto> {
  return request<LocalArtifactDto>(
    `/ecosystem/artifacts/${artifactId}/${enabled ? 'enable' : 'disable'}`,
    { method: 'POST' },
  )
}

/** 当前成员卸载个人安装记录。 */
export async function uninstallMemberArtifact(
  artifactId: number,
): Promise<MemberUninstallResponseDto> {
  return request<MemberUninstallResponseDto>(`/ecosystem/artifacts/${artifactId}/uninstall`, {
    method: 'POST',
  })
}

/** 管理员停用下架并清理插件镜像（若存在在途 CalcSnapshot 返回 409 ECO-UNINSTALL-IN-USE）。 */
export async function adminUninstallArtifactImage(
  artifactId: number,
  force = false,
): Promise<AdminUninstallImageResponseDto> {
  return request<AdminUninstallImageResponseDto>(
    `/ecosystem/admin/artifacts/${artifactId}/uninstall-image`,
    {
      method: 'POST',
      body: { force },
    },
  )
}

/** 读取联网远端市场索引（local 模式返回 403 ECO-MARKET-LOCAL-MODE）。 */
export async function getMarketIndex(): Promise<MarketIndexResponseDto> {
  return request<MarketIndexResponseDto>('/ecosystem/market/index')
}

/** 从联网远端市场强校验下载并安装启用制品。 */
export async function installMarketArtifact(
  body: MarketInstallRequestDto,
): Promise<MarketInstallResponseDto> {
  return request<MarketInstallResponseDto>('/ecosystem/market/install', {
    method: 'POST',
    body,
    timeoutMs: 120_000,
  })
}

/** 投稿网关第 1 步：请求邮箱一次性验证码（无状态挑战凭证）。 */
export async function requestMarketSubmitOtp(
  email: string,
): Promise<MarketOtpRequestResponseDto> {
  return request<MarketOtpRequestResponseDto>('/ecosystem/market/submit/otp-request', {
    method: 'POST',
    body: { email },
  })
}

/** 投稿网关第 2 步：校验验证码换取短期无状态投稿凭证。 */
export async function verifyMarketSubmitOtp(input: {
  email: string
  otp_code: string
  challenge_token: string
}): Promise<MarketOtpVerifyResponseDto> {
  return request<MarketOtpVerifyResponseDto>('/ecosystem/market/submit/otp-verify', {
    method: 'POST',
    body: input,
  })
}

/** 投稿网关第 3 步：上传制品并发起公共市场 PR。 */
export async function submitMarketArtifact(input: {
  submission_token: string
  kind: ArtifactKindDto
  artifact_id: string
  version_or_revision: string
  publisher: string
  license: string
  description: string
  readme_markdown: string
  file: File
}): Promise<MarketSubmitResponseDto> {
  const formData = new FormData()
  formData.append('submission_token', input.submission_token)
  formData.append('kind', input.kind)
  formData.append('artifact_id', input.artifact_id)
  formData.append('version_or_revision', input.version_or_revision)
  formData.append('publisher', input.publisher)
  formData.append('license', input.license)
  formData.append('description', input.description)
  formData.append('readme_markdown', input.readme_markdown)
  formData.append('file', input.file)
  return request<MarketSubmitResponseDto>('/ecosystem/market/submit', {
    method: 'POST',
    formData,
    timeoutMs: 120_000,
  })
}
