/**
 * 建模画布领域逻辑(纯函数,无 React 依赖)。
 *
 * 职责:
 * - 设备本地视图模型(LocalDevice)与注册表 DeviceTypeSpec 的转换。
 * - 端口句柄派生:直接消费注册表 DTO 的真实端口声明(DeviceTypeSpec.ports,
 *   来源为设备 YAML 目录), 不维护按 type_id 的静态端口表, 也不做能源类型
 *   启发式兜底; 仅热泵按 mode 参数裁剪未启用的冷/热输出, 与后端端口生成
 *   规则一致(保证画布句柄与服务端端口一一对应)。
 * - 连接兼容校验:能源类型一致 且 方向为 out → in;不兼容返回可定位原因。
 * - 语义内容序列化(updateDraft 载荷)与 GraphModel 反序列化。
 * - 布局(节点坐标)与拓扑分离:坐标独立持久化,不进入语义内容。
 *
 * 本地端口 id 约定:`{carrier}:{direction}`(节点作用域内唯一;bidirectional
 * 端口展开为 in/out 两个句柄, 共享同一服务器端口)。
 */

import type {
  Connection,
  Device,
  DeviceKind,
  DeviceTypeSpec,
  EnergyCarrier,
  Fidelity,
  GraphModel,
  ParameterSpec,
} from '../../types'

// ---------------------------------------------------------------------------
// 本地视图模型
// ---------------------------------------------------------------------------

export interface LocalDevice {
  /** 本地 id:服务器设备 id 的字符串形式;新建设备为 d_ 前缀本地 id。 */
  id: string
  deviceType: string
  kind: DeviceKind
  name: string
  params: Record<string, unknown>
  fidelity: Fidelity
  position: { x: number; y: number }
}

export interface PortDef {
  /** YAML 端口声明名(权威来源,RR-P1-04);同载能多端口按 name 区分。 */
  name: string
  carrier: EnergyCarrier
  direction: 'in' | 'out'
}

export interface LocalConnection {
  id: string
  fromDeviceId: string
  /** 端口句柄 id,形如 `{carrier}:{direction}`。 */
  fromHandle: string
  toDeviceId: string
  toHandle: string
  carrier: EnergyCarrier
}

/** 本地 id 前缀,避免与服务器数字 id 冲突。 */
const LOCAL_PREFIX = 'd_'

let idCounter = 0

/** 生成本地设备/连接 id。 */
export function localId(): string {
  idCounter += 1
  return `${LOCAL_PREFIX}${Date.now().toString(36)}_${idCounter.toString(36)}`
}

export function isLocalId(id: string): boolean {
  return id.startsWith(LOCAL_PREFIX)
}

export function handleId(name: string, carrier: EnergyCarrier, direction: 'in' | 'out'): string {
  return `${carrier}:${direction}:${name}`
}

export function parseHandle(
  handle: string,
): { carrier: EnergyCarrier; direction: 'in' | 'out'; name: string } | null {
  // 兼容旧句柄 `{carrier}:{direction}`(RR-P1-04 整改前的产物)。
  // 新句柄格式 `{carrier}:{direction}:{name}`, 旧格式 name 留空, 提交时由
  // ModelPage 走 carrier+direction 取第一个匹配端口(降级路径, 仅历史存档可触发)。
  const parts = handle.split(':')
  if (parts.length < 2) return null
  const carrier = parts[0] as EnergyCarrier
  const direction = parts[1] as 'in' | 'out'
  if (!ENERGY_CARRIERS.includes(carrier)) return null
  if (direction !== 'in' && direction !== 'out') return null
  const name = parts.slice(2).join(':') || ''
  return { carrier, direction, name }
}

export const ENERGY_CARRIERS: EnergyCarrier[] = ['electric', 'heat', 'cool', 'gas', 'solar']

// ---------------------------------------------------------------------------
// 端口句柄派生(注册表 DTO 真实端口,RR-P1-04)
// ---------------------------------------------------------------------------

/**
 * 按设备类型声明(DeviceTypeSpec.ports, 来源为设备 YAML 公开 descriptor)
 * 派生画布句柄:
 *
 * - 每个声明端口映射一个句柄;bidirectional 端口展开为 in/out 两个句柄
 *   (电池充放电互斥、锅炉燃气购入等, 共享同一服务器端口);
 * - 热泵按 mode 参数裁剪: heating 无 cool 输出, cooling 无 heat 输出 ——
 *   与后端 _descriptor_ports 裁剪规则一致, 句柄与服务器端口一一对应;
 * - 不维护按 type_id 的静态端口表, 无能源类型启发式兜底 —— 端口唯一权威
 *   来源是注册表 DTO(RR-P1-04: 携带 YAML 端口名, 用于同载能多端口区分)。
 */
