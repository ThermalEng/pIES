/**
 * useCandidateSave: 候选校验/保存单用例(编辑中/临时已上传/校验中/校验失败/正式已保存)。
 *
 * 与 frontend.md「新建并保存项目模型」一致:
 * - 提交时获取项目草稿修订(乐观锁)并生成幂等键, 再调用候选保存;
 * - 校验失败: 保持输入, 展示聚合诊断, 不显示保存成功;
 * - 校验成功: 以后端返回的最终 _N ID、规范 YAML、内容摘要、项目 revision
 *   替换编辑状态(前端不预分配编号);
 * - 传输/服务器错误与校验失败区分(不把 500 解释为"校验不过")。
 */

import { useCallback, useMemo, useState } from 'react'

import { api } from '../../../api/client'
import { saveCandidate, uploadTempDataFile } from '../api'
import type { CandidateModel, DataFileRef, ModelDiagnostic, ModelSavePhase, SavedModelInfo } from '../model'
import { CandidateSaveError } from '../model'

export interface CandidateSaveController {
  phase: ModelSavePhase
  diagnostics: ModelDiagnostic[]
  saved: SavedModelInfo | null
  /** 传输/服务器错误(与校验失败区分; 由组件 translateError 渲染)。 */
  lastError: Error | null
  /** 提交候选(校验+保存)。返回是否进入 saved。 */
  submit: (candidate: CandidateModel) => Promise<boolean>
  /** 数据文件上传完成 → 编辑中 → 临时已上传。 */
  markUploaded: () => void
  /** 校验失败后再次编辑 → 回到编辑中(保留输入)。 */
  backToEditing: () => void
  reset: () => void
  /** 上传临时数据文件(临时隔离区; 上传完成 ≠ 模型已保存)。 */
  uploadTempFile: (file: File, dataRef: string) => Promise<DataFileRef>
}

/** 幂等键: 优先 crypto.randomUUID; 非安全上下文(http 非 localhost)回退时间戳+随机段。 */
function newIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `fe-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

export function useCandidateSave(projectId: number): CandidateSaveController {
  const [phase, setPhase] = useState<ModelSavePhase>('editing')
  const [diagnostics, setDiagnostics] = useState<ModelDiagnostic[]>([])
  const [saved, setSaved] = useState<SavedModelInfo | null>(null)
  const [lastError, setLastError] = useState<Error | null>(null)

  /** 读取项目草稿修订(乐观锁; 失败时用 1 兜底会掩盖冲突, 因此直接抛出)。 */
  const resolveProjectRevision = useCallback(async (): Promise<number> => {
    const project = await api.projects.get(projectId)
    const revision = project.draft?.revision
    if (typeof revision !== 'number' || revision < 1) {
      throw new Error(`项目草稿修订不可用: ${String(revision)}`)
    }
    return revision
  }, [projectId])

  const submit = useCallback(
    async (candidate: CandidateModel): Promise<boolean> => {
      setPhase('validating')
      setDiagnostics([])
      setLastError(null)
      try {
        const revision = await resolveProjectRevision()
        const info = await saveCandidate(projectId, {
          ...candidate,
          project_revision: revision,
          idempotency_key: candidate.idempotency_key || newIdempotencyKey(),
        })
        setSaved(info)
        setPhase('saved')
        return true
      } catch (err) {
        if (err instanceof CandidateSaveError) {
          setDiagnostics(err.diagnostics)
          setPhase('validation_failed')
        } else {
          // 传输/契约/服务器错误: 不伪装成"校验不过", 保持编辑状态并提示重试
          setLastError(err instanceof Error ? err : new Error(String(err)))
          setPhase((prev) => (prev === 'validating' ? 'editing' : prev))
        }
        return false
      }
    },
    [projectId, resolveProjectRevision],
  )

  const markUploaded = useCallback(() => {
    setPhase((prev) => (prev === 'editing' ? 'temporary_uploaded' : prev))
  }, [])

  const backToEditing = useCallback(() => {
    setPhase((prev) => (prev === 'validation_failed' || prev === 'temporary_uploaded' ? 'editing' : prev))
  }, [])

  const reset = useCallback(() => {
    setPhase('editing')
    setDiagnostics([])
    setSaved(null)
    setLastError(null)
  }, [])

  const uploadTempFile = useCallback(
    async (file: File, dataRef: string) => {
      const result = await uploadTempDataFile(projectId, file, dataRef)
      return {
        data_ref: dataRef,
        upload_id: result.upload_id,
        object_id: result.temp_file.object_id,
        sha256: result.temp_file.sha256,
      }
    },
    [projectId],
  )

  return useMemo(
    () => ({ phase, diagnostics, saved, lastError, submit, markUploaded, backToEditing, reset, uploadTempFile }),
    [phase, diagnostics, saved, lastError, submit, markUploaded, backToEditing, reset, uploadTempFile],
  )
}
