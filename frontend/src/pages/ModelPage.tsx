/**
 * 建模画布页面(/projects/:id/model)。
 *
 * 能力:
 * - 左侧设备面板(注册表 /api/model/device-types),拖拽添加到画布;
 *   节点展示 名称/类型/存量新增徽章/容量(P1 简化 / P2 标准 / P3 详细)。
 * - 端口间连线:按 能源类型 + 方向(out 源 → in 汇)校验,不兼容连接被阻止
 *   并生成可定位诊断(可跳转到相关设备)。
 * - 设备参数侧栏:按设备类型 schema 渲染表单(单位/范围/默认值/帮助键),
 *   存量/新增切换(默认值随属性切换),模型精度选择。
 * - 保存:语义命令提交(projects.updateDraft)+ 修订号冲突检测;
 *   自动保存(1.2s 防抖)+ 手动保存按钮;冲突时提示"加载服务器版本/覆盖保存"。
 * - 布局与拓扑分离:节点坐标独立持久化到 localStorage,不进入语义内容。
 * - 工具栏:缩放 / 适应视图 / 校验(调用 graph/validate 显示诊断列表,可定位)。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import type { DragEvent as ReactDragEvent } from 'react'
import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from '@xyflow/react'
import type { Edge as RFEdge, Connection as RFConnection, Node as RFNode } from '@xyflow/react'
import '@xyflow/react/dist/style.css'

import { api } from '../api/client'
import { ApiError } from '../types'
import type { Diagnostic, DeviceTypeSpec, Fidelity, Port } from '../types'
import { useI18n, translateDiagnostic, translateError } from '../i18n'
import {
  Alert,
  Button,
  Icon,
  IconButton,
  Input,
  Select,
  SeverityBadge,
  Spinner,
} from '../components/ui'
import { useWorkbench } from './workbench'
import { DeviceNode } from './model/DeviceNode'
import type { DeviceNodeData } from './model/DeviceNode'
import {
  buildDefaultParams,
  carrierLabelKey,
  carrierToConnType,
  checkConnection,
  connectionFromServer,
  defaultDeviceName,
  defaultParamValue,
  deviceFromServer,
  isLocalId,
  loadLayout,
  localId,
  parseHandle,
  saveLayout,
} from './model/canvasModel'
import type { LocalConnection, LocalDevice, PortDef } from './model/canvasModel'
import { lt } from './model/text'
import './model/model.css'

/** 保存状态:头部指示 + 页面状态点。 */
type SaveState = 'idle' | 'dirty' | 'saving' | 'saved' | 'error' | 'conflict'

/** 连接失败/校验诊断的展示条目(可定位)。 */
interface IssueItem {
  id: string
  severity: 'error' | 'warning'
  message: string
  deviceId?: string
}

/** 参数规格(类型已在 types.ParameterSpec 声明 enum, 此处直接复用)。 */
type ParamSpecExt = DeviceTypeSpec['parameters'][string]

interface ExtendedDeviceTypeSpec extends Omit<DeviceTypeSpec, 'parameters'> {
  parameters: Record<string, ParamSpecExt>
}

const CARRIER_COLOR: Record<string, string> = {
  electric: '#0e5cad',
  heat: '#c2410c',
  cool: '#0891b2',
  gas: '#8a5a00',
}

/** 任意异常 → ApiError(供 translateError 渲染)。 */
function toApiError(err: unknown): ApiError {
  if (err instanceof ApiError) return err
  if (err instanceof Error) return new ApiError(0, null, err.message || 'ies.error.unknown')
  return new ApiError(0, null, 'ies.error.unknown')
}

// ===========================================================================
// 主页面
// ===========================================================================

export default function ModelPage() {
  const { projectId } = useWorkbench()
  return (
    <ReactFlowProvider>
      <ModelCanvas projectId={projectId} />
    </ReactFlowProvider>
  )
}

// ===========================================================================
// 画布主逻辑
// ===========================================================================

