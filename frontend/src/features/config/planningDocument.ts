/**
 * Planning YAML is an edit view of the existing PlanningConfig JSON contract,
 * not a second resource or new public file protocol. Never convert decimal
 * bounds through JavaScript Number when reading plain YAML scalars.
 */
import { Document, isMap, isScalar, Pair, parseDocument, stringify, YAMLMap } from 'yaml'
import type { PlanningConfigBody, PlanningConstraintDto, PlanningVariableDto } from './contracts'

export const EMPTY_PLANNING: PlanningConfigBody = {
  objective: { sense: 'minimize', expression: '' },
  variables: {},
  constraints: {},
}

export function planningToYaml(body: PlanningConfigBody): string {
  return stringify(body, { lineWidth: 0 })
}

/** Editing rows may have empty/duplicate names; do not collapse them into objects. */
export function planningDraftToYaml(
  body: PlanningConfigBody,
  variables: ReadonlyArray<readonly [string, PlanningVariableDto]>,
  constraints: ReadonlyArray<readonly [string, PlanningConstraintDto]>,
): string {
  const doc = new Document(body)
  function rows<T>(entries: ReadonlyArray<readonly [string, T]>): YAMLMap {
    const mapping = new YAMLMap(doc.schema)
    for (const [name, value] of entries) {
      mapping.items.push(new Pair(doc.createNode(name), doc.createNode(value)))
    }
    return mapping
  }
  doc.set('variables', rows(variables))
  doc.set('constraints', rows(constraints))
  return doc.toString({ lineWidth: 0 })
}

export function planningFromYaml(source: string): PlanningConfigBody {
  const doc = parseDocument(source, { uniqueKeys: true, strict: true })
  if (doc.errors.length) {
    throw new Error(doc.errors.map((e) => e.message).join('; '))
  }
  if (!isMap(doc.contents)) throw new Error('Planning YAML root must be a mapping')
  const body = doc.toJS({ maxAliasCount: 0 }) as PlanningConfigBody
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    throw new Error('Planning YAML root must be a mapping')
  }

  // YAML numerical scalars are parsed as JS numbers by default. Read the
  // lexical source of each boundary scalar from the AST *instead* so long
  // decimals (and precision beyond 2^53) are passed to Python as strings.
  const variables = doc.get('variables', true)
  if (isMap(variables) && body.variables && typeof body.variables === 'object') {
    for (const pair of variables.items) {
      if (!isScalar(pair.key) || !isMap(pair.value)) continue
      const name = String(pair.key.value)
      const variable = body.variables[name]
      if (!variable || typeof variable !== 'object') continue
      for (const bound of ['lower_bound', 'upper_bound'] as const) {
        const node = pair.value.get(bound, true)
        if (!isScalar(node) || node.value === null) continue
        if (typeof node.value === 'number') {
          if (!node.source) throw new Error(`variables.${name}.${bound}: missing decimal source`)
          variable[bound] = node.source
        }
      }
    }
  }
  return body
}
