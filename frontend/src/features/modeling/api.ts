/**
 * 建模 feature API 客户端(与真实后端契约一致, 切片 dm2 联调完成)。
 *
 * 端点清单(prefix /api):
 *   GET  /api/model-templates/catalog                    可用模板目录 → {items: [...]}
 *   GET  /api/model-templates/{template_id}              模板详情 → {template, document, diagnostics}
 *   GET  /api/projects/{pid}/models                      项目模型清单 → {project_models: [...]}
 *   POST /api/projects/{pid}/models/validate             候选模型门禁 → {valid, diagnostics}
 *   POST /api/projects/{pid}/models/temp-files           临时数据文件上传(multipart)
 *        → {temp_file: {object_id, oid, sha256, ...}, upload_id}
 *   POST /api/projects/{pid}/models                      正式保存:
 *        source=template: {template_id, template_revision, template_sha256, template_inputs}
 *        source=yaml:     {model_yaml}
 *        成功 201 → {project_model, receipt, project_revision}; 失败 400 → 标准
 *        错误信封, params.diagnostics 为聚合诊断(message_key/字段路径/YAML 行列/
 *        expected/actual)。
 *
 * 本层只做路径拼接、信封解包与严格解析; 不组织跨端点工作流
 * (项目修订获取、幂等键生成等编排在 hooks/ 完成)。
 */

import { request } from '../../api/client'
import { ApiError } from '../../types'
import {
  buildCandidateRequest,
  diagnosticsFromError,
  diagFromServer,
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

/** 模板详情(供表单生成: 递归解析 document.inputs 树)。 */
export async function getTemplate(templateId: string, locale: string): Promise<TemplateDetail> {
  const body = await request<unknown>(`/model-templates/${encodeURIComponent(templateId)}`)
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

/** 临时数据文件上传(临时隔离区; 上传完成 ≠ 模型已保存)。 */
export async function uploadTempDataFile(
  projectId: number,
  file: File,
  dataRef: string,
): Promise<{ temp_file: { object_id: string; sha256: string; size_bytes: number; oid: string }; upload_id: string }> {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('data_ref', dataRef)
  const body = await request<unknown>(`/projects/${projectId}/models/temp-files`, {
    method: 'POST',
    formData,
    timeoutMs: 0,
  })
  const rec =
    body !== null && typeof body === 'object' && !Array.isArray(body) ? (body as Record<string, unknown>) : null
  const tf = rec?.temp_file as Record<string, unknown> | undefined
  if (
    !rec ||
    typeof rec.upload_id !== 'string' ||
    !tf ||
    typeof tf.object_id !== 'string' ||
    typeof tf.sha256 !== 'string'
  ) {
    throw new MapperError('POST /models/temp-files 响应缺少 temp_file/upload_id')
  }
  return {
    temp_file: {
      object_id: tf.object_id as string,
      oid: typeof tf.oid === 'string' ? (tf.oid as string) : '',
      sha256: tf.sha256 as string,
      size_bytes: typeof tf.size_bytes === 'number' ? (tf.size_bytes as number) : 0,
    },
    upload_id: rec.upload_id as string,
  }
}
