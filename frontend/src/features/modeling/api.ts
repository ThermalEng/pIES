/**
 * 建模 feature API 客户端(契约草案, 待 C 合并后联调)。
 *
 * 端点与字段按阶段 2 worktree C 的候选校验/保存契约草案编写; C 尚未合并,
 * 联调说明见本目录 README.md「待 C 合并后联调」节。端点清单:
 *
 *   GET  /api/model-templates                    模板列表 → {items: [...]}
 *   GET  /api/model-templates/{template_id}      模板详情 → {template, document}
 *   POST /api/projects/{pid}/model-candidates    候选校验+保存:
 *        source=template: {template_id, inputs}  后端模板实例化后完整校验;
 *        source=yaml:     {content}              后端直接解析校验。
 *        成功 201 → {model, project_revision}; 失败 400 → 标准错误信封,
 *        params.diagnostics 为聚合诊断(message_key/字段路径/YAML 行列/expected/actual)。
 *   POST /api/projects/{pid}/temp-files          临时数据文件上传(multipart)
 *        → {temp_file_ref, file_name}(临时隔离区, 不等于模型已保存)。
 *
 * 本层只做路径拼接、信封解包与严格解析; 不组织跨端点工作流
 * (项目修订获取、幂等键生成等编排在 hooks/ 完成)。
 */

import { request } from '../../api/client'
import { ApiError } from '../../types'
import {
  buildCandidateRequest,
  diagnosticsFromError,
  savedModelFromServer,
  templateDocumentFromServer,
  templateSummaryFromServer,
} from './mappers'
import type { CandidateModel, ModelDiagnostic, SavedModelInfo, TemplateDocument, TemplateSummary } from './model'
import { CandidateSaveError, MapperError } from './model'

/** 模板列表(清单端点 {items: [...]} 严格解析, 形状不符不静默返回空列表)。 */
export async function listTemplates(locale: string): Promise<TemplateSummary[]> {
  const body = await request<unknown>('/model-templates')
  const rec =
    body !== null && typeof body === 'object' && !Array.isArray(body) ? (body as Record<string, unknown>) : null
  if (!rec || !Array.isArray(rec.items)) {
    throw new MapperError('GET /model-templates 响应缺少 items')
  }
  return rec.items.map((item) => templateSummaryFromServer(item, locale))
}

/** 模板详情(供表单生成: 递归解析 document.inputs 树)。 */
export async function getTemplate(templateId: string, locale: string): Promise<TemplateDocument> {
  const body = await request<unknown>(`/model-templates/${encodeURIComponent(templateId)}`)
  return templateDocumentFromServer(body, locale)
}

/**
 * 候选保存(校验 + 保存单用例)。
 * - 成功: 返回后端权威结果(最终 _N ID / 规范 YAML / 摘要 / 项目 revision);
 * - 400 校验失败: 抛 CandidateSaveError(携带聚合诊断, 输入必须保留);
 * - 其他错误: 原样抛出 ApiError。
 */
export async function saveCandidate(projectId: number, candidate: CandidateModel): Promise<SavedModelInfo> {
  try {
    const body = await request<unknown>(`/projects/${projectId}/model-candidates`, {
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
): Promise<{ temp_file_ref: string; file_name: string }> {
  const formData = new FormData()
  formData.append('file', file)
  const body = await request<unknown>(`/projects/${projectId}/temp-files`, {
    method: 'POST',
    formData,
    timeoutMs: 0,
  })
  const rec =
    body !== null && typeof body === 'object' && !Array.isArray(body) ? (body as Record<string, unknown>) : null
  if (!rec || typeof rec.temp_file_ref !== 'string' || typeof rec.file_name !== 'string') {
    throw new MapperError('POST /temp-files 响应缺少 temp_file_ref/file_name')
  }
  return { temp_file_ref: rec.temp_file_ref, file_name: rec.file_name }
}
