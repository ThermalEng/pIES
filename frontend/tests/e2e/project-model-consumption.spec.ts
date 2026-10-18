// 项目已保存模型消费链验收(真实 GUI): 保存 → 选择器 → 拖放/参数/连线 → 装配冻结 → 真实求解。
//
// 原缺陷(修复前): 项目模型保存成功且清单可见, 但画布选择器只消费公开目录、
// 建模命令只认目录注册、装配闸门同样以未注册类型阻断 —— 已保存模型无法进入
// 画布与计算。本用例走真实 GUI 全链路验收修复后的公开消费路径。
//
// 被测动作(真实 GUI, 不可替代): YAML 页签保存项目模型 → 设备面板出现该类型
// (目录项并存) → 画布拖放 → 改名/改参数 → 连线 → 服务端草稿真值 → 装配预检 →
// 提交 → 真实 Worker 求解 → 结果页封存卡。项目/画像/装配/冻结/配置复用公开
// API 脚手架(financial-scaffold, 与 0.15 外部设备验收同模式); 不注入页面状态、
// 不 mock 求解、不预写结果。
//
// 样例形状(与 0.15 外部设备 constant_load 同模式): 属性驱动的消费侧恒定热负荷
// —— ``heat_in(in)`` 由 ``rated_demand_kw`` 恒等驱动。取"汇"而非同载体穿通
// (in+out)设备, 因为现行母线语义为"载体 × 无向连通分量"(装配
// solvability.build_buses / 生成器 _compute_buses 一致), 同载体 in/out 穿通
// 设备在单一母线上可被零流量旁路, 其损耗/效率无法成为求解值区分度; 汇的属性
// 需求为等式强制, 不随拓扑被旁路, 参数消费直接进入最优解。
//
// 手算关键量(K=1/N=2 → 全期 17520 h; 热负荷 30 + 项目负荷实例参数 20 = 50 kW
// 热需求, hp_cap 下界 50 kW 恰为最优):
//   hp 电耗 = 50/3.5 = 100/7 kW/h;
//   年购电 = 0.7×(100/7)×8760 = 87600 CNY
//     (若消费模型默认 10 则 70080; 若项目模型/关系未进 LP 则 52560, 三者互斥);
//   年固定运维 = 28×50 = 1400; 年变动运维 = 0.008×(100/7)×8760 = 1001.1428571428571;
//   建设总额 = 2800×50 = 140000; 付款 0.6/0.4 → CF[t=0]=-84000, CF[t=1]=-56000
//   (无维护, 全负现金流无 IRR);
//   目标(公开事实同构) = 2×87600 + 1400 + 2×1001.1428571428571 + 140000
//     = 318602.28571428571 CNY。
//
// oracle 说明: /tasks/{id}/inputs 按已验收契约不返回规范装配文本(体量大且无界面
// 消费者), 故实例参数 20 被冻结消费由求解值(87600 vs 默认 70080)与真实逐时序列
// (userload.heat_in ≡ 20)证明; 快照身份与模型数学(rated_demand_kw 单位/默认值/
// 关系引用)经 inputs 投影核对。
//
// 运行(仅在本工作树根目录执行: 数据目录即该工作树隔离 ./data, 勿从主线仓库根
// 执行以免落到主线数据; 项目名/端口任取隔离值, 不碰其他栈):
//   export IESPLAN_PORT=18087   # 任取空闲端口, 避免与其他栈冲突
//   docker compose -p pies-pmc up -d --build web backend io_worker compute_worker
//   docker compose -p pies-pmc --profile e2e run --rm --build --no-deps e2e \
//     npx playwright test project-model-consumption.spec.ts

import fs from 'node:fs'
import path from 'node:path'
import { test, expect } from '@playwright/test'
import type { BrowserContext, Page } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

const SINK_TYPE = 'acme.device.consumption_load'
const SINK_TYPE_FINAL = `${SINK_TYPE}_1`
const SINK_LABEL = '消费链负荷'
const SINK_NAME = 'userload'
const SINK_DEFAULT_DEMAND_KW = 10
const SINK_DEMAND_KW = 20
const HEATLOAD_DEMAND_KW = 30
const HP_COP = 3.5
const HP_ELEC_KW = (HEATLOAD_DEMAND_KW + SINK_DEMAND_KW) / HP_COP
const ANNUAL_HOURS = 8760
const EXPECTED_CONSTRUCTION_TOTAL = 140000
const EXPECTED_FIXED_OM = 28 * 50
const EXPECTED_VARIABLE_OM = 0.008 * HP_ELEC_KW * ANNUAL_HOURS
const EXPECTED_ENERGY_NET = 0.7 * HP_ELEC_KW * ANNUAL_HOURS
const EXPECTED_OBJECTIVE =
  2 * EXPECTED_ENERGY_NET + EXPECTED_FIXED_OM + 2 * EXPECTED_VARIABLE_OM + EXPECTED_CONSTRUCTION_TOTAL

