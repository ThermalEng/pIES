/**
 * 数据集 feature 请求(传输层实现, 经 shared/http 直调)。
 *
 * 成功正文按 contracts 具名键直接消费后交 mappers 映射; 畸形体自然抛错可见。
 * 绑定/解绑经 features/projects 唯一公开草稿传输发送(dataset.bind/unbind
 * 业务 payload), 不在本家族复制命令组装。
 */

import { ApiError } from '../../types'
import type { Dataset, DatasetVersion, Diagnostic, PageResult } from '../../types'
import type { DatasetUploadInput } from '../../types'
import { request, requestBlob } from '../../shared/http'
import { sendDraftCommand } from '../projects'
import type {
  DatasetCreateResponseDto,
  DatasetDetailResponseDto,
  DatasetsListResponseDto,
  DatasetUploadResponseDto,
} from './contracts'
import { datasetVersionFromServer } from './mappers'

/** 数据集上传明细输入(与旧聚合器 DatasetUploadDetailedInput 同形)。 */
export interface DatasetUploadDetailedInput extends DatasetUploadInput {
  /** 分辨率说明(如 '1h'、'15min'),对齐 dataset_versions.resolution。 */
  resolution?: string
  /** 数据时间戳的固定 UTC 偏移(分钟),对齐 dataset_versions.fixed_utc_offset_minutes。 */
  fixed_utc_offset_minutes?: number
  /** 字段定义(名称 -> 单位/说明)。 */
  fields?: Record<string, { unit?: string | null; description?: string }>
  /** 本版本许可证或授权信息。 */
  license?: string
}

/** 数据集列表(项目 ID 必填; 精确 {datasets: [{dataset, latest_version}]} 信封)。 */
export async function listDatasets(params: {
  project_id: number
  status?: string
  cursor?: string
  limit?: number
}): Promise<PageResult<Dataset>> {
  // 数据集按项目挂载(/api/projects/{pid}/datasets); 项目 ID 必填,
  // 直接调用公开接口, 不做无项目空页伪成功, 不复核 ID。
  // 空数组与 latest_version=null 均为合法(分别表示无数据集 / 尚无版本), 原样保留。
  const projectId = params.project_id
  const body = await request<DatasetsListResponseDto>(`/projects/${projectId}/datasets`)
  const entries = body.datasets
  return {
    items: entries.map((e) => e.dataset),
    next_cursor: null,
    limit: entries.length,
  }
}

/** 数据集模板下载(后端模板失败不得本地回退伪成功, 由调用方显错)。 */
export async function downloadTemplate(): Promise<Blob> {
  return requestBlob('/datasets/template')
}

/**
 * 数据集上传(两步: 创建元数据 → 上传版本)。
 * 后端拆分为「创建数据集元数据」+「上传版本(multipart)」; project_id 缺席即 400。
 */
export async function uploadDataset(
  input: DatasetUploadDetailedInput,
): Promise<DatasetVersion & { diagnostics?: Diagnostic[] }> {
  const projectId = input.project_id
  if (projectId === undefined || projectId === null) {
    throw new ApiError(400, null, 'ies.error.unknown')
  }
  // 1) 创建数据集元数据
  const created = await request<DatasetCreateResponseDto>(`/projects/${projectId}/datasets`, {
    method: 'POST',
    body: {
      name: input.name,
      description: input.description ?? null,
      source_category: 'user_upload',
      license: input.license ?? null,
    },
  })
  const datasetId = created.dataset.id
  // 2) 上传版本(multipart: file/resolution/utc_offset_minutes/fields/meta)
  const formData = new FormData()
  formData.append('file', input.file)
  formData.append('resolution', input.resolution ?? '1h')
  if (input.fixed_utc_offset_minutes !== undefined && input.fixed_utc_offset_minutes !== null) {
    formData.append('utc_offset_minutes', String(input.fixed_utc_offset_minutes))
  }
  if (input.fields && Object.keys(input.fields).length > 0) {
    formData.append(
      'fields',
      JSON.stringify(
        Object.fromEntries(
          Object.entries(input.fields).map(([k, v]) => [k, { unit: v.unit ?? '' }]),
        ),
      ),
    )
  }
  formData.append(
    'meta',
    JSON.stringify({
      source_category: 'user_upload',
      // 空许可证省略(后端 meta 白名单要求 license 为字符串, 显式 null 会阻断)
      ...(input.license ? { license: input.license } : {}),
      created_reason: 'upload',
    }),
  )
  const uploaded = await request<DatasetUploadResponseDto>(
    `/projects/${projectId}/datasets/${datasetId}/versions`,
    {
      method: 'POST',
      formData,
      timeoutMs: 0,
    },
  )
  const version = datasetVersionFromServer(uploaded.dataset_version)
  const diagnostics = uploaded.diagnostics ?? []
  return { ...version, diagnostics }
}

/** 数据集版本列表(精确 {dataset, versions} 信封; 畸形体自然抛错)。 */
export async function listVersions(projectId: number, id: number): Promise<DatasetVersion[]> {
  const body = await request<DatasetDetailResponseDto>(`/projects/${projectId}/datasets/${id}`)
  return body.versions.map(datasetVersionFromServer)
}

/**
 * 最新版本字段名(后端无数据行预览端点: 只返回真实字段名;
 * 无版本为空数组, 不设行/行数载体, 不伪造空行冒充真实空数据集)。
 */
export async function versionFieldNames(projectId: number, id: number): Promise<string[]> {
  const body = await request<DatasetDetailResponseDto>(`/projects/${projectId}/datasets/${id}`)
  const latest = body.versions[0]
  if (latest === undefined) return []
  return Object.keys(latest.fields)
}

/** 绑定数据集版本到项目草稿(dataset.bind 语义命令, 经 projects 传输)。 */
export async function bindVersion(
  projectId: number,
  datasetVersionId: number,
  datasetId: number,
): Promise<{ revision: number }> {
  return sendDraftCommand(projectId, 'dataset.bind', {
    dataset_version_id: datasetVersionId,
    dataset_id: datasetId,
    role: 'annual',
  })
}

/** 解除数据集版本绑定(dataset.unbind 语义命令, 经 projects 传输)。 */
export async function unbindVersion(
  projectId: number,
  datasetVersionId: number,
): Promise<{ revision: number }> {
  return sendDraftCommand(projectId, 'dataset.unbind', { dataset_version_id: datasetVersionId })
}