function ModelCanvas({ projectId }: { projectId: number }) {
  const { t, locale } = useI18n()
  const { setAutosave, offline } = useWorkbench()
  const { screenToFlowPosition, zoomIn, zoomOut, fitView } = useReactFlow()

  // -- 注册表与模型状态 ------------------------------------------------
  const [deviceTypes, setDeviceTypes] = useState<ExtendedDeviceTypeSpec[] | null>(null)
  const [typesError, setTypesError] = useState(false)
  const [devices, setDevices] = useState<LocalDevice[]>([])
  const [connections, setConnections] = useState<LocalConnection[]>([])
  const [ports, setPorts] = useState<Port[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)

  // -- 选择与侧栏 ------------------------------------------------------
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(null)
  const [selectedConnId, setSelectedConnId] = useState<string | null>(null)

  // -- 保存 ------------------------------------------------------------
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [lastError, setLastError] = useState<string | null>(null)

  // -- 校验与连接诊断 --------------------------------------------------
  const [issues, setIssues] = useState<IssueItem[]>([])
  const [validateResult, setValidateResult] = useState<Diagnostic[] | null>(null)
  const [validating, setValidating] = useState(false)
  const [validateError, setValidateError] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)

  const typeMap = useMemo(() => {
    const map: Record<string, ExtendedDeviceTypeSpec> = {}
    for (const spec of deviceTypes ?? []) map[spec.type_id] = spec
    return map
  }, [deviceTypes])

  const specOf = useCallback((typeId: string): ExtendedDeviceTypeSpec | null => typeMap[typeId] ?? null, [typeMap])

  // -- 加载 ------------------------------------------------------------
  const reloadGraph = useCallback(async () => {
    setLoading(true)
    try {
      const graph = await api.model.getGraph(projectId)
      const layout = loadLayout(projectId)
      const deviceList = graph.devices.map((d, i) =>
        // 缺省布局须保证节点互不重叠(节点宽 ≥190px 含端口手柄, 手柄外伸约 7px):
        // 之前 80px 横距/60px 纵距的密集网格会让相邻节点盖住源端口,
        // 源端口中心点被邻居节点的 .mp-node__type 命中, 连线手势无法开始。
        deviceFromServer(d, layout[String(d.id)] ?? { x: 80 + (i % 4) * 260, y: 80 + Math.floor(i / 4) * 180 }),
      )
      const deviceById = new Map(deviceList.map((d) => [d.id, d]))
      const connectionList = graph.connections
        .map((c) => connectionFromServer(c, graph.ports, deviceById))
        .filter((c): c is LocalConnection => c !== null)
      setDevices(deviceList)
      setConnections(connectionList)
      setPorts(graph.ports)
      setLoadError(null)
    } catch (err) {
      // 后端未就绪/尚无图:以空画布继续(可本地编辑,保存时再校验)
      setLoadError(translateError(toApiError(err)))
      setDevices([])
      setConnections([])
      setPorts([])
    } finally {
      setLoading(false)
      setSelectedDeviceId(null)
      setSelectedConnId(null)
      setValidateResult(null)
      setSaveState('saved')
      setAutosave('saved')
    }
  }, [projectId, setAutosave])

  useEffect(() => {
    let cancelled = false
    void api.model
      .deviceTypes()
      .then((types) => {
        if (!cancelled) {
          // 注册表 schema 携带 enum 字段,前端参数表单按 spec.enum 渲染下拉/单选。
          const ext = types.map((t) => ({ ...t })) as ExtendedDeviceTypeSpec[]
          setDeviceTypes(ext)
        }
      })
      .catch(() => {
        if (!cancelled) setTypesError(true)
      })
    void reloadGraph()
    return () => {
      cancelled = true
    }
  }, [reloadGraph])

  // -- 设备增删改(写 SystemGraph) -------------------------------------
  const addDevice = useCallback(
    async (spec: ExtendedDeviceTypeSpec, position: { x: number; y: number }) => {
      try {
        const sameTypeCount = devices.filter((d) => d.deviceType === spec.type_id).length
        const name = defaultDeviceName(spec as DeviceTypeSpec, sameTypeCount, locale)
        const params = buildDefaultParams(spec as DeviceTypeSpec, 'new')
        const created = await api.model.addDevice(projectId, {
          device_type: spec.type_id,
          name,
          params,
          kind: 'new',
          model_fidelity: 'medium',
        })
        const device: LocalDevice = {
          id: String(created.id),
          deviceType: spec.type_id,
          kind: 'new',
          name,
          params,
          fidelity: 'medium',
          position,
        }
        setDevices((prev) => [...prev, device])
        setSelectedDeviceId(device.id)
        setSelectedConnId(null)
        // 重新加载端口映射(新增设备后服务器侧会自动创建端口)
        const graph = await api.model.getGraph(projectId)
        setPorts(graph.ports)
        setSaveState('saved')
        setAutosave('saved')
        setFlash(t('ies.modeling.device_added', { name }))
        window.setTimeout(() => setFlash(null), 1800)
      } catch (err) {
        setLastError(translateError(toApiError(err)))
        setSaveState('error')
        setAutosave('error')
      }
    },
    [devices, locale, projectId, setAutosave, t],
  )

  const patchDevice = useCallback(
    async (deviceId: string, patch: Partial<Pick<LocalDevice, 'name' | 'kind' | 'fidelity' | 'params'>>) => {
      try {
        await api.model.updateDevice(projectId, Number(deviceId), {
          name: patch.name,
          params: patch.params,
        })
        setDevices((prev) => prev.map((d) => (d.id === deviceId ? { ...d, ...patch } : d)))
        setSaveState('saved')
        setAutosave('saved')
      } catch (err) {
        setLastError(translateError(toApiError(err)))
        setSaveState('error')
        setAutosave('error')
      }
    },
    [projectId, setAutosave],
  )

  const deleteDevice = useCallback(
    async (deviceId: string) => {
      try {
        await api.model.deleteDevice(projectId, Number(deviceId))
        setDevices((prev) => prev.filter((d) => d.id !== deviceId))
        setConnections((prev) => prev.filter((c) => c.fromDeviceId !== deviceId && c.toDeviceId !== deviceId))
        setSelectedDeviceId((cur) => (cur === deviceId ? null : cur))
        setSelectedConnId(null)
        setSaveState('saved')
        setAutosave('saved')
      } catch (err) {
        setLastError(translateError(toApiError(err)))
        setSaveState('error')
        setAutosave('error')
      }
    },
    [projectId, setAutosave],
  )

  // -- 连接 ------------------------------------------------------------
  const connectionLabel = useCallback(
    (carrier: LocalConnection['carrier']): string => {
      const key = carrierLabelKey(carrier)
      return key === 'port.solar' ? lt(key) : t(key)
    },
    [t],
  )

  const handleConnect = useCallback(
    async (conn: RFConnection) => {
      if (!conn.source || !conn.target || !conn.sourceHandle || !conn.targetHandle) return
      const fromDevice = devices.find((d) => d.id === conn.source)
      const toDevice = devices.find((d) => d.id === conn.target)
      if (!fromDevice || !toDevice) return
      const fromPort = parseHandle(conn.sourceHandle)
      const toPort = parseHandle(conn.targetHandle)
      if (!fromPort || !toPort) return
      const result = checkConnection(fromDevice, fromPort, toDevice, toPort, connections)
      if (!result.ok) {
        // 不兼容:生成可定位诊断(不调用服务器)
        const message = incompatMessage(result.reason, fromDevice, fromPort, toDevice, toPort, connectionLabel)
        const item: IssueItem = {
          id: localId(),
          severity: 'error',
          message,
          deviceId: result.reason === 'type' || result.reason === 'direction' ? fromDevice.id : toDevice.id,
        }
        setIssues((prev) => [...prev.slice(-3), item])
        return
      }
      // 服务器侧必须使用真实 port id;若本地设备为尚未落库的临时 id,先不连线
      if (isLocalId(fromDevice.id) || isLocalId(toDevice.id)) {
        const item: IssueItem = {
          id: localId(),
          severity: 'warning',
          message: lt('save.wait'),
          deviceId: fromDevice.id,
        }
        setIssues((prev) => [...prev.slice(-3), item])
        return
      }
      const fromServerPort = ports.find((p) => p.device_id === Number(fromDevice.id) && p.direction === 'out')
      const toServerPort = ports.find((p) => p.device_id === Number(toDevice.id) && p.direction === 'in')
      if (!fromServerPort || !toServerPort) return
      try {
        const created = await api.model.connect(projectId, {
          from_port_id: fromServerPort.id,
          to_port_id: toServerPort.id,
          conn_type: carrierToConnType(fromPort.carrier) ?? 'electric_line',
        })
        const newConn: LocalConnection = {
          id: String(created.id),
          fromDeviceId: fromDevice.id,
          fromHandle: conn.sourceHandle,
          toDeviceId: toDevice.id,
          toHandle: conn.targetHandle,
          carrier: fromPort.carrier,
        }
        setConnections((prev) => [...prev, newConn])
        setSelectedConnId(newConn.id)
        setSelectedDeviceId(null)
        setIssues((prev) => prev.filter((i) => i.deviceId !== fromDevice.id && i.deviceId !== toDevice.id))
        setSaveState('saved')
        setAutosave('saved')
      } catch (err) {
        const item: IssueItem = {
          id: localId(),
          severity: 'error',
          message: translateError(toApiError(err)),
          deviceId: fromDevice.id,
        }
        setIssues((prev) => [...prev.slice(-3), item])
      }
    },
    [devices, connections, ports, projectId, connectionLabel, setAutosave],
  )

  const disconnectConn = useCallback(
    async (connId: string) => {
      try {
        await api.model.disconnect(projectId, Number(connId))
        setConnections((prev) => prev.filter((c) => c.id !== connId))
        setSelectedConnId((cur) => (cur === connId ? null : cur))
        setSaveState('saved')
        setAutosave('saved')
      } catch (err) {
        setLastError(translateError(toApiError(err)))
        setSaveState('error')
        setAutosave('error')
      }
    },
    [projectId, setAutosave],
  )

  // -- 布局(与拓扑分离:仅坐标,不触发脏标记) --------------------------
  // 注:onNodeDragStop 的 event 为 DOM MouseEvent|TouchEvent(@xyflow/react 类型)。
  const handleNodeDragStop = useCallback(
    (_: MouseEvent | TouchEvent, node: RFNode) => {
      setDevices((prev) => prev.map((d) => (d.id === node.id ? { ...d, position: node.position } : d)))
      const next = loadLayout(projectId)
      next[node.id] = node.position
      saveLayout(projectId, next)
    },
    [projectId],
  )

  // -- 定位(选择 + 视野跳转) ------------------------------------------
  const locateDevice = useCallback(
    (deviceId: string) => {
      setSelectedDeviceId(deviceId)
      setSelectedConnId(null)
      void fitView({ nodes: [{ id: deviceId }], padding: 0.4, duration: 300 })
    },
    [fitView],
  )

  const dismissIssue = useCallback((id: string) => {
    setIssues((prev) => prev.filter((i) => i.id !== id))
  }, [])

  // -- 校验 ------------------------------------------------------------
  const runValidate = useCallback(async () => {
    setValidating(true)
    setValidateError(null)
    try {
      const result = await api.model.validate(projectId)
      setValidateResult(result.diagnostics)
    } catch (err) {
      setValidateError(translateError(toApiError(err)))
      setValidateResult([])
    } finally {
      setValidating(false)
    }
  }, [projectId])

  const locateDiagnostic = useCallback(
    (diag: Diagnostic) => {
      const loc = diag.location
      if (!loc || loc.object_id === null) return
      if (loc.object_type === 'connection') {
        const conn = connections.find((c) => c.id === loc.object_id)
        if (conn) {
          setSelectedConnId(conn.id)
          setSelectedDeviceId(null)
          void fitView({
            nodes: [conn.fromDeviceId, conn.toDeviceId].map((id) => ({ id })),
            padding: 0.4,
            duration: 300,
          })
        }
        return
      }
      if (loc.object_type === 'device') {
        locateDevice(loc.object_id)
      }
    },
    [connections, fitView, locateDevice],
  )

  // -- 拖拽放置 --------------------------------------------------------
  const handleDragOver = useCallback((event: ReactDragEvent) => {
    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
  }, [])

  const handleDrop = useCallback(
    (event: ReactDragEvent) => {
      event.preventDefault()
      const typeId = event.dataTransfer.getData('application/ies-device-type')
      if (!typeId) return
      const spec = specOf(typeId)
      if (!spec) return
      addDevice(spec, screenToFlowPosition({ x: event.clientX, y: event.clientY }))
    },
    [addDevice, screenToFlowPosition, specOf],
  )

  const clearSelection = useCallback(() => {
    setSelectedDeviceId(null)
    setSelectedConnId(null)
  }, [])

  // -- ReactFlow 节点/边 -----------------------------------------------
  const nodeTypes = useMemo(() => ({ device: DeviceNode }), [])

  const nodes: RFNode<DeviceNodeData, 'device'>[] = useMemo(
    () =>
      devices.map((d) => ({
        id: d.id,
        type: 'device' as const,
        position: d.position,
        selected: d.id === selectedDeviceId,
        data: {
          device: d,
          spec: specOf(d.deviceType) ?? EMPTY_SPEC,
          locale,
          onSelect: (deviceId: string) => {
            setSelectedDeviceId(deviceId)
            setSelectedConnId(null)
          },
        },
      })),
    [devices, selectedDeviceId, specOf, locale],
  )

  const edges: RFEdge[] = useMemo(
    () =>
      connections.map((c) => ({
        id: c.id,
        source: c.fromDeviceId,
        sourceHandle: c.fromHandle,
        target: c.toDeviceId,
        targetHandle: c.toHandle,
        label: connectionLabel(c.carrier),
        labelStyle: { fontSize: 10, fontWeight: 600, fill: CARRIER_COLOR[c.carrier] ?? '#525c69' },
        labelBgStyle: { fill: '#ffffff', fillOpacity: 0.85 },
        labelBgPadding: [4, 2] as [number, number],
        labelBgBorderRadius: 3,
        markerEnd: { type: MarkerType.ArrowClosed, color: CARRIER_COLOR[c.carrier] ?? '#525c69' },
        style: { stroke: CARRIER_COLOR[c.carrier] ?? '#525c69', strokeWidth: 1.8 },
        selected: c.id === selectedConnId,
      })),
    [connections, selectedConnId, connectionLabel],
  )

  const selectedDevice = selectedDeviceId ? devices.find((d) => d.id === selectedDeviceId) ?? null : null
  const selectedConn = selectedConnId ? connections.find((c) => c.id === selectedConnId) ?? null : null
  const hasDevices = devices.length > 0

  return (
    <div className="mp-page">
      <div className="mp-topbar">
        <div className="mp-topbar__left">
          <h2 className="mp-topbar__title">{t('ies.modeling.title')}</h2>
          <SaveStateChip state={saveState} />
        </div>
        <div className="mp-toolbar">
          <IconButton aria-label={lt('toolbar.zoom_in')} onClick={() => void zoomIn({ duration: 200 })}>
            <Icon name="plus" size={15} />
          </IconButton>
          <IconButton aria-label={lt('toolbar.zoom_out')} onClick={() => void zoomOut({ duration: 200 })}>
            <Icon name="search" size={15} />
          </IconButton>
          <IconButton aria-label={lt('toolbar.fit')} onClick={() => void fitView({ padding: 0.2, duration: 300 })}>
            <Icon name="info" size={15} />
          </IconButton>
          <Button
            variant="secondary"
            size="sm"
            icon={validating ? undefined : 'check'}
            loading={validating}
            onClick={() => void runValidate()}
            disabled={validating}
          >
            {t('ies.modeling.graph_validate')}
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => void reloadGraph()}
            disabled={loading || offline}
            title={offline ? lt('save.error') : undefined}
          >
            {t('ies.common.refresh')}
          </Button>
        </div>
      </div>

      {saveState === 'error' && lastError ? (
        <Alert variant="error" closable onClose={() => setLastError(null)}>
          {lastError}
        </Alert>
      ) : null}

      {loadError ? (
        <Alert variant="warning" title={lt('load.error', { reason: loadError })} closable onClose={() => setLoadError(null)}>
          <Button variant="secondary" size="sm" onClick={() => void reloadGraph()}>
            {t('ies.common.retry')}
          </Button>
        </Alert>
      ) : null}

      <div className="mp-body">
        <Palette
          specs={deviceTypes ?? []}
          loading={deviceTypes === null && !typesError}
          error={typesError}
          onRetry={() => {
            setTypesError(false)
            setDeviceTypes(null)
            void api.model
              .deviceTypes()
              .then(setDeviceTypes)
              .catch(() => setTypesError(true))
          }}
        />

        <div className="mp-canvas-wrap" onDragOver={handleDragOver} onDrop={handleDrop}>
          <div className={`mp-canvas ${hasDevices ? 'mp-canvas--has-devices' : ''}`}>
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              onConnect={handleConnect}
              onNodeDragStop={handleNodeDragStop}
              onNodeClick={(_, node) => {
                setSelectedDeviceId(node.id)
                setSelectedConnId(null)
              }}
              onEdgeClick={(_, edge) => {
                setSelectedConnId(edge.id)
                setSelectedDeviceId(null)
              }}
              onPaneClick={clearSelection}
              fitView
              minZoom={0.2}
              maxZoom={2.5}
              deleteKeyCode={null}
              defaultEdgeOptions={{ type: 'default' }}
            >
              <Background variant={BackgroundVariant.Dots} gap={18} size={1} />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
          {!hasDevices && !loading ? <div className="mp-canvas-empty">{lt('canvas.placeholder')}</div> : null}
          {issues.length > 0 ? (
            <div className="mp-issues" role="status" aria-live="polite">
              {issues.map((item) => (
                <div key={item.id} className={`mp-issues__item mp-issues__item--${item.severity}`}>
                  <Icon name={item.severity === 'error' ? 'warning' : 'info'} size={14} />
                  <span>{item.message}</span>
                  {item.deviceId ? (
                    <Button variant="ghost" size="sm" className="mp-issues__locate" onClick={() => locateDevice(item.deviceId!)}>
                      {lt('diag.locate')}
                    </Button>
                  ) : null}
                  <IconButton aria-label={lt('diag.close')} size="sm" onClick={() => dismissIssue(item.id)}>
                    <Icon name="cross" size={12} />
                  </IconButton>
                </div>
              ))}
            </div>
          ) : null}
        </div>

        <div className="mp-side-stack">
          {selectedConn ? (
            <ConnectionPanel
              conn={selectedConn}
              devices={devices}
              labelOf={connectionLabel}
              onDisconnect={() => disconnectConn(selectedConn.id)}
              onClose={() => setSelectedConnId(null)}
            />
          ) : null}
          {selectedDevice ? (
            <DeviceSidebar
              device={selectedDevice}
              spec={specOf(selectedDevice.deviceType) ?? EMPTY_SPEC}
              onPatch={(patch) => patchDevice(selectedDevice.id, patch)}
              onDelete={() => deleteDevice(selectedDevice.id)}
              onClose={() => setSelectedDeviceId(null)}
            />
          ) : null}
          {validateResult !== null ? (
            <DiagnosticsPanel
              diagnostics={validateResult}
              error={validateError}
              busy={validating}
              onRun={() => void runValidate()}
              onLocate={locateDiagnostic}
              onClose={() => setValidateResult(null)}
            />
          ) : null}
        </div>
      </div>
    {flash ? (
        <div
          role="status"
          style={{
            position: 'fixed',
            right: 'var(--ies-space-5)',
            bottom: 'var(--ies-space-5)',
            zIndex: 90,
          }}
        >
          <Alert variant="success" closable onClose={() => setFlash(null)}>
            {flash}
          </Alert>
        </div>
      ) : null}
    </div>
  )
}

