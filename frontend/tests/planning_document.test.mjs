// Planning YAML is an editing view: references and disabled constraints survive
// form overlays; numeric bounds must remain decimal strings, never JS floats.
import { planningFromYaml, planningToYaml } from '../src/features/config/planningDocument.ts'

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
const restored = planningFromYaml(planningToYaml(parsed))
check('YAML round-trip preserves full body', JSON.stringify(restored) === JSON.stringify(parsed))
check('other form fields can change without dropping references', (() => {
  const modified = { ...parsed, objective: { ...parsed.objective, expression: 'hp1__upfront_capex + 0 CNY' } }
  const read = planningFromYaml(planningToYaml(modified))
  return read.constraints.heat_gate.references.heat.interface === 'heat_out' && !read.constraints.disabled_gate.enabled
})())
for (const [name, invalid] of [
  ['broken YAML', source + '\ninvalid: ['],
  ['duplicate keys', source.replace('  heat_gate:', '  scalar:\n    type: ratio\n    expression: 1 <= 2\n  heat_gate:')],
]) {
  let rejected = false
  try { planningFromYaml(invalid) } catch { rejected = true }
  check(name + ' rejected without fallback', rejected)
}
if (failed) process.exitCode = 1
