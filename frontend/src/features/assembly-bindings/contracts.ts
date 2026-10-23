/**
 * Existing ies.assembly cost/tariff binding sections as stored in project draft.
 * Costs belong to finance_binding.instances; energy purchases/sales are separate
 * tariff_bindings. No inferred device category or business parameter defaults.
 */
export type DriverTargetDto =
  | { property: string }
  | { interface: string; aggregation: 'integrate_positive' | 'sum_positive' }

export type CostBindingDto =
  | { type: 'none' }
  | { type: 'costed'; finance_type: string; drivers: Record<string, DriverTargetDto> }

export interface TariffBindingDto {
  price: string
  instance: string
  interface: string
  aggregation: 'integrate_positive' | 'sum_positive'
}