/** 注册表缺失时兜底的空 spec(节点仍可渲染,参数表单为空)。 */
const EMPTY_SPEC: ExtendedDeviceTypeSpec = {
  type_id: '',
  version: '',
  name_zh: '',
  name_en: '',
  energy_carriers: [],
  is_load: false,
  parameters: {},
}

// ===========================================================================
// 保存状态指示
// ===========================================================================

const SAVE_STATE_TEXT: Record<SaveState, { cls: string; label: string }> = {
  idle: { cls: 'mp-save-state', label: 'save.auto' },
  dirty: { cls: 'mp-save-state mp-save-state--dirty', label: 'save.dirty' },
  saving: { cls: 'mp-save-state mp-save-state--saving', label: 'save.saving' },
  saved: { cls: 'mp-save-state mp-save-state--saved', label: 'save.saved' },
  error: { cls: 'mp-save-state mp-save-state--error', label: 'save.error' },
  conflict: { cls: 'mp-save-state mp-save-state--conflict', label: 'save.conflict' },
}

function SaveStateChip({ state }: { state: SaveState }) {
  const cfg = SAVE_STATE_TEXT[state]
  return (
    <span className={cfg.cls} role="status" aria-live="polite">
      <span className="mp-save-state__dot" aria-hidden="true" />
      {lt(cfg.label)}
    </span>
  )
}

