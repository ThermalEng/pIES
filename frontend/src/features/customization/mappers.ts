/**
 * 定制化 / 开放生态纯映射与 YAML 双向表单辅助（无网络、无 React 依赖）。
 */

import { parse as parseYaml, stringify as stringifyYaml } from 'yaml'
import type {
  ArtifactKindDto,
  LocalArtifactDto,
  MarketIndexItemDto,
  MarketModeDto,
  MemberArtifactStateDto,
} from './contracts'

export type ArtifactLifecycleStatus =
  | 'pending_approval'
  | 'rejected'
  | 'building'
  | 'ready'
  | 'build_failed'
  | 'disabled'

export interface UnifiedCatalogEntry {
  key: string
  id: string
  kind: ArtifactKindDto
  versionOrRevision: string
  displayNameZh: string
  displayNameEn: string
  description: string
  publisher: string | null
  license: string | null
  compatibility: Record<string, string>
  consumes: string[]
  produces: string[]
  sha256: string | null
  sizeBytes: number | null
  readmeUrl: string | null
  memberState: MemberArtifactStateDto
  localArtifact: LocalArtifactDto | null
  updateAvailable: boolean
  remoteVersionOrRevision: string | null
}

export interface DeviceFormPropertyRow {
  name: string
  value: string
  unit: string
  min: string
  max: string
}

export interface DeviceFormInterfaceRow {
  name: string
  type: string
  carrier: string
  unit: string
}

export interface DeviceBasicFormState {
  deviceId: string
  nameZh: string
  nameEn: string
  properties: DeviceFormPropertyRow[]
  interfaces: DeviceFormInterfaceRow[]
  valid: boolean
}

/** 统一本地制品后台生命周期状态映射（approval_status × install_status）。 */
export function resolveArtifactLifecycleStatus(
  item: Pick<LocalArtifactDto, 'approval_status' | 'install_status'>,
): ArtifactLifecycleStatus {
  if (item.approval_status === 'pending') return 'pending_approval'
  if (item.approval_status === 'rejected') return 'rejected'
  if (item.install_status === 'building' || item.install_status === 'testing') return 'building'
  if (item.install_status === 'build_failed') return 'build_failed'
  if (item.install_status === 'disabled') return 'disabled'
  if (item.install_status === 'ready') return 'ready'
  return 'building'
}

function localizedFromMetadata(meta: Record<string, unknown>, fallbackId: string): { zh: string; en: string } {
  const names = meta.names && typeof meta.names === 'object' ? (meta.names as Record<string, unknown>) : null
  const zh = typeof names?.['zh-CN'] === 'string' && names['zh-CN']
    ? names['zh-CN']
    : typeof names?.zh_cn === 'string' && names.zh_cn
      ? names.zh_cn
      : fallbackId
  const en = typeof names?.['en-US'] === 'string' && names['en-US']
    ? names['en-US']
    : typeof names?.en_us === 'string' && names.en_us
      ? names.en_us
      : fallbackId
  return { zh, en }
}

function versionDisplayForLocal(item: LocalArtifactDto): string {
  return item.kind === 'device_model' ? `r${item.version}` : item.version
}

function versionDisplayForMarket(item: MarketIndexItemDto): string {
  if (item.kind === 'device_model') {
    return item.revision !== undefined && item.revision !== null ? `r${item.revision}` : 'r1'
  }
  return item.version ?? ''
}

/**
 * 合并本地生态目录与远端市场索引为单一「市场与已安装」列表：
 * - local 模式不消费远端条目，仅展示已审批就绪的本地制品及成员状态；
 * - online 模式保留远端条目的权威三态（remote / local_uninstalled / local_installed），
 *   并将仅存于本地的 ready/installed 条目合并展示而不重复列出。
 */
