/**
 * 建模画布页面(/projects/:id/model)。
 *
 * 能力:
 * - 左侧设备面板(项目作用域目录 /api/projects/{id}/device-types),拖拽添加到画布;
 *   节点展示 名称/类型/存量新增徽章/容量。
 * - 端口间连线:按 能源类型 + 方向(out 源 → in 汇)校验,不兼容连接被阻止
 *   并生成可定位诊断(可跳转到相关设备)。
 * - 设备参数侧栏:按设备类型 schema 渲染表单(单位/范围/默认值/帮助键),
 *   存量/新增切换(默认值随属性切换)。
 * - 保存:设备/连接变更即时经草稿语义命令提交
 *   (PUT /api/projects/{id}/draft, model.upsert_device/remove_device/
 *   upsert_connection/remove_connection); 设备与连接均以名称为键。
 * - 布局与拓扑分离:节点坐标独立持久化到 localStorage,不进入语义内容。
 * - 工具栏:缩放 / 适应视图 / 校验(调用 validation/run 显示诊断列表,可定位)。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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
import { useNavigate } from 'react-router-dom'

import {
  getModel,
  listDeviceTypes,
  removeConnection,
  removeDevice,
  sendCommands,
  upsertConnection,
  upsertDevice,
  validateModel,
} from '../features/modeling'
import { listDatasets, listVersions } from '../features/datasets'
import { ApiError } from '../types'
import type { Diagnostic, DeviceTypeSpec, ParameterSpec } from '../types'
import { useI18n, translateDiagnostic, errorMessage } from '../i18n'
import { pt } from '../i18n/pageMessages'
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
  checkConnection,
  connectionFromDraft,
  defaultDeviceName,
  defaultParamValue,
  derivePorts,
  deviceFromDraft,
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

/** 参数规格。 */
type ParamSpecExt = ParameterSpec
type ExtendedDeviceTypeSpec = DeviceTypeSpec

const CARRIER_COLOR: Record<string, string> = {
  electricity: '#0e5cad',
  heat: '#c2410c',
  cool: '#0891b2',
  gas: '#8a5a00',
}

// ===========================================================================
// 主页面
// ===========================================================================

export default function ModelPage() {
  const { projectId } = useWorkbench()
  return (
    <ReactFlowProvider>
      {/* 项目键控: 切换项目即全新挂载, 旧项目的读在途标记/草稿/就绪态不继承,
          旧读回包不得发布到新项目; 项目内刷新不受影响。 */}
      <ModelCanvas key={projectId} projectId={projectId} />
    </ReactFlowProvider>
  )
}

// ===========================================================================
// 画布主逻辑
// ===========================================================================

