/**
 * 建模画布 2.0 草稿映射单元测试(S2c)。
 *
 * 覆盖: 草稿 devices/connections(名键) → LocalDevice/LocalConnection,
 * 端口名按真实 descriptor 声明解析(heat_pump.heat_out → transport_pipe.heat_in),
 * 未知设备/未知端口/载能不一致返回 null。
 *
 * 纯 Node 环境(无浏览器/无 React): 用 esbuild 把 canvasModel.ts 打包为 CJS 后
 * 直接调用(与 tests/modeling_mappers.test.mjs 同一模式)。
 *
 * 运行(在 frontend 目录, Docker 内):
 *   npx esbuild src/pages/model/canvasModel.ts --bundle --format=cjs \
 *     --outfile=/tmp/modeling-draft-mapper.cjs
 *   node tests/modeling_draft_mapper.test.mjs
 */

import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

let failures = 0
function check(name, cond, detail = '') {
  const ok = cond ? 'PASS' : 'FAIL'
  if (!cond) failures++
  console.log(`[${ok}] ${name}${detail ? ` — ${detail}` : ''}`)
}

const cm = require('/tmp/modeling-draft-mapper.cjs')

// 真实 descriptor 端口名(与 backend devices/catalog/*.yaml 一致)
const HP_SPEC = {
  type_id: 'ies.device.heat_pump',
  parameters: {},
  ports: [
    { name: 'electricity_in', energy_carrier: 'electric', direction: 'in' },
    { name: 'heat_out', energy_carrier: 'heat', direction: 'out' },
  ],
}
const PIPE_SPEC = {
  type_id: 'ies.device.transport_pipe',
  parameters: {},
  ports: [
    { name: 'heat_in', energy_carrier: 'heat', direction: 'in' },
    { name: 'heat_out', energy_carrier: 'heat', direction: 'out' },
  ],
}
const specOf = (typeId) =>
  typeId === HP_SPEC.type_id ? HP_SPEC : typeId === PIPE_SPEC.type_id ? PIPE_SPEC : null

// ---------------------------------------------------------------------------
// deviceFromDraft
// ---------------------------------------------------------------------------
const hp = cm.deviceFromDraft(
  { name: 'hp1', device_type: 'ies.device.heat_pump', kind: 'new', params: { cop: 3.5 } },
  { x: 80, y: 80 },
)
check('设备 id 即设备名(名键)', hp.id === 'hp1')
check('设备类型/属性/参数透传', hp.deviceType === 'ies.device.heat_pump' && hp.kind === 'new' && hp.params.cop === 3.5)
check('无旧 id/精度字段', !('model_fidelity' in hp))

// ---------------------------------------------------------------------------
// connectionFromDraft
// ---------------------------------------------------------------------------
const byName = new Map([
  ['hp1', hp],
  [ 'pipe1', cm.deviceFromDraft(
    { name: 'pipe1', device_type: 'ies.device.transport_pipe', kind: 'new', params: {} },
    { x: 340, y: 80 },
  ) ],
])
const conn = cm.connectionFromDraft(
  { name: 'c2', from_device: 'hp1', from_port: 'heat_out', to_device: 'pipe1', to_port: 'heat_in' },
  byName,
  specOf,
)
check('合法端点连接解析成功', conn !== null)
check('连接 id 即服务端连接名', conn && conn.id === 'c2')
check(
  '句柄为载能:方向:端口名',
  conn && conn.fromHandle === 'heat:out:heat_out' && conn.toHandle === 'heat:in:heat_in',
)
check('边载能为 heat', conn && conn.carrier === 'heat')

check(
  '未知端口名返回 null',
  cm.connectionFromDraft(
    { name: 'cb', from_device: 'hp1', from_port: 'heat_nope', to_device: 'pipe1', to_port: 'heat_in' },
    byName,
    specOf,
  ) === null,
)
check(
  '未知设备返回 null',
  cm.connectionFromDraft(
    { name: 'cb', from_device: 'hp1', from_port: 'heat_out', to_device: 'ghost', to_port: 'heat_in' },
    byName,
    specOf,
  ) === null,
)
check(
  '源端方向不匹配返回 null(electricity_in 不能作 out)',
  cm.connectionFromDraft(
    { name: 'cb', from_device: 'hp1', from_port: 'electricity_in', to_device: 'pipe1', to_port: 'heat_in' },
    byName,
    specOf,
  ) === null,
)

console.log(failures === 0 ? '\n草稿映射测试: 全部通过' : `\n共 ${failures} 项失败`)
process.exit(failures === 0 ? 0 : 1)