export function mergeCatalogEntries(params: {
  localArtifacts: LocalArtifactDto[]
  marketItems: MarketIndexItemDto[]
  marketMode: MarketModeDto
}): UnifiedCatalogEntry[] {
  const { localArtifacts, marketItems, marketMode } = params
  const localByKey = new Map<string, LocalArtifactDto>()
  for (const art of localArtifacts) {
    localByKey.set(`${art.kind}:${art.stable_id}`, art)
  }

  const out: UnifiedCatalogEntry[] = []
  const seenKeys = new Set<string>()

  if (marketMode === 'online') {
    for (const m of marketItems) {
      const key = `${m.kind}:${m.id}`
      seenKeys.add(key)
      const local = localByKey.get(key) ?? null
      const meta = local?.metadata ?? {}
      const memberState: MemberArtifactStateDto = local
        ? local.installed
          ? 'local_installed'
          : 'local_uninstalled'
        : m.member_state
      const remoteVer = versionDisplayForMarket(m)
      out.push({
        key,
        id: m.id,
        kind: m.kind,
        versionOrRevision: local ? versionDisplayForLocal(local) : remoteVer,
        displayNameZh: m.name?.zh_cn || m.name?.en_us || m.id,
        displayNameEn: m.name?.en_us || m.name?.zh_cn || m.id,
        description: m.description,
        publisher: m.publisher,
        license: m.license,
        compatibility: m.compatibility ?? {},
        consumes: Array.isArray(m.consumes) ? m.consumes : Array.isArray(meta.consumes) ? (meta.consumes as string[]) : [],
        produces: Array.isArray(m.produces) ? m.produces : Array.isArray(meta.produces) ? (meta.produces as string[]) : [],
        sha256: m.sha256,
        sizeBytes: m.size_bytes,
        readmeUrl: m.readme_url ?? null,
        memberState,
        localArtifact: local,
        updateAvailable: Boolean(m.update_available),
        remoteVersionOrRevision: remoteVer,
      })
    }
  }

  for (const art of localArtifacts) {
    if (art.approval_status !== 'approved' || art.install_status !== 'ready') continue
    const key = `${art.kind}:${art.stable_id}`
    if (seenKeys.has(key)) continue
    seenKeys.add(key)
    const meta = art.metadata ?? {}
    const names = localizedFromMetadata(meta, art.stable_id)
    out.push({
      key,
      id: art.stable_id,
      kind: art.kind,
      versionOrRevision: versionDisplayForLocal(art),
      displayNameZh: names.zh,
      displayNameEn: names.en,
      description: typeof meta.description === 'string' ? meta.description : '',
      publisher: null,
      license: typeof meta.license === 'string' ? meta.license : null,
      compatibility:
        meta.compatibility && typeof meta.compatibility === 'object' && !Array.isArray(meta.compatibility)
          ? (meta.compatibility as Record<string, string>)
          : {},
      consumes: Array.isArray(meta.consumes) ? (meta.consumes as string[]) : [],
      produces: Array.isArray(meta.produces) ? (meta.produces as string[]) : [],
      sha256: null,
      sizeBytes: null,
      readmeUrl: null,
      memberState: art.installed ? 'local_installed' : 'local_uninstalled',
      localArtifact: art,
      updateAvailable: false,
      remoteVersionOrRevision: null,
    })
  }

  return out
}

/** 从 device-model YAML/JSON 文本解析常用基础字段表单。 */
export function parseDeviceFormFromText(rawText: string): DeviceBasicFormState {
  try {
    const parsed = parseYaml(rawText) as unknown
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { deviceId: '', nameZh: '', nameEn: '', properties: [], interfaces: [], valid: false }
    }
    const doc = parsed as Record<string, unknown>
    const device = doc.device && typeof doc.device === 'object' && !Array.isArray(doc.device)
      ? (doc.device as Record<string, unknown>)
      : {}
    const names = device.names && typeof device.names === 'object' && !Array.isArray(device.names)
      ? (device.names as Record<string, unknown>)
      : {}
    const propsObj = doc.properties && typeof doc.properties === 'object' && !Array.isArray(doc.properties)
      ? (doc.properties as Record<string, unknown>)
      : {}
    const ifacesObj = doc.interfaces && typeof doc.interfaces === 'object' && !Array.isArray(doc.interfaces)
      ? (doc.interfaces as Record<string, unknown>)
      : {}

    const properties: DeviceFormPropertyRow[] = Object.entries(propsObj).map(([name, val]) => {
      const obj = val && typeof val === 'object' && !Array.isArray(val) ? (val as Record<string, unknown>) : {}
      const vr = obj.valid_range && typeof obj.valid_range === 'object' && !Array.isArray(obj.valid_range)
        ? (obj.valid_range as Record<string, unknown>)
        : {}
      return {
        name,
        value: obj.value === null || obj.value === undefined ? '' : String(obj.value),
        unit: typeof obj.unit === 'string' ? obj.unit : '',
        min: vr.minimum === null || vr.minimum === undefined ? '' : String(vr.minimum),
        max: vr.maximum === null || vr.maximum === undefined ? '' : String(vr.maximum),
      }
    })

    const interfaces: DeviceFormInterfaceRow[] = Object.entries(ifacesObj).map(([name, val]) => {
      const obj = val && typeof val === 'object' && !Array.isArray(val) ? (val as Record<string, unknown>) : {}
      return {
        name,
        type: typeof obj.type === 'string' ? obj.type : 'in',
        carrier: typeof obj.carrier === 'string' ? obj.carrier : 'electricity',
        unit: typeof obj.unit === 'string' ? obj.unit : 'kW',
      }
    })

    return {
      deviceId: typeof device.id === 'string' ? device.id : '',
      nameZh: typeof names['zh-CN'] === 'string' ? names['zh-CN'] : '',
      nameEn: typeof names['en-US'] === 'string' ? names['en-US'] : '',
      properties,
      interfaces,
      valid: true,
    }
  } catch {
    return { deviceId: '', nameZh: '', nameEn: '', properties: [], interfaces: [], valid: false }
  }
}

