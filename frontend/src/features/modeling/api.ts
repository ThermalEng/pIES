/**
 * 建模 feature API 客户端(与真实后端契约一致, 切片 dm2 联调完成)。
 *
 * 端点清单(prefix /api):
 *   GET  /api/model-templates/catalog                    可用模板目录 → {items: [...]}
 *   GET  /api/model-templates/{template_id}/revisions/{revision}
 *                                                        固定发布版详情
 *   GET  /api/projects/{pid}/models                      项目模型清单 → {project_models: [...]}
 *   POST /api/projects/{pid}/models/validate             候选模型门禁 → {valid, diagnostics}
 *   POST /api/projects/{pid}/models                      正式保存:
 *        source=template: {template_id, template_revision, template_inputs}
 *        source=yaml:     {model_yaml}
 *        成功 201 → {project_model, receipt, project_revision}; 失败 400 → 标准
 *        错误信封, params.diagnostics 为聚合诊断(message_key/字段路径/YAML 行列/
 *        expected/actual)。
 *
 * 本层只做路径拼接、信封解包与严格解析; 不组织跨端点工作流
 * (项目修订获取、幂等键生成等编排在 hooks/ 完成)。
 */

import { putDraftCommands } from '../projects'
import type { ProjectViewResponseDto } from '../projects'
import { runValidation } from '../validation'
import { request } from '../../shared/http'
import { ApiError } from '../../types'
import type { ConnectionInput, DeviceInput, DeviceTypeSpec, DraftModelSection, ValidationResult } from '../../types'
import type { DeviceTypeListResponseDto } from './contracts'
import {
  buildCandidateRequest,
  connectionName,
  deviceTypeListFromServer,
  diagnosticsFromError,
  diagFromServer,
  modelSectionFromView,
  projectModelFromServer,
  savedModelFromServer,
  templateDetailFromServer,
  templateSummaryFromServer,
} from './mappers'
import type { CandidateModel, ModelDiagnostic, ProjectModelSummary, SavedModelInfo, TemplateDetail, TemplateSummary } from './model'
import { CandidateSaveError, MapperError } from './model'

/** 可用模板目录(已发布且启用; 列表信封 {items: [...]} 严格解析)。 */
export async function listTemplates(locale: string): Promise<TemplateSummary[]> {
  const body = await request<unknown>('/model-templates/catalog')
  const rec =
    body !== null && typeof body === 'object' && !Array.isArray(body) ? (body as Record<string, unknown>) : null
  if (!rec || !Array.isArray(rec.items)) {
    throw new MapperError('GET /model-templates/catalog 响应缺少 items')
  }
  return rec.items.map((item) => templateSummaryFromServer(item, locale))
}

/**
 * 精确发布版模板详情(供项目模型表单生成)。
 *
 * 项目建模必须读取目录选中时的不可变 revision，不能读取随后可能继续编辑的
 * 管理草稿，否则表单 inputs 与保存请求携带的 revision/hash 会来自不同内容。
 */
export async function getTemplate(
  templateId: string,
  revision: number,
  locale: string,
): Promise<TemplateDetail> {
  const body = await request<unknown>(
    `/model-templates/${encodeURIComponent(templateId)}/revisions/${revision}`,
  )
  return templateDetailFromServer(body, locale)
}

/** 项目模型清单(最新在前; 编号对用户可见)。 */
export async function listProjectModels(projectId: number): Promise<ProjectModelSummary[]> {
  const body = await request<unknown>(`/projects/${projectId}/models`)
  const rec =
    body !== null && typeof body === 'object' && !Array.isArray(body) ? (body as Record<string, unknown>) : null
  if (!rec || !Array.isArray(rec.project_models)) {
    throw new MapperError('GET /projects/{pid}/models 响应缺少 project_models')
  }
  return rec.project_models.map((item) => projectModelFromServer(item))
}

/**
 * 候选校验(不保存): 返回 {valid, diagnostics}。
 * 校验失败在 validate 端点以 200 + {valid: false, diagnostics} 表达;
 * 传输/服务器错误原样抛出 ApiError。
 */
