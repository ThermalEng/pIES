/**
 * CandidateStatusBar: 保存状态指示(编辑中 / 临时已上传 / 校验中 / 校验失败 / 正式已保存)。
 *
 * 校验失败不显示保存成功; 只有正式已保存才提示"已进入项目模型列表, 可进入装配"。
 */

import { pt } from '../../../i18n/pageMessages'
import { Badge } from '../../../components/ui'
import type { ModelSavePhase } from '../model'

const PHASE_LABEL_KEYS: Record<ModelSavePhase, string> = {
  editing: 'ies.modeling.save.phase_editing',
  temporary_uploaded: 'ies.modeling.save.phase_uploaded',
  validating: 'ies.modeling.save.phase_validating',
  validation_failed: 'ies.modeling.save.phase_failed',
  saved: 'ies.modeling.save.phase_saved',
}

const PHASE_DESC_KEYS: Record<ModelSavePhase, string> = {
  editing: 'ies.modeling.save.phase_editing_desc',
  temporary_uploaded: 'ies.modeling.save.phase_uploaded_desc',
  validating: 'ies.modeling.save.phase_validating_desc',
  validation_failed: 'ies.modeling.save.phase_failed_desc',
  saved: 'ies.modeling.save.phase_saved_desc',
}

const PHASE_VARIANTS: Record<ModelSavePhase, 'neutral' | 'warning' | 'info' | 'danger' | 'success'> = {
  editing: 'neutral',
  temporary_uploaded: 'warning',
  validating: 'info',
  validation_failed: 'danger',
  saved: 'success',
}

export function CandidateStatusBar({ phase }: { phase: ModelSavePhase }) {
  return (
    <div className="ies-modeling__status-bar" role="status" aria-label={pt(PHASE_LABEL_KEYS[phase])}>
      <Badge variant={PHASE_VARIANTS[phase]} label={pt(PHASE_LABEL_KEYS[phase])} />
      <span className="ies-modeling__status-desc">{pt(PHASE_DESC_KEYS[phase])}</span>
    </div>
  )
}