function ModelCanvas({ projectId }: { projectId: number }) {
  const { t, locale } = useI18n()
  const { setAutosave, offline } = useWorkbench()
  const { screenToFlowPosition, fitView } = useReactFlow()
  const navigate = useNavigate()

  // -- 注册表与模型状态 ------------------------------------------------
  const [deviceTypes, setDeviceTypes] = useState<ExtendedDeviceTypeSpec[] | null>(null)
  const [typesError, setTypesError] = useState(false)
  const [devices, setDevices] = useState<LocalDevice[]>([])
  const [connections, setConnections] = useState<LocalConnection[]>([])
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  // 图就绪(当前项目一次真实成功的模型读取, 含可解释的描述符):
  // 未就绪时不渲染可写画布/面板, 读写状态不标已保存; 成功前失败保留旧输入
  // (不清为空图), 刷新失败不重置保存/冲突态。项目切换时重置。
  const [graphReady, setGraphReady] = useState(false)
  // 就绪镜像(reload 回调内读取, 避免 graphReady 进依赖导致加载 effect 重放)。
  const graphReadyRef = useRef(false)
  // 读在途(单飞: 重试/刷新按钮加载中禁用, 回调内再保险, 不搭请求框架)。
  const loadInflightRef = useRef(false)
  // 归属作废(卸载/键控重挂): 旧读回包不得发布图/修订/就绪/错误,
  // 也不得再碰共享 autosave 状态。setup 内复位(StrictMode 开发重放
  // setup→cleanup→setup 后仍为有效归属, 不得删 StrictMode 或此保护)。
  const cancelledRef = useRef(false)
  useEffect(() => {
    cancelledRef.current = false
    return () => {
      cancelledRef.current = true
    }
  }, [])
  // 读阻塞(未就绪 / 刷新在途 / 读失败未恢复): 可写控件只读或隐藏, 不接受变更。
  const readBlocked = !graphReady || loading || loadError !== null

  // 已有设备后继续新增 → 自动 fit 全部节点, 避免节点堆叠遮挡端口句柄
  // (ReactFlow fitView prop 只在 hasDevices false→true 边沿触发一次, 后续
  // 新增不重 fit; 连续放置的节点会堆在画布同侧互相遮挡, 连线手势 mouse.down
  // 命中节点而非句柄, 见 QA-E2E-01 场景 3 专项调试)。
  // 设备数量增加沿触发: 首个设备由 ReactFlow prop 负责, 删除/reload 不触发;
  // duration: 0 消除 300ms 动画期间下一次拖放坐标换算的竞态。
  const AUTO_FIT_OPTIONS = useMemo(() => ({ padding: 0.2, maxZoom: 1 }) as const, [])
  const previousDeviceCount = useRef(0)
  useEffect(() => {
    const previous = previousDeviceCount.current
    const current = devices.length
    if (previous > 0 && current > previous) {
      void fitView({ ...AUTO_FIT_OPTIONS, duration: 0 })
    }
    previousDeviceCount.current = current
  }, [devices.length, fitView, AUTO_FIT_OPTIONS])

  // -- 选择与侧栏 ------------------------------------------------------
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(null)
  const [selectedConnId, setSelectedConnId] = useState<string | null>(null)

  // -- 保存 ------------------------------------------------------------
  const [saveState, setSaveState] = useState<SaveState>('idle')
  const [lastError, setLastError] = useState<string | null>(null)
  // 草稿修订(乐观锁): 已加载值, 写操作直接使用, 成功后以后端返回为准;
  // ref 镜像防连续写之间闭包过期, 写前不再 GET(单 PUT 即原子提交)。
  const revisionRef = useRef<number>(1)
  const devicesRef = useRef(devices)
  devicesRef.current = devices
  const devicePatchQueue = useRef<Promise<LocalDevice | undefined>>(Promise.resolve(undefined))
  /** 写冲突(409)进冲突态: 提示加载服务器版本(本地画布未动, 与服务端一致)。 */
  const handleWriteError = useCallback(
    (err: unknown, onOther: () => void) => {
      if (err instanceof ApiError && err.status === 409) {
        setSaveState('conflict')
        setAutosave('error')
      } else {
        onOther()
      }
    },
    [setAutosave],
  )

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
  // 只读入口: 描述符缺失时图不可解释, 直接立错而不以空描述呈现有效连接。
  // 单飞: 未就绪重试先清旧错进入真实加载态(按钮随视图卸载); 已就绪刷新
  // 保留陈旧提示直至成功恢复, 期间重试按钮禁用, 不叠加请求、不乱序覆盖。
  const reloadGraph = useCallback(async () => {
    if (loadInflightRef.current) return
    loadInflightRef.current = true
    setLoading(true)
    if (!graphReadyRef.current) setLoadError(null)
    if (deviceTypes === null) {
      setLoadError(lt('panel.load_failed'))
      setLoading(false)
      loadInflightRef.current = false
      return
    }
    try {
      // 2.0 唯一项目建模事实: 项目视图草稿内容 model 节(名键设备与端点连接)。
      const { model, revision } = await getModel(projectId)
      // 归属已作废(切换项目/卸载): 旧回包整体丢弃, 不发布任何状态。
      if (cancelledRef.current) return
      revisionRef.current = revision
      const layout = loadLayout(projectId)
      const deviceList = model.devices.map((d, i) =>
        // 缺省布局须保证节点互不重叠(节点宽 ≥190px 含端口手柄, 手柄外伸约 7px):
        // 之前 80px 横距/60px 纵距的密集网格会让相邻节点盖住源端口,
        // 源端口中心点被邻居节点的 .mp-node__type 命中, 连线手势无法开始。
        deviceFromDraft(d, layout[d.name] ?? { x: 80 + (i % 4) * 260, y: 80 + Math.floor(i / 4) * 180 }),
      )
      const deviceByName = new Map(deviceList.map((d) => [d.name, d]))
      const connectionList = model.connections
        .map((c) => connectionFromDraft(c, deviceByName, (typeId) => typeMap[typeId] ?? null))
        .filter((c): c is LocalConnection => c !== null)
      setDevices(deviceList)
      setConnections(connectionList)
      setLoadError(null)
      setGraphReady(true)
      graphReadyRef.current = true
      setSelectedDeviceId(null)
      setSelectedConnId(null)
      setValidateResult(null)
      setSaveState('saved')
      setAutosave('saved')
    } catch (err) {
      // 读取失败(网络/权限等): 仅显示错误与重试入口; 已有画布原样保留
      // (陈旧但不伪装成当前成功读取), 从未成功则保持未就绪、不可写。
      setLoadError(errorMessage(err))
    } finally {
      setLoading(false)
      loadInflightRef.current = false
    }
  }, [projectId, setAutosave, deviceTypes, typeMap])

  // 读取重试: 描述符失败时一并重取注册表(图依赖其解释连接), 否则只重读模型。
  // 重试即进入加载态(清旧错, 按钮加载中禁用/卸载), 与读回调单飞互锁。
  const retryLoad = useCallback(() => {
    if (loadInflightRef.current) return
    if (typesError) {
      setLoadError(null)
      setLoading(true)
      setTypesError(false)
      setDeviceTypes(null)
    } else {
      void reloadGraph()
    }
  }, [typesError, reloadGraph])

  // 项目切换无需重置: 画布按 projectId 键控重挂, 状态天然全新;
  // 旧读回包由 cancelledRef 丢弃, 不会发布到新项目。

  // 设备面板按项目作用域加载(公开目录 ∪ 本项目已保存模型); 画布按
  // projectId 键控重挂, 状态天然全新, 不会跨项目复用。
  useEffect(() => {
    if (deviceTypes !== null || typesError) return
    let cancelled = false
    void listDeviceTypes(projectId)
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
    return () => {
      cancelled = true
    }
  }, [projectId, deviceTypes, typesError])

  // 草稿加载: 初挂载 / 项目切换 / 手动刷新时取数; 连接解析依赖 descriptor,
  // 故等待注册表就绪(或失败)后再请求, 之后 deviceTypes 不再变化, 无循环。
  // 注册表失败时图不可解释: 立错而不标记就绪, 重试一并重取注册表。
  useEffect(() => {
    if (deviceTypes === null && !typesError) return
    if (typesError) {
      setLoadError(lt('panel.load_failed'))
      setLoading(false)
      return
    }
    void reloadGraph()
  }, [reloadGraph, deviceTypes, typesError])

  // -- 设备增删改(2.0 草稿语义命令, 名键) --------------------------------
  /** 默认名与已有效果冲突时追加序号(服务端 upsert 按名覆盖, 不可静默复用)。 */
  const uniqueDeviceName = useCallback(
    (base: string, exceptId: string | null = null): string => {
      const taken = new Set(devicesRef.current.filter((d) => d.id !== exceptId).map((d) => d.name))
      if (!taken.has(base)) return base
      let i = 2
      while (taken.has(`${base} ${i}`)) i += 1
      return `${base} ${i}`
    },
    [],
  )

  const addDevice = useCallback(
    async (spec: ExtendedDeviceTypeSpec, position: { x: number; y: number }) => {
      try {
        // 写在途标保存中: 刷新按钮此间禁用, 读回旧修订不得覆盖本次保存。
        setSaveState('saving')
        const sameTypeCount = devices.filter((d) => d.deviceType === spec.type_id).length
        const name = uniqueDeviceName(defaultDeviceName(spec as DeviceTypeSpec, sameTypeCount, locale))
        const params = buildDefaultParams(spec as DeviceTypeSpec, 'new')
        const { revision } = await upsertDevice(
          projectId,
          {
            device_type: spec.type_id,
            name,
            params,
            kind: 'new',
          },
          revisionRef.current,
        )
        revisionRef.current = revision
        // 创建入口可能携带模板保存的显式来源声明(服务端补齐到实例 params);
        // 读回权威实例内容, 保证画布/侧栏与已保存事实一致。
        let savedParams: Record<string, unknown>
        try {
          const view = await getModel(projectId)
          const saved = (view.model.devices ?? []).find((d) => d.name === name)
          if (!saved) throw new Error(lt('load.created_missing'))
          savedParams = saved.params
        } catch (err) {
          // 写入已成功，但当前画布尚未读到权威参数；重载前不能继续写旧副本。
          setLoadError(errorMessage(err))
          throw err
        }
        const device: LocalDevice = {
          id: name,
          editorId: Symbol(),
          deviceType: spec.type_id,
          kind: 'new',
          name,
          params: savedParams,
          position,
        }
        setDevices((prev) => [...prev, device])
        setSelectedDeviceId(device.id)
        setSelectedConnId(null)
        setSaveState('saved')
        setAutosave('saved')
        setFlash(t('ies.modeling.device_added', { name }))
        window.setTimeout(() => setFlash(null), 1800)
      } catch (err) {
        handleWriteError(err, () => {
          setLastError(errorMessage(err))
          setSaveState('error')
          setAutosave('error')
        })
      }
    },
    [devices, locale, projectId, setAutosave, t, uniqueDeviceName, handleWriteError],
  )

  const patchDevice = useCallback(
    async (selectedId: string, patch: Partial<Pick<LocalDevice, 'name' | 'kind' | 'params'>>, paramKeys: string[] = []) => {
      const original = devices.find((d) => d.id === selectedId)
      if (!original) return
      // 排队时记录字段意图，执行时合并最新参数/名称/修订，避免旧整包覆盖。
      const paramChanges = paramKeys.map((key) => ({
        key, present: key in patch.params!, value: patch.params![key],
      }))
      const execute = async () => {
        if (cancelledRef.current) return
        const current = devicesRef.current.find((d) => d.editorId === original.editorId)
        if (!current) return
        const deviceId = current.id
        const params = { ...current.params }
        for (const change of paramChanges) {
          if (change.present) params[change.key] = change.value
          else delete params[change.key]
        }
        const nextName = patch.name !== undefined ? patch.name.trim() : current.name
        if (!nextName) return
        const renamed = nextName !== current.name
        const finalName = renamed ? uniqueDeviceName(nextName, deviceId) : current.name
        const next = {
          ...current,
          ...patch,
          id: finalName,
          name: finalName,
          params,
          kind: patch.kind ?? current.kind,
        }
        try {
          // 写在途标保存中: 刷新按钮此间禁用, 读回旧修订不得覆盖本次保存。
          setSaveState('saving')
          if (!renamed) {
            const { revision } = await upsertDevice(
              projectId,
              {
                device_type: current.deviceType,
                name: current.name,
                kind: next.kind,
                params: next.params,
              },
              revisionRef.current,
            )
            revisionRef.current = revision
            devicesRef.current = devicesRef.current.map((d) => (d.id === deviceId ? next : d))
            setDevices(devicesRef.current)
          } else {
            // 原地改名保留实例来源、连接和布局，不触发新建模板输入。
            const { revision } = await sendCommands(
              projectId,
              [
                { type: 'model.rename_device', payload: { name: current.name, new_name: finalName } },
                {
                  type: 'model.upsert_device',
                  payload: {
                    name: finalName,
                    device_type: current.deviceType,
                    kind: next.kind,
                    params: next.params,
                  },
                },
              ],
              revisionRef.current,
            )
            revisionRef.current = revision
            devicesRef.current = devicesRef.current.map((d) => (d.id === deviceId ? next : d))
            setDevices(devicesRef.current)
            setConnections((prev) =>
              prev.map((c) =>
                c.fromDeviceId === deviceId || c.toDeviceId === deviceId
                  ? {
                      ...c,
                      fromDeviceId: c.fromDeviceId === deviceId ? finalName : c.fromDeviceId,
                      toDeviceId: c.toDeviceId === deviceId ? finalName : c.toDeviceId,
                    }
                  : c,
              ),
            )
            // 布局跟随新键, 旧键清理
            const layout = loadLayout(projectId)
            if (layout[deviceId] !== undefined) {
              layout[finalName] = layout[deviceId]
              delete layout[deviceId]
              saveLayout(projectId, layout)
            }
            setSelectedDeviceId((selected) => selected === deviceId ? finalName : selected)
          }
          setSaveState('saved')
          setAutosave('saved')
          return next
        } catch (err) {
          handleWriteError(err, () => {
            setLastError(errorMessage(err))
            setSaveState('error')
            setAutosave('error')
          })
        }
      }
      const pending = devicePatchQueue.current.then(execute)
      devicePatchQueue.current = pending
      return pending
    },
    [devices, projectId, setAutosave, uniqueDeviceName, handleWriteError],
  )

  const deleteDevice = useCallback(
    async (deviceId: string) => {
      const current = devices.find((d) => d.id === deviceId)
      if (!current) return
      try {
        // 写在途标保存中: 刷新按钮此间禁用, 读回旧修订不得覆盖本次保存。
        setSaveState('saving')
        // 服务端删设备级联移除关联连接, 本地同步裁剪。
        const { revision } = await removeDevice(projectId, current.name, revisionRef.current)
        revisionRef.current = revision
        setDevices((prev) => prev.filter((d) => d.id !== deviceId))
        setConnections((prev) => prev.filter((c) => c.fromDeviceId !== deviceId && c.toDeviceId !== deviceId))
        setSelectedDeviceId((cur) => (cur === deviceId ? null : cur))
        setSelectedConnId(null)
        setSaveState('saved')
        setAutosave('saved')
      } catch (err) {
        handleWriteError(err, () => {
          setLastError(errorMessage(err))
          setSaveState('error')
          setAutosave('error')
        })
      }
    },
    [devices, projectId, setAutosave, handleWriteError],
  )

  // -- 连接 ------------------------------------------------------------
  const connectionLabel = useCallback(
    (carrier: LocalConnection['carrier']): string => {
      const key = carrierLabelKey(carrier)
      if (key === null) return carrier
      return key === 'port.solar' ? lt(key) : t(key)
    },
    [t],
  )

  /**
   * 画布句柄(carrier:direction:name) → 注册表端口声明(按 YAML 端口名精确匹配)。
   *
   * 找不到指定名称的端口返回 null(由调用方给出可定位诊断, 不允许静默回退
   * 到 carrier+direction 的首个匹配 —— 宪法 5.3: 正确性优先于兼容)。
   */
  const findRegistryPort = useCallback(
    (
      device: LocalDevice,
      wantDirection: 'in' | 'out',
      portName: string,
    ): { carrier: LocalConnection['carrier']; name: string } | null => {
      if (!portName) return null
      const spec = specOf(device.deviceType)
      if (!spec) return null
      const hit = derivePorts(spec as DeviceTypeSpec, device.params).find(
        (p) => p.name === portName && p.direction === wantDirection,
      )
      return hit ? { carrier: hit.carrier, name: hit.name } : null
    },
    [specOf],
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
      // 2.0 设备即时落库(名键), 无临时 id 等待态; 端口名须命中注册表声明
      const fromReg = findRegistryPort(fromDevice, 'out', fromPort.name)
      const toReg = findRegistryPort(toDevice, 'in', toPort.name)
      if (!fromReg || !toReg) {
        // 注册表端口缺失/类型不匹配: 可定位诊断(前端本地检查只是即时提示,
        // 后端仍为最终权威)
        const item: IssueItem = {
          id: localId(),
          severity: 'error',
          message: lt('conn.port_missing'),
          deviceId: fromDevice.id,
        }
        setIssues((prev) => [...prev.slice(-3), item])
        return
      }
      try {
        // 写在途标保存中: 刷新按钮此间禁用, 读回旧修订不得覆盖本次保存。
        setSaveState('saving')
        // 2.0 连接只表达真实端点(设备名 + 注册表端口名), 无类型/容量/损耗字段
        const { name, revision } = await upsertConnection(
          projectId,
          {
            from_device: fromDevice.name,
            from_port: fromReg.name,
            to_device: toDevice.name,
            to_port: toReg.name,
          },
          revisionRef.current,
        )
        revisionRef.current = revision
        const newConn: LocalConnection = {
          id: name,
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
        handleWriteError(err, () => {
          const item: IssueItem = {
            id: localId(),
            severity: 'error',
            message: errorMessage(err),
            deviceId: fromDevice.id,
          }
          setIssues((prev) => [...prev.slice(-3), item])
          // 写失败同样离开 saving(否则刷新永久禁用); 诊断面板已定位问题。
          setSaveState('error')
          setAutosave('error')
        })
      }
    },
    [devices, connections, projectId, connectionLabel, findRegistryPort, setAutosave, handleWriteError],
  )

  const disconnectConn = useCallback(
    async (connId: string) => {
      try {
        // 写在途标保存中: 刷新按钮此间禁用, 读回旧修订不得覆盖本次保存。
        setSaveState('saving')
        const { revision } = await removeConnection(projectId, connId, revisionRef.current)
        revisionRef.current = revision
        setConnections((prev) => prev.filter((c) => c.id !== connId))
        setSelectedConnId((cur) => (cur === connId ? null : cur))
        setSaveState('saved')
        setAutosave('saved')
      } catch (err) {
        handleWriteError(err, () => {
          setLastError(errorMessage(err))
          setSaveState('error')
          setAutosave('error')
        })
      }
    },
    [projectId, setAutosave, handleWriteError],
  )

  // -- 布局(与拓扑分离:仅坐标,不触发脏标记; 按设备名存取, 与 2.0 名键一致) --
  // 注:onNodeDragStop 的 event 为 DOM MouseEvent|TouchEvent(@xyflow/react 类型)。
  const handleNodeDragStop = useCallback(
    (_: MouseEvent | TouchEvent, node: RFNode) => {
      const device = devices.find((d) => d.id === node.id)
      if (!device) return
      setDevices((prev) => prev.map((d) => (d.id === node.id ? { ...d, position: node.position } : d)))
      const next = loadLayout(projectId)
      next[device.name] = node.position
      saveLayout(projectId, next)
    },
    [devices, projectId],
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
      const result = await validateModel(projectId)
      setValidateResult(result.diagnostics)
    } catch (err) {
      setValidateError(errorMessage(err))
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
      // 2.0 诊断定位键为名称(设备名/连接名), 本地 id 与之一致
      if (loc.object_type === 'device') {
        const dev = devices.find((d) => d.name === loc.object_id || d.id === loc.object_id)
        if (dev) locateDevice(dev.id)
      }
    },
    [connections, devices, fitView, locateDevice],
  )

  // -- 拖拽放置 --------------------------------------------------------
  const handleDragOver = useCallback((event: ReactDragEvent) => {
    event.preventDefault()
    event.dataTransfer.dropEffect = 'copy'
  }, [])

  const handleDrop = useCallback(
    (event: ReactDragEvent) => {
      event.preventDefault()
      // 读阻塞时画布不接受放置(面板项已不可拖拽, 此处再保险)。
      if (readBlocked) return
      const typeId = event.dataTransfer.getData('application/ies-device-type')
      if (!typeId) return
      const spec = specOf(typeId)
      if (!spec) return
      addDevice(spec, screenToFlowPosition({ x: event.clientX, y: event.clientY }))
    },
    [addDevice, screenToFlowPosition, specOf, readBlocked],
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
          <Button
            variant="secondary"
            size="sm"
            onClick={() => navigate(`/projects/${projectId}/model/new`)}
            title={lt('toolbar.new_model')}
          >
            {pt('ies.modeling.new_model')}
          </Button>
          <IconButton aria-label={lt('toolbar.fit')} onClick={() => void fitView({ padding: 0.2, duration: 300 })}>
            <Icon name="info" size={15} />
          </IconButton>
          <Button
            variant="secondary"
            size="sm"
            icon={validating ? undefined : 'check'}
            loading={validating}
            onClick={() => void runValidate()}
            disabled={validating || readBlocked}
          >
            {t('ies.modeling.graph_validate')}
          </Button>
          <Button
            variant="primary"
            size="sm"
            onClick={() => void reloadGraph()}
            disabled={loading || offline || saveState === 'saving'}
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

      {saveState === 'conflict' ? (
        <Alert variant="warning" title={lt('save.conflict')}>
          {lt('save.conflict_banner')}{' '}
          <Button variant="secondary" size="sm" onClick={() => void reloadGraph()} disabled={loading}>
            {lt('save.conflict_reload')}
          </Button>
        </Alert>
      ) : null}

      {loadError && graphReady ? (
        <Alert variant="warning" title={lt('load.error', { reason: loadError })}>
          <Button variant="secondary" size="sm" onClick={retryLoad} disabled={loading}>
            {t('ies.common.retry')}
          </Button>
        </Alert>
      ) : null}

      {!graphReady ? (
        <div className="mp-body">
          <div className="mp-load-state" role="status">
            {loadError ? (
              <Alert variant="error" title={lt('load.error', { reason: loadError })}>
                <Button variant="secondary" size="sm" onClick={retryLoad}>
                  {t('ies.common.retry')}
                </Button>
              </Alert>
            ) : (
              <>
                <Spinner size="md" />
                <p>{lt('load.loading')}</p>
              </>
            )}
          </div>
        </div>
      ) : (
      <div className="mp-body">
        <Palette
          specs={deviceTypes ?? []}
          disabled={readBlocked}
          loading={deviceTypes === null && !typesError}
          error={typesError}
          onRetry={() => {
            setTypesError(false)
            setDeviceTypes(null)
            void listDeviceTypes(projectId)
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
              nodesDraggable={!readBlocked}
              nodesConnectable={!readBlocked}
              elementsSelectable={!readBlocked}
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
              // 空图时不 fitView: 空图 fitView 会把 zoom 撑到 maxZoom(2.5),
              // 导致拖放放置的节点屏幕尺寸放大、flow 坐标间距缩小,
              // 连续放置多个设备必然互相重叠、端口句柄被相邻节点遮挡无法连线;
              // 有节点后 prop 变化(fitViewQueued)自动 fit 全部节点。
              // 已有设备后继续新增由 AUTO_FIT_OPTIONS effect 触发重 fit
              // (见前文 effect 注释; 单靠 prop 边沿只会 fit 一次)。
              // maxZoom 1: 视图永不放大, 放置坐标换算间距恒等于屏幕落点间距,
              // 任意两个不同落点放置的节点(宽 190 flow)不会重叠(见 QA-E2E-01 场景 6 专项调试)
              fitView={hasDevices}
              fitViewOptions={AUTO_FIT_OPTIONS}
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
          {selectedConn && !readBlocked ? (
            <ConnectionPanel
              conn={selectedConn}
              devices={devices}
              labelOf={connectionLabel}
              onDisconnect={() => disconnectConn(selectedConn.id)}
              onClose={() => setSelectedConnId(null)}
            />
          ) : null}
          {selectedDevice && !readBlocked ? (
            <DeviceSidebar
              projectId={projectId}
              device={selectedDevice}
              spec={specOf(selectedDevice.deviceType) ?? EMPTY_SPEC}
              onPatch={(patch, paramKeys) => patchDevice(selectedDevice.id, patch, paramKeys)}
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
      )}
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
  schema_version: '2.0.0',
  name_zh: '',
  name_en: '',
  energy_carriers: [],
  ports: [],
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
  disabled = false,
  loading,
  error,
  onRetry,
}: {
  specs: DeviceTypeSpec[]
  /** 读阻塞时只读展示: 条目不可拖拽(画布刷新在途/失败未恢复时不接受变更)。 */
  disabled?: boolean
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
              draggable={!disabled}
              aria-disabled={disabled}
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
  projectId,
  device,
  spec,
  onPatch,
  onDelete,
  onClose,
}: {
  projectId: number
  device: LocalDevice
  spec: ExtendedDeviceTypeSpec
  onPatch: (patch: Partial<Pick<LocalDevice, 'name' | 'kind' | 'params'>>, paramKeys?: string[]) => Promise<LocalDevice | undefined>
  onDelete: () => void
  onClose: () => void
}) {
  const { t } = useI18n()
  const [nameDraft, setNameDraft] = useState(device.name)
  const currentEditor = useRef({ id: device.editorId })
  if (currentEditor.current.id !== device.editorId) currentEditor.current = { id: device.editorId }
  const pendingNameSaves = useRef(0)
  const editSequence = useRef(0)
  const nameEdit = useRef<number | null>(null)
  const parameterEdits = useRef(new Map<string, number>())
  const sourceEdits = useRef(new Map<string, number>())
  const previousDevice = useRef(device)
  const touchParameter = (key: string) => parameterEdits.current.set(key, ++editSequence.current)
  const [paramDrafts, setParamDrafts] = useState<Record<string, ParamDraft>>(() => draftsOf(device.params))
  const [errors, setErrors] = useState<Record<string, string | null>>({})
  const [armed, setArmed] = useState(false)
  const [datasetVersions, setDatasetVersions] = useState<Array<{ id: number; label: string }>>([])
  const [datasetVersionsState, setDatasetVersionsState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [datasetVersionsReload, setDatasetVersionsReload] = useState(0)

  // 项目数据集版本(实例来源声明的可选目标; 只读列表, 绑定写入实例 params)。
  // 加载失败显式可见并重试, 不伪装成"项目没有版本"; 已保存声明保持原值。
  useEffect(() => {
    let cancelled = false
    setDatasetVersionsState('loading')
    void (async () => {
      try {
        const datasets = await listDatasets({ project_id: projectId })
        const items: Array<{ id: number; label: string }> = []
        for (const dataset of datasets.items) {
          const versions = await listVersions(projectId, dataset.id)
          for (const version of versions) {
            items.push({
              id: version.id,
              label: `${dataset.name} v${version.version_no}${version.resolution ? ` (${version.resolution})` : ''}`,
            })
          }
        }
        if (!cancelled) {
          setDatasetVersions(items)
          setDatasetVersionsState('ready')
        }
      } catch {
        if (!cancelled) setDatasetVersionsState('error')
      }
    })()
    return () => {
      cancelled = true
    }
  }, [projectId, datasetVersionsReload])

  // 新侧栏可能先收到保存前的设备值。后续回包只同步未编辑字段，
  // 每次用户输入的代次同时保护“改回旧值”及同字段连续提交。
  useEffect(() => {
    const switched = previousDevice.current.editorId !== device.editorId
    previousDevice.current = device
    if (switched) {
      pendingNameSaves.current = 0
      nameEdit.current = null
      parameterEdits.current.clear()
      sourceEdits.current.clear()
      setErrors({})
      setArmed(false)
    }
    if (nameEdit.current === null) setNameDraft(device.name)
    const saved = draftsOf(device.params)
    setParamDrafts((drafts) => {
      const next = { ...drafts }
      for (const key of new Set([...Object.keys(drafts), ...Object.keys(saved)])) {
        if (parameterEdits.current.has(key)) continue
        if (key in saved) next[key] = saved[key]
        else delete next[key]
      }
      return next
    })
  }, [device.editorId, device.name, device.params])

  const paramKeys = useMemo(() => Object.keys(spec.parameters), [spec])

  const commitParam = (key: string, specParam: ParamSpecExt) => {
    if (!parameterEdits.current.has(key)) return
    const draft = paramDrafts[key] ?? ''
    // 结构化参数(字典,如 import_tariff={peak,flat,valley}):子键全部填齐才提交。
    if (typeof draft === 'object') {
      const subKeys = Object.keys(draft)
      const filled = subKeys.filter((k) => draft[k] !== '')
      if (filled.length === 0) {
        const next = { ...device.params }
        delete next[key]
        saveParameterPatch({ params: next }, [key])
        setErrors((prev) => ({ ...prev, [key]: null }))
        return
      }
      if (filled.length < subKeys.length) {
        setErrors((prev) => ({ ...prev, [key]: lt('sidebar.param_dict_incomplete') }))
        return
      }
      const obj: Record<string, number> = {}
      for (const k of subKeys) {
        const v = Number(draft[k])
        if (!Number.isFinite(v)) {
          setErrors((prev) => ({ ...prev, [key]: lt('sidebar.param_dict_invalid', { key: k }) }))
          return
        }
        obj[k] = v
      }
      saveParameterPatch({ params: { ...device.params, [key]: obj } }, [key])
      setErrors((prev) => ({ ...prev, [key]: null }))
      return
    }
    const raw = String(draft).trim()
    if (raw === '') {
      const next = { ...device.params }
      delete next[key]
      saveParameterPatch({ params: next }, [key])
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
    saveParameterPatch({ params: { ...device.params, [key]: value } }, [key])
    setErrors((prev) => ({ ...prev, [key]: null }))
  }

  const restoreDefault = (key: string, specParam: ParamSpecExt) => {
    touchParameter(key)
    const value = defaultParamValue(specParam as DeviceTypeSpec['parameters'][string], device.kind)
    setParamDrafts((prev) => ({ ...prev, [key]: draftsOfValue(value) }))
    if (value === null) {
      const next = { ...device.params }
      delete next[key]
      saveParameterPatch({ params: next }, [key])
    } else {
      saveParameterPatch({ params: { ...device.params, [key]: value } }, [key])
    }
    setErrors((prev) => ({ ...prev, [key]: null }))
  }

  const switchKind = (kind: LocalDevice['kind']) => {
    if (kind === device.kind) return
    // 保留用户已修改的参数,仅替换"仍为另一属性默认值"的参数
    const otherDefaults = buildDefaultParams(spec as DeviceTypeSpec, kind === 'existing' ? 'new' : 'existing')
    const nextParams: Record<string, unknown> = { ...device.params }
    const changedKeys: string[] = []
    for (const [key, specParam] of Object.entries(spec.parameters)) {
      const current = device.params[key]
      // blur 保存可能尚未返回；当前输入已改变的字段不再套用旧默认值。
      if (JSON.stringify(paramDrafts[key] ?? '') !== JSON.stringify(draftsOfValue(current))) continue
      const other = otherDefaults[key]
      const target = defaultParamValue(specParam as DeviceTypeSpec['parameters'][string], kind)
      if (JSON.stringify(target) === JSON.stringify(current)) continue
      if (current === undefined || (typeof current === 'number' && current === other) || current === other) {
        touchParameter(key)
        changedKeys.push(key)
        if (target === null) delete nextParams[key]
        else nextParams[key] = target
      }
    }
    const drafts = draftsOf(nextParams)
    setParamDrafts((previous) => {
      const next = { ...previous }
      for (const key of changedKeys) {
        if (key in drafts) next[key] = drafts[key]
        else delete next[key]
      }
      return next
    })
    saveParameterPatch({ kind, params: nextParams }, changedKeys)
  }

  const onDeleteClick = () => {
    if (!armed) {
      setArmed(true)
      window.setTimeout(() => setArmed(false), 3000)
      return
    }
    onDelete()
  }

  // ---- 预定义接口来源(实例显式事实) --------------------------------------
  type SourceDraft = { mode: string; value: string; datasetVersionId: string; targetType: string }
  const draftFromParams = (iface: string, params = device.params): SourceDraft => {
    const raw = params[iface]
    if (typeof raw === 'number') {
      return { mode: 'constant', value: String(raw), datasetVersionId: '', targetType: '' }
    }
    if (raw !== null && typeof raw === 'object') {
      const obj = raw as Record<string, unknown>
      return {
        mode: typeof obj.mode === 'string' ? obj.mode : '',
        value: '',
        datasetVersionId: obj.dataset_version_id != null ? String(obj.dataset_version_id) : '',
        targetType: typeof obj.target_type === 'string' ? obj.target_type : '',
      }
    }
    return { mode: '', value: '', datasetVersionId: '', targetType: '' }
  }
  const predefinedPorts = spec.ports.filter((port) => port.direction === 'predefined')
  const [sourceDrafts, setSourceDrafts] = useState<Record<string, SourceDraft>>({})

  useEffect(() => {
    setSourceDrafts((drafts) => {
      const next = { ...drafts }
      for (const port of predefinedPorts) {
        if (!sourceEdits.current.has(port.name)) next[port.name] = draftFromParams(port.name)
      }
      return next
    })
    // 来源和普通参数遵循同一逐字段同步规则，不覆盖未提交的模式/值/版本。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [device.editorId, device.params, spec.ports])

  const saveParameterPatch = (
    patch: Partial<Pick<LocalDevice, 'name' | 'kind' | 'params'>>,
    keys: string[],
  ) => {
    const editor = currentEditor.current
    const parameters = new Map(keys.map((key) => [key, parameterEdits.current.get(key)]))
    const sources = new Map(keys.map((key) => [key, sourceEdits.current.get(key)]))
    return onPatch(patch, keys).then((saved) => {
      if (!saved || currentEditor.current !== editor) return
      const cleanParameters = keys.filter((key) =>
        parameterEdits.current.get(key) === parameters.get(key))
      const cleanSources = keys.filter((key) => sourceEdits.current.get(key) === sources.get(key))
      for (const key of cleanParameters) parameterEdits.current.delete(key)
      for (const key of cleanSources) sourceEdits.current.delete(key)
      setParamDrafts((drafts) => {
        const next = { ...drafts }
        for (const key of cleanParameters) next[key] = draftsOfValue(saved.params[key])
        return next
      })
      setSourceDrafts((drafts) => {
        const next = { ...drafts }
        for (const key of cleanSources) next[key] = draftFromParams(key, saved.params)
        return next
      })
    })
  }

  const patchSource = (iface: string, patch: Partial<SourceDraft>) => {
    sourceEdits.current.set(iface, ++editSequence.current)
    setSourceDrafts((prev) => {
      const current = prev[iface] ?? draftFromParams(iface)
      return { ...prev, [iface]: { ...current, ...patch } }
    })
  }

  const [sourceErrors, setSourceErrors] = useState<Record<string, string | null>>({})

  /** 完整合法的来源才写入已保存事实; 未填完只保留本地草稿(不造默认值)。 */
  const persistSource = (iface: string, draft: SourceDraft): boolean => {
    if (!draft.mode) {
      const params: Record<string, unknown> = { ...device.params }
      delete params[iface]
      saveParameterPatch({ params }, [iface])
      return true
    }
    if (draft.mode === 'constant') {
      const num = Number(draft.value)
      if (draft.value.trim() === '' || !Number.isFinite(num)) return false
      saveParameterPatch({ params: { ...device.params, [iface]: num } }, [iface])
      return true
    }
    if (!draft.datasetVersionId) return false
    const decl: Record<string, unknown> = {
      mode: draft.mode,
      dataset_version_id: Number(draft.datasetVersionId),
    }
    if (draft.mode === 'data_predict' && draft.targetType.trim()) {
      decl.target_type = draft.targetType.trim()
    }
    saveParameterPatch({ params: { ...device.params, [iface]: decl } }, [iface])
    return true
  }

  const commitSource = (iface: string) => {
    if (!sourceEdits.current.has(iface)) return
    const draft = sourceDrafts[iface] ?? draftFromParams(iface)
    const ok = persistSource(iface, draft)
    setSourceErrors((prev) => ({
      ...prev,
      [iface]: ok ? null : lt('source.value_required'),
    }))
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
            onChange={(event) => {
              nameEdit.current = ++editSequence.current
              setNameDraft(event.target.value)
            }}
            onBlur={() => {
              const name = nameDraft.trim()
              if (name && (name !== device.name || nameEdit.current !== null || pendingNameSaves.current > 0)) {
                const submitted = nameDraft
                const editor = currentEditor.current
                const edit = nameEdit.current
                pendingNameSaves.current += 1
                void onPatch({ name }).then((saved) => {
                  if (currentEditor.current !== editor) return
                  pendingNameSaves.current -= 1
                  if (saved && nameEdit.current === edit) {
                    nameEdit.current = null
                    setNameDraft((draft) => draft === submitted ? saved.name : draft)
                  }
                })
              }
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

        {/* 参数(schema 驱动) */}
        <p className="mp-sidebar__section">{t('ies.modeling.params')}</p>
        {paramKeys.length === 0 ? <p>{lt('sidebar.params_none')}</p> : null}
        {paramKeys.map((key) => {
          const p = spec.parameters[key]
          // 结构化参数(默认值为对象,如 import_tariff):按 spec 判定,避免草稿缺失时渲染错位
          const isDict = typeof p.default === 'object' && p.default !== null
          // 字典草稿:未初始化(参数被清空)时回退 spec 默认值做子键骨架
          const dictDraft: Record<string, string> =
            typeof paramDrafts[key] === 'object' && paramDrafts[key] !== null
              ? (paramDrafts[key] as Record<string, string>)
              : (draftsOfValue(isDict ? p.default : null) as Record<string, string>)
          const range = isDict ? (p.unit ?? '') : `${p.min ?? '—'} ~ ${p.max ?? '—'}`
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
                {isDict ? (
                  /* 结构化参数(如 import_tariff 分时电价):按子键渲染子输入 */
                  <div className="mp-param__dict">
                    {Object.keys(dictDraft).map((subKey) => (
                      <label key={subKey} className="mp-param__dict-item">
                        <span className="mp-param__dict-label">{subKey}</span>
                        <Input
                          className="mp-param__input"
                          type="number"
                          value={dictDraft[subKey] ?? ''}
                          aria-label={`${key}.${subKey}${p.unit ? ` (${p.unit})` : ''}`}
                          invalid={Boolean(errors[key])}
                          onChange={(event) => {
                            // 基于完整子键骨架合并(不能只展开 paramDrafts[key]:
                            // 未初始化时它是 undefined,展开后只剩当前编辑的子键,其余丢失)
                            touchParameter(key)
                            setParamDrafts((prev) => ({
                              ...prev,
                              [key]: { ...dictDraft, [subKey]: event.target.value },
                            }))
                          }}
                          onBlur={() => commitParam(key, p)}
                        />
                      </label>
                    ))}
                  </div>
                ) : (
                  <Input
                    className="mp-param__input"
                    type="number"
                    value={String(paramDrafts[key] ?? '')}
                    aria-label={`${key}${p.unit ? ` (${p.unit})` : ''}`}
                    aria-describedby={errors[key] ? `mp-param-err-${key}` : undefined}
                    invalid={Boolean(errors[key])}
                    onChange={(event) => {
                      touchParameter(key)
                      setParamDrafts((prev) => ({ ...prev, [key]: event.target.value }))
                    }}
                    onBlur={() => commitParam(key, p)}
                  />
                )}
                <span className="mp-param__range">
                  {range}
                </span>
              </div>
              {errors[key] ? (
                <div id={`mp-param-err-${key}`} className="mp-param__range" role="alert" style={{ color: 'var(--ies-danger-text)' }}>
                  {errors[key]}
                </div>
              ) : null}
            </div>
          )
        })}
        {predefinedPorts.length > 0 ? (
          <>
            <p className="mp-sidebar__section">{lt('source.section')}</p>
            <p className="mp-sidebar__hint">{lt('source.hint')}</p>
            {predefinedPorts.map((port) => {
              const draft = sourceDrafts[port.name] ?? draftFromParams(port.name)
              return (
                <div className="mp-source" key={port.name} data-testid={`mp-source-${port.name}`}>
                  <div className="mp-param__head">
                    <span className="mp-param__name">{port.name}</span>
                    <span className="mp-param__unit">{port.unit ?? port.energy_carrier}</span>
                  </div>
                  <Select
                    id={`mp-source-mode-${port.name}`}
                    value={draft.mode}
                    onChange={(event) => {
                      const mode = event.target.value
                      const next = { ...draft, mode }
                      patchSource(port.name, { mode })
                      // 空模式=清除; constant/data 未填完只保留本地草稿, 完整后才持久化。
                      persistSource(port.name, next)
                      setSourceErrors((prev) => ({ ...prev, [port.name]: null }))
                    }}
                  >
                    <option value="">{lt('source.mode_none')}</option>
                    <option value="constant">constant</option>
                    <option value="data_repeat">data_repeat</option>
                    <option value="data_full">data_full</option>
                    <option value="data_predict">data_predict</option>
                  </Select>
                  {draft.mode === 'constant' ? (
                    <Input
                      id={`mp-source-value-${port.name}`}
                      type="text"
                      inputMode="decimal"
                      aria-label={`${port.name} ${lt('source.constant_value')}`}
                      value={draft.value}
                      invalid={Boolean(sourceErrors[port.name])}
                      onChange={(event) => {
                        patchSource(port.name, { value: event.target.value })
                        setSourceErrors((prev) => ({ ...prev, [port.name]: null }))
                      }}
                      onBlur={() => commitSource(port.name)}
                    />
                  ) : null}
                  {draft.mode.startsWith('data_') ? (
                    <>
                      {datasetVersionsState === 'loading' ? (
                        <p className="mp-sidebar__hint">{lt('source.loading')}</p>
                      ) : null}
                      {datasetVersionsState === 'error' ? (
                        <div className="mp-sidebar__hint" role="alert">
                          <span>{lt('source.load_failed')} </span>
                          <Button
                            size="sm"
                            variant="ghost"
                            id={`mp-source-versions-retry-${port.name}`}
                            onClick={() => setDatasetVersionsReload((n) => n + 1)}
                          >
                            {lt('source.retry')}
                          </Button>
                        </div>
                      ) : null}
                      <Select
                        id={`mp-source-version-${port.name}`}
                        value={draft.datasetVersionId}
                        onChange={(event) => {
                          const next = { ...draft, datasetVersionId: event.target.value }
                          patchSource(port.name, { datasetVersionId: event.target.value })
                          persistSource(port.name, next)
                        }}
                      >
                        <option value="">
                          {datasetVersionsState === 'ready' && datasetVersions.length === 0
                            ? lt('source.version_empty')
                            : lt('source.version_choose')}
                        </option>
                        {draft.datasetVersionId
                          && !datasetVersions.some((version) => String(version.id) === draft.datasetVersionId) ? (
                            <option value={draft.datasetVersionId}>
                              {lt('source.version_saved', { id: draft.datasetVersionId })}
                            </option>
                          ) : null}
                        {datasetVersions.map((version) => (
                          <option key={version.id} value={String(version.id)}>{version.label}</option>
                        ))}
                      </Select>
                    </>
                  ) : null}
                  {draft.mode === 'data_predict' ? (
                    <Input
                      id={`mp-source-target-${port.name}`}
                      type="text"
                      aria-label={`${port.name} ${lt('source.target_type')}`}
                      value={draft.targetType}
                      onChange={(event) => patchSource(port.name, { targetType: event.target.value })}
                      onBlur={() => commitSource(port.name)}
                    />
                  ) : null}
                  {sourceErrors[port.name] ? (
                    <p className="mp-sidebar__hint" role="alert">{sourceErrors[port.name]}</p>
                  ) : null}
                </div>
              )
            })}
          </>
        ) : null}
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

/** 参数草稿值:字符串(枚举/数值输入)或对象(结构化参数,如 import_tariff 分时电价)。 */
type ParamDraft = string | Record<string, string>

/** 参数值 → 草稿:number/string 转字符串,对象转字符串子键表。 */
function draftsOfValue(value: unknown): ParamDraft {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number') return String(value)
  if (typeof value === 'string') return value
  if (typeof value === 'object') {
    const out: Record<string, string> = {}
    for (const [key, val] of Object.entries(value)) {
      if (typeof val === 'number') out[key] = String(val)
      else if (typeof val === 'string') out[key] = val
    }
    return out
  }
  return ''
}

function draftsOf(params: Record<string, unknown>): Record<string, ParamDraft> {
  const out: Record<string, ParamDraft> = {}
  for (const [key, value] of Object.entries(params)) {
    const draft = draftsOfValue(value)
    if (draft !== '') out[key] = draft
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