// ===========================================================================
// 设备面板(注册表)
// ===========================================================================

function Palette({
  specs,
  loading,
  error,
  onRetry,
}: {
  specs: DeviceTypeSpec[]
  loading: boolean
  error: boolean
  onRetry: () => void
}) {
  const { locale } = useI18n()
  if (loading) {
    return (
      <aside className="mp-palette" aria-label={lt('panel.title')}>
        <div className="mp-palette__head">
          <h3 className="mp-palette__title">{lt('panel.title')}</h3>
        </div>
        <div className="mp-palette__list">
          <Spinner size="md" />
        </div>
      </aside>
    )
  }
  if (error) {
    return (
      <aside className="mp-palette" aria-label={lt('panel.title')}>
        <div className="mp-palette__head">
          <h3 className="mp-palette__title">{lt('panel.title')}</h3>
        </div>
        <div className="mp-palette__list">
          <p>{lt('panel.load_failed')}</p>
          <Button variant="secondary" size="sm" onClick={onRetry}>
            {lt('panel.retry')}
          </Button>
        </div>
      </aside>
    )
  }
  return (
    <aside className="mp-palette" aria-label={lt('panel.title')}>
      <div className="mp-palette__head">
        <h3 className="mp-palette__title">{lt('panel.title')}</h3>
        <p className="mp-palette__hint">{lt('panel.hint')}</p>
      </div>
      <div className="mp-palette__list">
        {specs.length === 0 ? <p>{lt('panel.none')}</p> : null}
        {specs.map((spec) => {
          const name = locale === 'zh' ? spec.name_zh : spec.name_en
          const carriers = spec.energy_carriers.join(' / ')
          return (
            <div
              key={spec.type_id}
              className="mp-palette-item"
              draggable
              onDragStart={(event) => {
                event.dataTransfer.setData('application/ies-device-type', spec.type_id)
                event.dataTransfer.effectAllowed = 'copy'
              }}
              aria-label={lt('canvas.drop_new', { type: name })}
              title={lt('canvas.drop_new', { type: name })}
            >
              <span className="mp-palette-item__name">{name}</span>
              <span className="mp-palette-item__meta">{carriers}</span>
            </div>
          )
        })}
      </div>
    </aside>
  )
}

