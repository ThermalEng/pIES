/** Assembly binding GUI transport: only the existing project public draft contract. */
import { request } from '../../shared/http'
import { putDraftCommands } from '../projects'
import type { ProjectViewResponseDto, DraftContentDeviceDto, CostBindingDto, TariffBindingDto } from '../projects'

export interface BindingDraft {
  revision: number
  devices: DraftContentDeviceDto[]
  instances: Record<string, CostBindingDto>
  tariffs: Record<string, TariffBindingDto>
}

/** Reads the actual project draft (no extra cache or separate binding state). */
export async function getBindingDraft(projectId: number): Promise<BindingDraft> {
  const response = await request<ProjectViewResponseDto>('/projects/' + projectId)
  const content = response.draft.content
  return {
    revision: response.draft.revision,
    devices: content.model.devices,
    instances: content.finance_binding?.instances ?? {},
    tariffs: content.tariff_bindings ?? {},
  }
}

/** Both existing whole-section commands in one optimistic-lock draft transaction. */
export function saveBindings(
  projectId: number,
  revision: number,
  instances: Record<string, CostBindingDto>,
  tariffs: Record<string, TariffBindingDto>,
): Promise<{ revision: number }> {
  return putDraftCommands(projectId, [
    { type: 'assembly.set_finance_binding', payload: { instances } },
    { type: 'assembly.set_tariff_bindings', payload: { bindings: tariffs } },
  ], revision)
}