/** 项目模型 YAML(与后端 test_project_model_consumption.SINK_YAML 同构)。 */
const SINK_YAML = `schema: ies.device-model
schema_version: "2.0.0"
device:
  id: acme.device.consumption_load
  names:
    zh-CN: 消费链负荷
    en-US: Consumption Load
properties:
  rated_demand_kw:
    value: 10
    unit: kW
    valid_range:
      minimum: 0
      maximum: 100000
interfaces:
  heat_in:
    type: in
    carrier: heat
    unit: kW
    valid_range:
      minimum: 0
      maximum: null
equations:
  variables: {}
  relations:
    - id: serve
      expression: "heat_in[t] = rated_demand_kw"
`

const escapeRegExp = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** 确认 → 提交(K=1/N=2/付款 0.6+0.4/无维护), 业务步骤经 UI。 */
async function submitEvaluationViaUI(page: Page): Promise<void> {
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
  await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
  await page.locator('#task-type').selectOption('optimization')
  await page.locator('#task-construction-period').fill('1')
  await page.locator('#task-operation-years').fill('2')
  await page.locator('#task-discount-rate').fill('0.08')
  await page.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
  await page.locator('#task-maintenance-expenses').fill('[]')
  const submitBtn = page.getByRole('button', { name: /提交任务|Submit task/i }).last()
  await expect(submitBtn).toBeEnabled({ timeout: 10_000 })
  await submitBtn.click({ timeout: 30_000 })
  await expect(page.locator('.ies-table__row').first()).toBeVisible({ timeout: 15_000 })
}

/** 完成(真实 Worker 求解, 轮询详情状态)。 */
async function waitCompleted(page: Page): Promise<void> {
  await page.locator('.ies-table__row').first().click({ timeout: 30_000 })
  await expect(page.locator('.ies-task-detail')).toBeVisible({ timeout: 10_000 })
  const detailBadge = page.locator('.ies-task-detail .ies-badge__label').first()
  await expect(async () => {
    const text = (await detailBadge.textContent()) ?? ''
    expect(text).toMatch(/已完成|Completed/i)
  }).toPass({ timeout: 240_000, intervals: [10_000] })
}

/** 详情 → 结果页。 */
async function openResults(page: Page): Promise<void> {
  const viewResults = page.getByRole('link', { name: /查看结果|View results/i })
  await expect(viewResults).toBeVisible({ timeout: 10_000 })
  await viewResults.click({ timeout: 30_000 })
  await expect(page).toHaveURL(/\/results/)
}