export async function validateCandidate(
  projectId: number,
  candidate: CandidateModel,
): Promise<{ valid: boolean; diagnostics: ModelDiagnostic[] }> {
  const body = await request<unknown>(`/projects/${projectId}/models/validate`, {
    method: 'POST',
    body: buildCandidateRequest(candidate),
  })
  const rec =
    body !== null && typeof body === 'object' && !Array.isArray(body) ? (body as Record<string, unknown>) : null
  if (!rec || typeof rec.valid !== 'boolean' || !Array.isArray(rec.diagnostics)) {
    throw new MapperError('POST /models/validate 响应缺少 valid/diagnostics')
  }
  const diagnostics: ModelDiagnostic[] = []
  for (const item of rec.diagnostics as unknown[]) {
    const parsed = diagFromServer(item)
    if (parsed === null) throw new MapperError('诊断条目形状与契约不一致')
    diagnostics.push(parsed)
  }
  return { valid: rec.valid as boolean, diagnostics }
}

/**
 * 候选保存(校验 + 保存单用例)。
 * - 成功: 返回后端权威结果(最终 _N ID / 规范 YAML / 摘要 / 项目 revision);
 * - 400 校验失败: 抛 CandidateSaveError(携带聚合诊断, 输入必须保留);
 * - 其他错误: 原样抛出 ApiError。
 */
export async function saveCandidate(projectId: number, candidate: CandidateModel): Promise<SavedModelInfo> {
  try {
    const body = await request<unknown>(`/projects/${projectId}/models`, {
      method: 'POST',
      body: buildCandidateRequest(candidate),
    })
    return savedModelFromServer(body)
  } catch (err) {
    if (err instanceof ApiError) {
      const diagnostics: ModelDiagnostic[] = diagnosticsFromError(err)
      if (diagnostics.length > 0) {
        throw new CandidateSaveError(diagnostics)
      }
    }
    throw err
  }
}

// ---------------------------------------------------------------------------
// 草稿模型写(经 projects 唯一公开草稿传输; 各自准备业务 payload)
// ---------------------------------------------------------------------------

/** 读项目视图草稿内容 model 节(2.0 名键设备与端点连接, 附带草稿修订号)。 */
export async function getModel(projectId: number): Promise<{ revision: number; model: DraftModelSection }> {
  const body = await request<ProjectViewResponseDto>(`/projects/${projectId}`)
  return modelSectionFromView(body)
}

/** 发送模型语义命令(修订号由调用方已加载值传入, 写前不 GET; 成功以后端返回为准)。 */
export async function sendCommands(
  projectId: number,
  commands: Array<{ type: string; payload: Record<string, unknown> }>,
  expectedRevision: number,
): Promise<{ revision: number }> {
  return putDraftCommands(projectId, commands, expectedRevision)
}

/** 新增/更新设备实例(model.upsert_device)。 */
export async function upsertDevice(
  projectId: number,
  input: DeviceInput,
  expectedRevision: number,
): Promise<{ revision: number }> {
  return sendCommands(
    projectId,
    [
      {
        type: 'model.upsert_device',
        payload: {
          name: input.name,
          device_type: input.device_type,
          kind: input.kind,
          ...(input.params !== undefined ? { params: input.params } : {}),
        },
      },
    ],
    expectedRevision,
  )
}

/** 删除设备实例(model.remove_device)。 */
export async function removeDevice(projectId: number, name: string, expectedRevision: number): Promise<{ revision: number }> {
  return sendCommands(projectId, [{ type: 'model.remove_device', payload: { name } }], expectedRevision)
}

/** 新增/更新连接(model.upsert_connection; 缺省名与后端公式一致)。 */
export async function upsertConnection(
  projectId: number,
  input: ConnectionInput,
  expectedRevision: number,
): Promise<{ revision: number; name: string }> {
  const name =
    input.name ?? connectionName(input.from_device, input.from_port, input.to_device, input.to_port)
  const { revision } = await sendCommands(
    projectId,
    [
      {
        type: 'model.upsert_connection',
        payload: {
          name,
          from_device: input.from_device,
          from_port: input.from_port,
          to_device: input.to_device,
          to_port: input.to_port,
        },
      },
    ],
    expectedRevision,
  )
  return { revision, name }
}

/** 删除连接(model.remove_connection)。 */
export async function removeConnection(projectId: number, name: string, expectedRevision: number): Promise<{ revision: number }> {
  return sendCommands(projectId, [{ type: 'model.remove_connection', payload: { name } }], expectedRevision)
}

/** 运行项目模型校验(复用 validation feature 公开能力)。 */
export async function validateModel(projectId: number): Promise<ValidationResult> {
  return runValidation(projectId)
}

/** 设备类型注册表(公开, 供画布; 精确 {items} 信封)。 */
export async function listDeviceTypes(): Promise<DeviceTypeSpec[]> {
  const body = await request<DeviceTypeListResponseDto>('/registry/device-types')
  return deviceTypeListFromServer(body)
}