export function derivePorts(spec: DeviceTypeSpec, params: Record<string, unknown>): PortDef[] {
  const mode = String(params.mode ?? 'both')
  const ports: PortDef[] = []
  for (const p of spec.ports) {
    if (spec.type_id === 'ies.device.heat_pump') {
      if (mode === 'heating' && p.energy_carrier === 'cool') continue
      if (mode === 'cooling' && p.energy_carrier === 'heat') continue
    }
    if (p.direction === 'bidirectional') {
      ports.push({ name: p.name, carrier: p.energy_carrier, direction: 'in' })
      ports.push({ name: p.name, carrier: p.energy_carrier, direction: 'out' })
    } else {
      ports.push({ name: p.name, carrier: p.energy_carrier, direction: p.direction })
    }
  }
  return ports
}

// ---------------------------------------------------------------------------
// 连接兼容校验
// ---------------------------------------------------------------------------

export type IncompatReason = 'type' | 'direction' | 'same_device' | 'duplicate' | 'solar'

export interface Compatibility {
  ok: boolean
  reason: IncompatReason | null
}

/**
 * 校验连接是否兼容:
 * - 能源类型必须一致;
 * - 方向必须 out(源) → in(汇);
 * - 禁止自连接与重复连接;太阳辐射端口不可连线。
 */
export function checkConnection(
  fromDevice: LocalDevice,
  fromPort: PortDef,
  toDevice: LocalDevice,
  toPort: PortDef,
  existing: LocalConnection[],
): Compatibility {
  if (fromDevice.id === toDevice.id) {
    return { ok: false, reason: 'same_device' }
  }
  if (fromPort.carrier === 'solar' || toPort.carrier === 'solar') {
    return { ok: false, reason: 'solar' }
  }
  if (fromPort.carrier !== toPort.carrier) {
    return { ok: false, reason: 'type' }
  }
  if (fromPort.direction !== 'out' || toPort.direction !== 'in') {
    return { ok: false, reason: 'direction' }
  }
  const fromHandle = handleId(fromPort.name, fromPort.carrier, fromPort.direction)
  const toHandle = handleId(toPort.name, toPort.carrier, toPort.direction)
  const dup = existing.some(
    (c) =>
      (c.fromDeviceId === fromDevice.id && c.fromHandle === fromHandle && c.toDeviceId === toDevice.id && c.toHandle === toHandle) ||
      (c.fromDeviceId === toDevice.id && c.fromHandle === toHandle && c.toDeviceId === fromDevice.id && c.toHandle === fromHandle),
  )
  if (dup) {
    return { ok: false, reason: 'duplicate' }
  }
  return { ok: true, reason: null }
}

// ---------------------------------------------------------------------------
// 载能 → 连接类型 / 文案映射
// ---------------------------------------------------------------------------

/** 载能 → 工程连接类型(ConnectionInput.conn_type);solar 不可连线返回 null。 */
export function carrierToConnType(carrier: EnergyCarrier): Connection['conn_type'] | null {
  switch (carrier) {
    case 'electric':
      return 'electric_line'
    case 'heat':
      return 'thermal_pipe'
    case 'cool':
      return 'cooling_pipe'
    case 'gas':
      return 'fuel_pipe'
    case 'solar':
      return null
  }
}

/** 载能 → 展示文案键(全局 i18n 已覆盖的键)。 */
export function carrierLabelKey(carrier: EnergyCarrier): string {
  switch (carrier) {
    case 'electric':
      return 'ies.modeling.port_electric'
    case 'heat':
      return 'ies.modeling.port_thermal'
    case 'cool':
      return 'ies.modeling.port_cooling'
    case 'gas':
      return 'ies.modeling.port_fuel'
    case 'solar':
      return 'port.solar' // 页面级文案(text.ts)
  }
}

// ---------------------------------------------------------------------------
// 参数默认值
// ---------------------------------------------------------------------------

/** 参数默认值(与 ParameterSpec.default 同型:number | string | 字典对象 | null)。 */
export type ParamValue = number | string | Record<string, number> | null

/** 按设备属性(存量/新增)取参数默认值:存量优先 existing_default。
 *  枚举参数(如 mode)默认值为字符串字面量,数值参数为 number,
 *  结构化参数(如 import_tariff)为 {peak, flat, valley} 对象。 */
export function defaultParamValue(spec: ParameterSpec, kind: DeviceKind): ParamValue {
  if (kind === 'existing' && spec.existing_default !== null) return spec.existing_default
  return spec.default
}

/** 为新设备生成全参数默认值。 */
export function buildDefaultParams(
  spec: DeviceTypeSpec,
  kind: DeviceKind,
): Record<string, unknown> {
  const params: Record<string, unknown> = {}
  for (const [key, p] of Object.entries(spec.parameters)) {
    const value = defaultParamValue(p, kind)
    if (value !== null) params[key] = value
  }
  return params
}

