/** Assembly binding GUI transport: only the existing project public draft contract. */
import { request } from '../../shared/http'
import { putDraftCommands } from '../projects'
import type { ProjectViewResponseDto, DraftContentDeviceDto, CostBindingDto, TariffBindingDto, DemandBindingDto } from '../projects'

export interface BindingDraft {
  revision: number
  devices: DraftContentDeviceDto[]
  instances: Record<string, CostBindingDto>
  tariffs: Record<string, TariffBindingDto>
  demands: Record<string, DemandBindingDto>
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
    demands: content.demand_bindings ?? {},
  }
}

/** All three whole-section commands share one optimistic-lock draft transaction. */
export function saveBindings(
  projectId: number,
  revision: number,
  instances: Record<string, CostBindingDto>,
  tariffs: Record<string, TariffBindingDto>,
  demands: Record<string, DemandBindingDto>,
): Promise<{ revision: number }> {
  return putDraftCommands(projectId, [
    { type: 'assembly.set_finance_binding', payload: { instances } },
    { type: 'assembly.set_tariff_bindings', payload: { bindings: tariffs } },
    { type: 'assembly.set_demand_bindings', payload: { bindings: demands } },
  ], revision)
}
