// 0.15 首切片: 外部设备 YAML 注入公开消费链验收。
//
// 外部内容: frontend/tests/e2e/device-catalog-external/constant_load.yaml
//   (schema_version 1.0.0, 稳定 ID acme.device.constant_load) 经隔离覆盖
//   frontend/tests/e2e/docker-compose.ext-device.yml 只读挂载到扫描目录
//   新子目录; 不修改内置 catalog/生产 Compose/通用 runtime, 核心代码与
//   前端无该 ID 分支。2.0.0 vs 格式页目标 2.1.0 是既有差距, 本用例不
//   宣称 2.1.0 已交付。
//
// 被测动作(真实 GUI, 不可替代): 设备选择器 → 画布拖放 → 侧栏参数 →
// 连线 → 服务端草稿。项目/画像/装配/冻结/配置复用公开 API 脚手架
// (financial-scaffold, 与 0.14 财务验收同模式); 不注入页面状态、不
// mock 求解、不预写结果。非法 YAML/重复 ID 的拒收由定向 Docker 公开
// 初始化验证承担(见 docs/reviews 证据), 不在本用例重建整套 GUI 栈。
//
// 手算关键量(K=1/N=2 → 全期 17520 h; 2025/2026 均平年):
//   hp 电耗 = heat_demand 30 / cop 3.5 = 60/7 kW/h; 外部负荷 = 40 kW/h;
//   目标 = 0.7×(40+60/7)×17520 + 2800×50 + 28×50 + 0.008×(60/7)×17520
//        = 595680 + 140000 + 1400 + 1201.3714285714
//        = 738281.3714285714 CNY
//   (hp_cap 下界 50 kW 即最优: 成本随容量单调增; 60/7 随热负荷固定)。
//   建设总额 = 2800×50 = 140000 CNY; 付款 0.6/0.4 → CF[t=0]=-84000,
//   CF[t=1]=-56000 (无维护, 全负现金流无 IRR)。
//   封存年值: 购电 0.7×340/7×8760 = -297840, 固定运维 28×50 = 1400,
//   变动运维 0.008×60/7×8760 = 600.6857142857142 (CNY/a)。
//
// oracle 说明: LP 目标值字段(metrics_summary)按 0.14-R1/R2 已验收契约
// 对内置求解链缺席(runner 只发布 compute_result 块), 不补默认; 手算目标
// 值改用公开封存投影复核——planning_solution.params.hp_cap=50 kW +
// 封存现金流(建设总额/付款/年值)按 objective 原文同构展开 +
// 外部设备逐时 series(extload1.electricity_in)恒 40 kW。
//
// 测试入口: 默认套件(playwright.config.ts)以 testIgnore 排除本规格(标准
// 部署无外部挂载, 收集它必红); 本规格由专用配置
// playwright.ext-device.config.ts 清除该排除并恰好收集一项——不使用 skip,
// 也不在设备缺失时直接通过。
//
// 运行(仅在本工作树根目录执行: 数据目录即该工作树隔离 ./data, 勿从主线
// 仓库根执行以免落到主线数据; 项目名/端口任取隔离值, 不碰其他栈;
// 见 e2e README 同节):
//   export PIES_EXT_DEVICE_DIR="$PWD/frontend/tests/e2e/device-catalog-external"
//   export IESPLAN_PORT=18085   # 任取空闲端口, 避免与其他栈冲突
//   docker compose -p pies-ext-dev -f docker-compose.yml \
//     -f frontend/tests/e2e/docker-compose.ext-device.yml up -d --build \
//     web backend io_worker compute_worker
//   docker compose -p pies-ext-dev -f docker-compose.yml \
//     -f frontend/tests/e2e/docker-compose.ext-device.yml --profile e2e \
//     run --rm --build --no-deps e2e npx playwright test \
//     --config playwright.ext-device.config.ts

import fs from 'node:fs'
import path from 'node:path'
import { test, expect } from '@playwright/test'
import type { BrowserContext, Page } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