// ===========================================================================
// 连接详情面板
// ===========================================================================

function ConnectionPanel({
  conn,
  devices,
  labelOf,
  onDisconnect,
  onClose,
}: {
  conn: LocalConnection
  devices: LocalDevice[]
  labelOf: (carrier: LocalConnection['carrier']) => string
  onDisconnect: () => void
  onClose: () => void
}) {
  const { t } = useI18n()
  const from = devices.find((d) => d.id === conn.fromDeviceId)
  const to = devices.find((d) => d.id === conn.toDeviceId)
  const fromHandle = parseHandle(conn.fromHandle)
  const toHandle = parseHandle(conn.toHandle)
  return (
    <section className="mp-sidebar" aria-label={t('ies.modeling.connection')}>
      <header className="mp-sidebar__head">
        <h3 className="mp-sidebar__title">{t('ies.modeling.connection')}</h3>
        <IconButton aria-label={t('ies.common.close')} onClick={onClose}>
          <Icon name="cross" size={14} />
        </IconButton>
      </header>
      <div className="mp-sidebar__body">
        <div className="ies-form-field">
          <span className="ies-form-label">{lt('conn.from')}</span>
          <div>
            {from?.name ?? '—'} · {labelOf(conn.carrier)} · {fromHandle ? lt(`port.direction_${fromHandle.direction}`) : '—'}
          </div>
        </div>
        <div className="ies-form-field">
          <span className="ies-form-label">{lt('conn.to')}</span>
          <div>
            {to?.name ?? '—'} · {labelOf(conn.carrier)} · {toHandle ? lt(`port.direction_${toHandle.direction}`) : '—'}
          </div>
        </div>
        <div className="ies-form-field">
          <span className="ies-form-label">{t('ies.modeling.connection')}</span>
          <div>{t(`ies.modeling.conn_${carrierToConnType(conn.carrier) ?? 'electric_line'}`)}</div>
        </div>
      </div>
      <footer className="mp-sidebar__footer">
        <Button variant="danger" size="sm" onClick={onDisconnect}>
          <Icon name="cross" size={13} />
          {t('ies.modeling.disconnect')}
        </Button>
      </footer>
    </section>
  )
}

