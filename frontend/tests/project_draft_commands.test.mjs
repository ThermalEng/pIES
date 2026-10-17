/**
 * 项目草稿命令与模型节映射定向测试(无浏览器依赖)。
 *
 * 覆盖:
 * 1. putDraftCommands: 显式 expected_revision 原样发出, 正确组装单体命令,
 *    直接消费后端返回的新 revision; 409 冲突原样抛出可见(不回退、不猜 1)。
 * 2. sendDraftCommand: 先读视图当前草稿 revision 做乐观锁后发送, 409 原样透传。
 * 3. modelSectionFromView: 真实 ProjectViewResponseDto 结构映射, 保留真实空数组与修订号。
 * 4. deviceTypeFromServer: 精确匹配后端 selector.py 规范, 必须有 schema_version 字符串,
 *    端口与参数直映射, 不编造 is_load/capabilities/model_method/stateful/version。
 * 5. saveWorkingInputs: tasks feature 公开草稿输入保存, 经 projects 唯一草稿传输发出
 *    task.set_working_inputs 命令。
 *
 * 运行(在 frontend 目录, Docker 内):
 *   npx esbuild tests/project_draft_commands.test.mjs --bundle --platform=node \
 *     --format=esm --outfile=/tmp/project-draft-commands.mjs &&
 *   node /tmp/project-draft-commands.mjs
 */

import { putDraftCommands, sendDraftCommand } from '../src/features/projects/index.ts'
import { modelSectionFromView, deviceTypeFromServer } from '../src/features/modeling/index.ts'
import { saveWorkingInputs } from '../src/features/tasks/index.ts'
import { ApiError } from '../src/types.ts'

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

