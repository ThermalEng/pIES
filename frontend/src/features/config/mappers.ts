/**
 * 计算配置 feature 纯 mapper: 后端配置信封 → 前端 CalcConfig。
 *
 * 纯函数, 不访问网络、缓存或 React 状态, 不导入 HTTP 层。
 * 可信内部交接直接消费具名键: 后端在用户输入入口已唯一校验全文, 此处
 * 不补齐缺项(不伪造 ''/[]/null/0, 尤其 time_limit_seconds/options),
 * 不重复业务校验、不设信封守卫; 缺席即自然抛错可见。缺项原样透出,
 * 由编辑页显式呈现为空态。
 */

import type { CalcConfig } from '../../types'
import type { ConfigEnvelopeDto, DeploymentDefaultsDto } from './contracts'

/** 后端配置信封 {calculation_config, revision} → 前端 CalcConfig。 */
export function configFromServer(body: ConfigEnvelopeDto, projectId: number): CalcConfig {
  return {
    project_id: projectId,
    calculation_config: body.calculation_config,
    revision: body.revision,
  }
}

/** 部署公开初值 → DeploymentDefaultsDto。 */
export function deploymentDefaultsFromServer(body: DeploymentDefaultsDto): DeploymentDefaultsDto {
  return {
    calculation_defaults: {
      time_limit_seconds: body.calculation_defaults.time_limit_seconds,
    },
  }
}

export interface SchemaFieldSpec {
  name: string
  type: 'string' | 'number' | 'integer' | 'boolean'
  title: string
  description: string
  required: boolean
  enumValues: string[] | null
  defaultValue: unknown
}

/** 从插件 JSON Schema (parameters_schema) 提取顶层表单字段规范（仅标量/枚举）。 */
export function extractSchemaFields(schema: Record<string, unknown> | null | undefined): SchemaFieldSpec[] {
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) return []
  const props = schema.properties
  if (!props || typeof props !== 'object' || Array.isArray(props)) return []
  const requiredSet = new Set(
    Array.isArray(schema.required) ? schema.required.filter((x): x is string => typeof x === 'string') : [],
  )
  const fields: SchemaFieldSpec[] = []
  for (const [name, rawSpec] of Object.entries(props as Record<string, unknown>)) {
    if (!rawSpec || typeof rawSpec !== 'object' || Array.isArray(rawSpec)) continue
    const spec = rawSpec as Record<string, unknown>
    const rawType = spec.type
    const type: SchemaFieldSpec['type'] =
      rawType === 'integer' || rawType === 'number' || rawType === 'boolean' || rawType === 'string'
        ? rawType
        : 'string'
    const enumValues = Array.isArray(spec.enum) ? spec.enum.map((v) => String(v)) : null
    fields.push({
      name,
      type,
      title: typeof spec.title === 'string' && spec.title ? spec.title : name,
      description: typeof spec.description === 'string' ? spec.description : '',
      required: requiredSet.has(name),
      enumValues,
      defaultValue: spec.default,
    })
  }
  return fields
}

/** 按 Schema 字段类型将表单输入转换为 JSON 参数值；空值返回 undefined（从参数对象移除）。 */
export function coerceSchemaFieldValue(
  spec: SchemaFieldSpec,
  raw: string | boolean,
): unknown {
  if (spec.type === 'boolean') return Boolean(raw)
  const text = String(raw).trim()
  if (text === '') return undefined
  if (spec.type === 'integer') {
    const n = Number(text)
    return Number.isInteger(n) ? n : text
  }
  if (spec.type === 'number') {
    const n = Number(text)
    return Number.isFinite(n) ? n : text
  }
  return String(raw)
}

export interface PredictionEntry {
  target: {
    instance: string
    interface: string
  }
  algorithm: string
  parameters: Record<string, unknown>
}

/** 按 CalculationConfig 1.0.0 契约向 predictions[] 追加一项（target 不可重复）。 */
export function addPredictionEntry(
  existing: PredictionEntry[],
  input: { instance: string; interfaceName: string; algorithm: string },
): PredictionEntry[] {
  const instance = input.instance.trim()
  const iface = input.interfaceName.trim()
  const algorithm = input.algorithm.trim()
  if (!instance || !iface || !algorithm) return existing
  if (existing.some((item) => item?.target?.instance === instance && item?.target?.interface === iface)) {
    return existing
  }
  return [
    ...existing,
    {
      target: { instance, interface: iface },
      algorithm,
      parameters: {},
    },
  ]
}