// ===========================================================================
// 设备参数侧栏(schema 驱动表单)
// ===========================================================================

function DeviceSidebar({
  device,
  spec,
  onPatch,
  onDelete,
  onClose,
}: {
  device: LocalDevice
  spec: ExtendedDeviceTypeSpec
  onPatch: (patch: Partial<Pick<LocalDevice, 'name' | 'kind' | 'fidelity' | 'params'>>) => void
  onDelete: () => void
  onClose: () => void
}) {
  const { t } = useI18n()
  const [nameDraft, setNameDraft] = useState(device.name)
  const [paramDrafts, setParamDrafts] = useState<Record<string, string>>(() => draftsOf(device.params))
  const [errors, setErrors] = useState<Record<string, string | null>>({})
  const [armed, setArmed] = useState(false)

  // 切换设备时重置表单草稿
  useEffect(() => {
    setNameDraft(device.name)
    setParamDrafts(draftsOf(device.params))
    setErrors({})
    setArmed(false)
  }, [device.id, device.name, device.params])

  const paramKeys = useMemo(() => Object.keys(spec.parameters), [spec])

  const commitParam = (key: string, specParam: ParamSpecExt) => {
    const raw = (paramDrafts[key] ?? '').trim()
    // 枚举类参数: 保留字符串字面量(如 'heating'),不做 Number 强制。
    if (Array.isArray(specParam.enum) && specParam.enum.length > 0) {
      const allowed = specParam.enum.map((v) => String(v))
      if (raw === '') {
        const next = { ...device.params }
        delete next[key]
        onPatch({ params: next })
        setErrors((prev) => ({ ...prev, [key]: null }))
        return
      }
      if (!allowed.includes(raw)) {
        setErrors((prev) => ({
          ...prev,
          [key]: lt('sidebar.param_invalid', { min: allowed.join('/'), max: allowed.join('/') }),
        }))
        return
      }
      onPatch({ params: { ...device.params, [key]: raw } })
      setErrors((prev) => ({ ...prev, [key]: null }))
      return
    }
    if (raw === '') {
      const next = { ...device.params }
      delete next[key]
      onPatch({ params: next })
      setErrors((prev) => ({ ...prev, [key]: null }))
      return
    }
    const value = Number(raw)
    if (!Number.isFinite(value)) {
      setErrors((prev) => ({ ...prev, [key]: lt('sidebar.param_invalid', { min: specParam.min ?? '—', max: specParam.max ?? '—' }) }))
      return
    }
    if (specParam.min !== null && value < specParam.min) {
      setErrors((prev) => ({ ...prev, [key]: lt('sidebar.param_invalid', { min: specParam.min ?? '—', max: specParam.max ?? '—' }) }))
      return
    }
    if (specParam.max !== null && value > specParam.max) {
      setErrors((prev) => ({ ...prev, [key]: lt('sidebar.param_invalid', { min: specParam.min ?? '—', max: specParam.max ?? '—' }) }))
      return
    }
    onPatch({ params: { ...device.params, [key]: value } })
    setErrors((prev) => ({ ...prev, [key]: null }))
  }

  const restoreDefault = (key: string, specParam: ParamSpecExt) => {
    const value = defaultParamValue(specParam as DeviceTypeSpec['parameters'][string], device.kind)
    setParamDrafts((prev) => ({ ...prev, [key]: value === null ? '' : String(value) }))
    if (value === null) {
      const next = { ...device.params }
      delete next[key]
      onPatch({ params: next })
    } else {
      onPatch({ params: { ...device.params, [key]: value } })
    }
    setErrors((prev) => ({ ...prev, [key]: null }))
  }

  const switchKind = (kind: LocalDevice['kind']) => {
    if (kind === device.kind) return
    // 保留用户已修改的参数,仅替换"仍为另一属性默认值"的参数
    const otherDefaults = buildDefaultParams(spec as DeviceTypeSpec, kind === 'existing' ? 'new' : 'existing')
    const nextParams: Record<string, unknown> = { ...device.params }
    for (const [key, specParam] of Object.entries(spec.parameters)) {
      const current = device.params[key]
      const other = otherDefaults[key]
      const target = defaultParamValue(specParam as DeviceTypeSpec['parameters'][string], kind)
      if (current === undefined || (typeof current === 'number' && current === other) || current === other) {
        if (target === null) delete nextParams[key]
        else nextParams[key] = target
      }
    }
    onPatch({ kind, params: nextParams })
  }

  const onDeleteClick = () => {
    if (!armed) {
      setArmed(true)
      window.setTimeout(() => setArmed(false), 3000)
      return
    }
    onDelete()
  }

  return (
    <section className="mp-sidebar" aria-label={t('ies.modeling.edit_device')}>
      <header className="mp-sidebar__head">
        <h3 className="mp-sidebar__title">{t('ies.modeling.edit_device')}</h3>
        <IconButton aria-label={t('ies.common.close')} onClick={onClose}>
          <Icon name="cross" size={14} />
        </IconButton>
      </header>
      <div className="mp-sidebar__body">
        <p className="mp-sidebar__section">{spec.type_id}</p>

        {/* 设备名称 */}
        <div className="ies-form-field">
          <label className="ies-form-label" htmlFor="mp-device-name">
            {t('ies.modeling.device_name')}
          </label>
          <Input
            id="mp-device-name"
            value={nameDraft}
            onChange={(event) => setNameDraft(event.target.value)}
            onBlur={() => {
              const name = nameDraft.trim()
              if (name && name !== device.name) onPatch({ name })
            }}
          />
        </div>

        {/* 存量 / 新增 */}
        <div className="ies-form-field">
          <span className="ies-form-label" id="mp-kind-label">
            {t('ies.modeling.kind')}
          </span>
          <div className="mp-kind-switch" role="radiogroup" aria-labelledby="mp-kind-label">
            <button
              type="button"
              role="radio"
              aria-checked={device.kind === 'existing'}
              className={`mp-kind-option ${device.kind === 'existing' ? 'mp-kind-option--active mp-kind-option--existing' : ''}`}
              onClick={() => switchKind('existing')}
            >
              <Icon name="clock" size={13} />
              {t('ies.modeling.kind_existing')}
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={device.kind === 'new'}
              className={`mp-kind-option ${device.kind === 'new' ? 'mp-kind-option--active' : ''}`}
              onClick={() => switchKind('new')}
            >
              <Icon name="plus" size={13} />
              {t('ies.modeling.kind_new')}
            </button>
          </div>
        </div>

        {/* 模型精度 */}
        <div className="ies-form-field">
          <label className="ies-form-label" htmlFor="mp-fidelity">
            {t('ies.modeling.fidelity')}
          </label>
          <Select
            id="mp-fidelity"
            value={device.fidelity}
            onChange={(event) => onPatch({ fidelity: event.target.value as Fidelity })}
          >
            <option value="low">{t('ies.modeling.fidelity_low')}</option>
            <option value="medium">{t('ies.modeling.fidelity_medium')}</option>
            <option value="high">{t('ies.modeling.fidelity_high')}</option>
          </Select>
        </div>

        {/* 参数(schema 驱动) */}
        <p className="mp-sidebar__section">{t('ies.modeling.params')}</p>
        {paramKeys.length === 0 ? <p>{lt('sidebar.params_none')}</p> : null}
        {paramKeys.map((key) => {
          const p = spec.parameters[key]
          const range = `${p.min ?? '—'} ~ ${p.max ?? '—'}`
          const hasEnum = Array.isArray(p.enum) && p.enum.length > 0
          return (
            <div className="mp-param" key={key}>
              <div className="mp-param__head">
                <span className="mp-param__name">{key}</span>
                {p.unit ? <span className="mp-param__unit">{p.unit}</span> : null}
                <span className="mp-param__actions">
                  <IconButton
                    aria-label={lt('sidebar.restore_default')}
                    size="sm"
                    title={lt('sidebar.restore_default')}
                    onClick={() => restoreDefault(key, p)}
                  >
                    <Icon name="clock" size={12} />
                  </IconButton>
                </span>
              </div>
              <div className="mp-param__input-wrap">
                {hasEnum ? (
                  <Select
                    className="mp-param__input"
                    value={String(paramDrafts[key] ?? '')}
                    aria-label={`${key}${p.unit ? ` (${p.unit})` : ''}`}
                    aria-describedby={errors[key] ? `mp-param-err-${key}` : undefined}
                    invalid={Boolean(errors[key])}
                    onChange={(event) => {
                      const v = event.target.value
                      setParamDrafts((prev) => ({ ...prev, [key]: v }))
                      // 下拉选择直接提交(避免浏览器对 number 输入的非数字清空)
                      const allowed = (p.enum ?? []).map((x) => String(x))
                      if (v === '') {
                        const next = { ...device.params }
                        delete next[key]
                        onPatch({ params: next })
                      } else if (allowed.includes(v)) {
                        onPatch({ params: { ...device.params, [key]: v } })
                      }
                      setErrors((prev) => ({ ...prev, [key]: null }))
                    }}
                  >
                    <option value="">{lt('sidebar.param_unset')}</option>
                    {(p.enum ?? []).map((opt) => {
                      const label = String(opt)
                      return (
                        <option key={label} value={label}>
                          {label}
                        </option>
                      )
                    })}
                  </Select>
                ) : (
                  <Input
                    className="mp-param__input"
                    type="number"
                    value={paramDrafts[key] ?? ''}
                    aria-label={`${key}${p.unit ? ` (${p.unit})` : ''}`}
                    aria-describedby={errors[key] ? `mp-param-err-${key}` : undefined}
                    invalid={Boolean(errors[key])}
                    onChange={(event) => setParamDrafts((prev) => ({ ...prev, [key]: event.target.value }))}
                    onBlur={() => commitParam(key, p)}
                  />
                )}
                <span className="mp-param__range" title={p.help_key}>
                  {range}
                </span>
              </div>
              {errors[key] ? (
                <div id={`mp-param-err-${key}`} className="mp-param__range" role="alert" style={{ color: 'var(--ies-danger-text)' }}>
                  {errors[key]}
                </div>
              ) : null}
              {p.is_optimizable ? (
                <div className="mp-param__opt">
                  <Icon name="info" size={11} />
                  {lt('sidebar.param_opt')}
                </div>
              ) : null}
            </div>
          )
        })}
      </div>
      <footer className="mp-sidebar__footer">
        <Button variant="danger" size="sm" className="mp-sidebar__delete" onClick={onDeleteClick}>
          <Icon name="trash" size={13} />
          {armed ? `${t('ies.common.confirm')}?` : t('ies.modeling.delete_device')}
        </Button>
      </footer>
    </section>
  )
}