// 模拟 fetch 拦截
let lastRequest = null
globalThis.fetch = async (url, init = {}) => {
  const urlStr = String(url)
  const method = String(init.method ?? 'GET').toUpperCase()
  const bodyText = typeof init.body === 'string' ? init.body : null
  const parsedBody = bodyText ? JSON.parse(bodyText) : null

  lastRequest = { url: urlStr, method, body: parsedBody, headers: init.headers }

  // 1. PUT /api/projects/10/draft
  if (method === 'PUT' && urlStr.endsWith('/projects/10/draft')) {
    if (parsedBody?.expected_revision === 999) {
      // 模拟并发修订冲突 409
      return new Response(
        JSON.stringify({
          error: {
            code: 'PROJ-REV-001',
            message_key: 'ies.error.draft_conflict',
            params: { current_revision: 5, expected_revision: 999 },
          },
        }),
        { status: 409, headers: { 'Content-Type': 'application/json' } },
      )
    }
    // 成功返回新修订号
    return new Response(
      JSON.stringify({ revision: (parsedBody?.expected_revision ?? 0) + 1 }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )
  }

  // 2. GET /api/projects/10 (视图读取)
  if (method === 'GET' && urlStr.endsWith('/projects/10')) {
    return new Response(
      JSON.stringify({
        project: {
          id: 10,
          name: 'p10',
          description: null,
          status: 'active',
          owner_id: 1,
          currency: 'CNY',
          project_baseline: { resolution: '1h', start_year: 2026, scenario_mode: 'deterministic' },
          schema_version: 1,
          current_draft_id: 101,
          current_version_id: null,
          created_at: '2026-10-17T00:00:00Z',
          updated_at: null,
          created_by: 1,
        },
        draft: {
          id: 101,
          project_id: 10,
          revision: 3,
          content_object_id: 201,
          is_current: true,
          updated_by: 1,
          updated_at: '2026-10-17T00:00:00Z',
          created_at: '2026-10-17T00:00:00Z',
          dataset_bindings: [],
          content: {
            model: { devices: [], connections: [] },
            task_working_inputs: { sample: 'val' },
          },
        },
        my_role: 'owner',
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )
  }

  return new Response('not found', { status: 404 })
}

// ---------------------------------------------------------------------------
// 1. putDraftCommands: 显式 expected_revision 原样发送与响应消费
// ---------------------------------------------------------------------------
{
  const res = await putDraftCommands(10, [{ type: 'model.upsert_device', payload: { name: 'dev1' } }], 4)
  check('putDraftCommands: 请求路径与方法', lastRequest.method === 'PUT' && lastRequest.url.endsWith('/projects/10/draft'))
  check('putDraftCommands: expected_revision 原样发出', lastRequest.body.expected_revision === 4)
  check('putDraftCommands: 命令组装包含 expected_revision 与 project_id', lastRequest.body.commands[0].expected_revision === 4 && lastRequest.body.commands[0].project_id === 10)
  check('putDraftCommands: 命令 unit 派生自 type 前缀', lastRequest.body.commands[0].unit === 'model')
  check('putDraftCommands: 成功直接消费返回新修订号', res.revision === 5)
}

// 409 冲突原样抛出
{
  let caught = null
  try {
    await putDraftCommands(10, [{ type: 'model.upsert_device', payload: { name: 'dev1' } }], 999)
  } catch (err) {
    caught = err
  }
  check('putDraftCommands: 409 冲突原样抛出', caught instanceof ApiError && caught.status === 409)
  check('putDraftCommands: 409 错误码保留', caught?.code === 'PROJ-REV-001')
}

// ---------------------------------------------------------------------------
// 2. sendDraftCommand: 先读视图草稿 revision 再 PUT
// ---------------------------------------------------------------------------
{
  const res = await sendDraftCommand(10, 'dataset.bind', { dataset_version_id: 12 })
  check('sendDraftCommand: 从视图读取 draft.revision(3) 作为 expected_revision 发出', lastRequest.body.expected_revision === 3)
  check('sendDraftCommand: 命令 unit 派生为 dataset', lastRequest.body.commands[0].unit === 'dataset')
  check('sendDraftCommand: 成功直接返回新修订号', res.revision === 4)
}

// ---------------------------------------------------------------------------
// 3. modelSectionFromView: 真实空数组与修订号透传
// ---------------------------------------------------------------------------
{
  const viewDto = {
    project: {
      id: 10,
      name: 'p10',
      description: null,
      status: 'active',
      owner_id: 1,
      currency: 'CNY',
      project_baseline: { resolution: '1h', start_year: 2026, scenario_mode: 'deterministic' },
      schema_version: 1,
      current_draft_id: 101,
      current_version_id: null,
      created_at: '2026-10-17T00:00:00Z',
      updated_at: null,
      created_by: 1,
    },
    draft: {
      id: 101,
      project_id: 10,
      revision: 7,
      content_object_id: 201,
      is_current: true,
      updated_by: 1,
      updated_at: '2026-10-17T00:00:00Z',
      created_at: '2026-10-17T00:00:00Z',
      content: {
        model: {
          devices: [
            { name: 'hp1', device_type: 'ies.device.heat_pump', kind: 'new', params: { cop: 3.2 } },
          ],
          connections: [
            { name: 'c1', from_device: 'hp1', from_port: 'heat_out', to_device: 'pipe1', to_port: 'heat_in' },
          ],
        },
      },
    },
    my_role: 'owner',
  }
  const section = modelSectionFromView(viewDto)
  check('modelSectionFromView: 修订号透传', section.revision === 7)
  check('modelSectionFromView: 设备映射', section.model.devices.length === 1 && section.model.devices[0].name === 'hp1' && section.model.devices[0].kind === 'new')
  check('modelSectionFromView: 连接映射', section.model.connections.length === 1 && section.model.connections[0].name === 'c1')

  // 空模型
  const emptyView = {
    ...viewDto,
    draft: {
      ...viewDto.draft,
      revision: 1,
      content: { model: { devices: [], connections: [] } },
    },
  }
  const emptySection = modelSectionFromView(emptyView)
  check('modelSectionFromView: 真实空数组保留', emptySection.revision === 1 && emptySection.model.devices.length === 0 && emptySection.model.connections.length === 0)
}

// ---------------------------------------------------------------------------
// 4. deviceTypeFromServer: 后端 selector.py 规范精确映射
// ---------------------------------------------------------------------------
{
  const typeDto = {
    type_id: 'ies.device.heat_pump',
    schema_version: '2.0.0',
    names: { 'zh-CN': '热泵', 'en-US': 'Heat Pump' },
    ports: [
      { name: 'electricity_in', direction: 'in', energy_carrier: 'electric', unit: 'kW' },
      { name: 'heat_out', direction: 'out', energy_carrier: 'heat', unit: 'kW' },
    ],
    parameters: {
      cop: { name: 'cop', unit: null, min: 1.0, max: 7.0, default: 3.5 },
      mode: { name: 'mode', unit: null, min: null, max: null, default: 'both' },
    },
  }
  const spec = deviceTypeFromServer(typeDto)
  check('deviceTypeFromServer: type_id 与 schema_version', spec.type_id === 'ies.device.heat_pump' && spec.schema_version === '2.0.0')
  check('deviceTypeFromServer: 多语言名称', spec.name_zh === '热泵' && spec.name_en === 'Heat Pump')
  check('deviceTypeFromServer: 载能去重派生', spec.energy_carriers.join(',') === 'electric,heat')
  check('deviceTypeFromServer: 端口映射', spec.ports.length === 2 && spec.ports[0].name === 'electricity_in' && spec.ports[0].direction === 'in')
  check('deviceTypeFromServer: 参数映射', spec.parameters.cop.name === 'cop' && spec.parameters.cop.default === 3.5 && spec.parameters.cop.min === 1.0)
  check('deviceTypeFromServer: 不包含编造属性', !('is_load' in spec) && !('capabilities' in spec) && !('model_method' in spec) && !('stateful' in spec) && !('version' in spec))
  check('deviceTypeFromServer: 参数不包含旧元数据', !('is_optimizable' in spec.parameters.cop) && !('existing_default' in spec.parameters.cop) && !('help_key' in spec.parameters.cop))
}

// ---------------------------------------------------------------------------
// 5. saveWorkingInputs: tasks feature 公开保存工作输入
// ---------------------------------------------------------------------------
{
  const working = { evaluation: { horizon_years: 20 } }
  // 带期望修订
  const res1 = await saveWorkingInputs(10, working, 4)
  check('saveWorkingInputs (带期望修订): 正确发送 PUT draft', lastRequest.method === 'PUT' && lastRequest.url.endsWith('/projects/10/draft'))
  check('saveWorkingInputs: 命令类型为 task.set_working_inputs', lastRequest.body.commands[0].type === 'task.set_working_inputs')
  check('saveWorkingInputs: 负载结构为 { working }', lastRequest.body.commands[0].payload.working?.evaluation?.horizon_years === 20)
  check('saveWorkingInputs: 返回新修订号', res1.revision === 5)

  // 无期望修订(先读草稿)
  const res2 = await saveWorkingInputs(10, working)
  check('saveWorkingInputs (无期望修订): 自动取视图 draft.revision(3) 发送', lastRequest.body.expected_revision === 3)
  check('saveWorkingInputs: 返回新修订号', res2.revision === 4)
}

// ---------------------------------------------------------------------------
if (failures > 0) {
  console.log(`\n失败: ${failures} 项检查未通过`)
  process.exitCode = 1
} else {
  console.log('\n项目草稿命令与模型节映射测试: 全部通过')
}
