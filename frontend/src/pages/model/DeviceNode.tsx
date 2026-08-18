/**
 * 建模画布自定义节点(ReactFlow Node)。
 *
 * 展示:名称、类型、存量/新增徽章、模型精度(P1/P2/P3)、容量;
 * 端口句柄按 载能:方向 推导,左右分布(in 左 / out 右),载能以颜色 + 文字双重编码。
 * 状态不只靠颜色:存量/新增徽章带图标与形状,精度带文字。
 */

import { memo, useMemo } from 'react'
import type { CSSProperties } from 'react'
import { Handle, Position } from '@xyflow/react'
import type { Node, NodeProps } from '@xyflow/react'

import type { DeviceTypeSpec, EnergyCarrier, ParameterSpec } from '../../types'
import {
  capacityParamKey,
  carrierLabelKey,
  derivePorts,
  handleId,
  paramNumber,
} from './canvasModel'
import type { LocalDevice } from './canvasModel'
import { lt } from './text'
import { useI18n } from '../../i18n'
import { Icon } from '../../components/ui'

/** 节点 data(ReactFlow Node<DeviceNodeData, 'device'> 的约束需要 type 别名)。 */
export type DeviceNodeData = {
  device: LocalDevice
  spec: DeviceTypeSpec
  locale: 'zh' | 'en'
  onSelect: (deviceId: string) => void
}

const KIND_BADGE: Record<LocalDevice['kind'], { cls: string; icon: 'plus' | 'clock'; labelKey: string }> = {
  new: { cls: 'mp-node__kind--new', icon: 'plus', labelKey: 'node.kind_new' },
  existing: { cls: 'mp-node__kind--existing', icon: 'clock', labelKey: 'node.kind_existing' },
}

const FIDELITY_LABEL: Record<LocalDevice['fidelity'], string> = {
  low: 'node.fidelity_low',
  medium: 'node.fidelity_medium',
  high: 'node.fidelity_high',
}

/** 端口句柄样式:in 左 / out 右,竖向按序号分布。 */
function handleStyle(index: number, count: number, side: 'in' | 'out'): CSSProperties {
  const top = count <= 1 ? 50 : 30 + (index * 55) / (count - 1)
  return side === 'in' ? { top: `${top}%`, left: -1 } : { top: `${top}%`, right: -1, left: 'auto' }
}

function PortHandles({ device, spec }: { device: LocalDevice; spec: DeviceTypeSpec }) {
  const { t } = useI18n()
  const ports = useMemo(() => derivePorts(spec, device.params), [spec, device.params])
  const ins = ports.filter((p) => p.direction === 'in')
  const outs = ports.filter((p) => p.direction === 'out')
  const labelOf = (carrier: EnergyCarrier): string => {
    const key = carrierLabelKey(carrier)
    return key === 'port.solar' ? lt(key) : t(key)
  }
  return (
    <>
      {ins.map((port, i) => {
        const label = labelOf(port.carrier)
        return (
          <Handle
            key={handleId(port.carrier, port.direction)}
            id={handleId(port.carrier, port.direction)}
            type="target"
            position={Position.Left}
            className={`mp-handle mp-handle--${port.carrier}`}
            style={handleStyle(i, ins.length, 'in')}
            aria-label={`${label} ${lt('port.direction_in')}`}
            title={`${label} (${lt('port.direction_in')})`}
          />
        )
      })}
      {outs.map((port, i) => {
        const label = labelOf(port.carrier)
        return (
          <Handle
            key={handleId(port.carrier, port.direction)}
            id={handleId(port.carrier, port.direction)}
            type="source"
            position={Position.Right}
            className={`mp-handle mp-handle--${port.carrier}`}
            style={handleStyle(i, outs.length, 'out')}
            aria-label={`${label} ${lt('port.direction_out')}`}
            title={`${label} (${lt('port.direction_out')})`}
          />
        )
      })}
    </>
  )
}

function DeviceNodeView({ data }: NodeProps<Node<DeviceNodeData, 'device'>>) {
  const { device, spec, onSelect } = data
  const { t } = useI18n()
  const typeLabel = data.locale === 'zh' ? spec.name_zh : spec.name_en
  const kind = KIND_BADGE[device.kind]
  const capacityKey = useMemo(() => capacityParamKey(spec), [spec])
  const capacityValue = capacityKey ? paramNumber(device.params, capacityKey) : null
  const capacitySpec: ParameterSpec | null = capacityKey ? spec.parameters[capacityKey] ?? null : null

  return (
    <div
      className="mp-node"
      data-kind={device.kind}
      role="button"
      tabIndex={0}
      aria-label={`${typeLabel}: ${device.name}`}
      onClick={(event) => {
        event.stopPropagation()
        onSelect(device.id)
      }}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect(device.id)
        }
      }}
    >
      <header className="mp-node__head">
        <span className="mp-node__name" title={device.name}>
          {device.name}
        </span>
        <span className={`mp-node__kind ${kind.cls}`} title={t('ies.modeling.kind')}>
          <Icon name={kind.icon} size={11} />
          {lt(kind.labelKey)}
        </span>
      </header>
      <div className="mp-node__type">{typeLabel}</div>
      <div className="mp-node__meta">
        <span className="mp-node__fidelity" title={t('ies.modeling.fidelity')}>
          {lt(FIDELITY_LABEL[device.fidelity])}
        </span>
        <span className="mp-node__capacity" title={capacitySpec?.help_key ?? lt('node.capacity')}>
          {capacityValue !== null
            ? `${t('ies.modeling.capacity')} ${capacityValue}${capacitySpec?.unit ? ` ${capacitySpec.unit}` : ''}`
            : lt('node.capacity_none')}
        </span>
      </div>
      <PortHandles device={device} spec={spec} />
    </div>
  )
}

/** 自定义节点需作为稳定引用传入 ReactFlow nodeTypes。 */
export const DeviceNode = memo(DeviceNodeView)