const EXT_DEVICE_TYPE = 'acme.device.constant_load'
const EXT_DEVICE_LABEL = '恒定电负荷'
const EXT_DEVICE_NAME = 'extload1'
const EXT_DEMAND_KW = 40
const EXPECTED_OBJECTIVE_CNY = 738281.3714285714
const EXPECTED_CONSTRUCTION_TOTAL = 140000
const EXPECTED_ENERGY_NET_CNY = 297840
const EXPECTED_FIXED_OM_CNY = 1400
const EXPECTED_VARIABLE_OM_CNY = 600.6857142857142

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

test.describe('0.15 外部设备 YAML 注入', () => {
  // 全期 17520 h 真实求解超过默认 90s 用例超时, 本文件单独放宽。
  test.setTimeout(900_000)
  test('选择器→拖放/参数/连线→装配→真实求解→结果', async ({ browser }) => {
    const username = uniqueName('es-extdev')
    const password = strongPassword('Init')
    const projectName = uniqueName('外部设备项目')
    const profileId = `ext-dev-${Date.now()}`
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    const token = await loginToken(apiCtx.request, username, password)
    // 其余配置复用公开 API 脚手架(无内置电负荷, 由外部设备承担该角色)。
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

    const canvas = page.locator('.mp-canvas-wrap')
    const palette = page.locator('.mp-palette')
    await expect(palette).toBeVisible({ timeout: 30_000 })

    // 选择器: 外部 YAML 类型出现在设备面板(注册表公开数据路径, 无 ID 分支)。
    const extItem = palette.locator(`[aria-label*="${EXT_DEVICE_LABEL}"]`).first()
    await extItem.scrollIntoViewIfNeeded()
    await expect(extItem).toBeVisible({ timeout: 15_000 })

    // 拖放 → 画布出现新设备节点(默认名 = 本地化名 + 序号)。
    const itemBox = await extItem.boundingBox()
    const canvasBox = await canvas.boundingBox()
    expect(itemBox && canvasBox).toBeTruthy()
    await page.mouse.move(itemBox!.x + itemBox!.width / 2, itemBox!.y + itemBox!.height / 2)
    await page.mouse.down()
    await page.mouse.move(
      canvasBox!.x + canvasBox!.width * 0.3,
      canvasBox!.y + canvasBox!.height * 0.72,
      { steps: 8 },
    )
    await page.mouse.up()
    const extNode = page.locator('.react-flow__node', { hasText: EXT_DEVICE_LABEL }).first()
    await expect(extNode).toBeVisible({ timeout: 15_000 })
    await expect(page.locator('.mp-save-state--saved, .wb-autosave--saved').first()).toBeVisible({ timeout: 15_000 })
    await page.waitForTimeout(500)

    // 端口(来自 YAML descriptor): 仅 electricity_in(in) 一个左句柄, 零右句柄。
    await expect(extNode.locator('.mp-handle')).toHaveCount(1)
    await expect(extNode.locator('.react-flow__handle-left.mp-handle--electricity')).toHaveCount(1)
    await expect(extNode.locator('.react-flow__handle-right')).toHaveCount(0)

    // 参数: 侧栏按注册表 schema 渲染 rated_demand_kw; 改名 + 设定真实负荷 40 kW。
    await extNode.click()
    const nameInput = page.locator('#mp-device-name')
    await expect(nameInput).toBeVisible({ timeout: 10_000 })
    const renameSave = draftPut()
    await nameInput.fill(EXT_DEVICE_NAME)
    await nameInput.press('Tab')
    await renameSave
    const extNodeNamed = page.locator('.react-flow__node', { hasText: EXT_DEVICE_NAME }).first()
    await expect(extNodeNamed).toBeVisible({ timeout: 15_000 })

    const demandInput = page.locator('input[aria-label="rated_demand_kw (kW)"]')
    await expect(demandInput).toBeVisible({ timeout: 10_000 })
    const paramSave = draftPut()
    await demandInput.fill(String(EXT_DEMAND_KW))
    await demandInput.press('Tab')
    await paramSave
    // 关闭参数侧栏(画布恢复全宽), 再连线, 避免面板占位影响命中。
    await page.locator('.mp-sidebar').getByRole('button', { name: /关闭|Close/i }).click()
    await expect(page.locator('.mp-sidebar')).toHaveCount(0)

    // 连线: 电网 electricity_import(out) → 外部设备 electricity_in(in), 真实鼠标拖拽。
    const gridNode = page.locator('.react-flow__node', { hasText: 'grid1' }).first()
    const gridOut = gridNode.locator('.mp-handle--electricity').last()
    const extIn = extNodeNamed.locator('.mp-handle--electricity').first()
    await expect(gridOut).toBeVisible({ timeout: 10_000 })
    await expect(extIn).toBeVisible({ timeout: 10_000 })
    const connectSave = draftPut()
    const src = await gridOut.boundingBox()
    const dst = await extIn.boundingBox()
    expect(src && dst).toBeTruthy()
    await page.mouse.move(src!.x + src!.width / 2, src!.y + src!.height / 2)
    await page.mouse.down()
    await page.mouse.move(dst!.x + dst!.width / 2, dst!.y + dst!.height / 2, { steps: 12 })
    await page.mouse.up()
    await connectSave
    // 三条连接: grid1-hp1, hp1-heatload(脚手架) + grid1-extload1(本用例)。
    await expect(page.locator('.react-flow__edge')).toHaveCount(3, { timeout: 10_000 })

    // 服务端草稿即真值: 设备类型 / 真实端口名 / 关键参数。
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
    const draftDevice = draftModel.devices.find((d) => d.name === EXT_DEVICE_NAME)
    expect(draftDevice).toBeTruthy()
    expect(draftDevice!.device_type).toBe(EXT_DEVICE_TYPE)
    expect(draftDevice!.kind).toBe('new')
    expect(draftDevice!.params.rated_demand_kw).toBe(EXT_DEMAND_KW)
    const draftConn = draftModel.connections.find((c) => c.to_device === EXT_DEVICE_NAME)
    expect(draftConn).toBeTruthy()
    expect(draftConn!.from_device).toBe('grid1')
    expect(draftConn!.from_port).toBe('electricity_import')
    expect(draftConn!.to_port).toBe('electricity_in')

    // 离开画布(卸载自动保存)后经当前 UI 会话补齐新实例的财务绑定覆盖
    // (装配配置, 非被测画布动作), 再跑装配预检。此处不复用造数 token:
    // 重新登录会顶掉进行中的 UI 会话, 后续提交仍需该会话。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    const rev = view.draft.revision
    const finBindResp = await page.request.put(`/api/projects/${pid}/draft`, {
      data: {
        expected_revision: rev,
        commands: [
          {
            id: 'ext-dev-finbind',
            unit: 'assembly',
            type: 'assembly.set_finance_binding',
            payload: {
              instances: {
                grid1: { type: 'none' },
                heatload: { type: 'none' },
                extload1: { type: 'none' },
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

    // 提交(K=1/N=2) → 真实 Worker 求解 → 完成 → 结果页封存卡。
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

    // HTTP oracle: 快照身份 + 结果身份 + 可手算关键量。
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
    const frozenExt = inputs.snapshot.device_models[EXT_DEVICE_NAME]
    expect(frozenExt.device_id).toBe(EXT_DEVICE_TYPE)
    expect(frozenExt.interfaces.electricity_in.type).toBe('in')
    expect(frozenExt.interfaces.electricity_in.carrier).toBe('electricity')
    expect(frozenExt.interfaces.electricity_in.unit).toBe('kW')
    expect(frozenExt.variables.rated_demand_kw.unit).toBe('kW')
    expect(
      frozenExt.relations.some((r: { rhs_refs: Array<{ name: string }> }) =>
        (r.rhs_refs ?? []).some((x) => x.name === 'rated_demand_kw'),
      ),
    ).toBeTruthy()

    const result = await requestJson(
      apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}/result`, undefined, token2,
    )
    // 指标摘要: 0.14-R1/R2 已验收契约——runner 只发布 compute_result 块,
    // 内置求解链下缺席是真实形态; 不作为缺陷、不补默认。
    expect(result.result.metrics_summary).toBeNull()
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
      expect(Number(row.energy_net)).toBeCloseTo(-EXPECTED_ENERGY_NET_CNY, 6)
      expect(Number(row.fixed_om)).toBeCloseTo(EXPECTED_FIXED_OM_CNY, 6)
      expect(Number(row.variable_om)).toBeCloseTo(EXPECTED_VARIABLE_OM_CNY, 6)
    }
    // objectiveFromPublicFacts: 脚手架 objective 原文(全期购电 + 建设 +
    // 1 年固定运维 + 全期变动运维)按公开封存财务事实的同构展开, 非读取
    // 求解器目标字段(LP 目标值未公开投影); 断言容差 <1 CNY。
    const objectiveFromPublicFacts =
      Math.abs(Number(ao[0].energy_net)) + Math.abs(Number(ao[1].energy_net)) +
      Number(ao[0].fixed_om) +
      Number(ao[0].variable_om) + Number(ao[1].variable_om) +
      Number(fin.construction_total)
    expect(Math.abs(objectiveFromPublicFacts - EXPECTED_OBJECTIVE_CNY)).toBeLessThan(1)

    // 外部设备的真实逐时结果: 身份/单位来自 YAML 端口声明, 值恒为 GUI 参数。
    const refs = result.result.hourly_refs as Array<{
      rows: number
      series: Array<{ series_id: string; instance_id: string; variable_id: string; unit: string }>
    }>
    expect(refs.length).toBeGreaterThan(0)
    expect(refs[0].rows).toBe(17520)
    const extSeries = `${EXT_DEVICE_NAME}.electricity_in`
    const extMeta = refs[0].series.find((s) => s.series_id === extSeries)
    expect(extMeta).toBeTruthy()
    expect(extMeta!.instance_id).toBe(EXT_DEVICE_NAME)
    expect(extMeta!.variable_id).toBe('electricity_in')
    expect(extMeta!.unit).toBe('kW')
    const hourly = await requestJson(
      apiCtx.request,
      'GET',
      `/api/projects/${pid}/tasks/${taskId}/result/hourly?field=${extSeries}&start=0&end=17520&limit=17520`,
      undefined,
      token2,
    )
    expect(hourly.total_rows).toBe(17520)
    expect(hourly.values).toHaveLength(17520)
    expect(hourly.instance_id).toBe(EXT_DEVICE_NAME)
    expect(hourly.variable_id).toBe('electricity_in')
    expect(hourly.unit).toBe('kW')
    expect(Math.min(...(hourly.values as number[]))).toBeCloseTo(EXT_DEMAND_KW, 6)
    expect(Math.max(...(hourly.values as number[]))).toBeCloseTo(EXT_DEMAND_KW, 6)

    // 结果页封存卡展示与 oracle 同一手算事实(建设总额), 非只检查页面可达。
    expect(pageConstructionTotal).toContain(fin.construction_total)

    // 证据落盘(本地, 不入库)。
    const outDir = process.env.E2E_OUTPUT_DIR ?? '/app/e2e/test-results'
    fs.writeFileSync(
      path.join(outDir, 'device-yaml-extension.json'),
      JSON.stringify(
        {
          pid,
          taskId,
          snapshotId,
          extDeviceType: EXT_DEVICE_TYPE,
          extDemandKw: EXT_DEMAND_KW,
          draftDevice,
          draftConnection: draftConn,
          frozenDeviceId: frozenExt.device_id,
          hpCap: hpCap?.value,
          objectiveFromPublicFacts,
          constructionTotal: fin.construction_total,
          energyNetPerYear: ao.map((r) => r.energy_net),
          variableOmPerYear: ao.map((r) => r.variable_om),
          fixedOmPerYear: ao.map((r) => r.fixed_om),
          hourlyExtload1Kw: {
            rows: hourly.total_rows,
            min: Math.min(...(hourly.values as number[])),
            max: Math.max(...(hourly.values as number[])),
          },
        },
        null,
        2,
      ),
    )
    console.log(
      `[evidence] device-yaml-extension: project=${pid} task=${taskId} snapshot=${snapshotId} ` +
        `device=${EXT_DEVICE_TYPE} demand_kw=${EXT_DEMAND_KW} hp_cap=${hpCap?.value} ` +
        `objective_from_public=${objectiveFromPublicFacts} construction_total=${fin.construction_total} ` +
        `hourly_extload1_kw=${Math.min(...(hourly.values as number[]))}..${Math.max(...(hourly.values as number[]))}`,
    )

    await apiCtx.close()
    session.assertNoErrors()
    await context.close()
  })
})