test.describe('项目已保存模型消费链', () => {
  // 全期 17520 h 真实求解超过默认 90s 用例超时, 本文件单独放宽。
  test.setTimeout(900_000)
  test('保存模型→选择器/拖放/参数/连线→装配→真实求解→结果', async ({ browser }) => {
    const username = uniqueName('es-projmodel')
    const password = strongPassword('Init')
    const projectName = uniqueName('项目模型消费项目')
    const profileId = `proj-model-${Date.now()}`
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    // 其余配置复用公开 API 脚手架(无内置电负荷: 购电只含热泵电耗, 与手算一致)。
    const pid = await scaffoldProject(requestJson, apiCtx.request, token, projectName, profileId, {
      includeElectricLoad: false,
      outputs: ['solution', 'metrics'],
    })

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+$/)

    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('系统建模').click()
    await expect(page).toHaveURL(/\/model$/)

    const draftPut = () =>
      page.waitForResponse(
        (res) =>
          res.request().method() === 'PUT' &&
          new URL(res.url()).pathname === `/api/projects/${pid}/draft` &&
          res.status() === 200,
        { timeout: 30_000 },
      )

    // 1) 项目模型保存(真实 UI: 新建模型页 YAML 页签); 后端分配项目内编号 _1。
    await page.getByRole('button', { name: /新建模型|New model/i }).click()
    await expect(page).toHaveURL(/\/model\/new$/)
    await page.getByRole('tab', { name: /直接编辑 YAML|Edit YAML directly/i }).click()
    const yamlEditor = page.getByLabel(/设备模型 YAML|Device model YAML/i)
    await expect(yamlEditor).toBeVisible({ timeout: 15_000 })
    await yamlEditor.fill(SINK_YAML)
    await page.getByRole('button', { name: /提交为候选|Submit as candidate/i }).click()
    await expect(page.getByRole('status', { name: /正式已保存|Saved/i })).toBeVisible({ timeout: 30_000 })
    const savedPanel = page.locator('.ies-modeling__saved')
    await expect(savedPanel.getByText(new RegExp(escapeRegExp(SINK_TYPE_FINAL))).first()).toBeVisible()
    await expect(page.getByText(/模型已进入项目模型列表|ready for assembly/i)).toBeVisible()

    // 2) 回画布: 选择器按项目作用域重新加载 —— 已保存模型出现, 目录设备并存。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('系统建模').click()
    await expect(page).toHaveURL(/\/model$/)
    const canvas = page.locator('.mp-canvas-wrap')
    const palette = page.locator('.mp-palette')
    await expect(palette).toBeVisible({ timeout: 30_000 })
    await expect(palette.locator('[aria-label*="热泵"]').first()).toBeVisible({ timeout: 15_000 })
    const sinkItem = palette.locator(`[aria-label*="${SINK_LABEL}"]`).first()
    await sinkItem.scrollIntoViewIfNeeded()
    await expect(sinkItem).toBeVisible({ timeout: 15_000 })
    // 脚手架两条连线: grid1-hp1, hp1-heatload。
    await expect(page.locator('.react-flow__edge')).toHaveCount(2, { timeout: 15_000 })

    // 3) 拖放 → 画布出现新设备节点(默认名 = 本地化名, 参数取项目模型默认值)。
    const itemBox = await sinkItem.boundingBox()
    const canvasBox = await canvas.boundingBox()
    expect(itemBox && canvasBox).toBeTruthy()
    const dropSave = draftPut()
    await page.mouse.move(itemBox!.x + itemBox!.width / 2, itemBox!.y + itemBox!.height / 2)
    await page.mouse.down()
    await page.mouse.move(
      canvasBox!.x + canvasBox!.width * 0.35,
      canvasBox!.y + canvasBox!.height * 0.72,
      { steps: 8 },
    )
    await page.mouse.up()
    await dropSave
    const sinkNode = page.locator('.react-flow__node', { hasText: SINK_LABEL }).first()
    await expect(sinkNode).toBeVisible({ timeout: 15_000 })

    // 端口(来自项目模型 YAML): heat_in(in) 左句柄, 无其他句柄(纯汇)。
    await expect(sinkNode.locator('.mp-handle')).toHaveCount(1)
    await expect(sinkNode.locator('.react-flow__handle-left.mp-handle--heat')).toHaveCount(1)
    await expect(sinkNode.locator('.react-flow__handle-right')).toHaveCount(0)

    // 4) 改名 + 改参数(侧栏默认值 10 来自已保存模型 schema):
    //    默认值断言证明选择器消费的是项目模型参数, 而非目录兜底。
    await sinkNode.click()
    const nameInput = page.locator('#mp-device-name')
    await expect(nameInput).toBeVisible({ timeout: 10_000 })
    await expect(page.locator('.mp-sidebar__section').first()).toHaveText(SINK_TYPE_FINAL)
    const renameSave = draftPut()
    await nameInput.fill(SINK_NAME)
    await nameInput.press('Tab')
    await renameSave
    const sinkNodeNamed = page.locator('.react-flow__node', { hasText: SINK_NAME }).first()
    await expect(sinkNodeNamed).toBeVisible({ timeout: 15_000 })

    const demandInput = page.locator('input[aria-label="rated_demand_kw (kW)"]')
    await expect(demandInput).toBeVisible({ timeout: 10_000 })
    await expect(demandInput).toHaveValue(String(SINK_DEFAULT_DEMAND_KW))
    const paramSave = draftPut()
    await demandInput.fill(String(SINK_DEMAND_KW))
    await demandInput.press('Tab')
    await paramSave
    // 关闭参数侧栏(画布恢复全宽), 再连线, 避免面板占位影响命中。
    await page.locator('.mp-sidebar').getByRole('button', { name: /关闭|Close/i }).click()
    await expect(page.locator('.mp-sidebar')).toHaveCount(0)

    // 5) 连线: hp1.heat_out(out) → userload.heat_in(in)(真实鼠标拖拽; 同源出句柄
    //    可接多个消费汇, 与脚手架 c-hp-heat 并存)。
    const hpNode = page.locator('.react-flow__node', { hasText: 'hp1' }).first()
    // 成功连线后画布自动选中新连接并打开连接面板(占位会盖住右侧节点句柄),
    // 拖拽前须关闭面板恢复全宽画布。
    const closeSidebar = async () => {
      const sidebar = page.locator('.mp-sidebar')
      if ((await sidebar.count()) > 0) {
        await sidebar.getByRole('button', { name: /关闭|Close/i }).click()
        await expect(page.locator('.mp-sidebar')).toHaveCount(0)
      }
    }
    await closeSidebar()
    await expect(hpNode).toBeVisible({ timeout: 10_000 })
    await expect(sinkNodeNamed).toBeVisible({ timeout: 10_000 })
    const src = await hpNode.locator('.react-flow__handle-right.mp-handle--heat').boundingBox()
    const dst = await sinkNodeNamed.locator('.react-flow__handle-left.mp-handle--heat').boundingBox()
    expect(src && dst).toBeTruthy()
    const connectSave = draftPut()
    await page.mouse.move(src!.x + src!.width / 2, src!.y + src!.height / 2)
    await page.mouse.down()
    await page.mouse.move(dst!.x + dst!.width / 2, dst!.y + dst!.height / 2, { steps: 12 })
    await page.mouse.up()
    await connectSave
    await closeSidebar()
    // 三条连接: grid1-hp1 + hp1-heatload(脚手架) + hp1→userload。
    await expect(page.locator('.react-flow__edge')).toHaveCount(3, { timeout: 10_000 })

    // 6) 服务端草稿即真值: 项目模型类型 / 真实端口名 / UI 参数 / 脚手架连线原样。
    const viewResp = await page.request.get(`/api/projects/${pid}`)
    expect(viewResp.ok()).toBeTruthy()
    const view = (await viewResp.json()) as {
      draft: {
        revision: number
        content: {
          model: {
            devices: Array<{ name: string; device_type: string; kind: string; params: Record<string, unknown> }>
            connections: Array<{
              name: string
              from_device: string
              from_port: string
              to_device: string
              to_port: string
            }>
          }
        }
      }
    }
    const draftModel = view.draft.content.model
    const draftDevice = draftModel.devices.find((d) => d.name === SINK_NAME)
    expect(draftDevice).toBeTruthy()
    expect(draftDevice!.device_type).toBe(SINK_TYPE_FINAL)
    expect(draftDevice!.kind).toBe('new')
    expect(draftDevice!.params.rated_demand_kw).toBe(SINK_DEMAND_KW)
    // 三条连接: grid1→hp1(脚手架) + hp1→heatload(脚手架) + hp1→userload(新)。
    expect(draftModel.connections).toHaveLength(3)
    const toSink = draftModel.connections.find((c) => c.to_device === SINK_NAME)
    expect(toSink).toBeTruthy()
    expect(toSink!.from_device).toBe('hp1')
    expect(toSink!.from_port).toBe('heat_out')
    expect(toSink!.to_port).toBe('heat_in')
    expect(
      draftModel.connections.find((c) => c.from_device === 'hp1' && c.to_device === 'heatload'),
    ).toBeTruthy()

    // 7) 离开画布(卸载自动保存)后经当前 UI 会话补齐新实例的财务绑定覆盖
    //    (装配配置, 非被测画布动作), 再跑装配预检。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    const rev = view.draft.revision
    const finBindResp = await page.request.put(`/api/projects/${pid}/draft`, {
      data: {
        expected_revision: rev,
        commands: [
          {
            id: 'proj-model-finbind',
            unit: 'assembly',
            type: 'assembly.set_finance_binding',
            payload: {
              instances: {
                grid1: { type: 'none' },
                heatload: { type: 'none' },
                userload: { type: 'none' },
                hp1: {
                  type: 'costed',
                  finance_type: 'heat_pump_system',
                  drivers: {
                    heat_kw: { property: 'rated_heat_kw' },
                    consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
                  },
                },
              },
            },
          },
        ],
      },
    })
    expect(finBindResp.ok(), await finBindResp.text()).toBeTruthy()
    const precheckResp = await page.request.post(`/api/projects/${pid}/validation/run`, { data: {} })
    expect(precheckResp.ok(), await precheckResp.text()).toBeTruthy()
    const precheck = await precheckResp.json()
    const blockers = (precheck.report?.diagnostics ?? []).filter(
      (d: { blocking: boolean }) => d.blocking,
    )
    expect(blockers).toEqual([])

    // 8) 提交(K=1/N=2) → 真实 Worker 求解 → 完成 → 结果页封存卡。
    await submitEvaluationViaUI(page)
    await waitCompleted(page)
    await openResults(page)
    await expect(page.getByText(/评价现金流\(封存\)|Sealed evaluation/i)).toBeVisible({ timeout: 30_000 })
    // 会话内读取封存卡展示的建设总额, 与 HTTP oracle 对账在其后完成
    // (重新登录取 token 会顶掉 UI 会话, 故所有 UI 读取先于取 token)。
    const ctorLabel = page.getByText(/建设总额|Construction total/i).first()
    await expect(ctorLabel).toBeVisible({ timeout: 30_000 })
    const pageConstructionTotal = (
      (await ctorLabel.locator('xpath=following-sibling::dd[1]').textContent()) ?? ''
    ).trim()
    // UI 阶段到此结束: 噪声检查与关闭 context 都须在重新取 token 之前。
    // 重新登录取 token 会顶掉 UI 会话, 仍打开的结果页后台请求随即 401,
    // 属测试自身动作制造的预期外交互; 若拖到最后再查, 该噪声随时序竞态。
    session.assertNoErrors()
    await context.close()

    // 9) HTTP oracle: 快照身份(项目模型 _1) + 模型数学 + 求解值(实例参数 20 被消费)。
    const token2 = await loginToken(apiCtx.request, username, password)
    const listed = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks?limit=10`, undefined, token2)
    const taskId: number = (listed.items ?? [])[0].id
    expect(taskId).toBeTruthy()
    const inputsResp = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/inputs`, undefined, token2,
    )
    const inputs = inputsResp.inputs
    const snapshotId = inputs.task.calc_snapshot_id
    expect(snapshotId).toBeTruthy()
    const frozenSink = inputs.snapshot.device_models[SINK_NAME]
    expect(frozenSink.device_id).toBe(SINK_TYPE_FINAL)
    expect(frozenSink.variables.rated_demand_kw.unit).toBe('kW')
    expect(frozenSink.variables.rated_demand_kw.value).toBe(SINK_DEFAULT_DEMAND_KW)
    expect(frozenSink.interfaces.heat_in.type).toBe('in')
    expect(frozenSink.interfaces.heat_in.carrier).toBe('heat')
    expect(
      frozenSink.relations.some((r: { rhs_refs: Array<{ name: string }> }) =>
        (r.rhs_refs ?? []).some((x) => x.name === 'rated_demand_kw'),
      ),
    ).toBeTruthy()

    const result = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, token2,
    )
    expect(result.result.snapshot_refs.snapshot_id).toBe(snapshotId)
    const hpCap = (result.result.planning_solution?.params ?? []).find(
      (p: { variable: string }) => p.variable === 'hp_cap',
    )
    expect(hpCap?.value).toBeCloseTo(50, 6)
    expect(hpCap?.unit).toBe('kW')
    const fin = result.result.financial_evaluation
    expect(fin.status).toBe('evaluated')
    expect(result.result.financial_absence).toBeNull()
    expect(Math.abs(Number(fin.construction_total) - EXPECTED_CONSTRUCTION_TOTAL)).toBeLessThan(0.01)
    expect(Number(fin.evaluation.rows[0].net)).toBeCloseTo(-0.6 * EXPECTED_CONSTRUCTION_TOTAL, 6)
    expect(Number(fin.evaluation.rows[1].net)).toBeCloseTo(-0.4 * EXPECTED_CONSTRUCTION_TOTAL, 6)
    expect(fin.evaluation.irr_status).toBe('no_solution')
    const ao = fin.annual_operation as Array<{
      energy_net: string
      fixed_om: string
      variable_om: string
      net: string
    }>
    expect(ao).toHaveLength(2)
    for (const row of ao) {
      // 87600(实例 20) vs 70080(模型默认 10) vs 52560(项目模型/关系未进 LP):
      // 证明实例参数确被冻结消费且关系真实进入求解。
      expect(Number(row.energy_net)).toBeCloseTo(-EXPECTED_ENERGY_NET, 6)
      expect(Number(row.fixed_om)).toBeCloseTo(EXPECTED_FIXED_OM, 6)
      expect(Number(row.variable_om)).toBeCloseTo(EXPECTED_VARIABLE_OM, 6)
    }
    // objectiveFromPublicFacts: 脚手架 objective 原文(全期购电 + 建设 +
    // 1 年固定运维 + 全期变动运维)按公开封存财务事实的同构展开, 非读取
    // 求解器目标字段(LP 目标值未公开投影); 断言容差 <1 CNY。
    const objectiveFromPublicFacts =
      Math.abs(Number(ao[0].energy_net)) + Math.abs(Number(ao[1].energy_net)) +
      Number(ao[0].fixed_om) +
      Number(ao[0].variable_om) + Number(ao[1].variable_om) +
      Number(fin.construction_total)
    expect(Math.abs(objectiveFromPublicFacts - EXPECTED_OBJECTIVE)).toBeLessThan(1)

    // 10) 项目模型的真实逐时结果: 身份/单位来自项目模型端口声明, 值恒为 GUI 参数。
    const refs = result.result.hourly_refs as Array<{
      rows: number
      series: Array<{ series_id: string; instance_id: string; variable_id: string; unit: string }>
    }>
    expect(refs.length).toBeGreaterThan(0)
    expect(refs[0].rows).toBe(17520)
    const sinkSeries = `${SINK_NAME}.heat_in`
    const sinkMeta = refs[0].series.find((s) => s.series_id === sinkSeries)
    expect(sinkMeta).toBeTruthy()
    expect(sinkMeta!.instance_id).toBe(SINK_NAME)
    expect(sinkMeta!.variable_id).toBe('heat_in')
    expect(sinkMeta!.unit).toBe('kW')
    const hourly = await requestJson(
      apiCtx.request,
      'GET',
      `/api/projects/${pid}/tasks/${taskId}/result/hourly?field=${sinkSeries}&start=0&end=17520&limit=17520`,
      undefined,
      token2,
    )
    expect(hourly.total_rows).toBe(17520)
    expect(hourly.values).toHaveLength(17520)
    expect(hourly.instance_id).toBe(SINK_NAME)
    expect(hourly.variable_id).toBe('heat_in')
    expect(hourly.unit).toBe('kW')
    expect(Math.min(...(hourly.values as number[]))).toBeCloseTo(SINK_DEMAND_KW, 6)
    expect(Math.max(...(hourly.values as number[]))).toBeCloseTo(SINK_DEMAND_KW, 6)

    // 结果页封存卡展示与 oracle 同一手算事实(建设总额), 非只检查页面可达。
    expect(pageConstructionTotal).toContain(fin.construction_total)

    // 证据落盘(本地, 不入库)。
    const outDir = process.env.E2E_OUTPUT_DIR ?? '/app/e2e/test-results'
    fs.writeFileSync(
      path.join(outDir, 'project-model-consumption.json'),
      JSON.stringify(
        {
          pid,
          taskId,
          snapshotId,
          sinkType: SINK_TYPE_FINAL,
          sinkDemandKw: SINK_DEMAND_KW,
          draftDevice,
          draftConnections: draftModel.connections,
          frozenSinkDeviceId: frozenSink.device_id,
          hpCap: hpCap?.value,
          objectiveFromPublicFacts,
          constructionTotal: fin.construction_total,
          energyNetPerYear: ao.map((r) => r.energy_net),
          fixedOmPerYear: ao.map((r) => r.fixed_om),
          variableOmPerYear: ao.map((r) => r.variable_om),
          hourlyUserloadHeatInKw: {
            rows: hourly.total_rows,
            min: Math.min(...(hourly.values as number[])),
            max: Math.max(...(hourly.values as number[])),
          },
          pageConstructionTotal,
        },
        null,
        2,
      ),
    )
    console.log(
      `[evidence] project-model-consumption: project=${pid} task=${taskId} snapshot=${snapshotId} ` +
        `sink=${SINK_TYPE_FINAL} demand_kw=${SINK_DEMAND_KW} hp_cap=${hpCap?.value} ` +
        `construction_total=${fin.construction_total} energy_net=${ao.map((r) => r.energy_net).join(',')} ` +
        `objective=${objectiveFromPublicFacts} hourly_heat_in=${Math.min(...(hourly.values as number[]))} ` +
        `page_construction_total=${pageConstructionTotal}`,
    )

    await apiCtx.close()
  })
})
