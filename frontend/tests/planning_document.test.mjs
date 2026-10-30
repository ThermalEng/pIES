// Planning YAML is an editing view: references and disabled constraints survive
// form overlays; numeric bounds must remain decimal strings, never JS floats.
// The Docker logic runner bundles tests as ESM while yaml's Node entry is
// CommonJS and dynamically requires built-in modules. Provide Node's require
// before the deferred import initializes the bundled yaml dependency.
import { createRequire } from 'node:module'
globalThis.require = createRequire(import.meta.url)
const { planningDraftToYaml, planningFromYaml, planningToYaml } = await import('../src/features/config/planningDocument.ts')
const { parseDocument } = await import('yaml')

let failed = 0
function check(name, ok) {
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}`)
  if (!ok) failed += 1
}
const source = `objective:
  sense: minimize
  expression: hp1__upfront_capex
variables:
  hp_cap:
    device_ref: hp1
    parameter: rated_heat_kw
    lower_bound: 50.000000000000000000000000000001
    upper_bound: "999999999999999999999.000001"
    unit: kW
constraints:
  scalar:
    type: ratio
    expression: 1 <= 2
    enabled: true
  heat_gate:
    type: step_relation
    references:
      heat:
        instance: hp1
        interface: heat_out
    expression: heat[t] <= 0.5 * hp_cap
    enabled: true
  disabled_gate:
    type: step_relation
    references:
      heat:
        instance: hp1
        interface: heat_out
    expression: heat[t] <= 0 kW
    enabled: false
`
const parsed = planningFromYaml(source)
check('unquoted high-precision decimal retained exactly', parsed.variables.hp_cap.lower_bound === '50.000000000000000000000000000001')
check('quoted high-precision decimal retained exactly', parsed.variables.hp_cap.upper_bound === '999999999999999999999.000001')
check('scalar and step coexist', parsed.constraints.scalar.type === 'ratio' && parsed.constraints.heat_gate.type === 'step_relation')
check('real instance/port ref remains distinct from alias', parsed.constraints.heat_gate.references.heat.instance === 'hp1' && parsed.constraints.heat_gate.references.heat.interface === 'heat_out')
check('disabled step stays present', parsed.constraints.disabled_gate.enabled === false)

// Omitted constraints and default enabled semantics
const withoutConstraints = `objective:
  sense: minimize
  expression: hp_cap
variables:
  v1:
    device_ref: hp1
    parameter: rated_heat_kw
`
const parsedWithoutConstraints = planningFromYaml(withoutConstraints)
check('omitted constraints parses without error', parsedWithoutConstraints.constraints === undefined)
check('round-trip without constraints preserved', JSON.stringify(planningFromYaml(planningToYaml(parsedWithoutConstraints))) === JSON.stringify(parsedWithoutConstraints))

const omittedEnabledYaml = `objective:
  sense: minimize
  expression: hp_cap
variables:
  v1:
    device_ref: hp1
    parameter: rated_heat_kw
constraints:
  c_omitted:
    type: ratio
    expression: 1 <= 2
  c_disabled:
    type: ratio
    expression: 1 <= 3
    enabled: false
`
const parsedOmittedEnabled = planningFromYaml(omittedEnabledYaml)
check('omitted enabled parses as undefined', parsedOmittedEnabled.constraints.c_omitted.enabled === undefined)
check('explicit enabled: false parses as false', parsedOmittedEnabled.constraints.c_disabled.enabled === false)

const restored = planningFromYaml(planningToYaml(parsed))
check('YAML round-trip preserves full body', JSON.stringify(restored) === JSON.stringify(parsed))
check('other form fields can change without dropping references', (() => {
  const modified = { ...parsed, objective: { ...parsed.objective, expression: 'hp1__upfront_capex + 0 CNY' } }
  const read = planningFromYaml(planningToYaml(modified))
  return read.constraints.heat_gate.references.heat.interface === 'heat_out' && !read.constraints.disabled_gate.enabled
})())

const variableRows = [
  ['variable_2', { ...parsed.variables.hp_cap }],
  ['', { device_ref: '', parameter: '', lower_bound: null, upper_bound: null, unit: null }],
  ['hp_cap', { ...parsed.variables.hp_cap, lower_bound: '11' }],
  ['hp_cap_5', { ...parsed.variables.hp_cap, lower_bound: '22' }],
  ['hp_cap', { ...parsed.variables.hp_cap, lower_bound: '33' }],
  ['', { ...parsed.variables.hp_cap, lower_bound: '44' }],
]
const constraintRows = [
  ['constraint_2', parsed.constraints.heat_gate],
  ['', { type: '', expression: '', enabled: true }],
  ['gate', parsed.constraints.heat_gate],
  ['gate_5', parsed.constraints.scalar],
  ['gate', parsed.constraints.disabled_gate],
]
const draftText = planningDraftToYaml(parsed, variableRows, constraintRows)
// Inspect rows directly: a JSON object cannot represent duplicate draft names.
const draftAst = parseDocument(draftText, { uniqueKeys: false })
const draftVariables = draftAst.get('variables', true).items
const draftConstraints = draftAst.get('constraints', true).items
check('draft preserves all variable names and row order', JSON.stringify(draftVariables.map(p => p.key.value)) === JSON.stringify(variableRows.map(([name]) => name)))
check('draft retains precise bounds in every variable row', JSON.stringify(draftVariables.map(p => p.value.get('lower_bound'))) === JSON.stringify(variableRows.map(([, value]) => value.lower_bound)))
check('draft preserves all constraint names and row order', JSON.stringify(draftConstraints.map(p => p.key.value)) === JSON.stringify(constraintRows.map(([name]) => name)))
check('draft retains every constraint expression', JSON.stringify(draftConstraints.map(p => p.value.get('expression'))) === JSON.stringify(constraintRows.map(([, value]) => value.expression)))
check('draft retains disabled state and real references', draftConstraints[4].value.get('enabled') === false && draftConstraints[4].value.getIn(['references', 'heat', 'instance']) === 'hp1')
let duplicateDraftRejected = false
try { planningFromYaml(draftText) } catch { duplicateDraftRejected = true }
check('duplicate draft rows remain rejected at save parsing', duplicateDraftRejected)

const completeDraft = planningDraftToYaml(parsed, Object.entries(parsed.variables), Object.entries(parsed.constraints))
check('complete form draft preserves the existing planning body', JSON.stringify(planningFromYaml(completeDraft)) === JSON.stringify(parsed))
for (const [name, invalid] of [
  ['broken YAML', source + '\ninvalid: ['],
  ['duplicate keys', source.replace('  heat_gate:', '  scalar:\n    type: ratio\n    expression: 1 <= 2\n  heat_gate:')],
]) {
  let rejected = false
  try { planningFromYaml(invalid) } catch { rejected = true }
  check(name + ' rejected without fallback', rejected)
}
if (failed) process.exitCode = 1
