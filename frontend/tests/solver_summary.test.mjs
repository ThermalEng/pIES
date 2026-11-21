import assert from 'node:assert/strict'
import { mapResultViewDtoToModel } from '../src/features/results/mappers.ts'
import { messagesEn } from '../src/i18n/messages_en.ts'
import { messagesZh } from '../src/i18n/messages_zh.ts'

const dto = {
  evidence_status: 'available', evidence: { id: 7 }, metrics_summary: null,
  financial_evaluation: null, financial_absence: 'outcome_not_normal',
  candidates: null, planning_solution: null, snapshot_refs: null,
}
for (const summary of [
  null,
  { termination_reason: 'optimal', math_status: 'solved', has_feasible_solution: true,
    objective_value: 10, objective_unit: 'CNY', best_bound: 10, relative_gap: 0 },
  { termination_reason: 'time_limit', math_status: 'feasible', has_feasible_solution: true,
    objective_value: 10, objective_unit: 'CNY', best_bound: null, relative_gap: null },
  { termination_reason: 'time_limit', math_status: 'not_solved', has_feasible_solution: false,
    objective_value: null, objective_unit: 'CNY', best_bound: 8, relative_gap: null },
  { termination_reason: 'optimal', math_status: 'numeric_failure', has_feasible_solution: false,
    objective_value: null, objective_unit: 'CNY', best_bound: 8, relative_gap: null },
]) {
  const result = mapResultViewDtoToModel({ ...dto, solver_summary: summary })
  assert.equal(result.solverSummary, summary)
  assert.equal(result.financial, null)
}
for (const status of ['solved', 'feasible', 'infeasible', 'unbounded', 'numeric_failure', 'not_solved']) {
  assert.ok(messagesZh[`ies.result.math_${status}`], `missing zh key for ${status}`)
  assert.ok(messagesEn[`ies.result.math_${status}`], `missing en key for ${status}`)
}
console.log('Sealed solver summary: states, unknown values and absence preserved without inference')