function draftsOf(params: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {}
  for (const [key, value] of Object.entries(params)) {
    if (typeof value === 'number') out[key] = String(value)
    else if (typeof value === 'string') out[key] = value
  }
  return out
}

// ===========================================================================
// 校验诊断面板
// ===========================================================================

function DiagnosticsPanel({
  diagnostics,
  error,
  busy,
  onRun,
  onLocate,
  onClose,
}: {
  diagnostics: Diagnostic[]
  error: string | null
  busy: boolean
  onRun: () => void
  onLocate: (diag: Diagnostic) => void
  onClose: () => void
}) {
  const { t } = useI18n()
  return (
    <section className="mp-diagnostics" aria-label={lt('diag.title')}>
      <header className="mp-diagnostics__head">
        <h3 className="mp-diagnostics__title">
          {lt('diag.title')} {diagnostics.length > 0 ? `(${lt('diag.count', { count: diagnostics.length })})` : ''}
        </h3>
        <div className="ies-flex" style={{ gap: 'var(--ies-space-1)' }}>
          <IconButton aria-label={lt('diag.locate')} size="sm" onClick={onRun} disabled={busy} title={t('ies.modeling.graph_validate')}>
            <Icon name="check" size={13} />
          </IconButton>
          <IconButton aria-label={t('ies.common.close')} size="sm" onClick={onClose}>
            <Icon name="cross" size={13} />
          </IconButton>
        </div>
      </header>
      <div className="mp-diagnostics__list">
        {busy ? <Spinner size="md" /> : null}
        {!busy && error ? <p className="mp-diagnostics__empty">{error}</p> : null}
        {!busy && !error && diagnostics.length === 0 ? <p className="mp-diagnostics__empty">{lt('diag.empty')}</p> : null}
        {!busy &&
          diagnostics.map((diag, index) => (
            <div key={diag.code + index} className="mp-diagnostic">
              <div className="mp-diagnostic__head">
                <SeverityBadge severity={diag.severity} />
                <span className="mp-diagnostic__code">{diag.code}</span>
              </div>
              <div className="mp-diagnostic__msg">{translateDiagnostic(diag)}</div>
              {diag.location && diag.location.object_id !== null ? (
                <div className="mp-diagnostic__head" style={{ marginTop: 'var(--ies-space-1)' }}>
                  <Button variant="ghost" size="sm" onClick={() => onLocate(diag)}>
                    {lt('diag.locate')}
                  </Button>
                </div>
              ) : null}
            </div>
          ))}
      </div>
    </section>
  )
}

// ===========================================================================
// 不兼容连接诊断文案
// ===========================================================================

function incompatMessage(
  reason: ReturnType<typeof checkConnection>['reason'],
  fromDevice: LocalDevice,
  fromPort: PortDef,
  toDevice: LocalDevice,
  toPort: PortDef,
  labelOf: (carrier: LocalConnection['carrier']) => string,
): string {
  switch (reason) {
    case 'type':
      return lt('conn.incompatible_type', { a: labelOf(fromPort.carrier), b: labelOf(toPort.carrier) })
    case 'direction':
      return lt('conn.incompatible_direction', {
        a: `${fromDevice.name}(${lt(`port.direction_${fromPort.direction}`)})`,
        b: `${toDevice.name}(${lt(`port.direction_${toPort.direction}`)})`,
      })
    case 'same_device':
      return lt('conn.incompatible_same_device')
    case 'duplicate':
      return lt('conn.incompatible_duplicate')
    case 'solar':
      return lt('conn.incompatible_solar')
    default:
      return lt('conn.incompatible_direction', { a: '?', b: '?' })
  }
}