/** 容量展示参数键:优先 rated_ > max_ > peak_ > capacity_ 前缀。 */
export function capacityParamKey(spec: DeviceTypeSpec): string | null {
  const keys = Object.keys(spec.parameters)
  const pref = ['rated_', 'max_', 'peak_', 'capacity_']
  for (const p of pref) {
    const hit = keys.find((k) => k.startsWith(p))
    if (hit) return hit
  }
  return keys.length > 0 ? keys[0] : null
}

/** 参数键 → 展示值(数字或占位)。 */
export function paramNumber(params: Record<string, unknown>, key: string): number | null {
  const value = params[key]
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value !== '') {
    const n = Number(value)
    if (Number.isFinite(n)) return n
  }
  return null
}

// ---------------------------------------------------------------------------
// 命名与序列化
// ---------------------------------------------------------------------------

/** 设备默认命名:`{类型名} {同类序号}`。 */
export function defaultDeviceName(spec: DeviceTypeSpec, sameTypeCount: number, locale: 'zh' | 'en'): string {
  const label = locale === 'zh' ? spec.name_zh : spec.name_en
  return `${label} ${sameTypeCount + 1}`
}

// ---------------------------------------------------------------------------
// GraphModel(服务器图)反序列化
// ---------------------------------------------------------------------------

/** 服务器 PortType → 载能(本产品设备集只出现四类能源端口)。 */
function portTypeToCarrier(portType: string): EnergyCarrier | null {
  switch (portType) {
    case 'electric':
      return 'electric'
    case 'thermal':
      return 'heat'
    case 'cooling':
      return 'cool'
    case 'fuel':
      return 'gas'
    default:
      return null
  }
}

/** 服务器设备 → 本地设备(坐标取自布局存储,缺省散列摆放)。 */
export function deviceFromServer(
  device: Device,
  fallbackPosition: { x: number; y: number },
): LocalDevice {
  return {
    id: String(device.id),
    deviceType: device.device_type,
    kind: device.kind,
    name: device.name,
    params: { ...device.params },
    fidelity: device.model_fidelity,
    position: fallbackPosition,
  }
}

/** 服务器连接 → 本地连接(经 ports 表把端口 id 映射回 载能:方向:端口名)。 */
export function connectionFromServer(
  conn: Connection,
  ports: GraphModel['ports'],
  deviceById: Map<string, LocalDevice>,
): LocalConnection | null {
  const fromPort = ports.find((p) => p.id === conn.from_port_id)
  const toPort = ports.find((p) => p.id === conn.to_port_id)
  if (!fromPort || !toPort) return null
  const fromCarrier = portTypeToCarrier(fromPort.port_type)
  const toCarrier = portTypeToCarrier(toPort.port_type)
  if (!fromCarrier || !toCarrier || fromCarrier !== toCarrier) return null
  const fromDirection: 'in' | 'out' = fromPort.direction === 'in' ? 'in' : 'out'
  const toDirection: 'in' | 'out' = toPort.direction === 'in' ? 'in' : 'out'
  const fromDeviceId = String(fromPort.device_id)
  const toDeviceId = String(toPort.device_id)
  if (!deviceById.has(fromDeviceId) || !deviceById.has(toDeviceId)) return null
  return {
    id: String(conn.id),
    fromDeviceId,
    fromHandle: handleId(fromPort.name, fromCarrier, fromDirection),
    toDeviceId,
    toHandle: handleId(toPort.name, toCarrier, toDirection),
    carrier: fromCarrier,
  }
}

// ---------------------------------------------------------------------------
// 布局持久化(与拓扑分离:坐标不进入语义内容,独立存储)
// ---------------------------------------------------------------------------

const LAYOUT_KEY_PREFIX = 'iesplan.model.layout.'

export function layoutStorageKey(projectId: number): string {
  return `${LAYOUT_KEY_PREFIX}${projectId}`
}

export function loadLayout(projectId: number): Record<string, { x: number; y: number }> {
  try {
    const raw = window.localStorage.getItem(layoutStorageKey(projectId))
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Record<string, { x: number; y: number }>
    if (typeof parsed !== 'object' || parsed === null) return {}
    const out: Record<string, { x: number; y: number }> = {}
    for (const [key, pos] of Object.entries(parsed)) {
      if (pos && typeof pos.x === 'number' && typeof pos.y === 'number' && Number.isFinite(pos.x) && Number.isFinite(pos.y)) {
        out[key] = { x: pos.x, y: pos.y }
      }
    }
    return out
  } catch {
    return {}
  }
}

export function saveLayout(projectId: number, layout: Record<string, { x: number; y: number }>): void {
  try {
    window.localStorage.setItem(layoutStorageKey(projectId), JSON.stringify(layout))
  } catch {
    // 隐私模式忽略
  }
}
