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
 * - 草稿内容(2.0 名键设备/连接)反序列化: 端口名经注册表声明解析为句柄。
 * - 布局(节点坐标)与拓扑分离:坐标独立持久化,不进入语义内容。
 *
 * 句柄 id 约定:`{carrier}:{direction}:{name}`, name 为注册表端口声明名;
 * bidirectional 端口展开为 in/out 两个句柄。
 */

import type {
  Connection,
  Device,
  DeviceKind,
  DeviceTypeSpec,
  EnergyCarrier,
  ParameterSpec,
} from '../../types'

// ---------------------------------------------------------------------------
// 本地视图模型
// ---------------------------------------------------------------------------

export interface LocalDevice {
  /** 本地 id 即设备名(2.0 名键唯一事实; 改名即换键, 调用方同步连接引用与布局)。 */
  id: string
  /** 仅用于编辑会话，重命名时保持不变。 */
  editorId: symbol
  deviceType: string
  kind: DeviceKind
  name: string
  params: Record<string, unknown>
  position: { x: number; y: number }
}

export interface PortDef {
  /** YAML 端口声明名(权威来源,RR-P1-04);同载能多端口按 name 区分。 */
  name: string
  carrier: EnergyCarrier
  direction: 'in' | 'out'
}

export interface LocalConnection {
  /** 本地 id 即服务端连接名(2.0 名键)。 */
  id: string
  fromDeviceId: string
  /** 端口句柄 id,形如 `{carrier}:{direction}:{name}`。 */
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

export function handleId(name: string, carrier: EnergyCarrier, direction: 'in' | 'out'): string {
  return `${carrier}:${direction}:${name}`
}

export function parseHandle(
  handle: string,
): { carrier: EnergyCarrier; direction: 'in' | 'out'; name: string } | null {
  // 句柄格式 `{carrier}:{direction}:{name}`, name 为注册表端口声明名(2.0 连线键)。
  // 载能是开放身份(公开契约非枚举): 只拒绝空载能, 不以成员表拒绝未知载能;
  // 方向保持 in/out 封闭语义。
  const parts = handle.split(':')
  if (parts.length < 3) return null
  const carrier = parts[0]
  const direction = parts[1] as 'in' | 'out'
  if (!carrier) return null
  if (direction !== 'in' && direction !== 'out') return null
  const name = parts.slice(2).join(':')
  if (!name) return null
  return { carrier, direction, name }
}

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
    } else if (p.direction === 'in') {
      ports.push({ name: p.name, carrier: p.energy_carrier, direction: 'in' })
    } else if (p.direction === 'out') {
      ports.push({ name: p.name, carrier: p.energy_carrier, direction: 'out' })
    }
    // predefined 与 blind 接口不连接其他设备, 格式契约严禁作为连接句柄暴露
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
// 载能 → 文案映射
// ---------------------------------------------------------------------------

/**
 * 载能 → 展示文案键(全局 i18n 已覆盖的键)。未知载能返回 null, 调用方直接
 * 展示真实载能 ID(文案表不是业务准入表, 不得据此拒绝端口)。
 */
export function carrierLabelKey(carrier: EnergyCarrier): string | null {
  switch (carrier) {
    case 'electricity':
      return 'ies.modeling.port_electric'
    case 'heat':
      return 'ies.modeling.port_thermal'
    case 'cool':
      return 'ies.modeling.port_cooling'
    case 'gas':
      return 'ies.modeling.port_fuel'
    case 'solar':
      return 'port.solar' // 页面级文案(text.ts)
    default:
      return null
  }
}

// ---------------------------------------------------------------------------
// 参数默认值
// ---------------------------------------------------------------------------

/** 参数默认值(与 ParameterSpec.default 同型:number | string | 字典对象 | null)。 */
export type ParamValue = number | string | Record<string, number> | null

/** 取参数默认值:数值参数为 number, 结构化参数为 {peak, flat, valley} 对象。 */
export function defaultParamValue(spec: ParameterSpec, _kind?: DeviceKind): ParamValue {
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
// 草稿内容(2.0 名键)反序列化
// ---------------------------------------------------------------------------

/** 草稿设备条目 → 本地设备(id 即设备名; 坐标取自布局存储,缺省散列摆放)。 */
export function deviceFromDraft(
  device: Device,
  fallbackPosition: { x: number; y: number },
): LocalDevice {
  return {
    id: device.name,
    editorId: Symbol(),
    deviceType: device.device_type,
    kind: device.kind,
    name: device.name,
    params: { ...device.params },
    position: fallbackPosition,
  }
}

/** 在注册表派生句柄中按端口名找定义(方向须匹配; 双向端口已展开为 in/out)。 */
function findPortDef(
  spec: DeviceTypeSpec | null,
  params: Record<string, unknown>,
  portName: string,
  direction: 'in' | 'out',
): PortDef | null {
  if (!spec) return null
  return derivePorts(spec, params).find((p) => p.name === portName && p.direction === direction) ?? null
}

/** 草稿连接条目 → 本地连接(端点名经注册表端口声明解析为句柄; 解析失败返回 null)。 */
export function connectionFromDraft(
  conn: Connection,
  deviceByName: Map<string, LocalDevice>,
  specOf: (typeId: string) => DeviceTypeSpec | null,
): LocalConnection | null {
  const fromDevice = deviceByName.get(conn.from_device)
  const toDevice = deviceByName.get(conn.to_device)
  if (!fromDevice || !toDevice) return null
  const fromDef = findPortDef(specOf(fromDevice.deviceType), fromDevice.params, conn.from_port, 'out')
  const toDef = findPortDef(specOf(toDevice.deviceType), toDevice.params, conn.to_port, 'in')
  if (!fromDef || !toDef || fromDef.carrier !== toDef.carrier) return null
  return {
    id: conn.name,
    fromDeviceId: fromDevice.id,
    fromHandle: handleId(fromDef.name, fromDef.carrier, 'out'),
    toDeviceId: toDevice.id,
    toHandle: handleId(toDef.name, toDef.carrier, 'in'),
    carrier: fromDef.carrier,
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