function parseScalarForYaml(text: string): unknown {
  const trimmed = text.trim()
  if (trimmed === '') return ''
  if (trimmed === 'true') return true
  if (trimmed === 'false') return false
  const num = Number(trimmed)
  if (Number.isFinite(num) && /^-?\d+(\.\d+)?([eE][+-]?\d+)?$/.test(trimmed)) {
    return num
  }
  return text
}

/** 将常用基础字段表单回写至 device-model YAML，保留 equations 与 inputs 等其余顶层节。 */
export function applyDeviceFormToText(rawText: string, form: Omit<DeviceBasicFormState, 'valid'>): string {
  let doc: Record<string, unknown> = {}
  try {
    const parsed = parseYaml(rawText) as unknown
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      doc = { ...(parsed as Record<string, unknown>) }
    }
  } catch {
    doc = {}
  }

  if (!doc.schema) doc.schema = 'ies.device-model'
  if (!doc.schema_version) doc.schema_version = '1.0.0'

  const prevDevice = doc.device && typeof doc.device === 'object' && !Array.isArray(doc.device)
    ? (doc.device as Record<string, unknown>)
    : {}
  const prevNames = prevDevice.names && typeof prevDevice.names === 'object' && !Array.isArray(prevDevice.names)
    ? (prevDevice.names as Record<string, unknown>)
    : {}
  const nextNames: Record<string, unknown> = { ...prevNames }
  if (form.nameZh.trim()) nextNames['zh-CN'] = form.nameZh
  else delete nextNames['zh-CN']
  if (form.nameEn.trim()) nextNames['en-US'] = form.nameEn
  else delete nextNames['en-US']

  doc.device = {
    ...prevDevice,
    id: form.deviceId.trim(),
    names: nextNames,
  }

  const prevProps = doc.properties && typeof doc.properties === 'object' && !Array.isArray(doc.properties)
    ? (doc.properties as Record<string, unknown>)
    : {}
  const nextProps: Record<string, unknown> = {}
  for (const row of form.properties) {
    const key = row.name.trim()
    if (!key) continue
    const existing = prevProps[key] && typeof prevProps[key] === 'object' && !Array.isArray(prevProps[key])
      ? ({ ...(prevProps[key] as Record<string, unknown>) })
      : {}
    existing.value = parseScalarForYaml(row.value)
    existing.unit = row.unit.trim() || '1'
    if (row.min.trim() !== '' || row.max.trim() !== '') {
      existing.valid_range = {
        minimum: row.min.trim() === '' ? null : Number(row.min),
        maximum: row.max.trim() === '' ? null : Number(row.max),
      }
    }
    nextProps[key] = existing
  }
  doc.properties = nextProps

  const prevIfaces = doc.interfaces && typeof doc.interfaces === 'object' && !Array.isArray(doc.interfaces)
    ? (doc.interfaces as Record<string, unknown>)
    : {}
  const nextIfaces: Record<string, unknown> = {}
  for (const row of form.interfaces) {
    const key = row.name.trim()
    if (!key) continue
    const existing = prevIfaces[key] && typeof prevIfaces[key] === 'object' && !Array.isArray(prevIfaces[key])
      ? ({ ...(prevIfaces[key] as Record<string, unknown>) })
      : {}
    existing.type = row.type.trim() || 'in'
    existing.carrier = row.carrier.trim() || 'electricity'
    existing.unit = row.unit.trim() || 'kW'
    if (!existing.valid_range) {
      existing.valid_range = { minimum: 0, maximum: null }
    }
    nextIfaces[key] = existing
  }
  doc.interfaces = nextIfaces

  if (!doc.equations || typeof doc.equations !== 'object') {
    doc.equations = { variables: {}, relations: [] }
  }

  return stringifyYaml(doc)
}

/** 提取本地制品诊断摘要文本（拒绝原因/构建报错等）。 */
export function formatArtifactDiagnostics(diagnostics: Record<string, unknown> | null): string | null {
  if (!diagnostics || typeof diagnostics !== 'object') return null
  if (typeof diagnostics.rejected_reason === 'string' && diagnostics.rejected_reason) {
    return diagnostics.rejected_reason
  }
  if (typeof diagnostics.error === 'string' && diagnostics.error) {
    return diagnostics.error
  }
  if (typeof diagnostics.message === 'string' && diagnostics.message) {
    return diagnostics.message
  }
  const keys = Object.keys(diagnostics)
  return keys.length > 0 ? JSON.stringify(diagnostics) : null
}
