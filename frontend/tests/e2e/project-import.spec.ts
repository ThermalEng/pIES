// S4-C 项目包导入 GUI 验收: 上传→提案→确认前无新项目→确认→新项目可用;
// 取消确认不创建项目; 非法包服务端原因可见; 模拟网络故障后同一提案可重试;
// C1 打开新项目后重开即全新流程; C2 导入成功与列表刷新区分展示、刷新失败
// 可单独重试(计数确认 POST, 不重发确认/不重传文件);
// S4-D 项目已保存模型随包导入: 副本画布设备类型与参数来自副本自己的模型行。
//
// 脚手架(用户/项目/财务/版本/导出)走公开 API, 上传/确认/取消的被测动作必须
// 经 GUI 完成, 不 mock、不注库、不注前端状态。历史关键值用两组不同电价
// (0.75/0.95)验证; 重试用路由中断模拟网络故障(与真实业务失败区分报告)。

import { test, expect } from '@playwright/test'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import type { APIRequestContext, Page } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, financeProfile } from './financial-scaffold'

const PRICE_A = '0.75'
const PRICE_B = '0.95'

//: S4-D 样例项目模型(与 project-model-consumption.spec.ts 同构: 属性驱动汇)。
const SINK_TYPE = 'acme.device.consumption_load'
const SINK_TYPE_FINAL = `${SINK_TYPE}_1`
const SINK_LABEL = '消费链负荷'
const SINK_NAME = 'userload'
const SINK_YAML = `schema: ies.device-model
schema_version: "2.1.0"
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

function overridesPayload(profileId: string, price: string): Record<string, unknown> {
  return {
    schema: 'ies.finance-overrides',
    schema_version: '5.0.0',
    profile_ref: { id: profileId },
    energy_prices: {
      grid_import: { kind: 'constant', value: { value: price, unit: 'CNY/kWh' } },
    },
  }
}

async function currentPrice(
  api: APIRequestContext, pid: number, token: string,
): Promise<string> {
  const ovr = await requestJson(api, 'GET', `/api/projects/${pid}/finance-overrides`, undefined, token)
  return ovr.finance_overrides.energy_prices.grid_import.value.value as string
}

/** 源项目: 画像 + 0.75 覆盖(hist-a) + 0.95 覆盖(hist-b), 当前 0.95。 */
async function scaffoldSource(
  api: APIRequestContext, token: string, projectName: string, profileId: string,
): Promise<{ pid: number; histA: number }> {
  const created = await requestJson(api, 'POST', '/api/projects', {
    name: projectName,
    baseline_resolution: '1h',
    baseline_start_year: 2025,
    baseline_scenario_mode: 'single',
  }, token)
  const pid: number = created.project?.id ?? created.id
  await requestJson(api, 'POST', '/api/finance-profiles', {
    finance_profile: financeProfile(profileId),
  }, token)
  await requestJson(api, 'PUT', `/api/projects/${pid}/finance-profile`, {
    profile_ref: { id: profileId },
  }, token)
  const rev1 = (await requestJson(api, 'GET', `/api/projects/${pid}/finance-overrides`, undefined, token)).revision
  await requestJson(api, 'PUT', `/api/projects/${pid}/finance-overrides`, {
    finance_overrides: overridesPayload(profileId, PRICE_A), expected_revision: rev1,
  }, token)
  const histA = await requestJson(api, 'POST', `/api/projects/${pid}/versions`, {
    name: 'hist-a', reason: 'manual_save',
  }, token)
  const rev2 = (await requestJson(api, 'GET', `/api/projects/${pid}/finance-overrides`, undefined, token)).revision
  await requestJson(api, 'PUT', `/api/projects/${pid}/finance-overrides`, {
    finance_overrides: overridesPayload(profileId, PRICE_B), expected_revision: rev2,
  }, token)
  await requestJson(api, 'POST', `/api/projects/${pid}/versions`, {
    name: 'hist-b', reason: 'manual_save',
  }, token)
  return { pid, histA: histA.version.id as number }
}

async function downloadPackage(
  api: APIRequestContext, pid: number, token: string, dest: string,
): Promise<void> {
  const exported = await requestJson(api, 'POST', `/api/projects/${pid}/exports/package`, undefined, token)
  const res = await api.fetch(`${APP}/api/projects/${pid}/exports/package/download?token=${exported.token}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (!res.ok()) throw new Error(`download package -> ${res.status()}`)
  fs.writeFileSync(dest, await res.body())
}

async function listProjectIds(api: APIRequestContext, token: string): Promise<number[]> {
  const listed = await requestJson(api, 'GET', '/api/projects', undefined, token)
  return (listed.projects as Array<{ id: number }>).map((p) => p.id)
}

async function openImportDialog(page: Page): Promise<void> {
  await page.getByRole('button', { name: '导入项目包' }).click({ timeout: 30_000 })
  await expect(page.getByRole('dialog')).toBeVisible({ timeout: 10_000 })
}

test.describe('S4-C: 项目包导入 GUI', () => {
  test.setTimeout(600_000)

  test('上传→提案→确认前无项目→确认→新项目可用, 历史值与源不变', async ({ browser }) => {
    const ownerName = uniqueName('es-imp-own')
    const importerName = uniqueName('es-imp-doer')
    const password = strongPassword('Init')
    const projectName = uniqueName('导入S4C项目')
    const profileId = uniqueName('s4c-tariff')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, ownerName, password)
    await createEngineer(apiCtx.request, importerName, password)
    const ownerToken = await loginToken(apiCtx.request, ownerName, password)
    const importerToken = await loginToken(apiCtx.request, importerName, password)
    const { pid, histA } = await scaffoldSource(apiCtx.request, ownerToken, projectName, profileId)
    const zipPath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pies-imp-')), 'pkg.zip')
    await downloadPackage(apiCtx.request, pid, ownerToken, zipPath)

    const context = await browser.newContext()
    const page = await context.newPage()
    await createSession(page, importerName, password)
    await page.goto('/')
    await openImportDialog(page)
    await page.locator('#import-package-file').setInputFiles(zipPath)
    // 提案展示(包内项目名), 确认前不导航; 上传 alone 不建项目由取消用例覆盖,
    // 此处确认后恰好一个项目即证确认前无预建(重登会接管浏览器会话, 留到最后)。
    await expect(page.getByRole('dialog').getByText(projectName)).toBeVisible({ timeout: 30_000 })
    await expect(page).toHaveURL(/^.*\/$/)

    await page.getByRole('button', { name: '确认导入' }).click({ timeout: 30_000 })
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByText(/已导入为新项目/)).toBeVisible({ timeout: 60_000 })
    // 正常路径: “已刷新”文案只在实际列表刷新成功后出现, 非确认成功即声称。
    await expect(dialog.getByText(/项目列表已刷新/)).toBeVisible({ timeout: 30_000 })
    await dialog.getByRole('button', { name: '打开新项目' }).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+/)
    const newPid = Number(page.url().match(/\/projects\/(\d+)/)?.[1])
    expect(newPid).toBeTruthy()
    // 草稿存在(详情可用), 当前配置为 0.95, 历史有两个版本。
    await expect(page.getByText(projectName).first()).toBeVisible({ timeout: 30_000 })
    // GUI 登录已接管会话, API 再读前重登(此后无 GUI 动作)。
    const verifyToken = await loginToken(apiCtx.request, importerName, password)
    expect(await currentPrice(apiCtx.request, newPid, verifyToken)).toBe(PRICE_B)
    const versions = await requestJson(apiCtx.request, 'GET', `/api/projects/${newPid}/versions`, undefined, verifyToken)
    expect(versions.versions).toHaveLength(2)
    // 历史恢复 hist-a 得 0.75; 源项目仍为 0.95。
    const histAVersion = (versions.versions as Array<{ id: number; name: string }>).find((v) => v.name === 'hist-a')
    expect(histAVersion).toBeTruthy()
    expect(histA).toBeTruthy()
    await requestJson(apiCtx.request, 'POST', `/api/projects/${newPid}/versions/${histAVersion?.id}/restore`, {}, verifyToken)
    expect(await currentPrice(apiCtx.request, newPid, verifyToken)).toBe(PRICE_A)
    expect(await currentPrice(apiCtx.request, pid, ownerToken)).toBe(PRICE_B)
    // 确认后恰好一个项目: 上传未预建、确认未重复创建。
    expect(await listProjectIds(apiCtx.request, verifyToken)).toHaveLength(1)
    await apiCtx.close()
    await context.close()
  })

  test('取消确认不创建项目', async ({ browser }) => {
    const ownerName = uniqueName('es-impc-own')
    const importerName = uniqueName('es-impc-doer')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, ownerName, password)
    await createEngineer(apiCtx.request, importerName, password)
    const ownerToken = await loginToken(apiCtx.request, ownerName, password)
    const importerToken = await loginToken(apiCtx.request, importerName, password)
    const { pid } = await scaffoldSource(apiCtx.request, ownerToken, uniqueName('取消导入项目'), uniqueName('s4c-cancel'))
    const zipPath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pies-impc-')), 'pkg.zip')
    await downloadPackage(apiCtx.request, pid, ownerToken, zipPath)

    const context = await browser.newContext()
    const page = await context.newPage()
    await createSession(page, importerName, password)
    await page.goto('/')
    await openImportDialog(page)
    await page.locator('#import-package-file').setInputFiles(zipPath)
    await expect(page.getByRole('button', { name: '确认导入' })).toBeVisible({ timeout: 30_000 })
    await page.getByRole('button', { name: '取消' }).click({ timeout: 10_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 10_000 })
    expect(await listProjectIds(apiCtx.request, await loginToken(apiCtx.request, importerName, password))).toEqual([])
    await apiCtx.close()
    await context.close()
  })

  test('非法包服务端原因逐项可见, 不创建项目', async ({ browser }) => {
    const importerName = uniqueName('es-impbad-doer')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, importerName, password)
    const importerToken = await loginToken(apiCtx.request, importerName, password)
    const badPath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pies-impbad-')), 'bad.zip')
    fs.writeFileSync(badPath, Buffer.from('not-a-package'))

    const context = await browser.newContext()
    const page = await context.newPage()
    await createSession(page, importerName, password)
    await page.goto('/')
    await openImportDialog(page)
    await page.locator('#import-package-file').setInputFiles(badPath)
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByText(/上传失败/)).toBeVisible({ timeout: 30_000 })
    // reasons 逐项渲染为列表(至少一项), 非笼统错误。
    expect(await dialog.locator('li').count()).toBeGreaterThan(0)
    expect(await listProjectIds(apiCtx.request, await loginToken(apiCtx.request, importerName, password))).toEqual([])
    await apiCtx.close()
    await context.close()
  })

  test('模拟网络故障确认失败后同一提案可重试成功', async ({ browser }) => {
    const ownerName = uniqueName('es-impr-own')
    const importerName = uniqueName('es-impr-doer')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, ownerName, password)
    await createEngineer(apiCtx.request, importerName, password)
    const ownerToken = await loginToken(apiCtx.request, ownerName, password)
    const importerToken = await loginToken(apiCtx.request, importerName, password)
    const { pid } = await scaffoldSource(apiCtx.request, ownerToken, uniqueName('重试导入项目'), uniqueName('s4c-retry'))
    const zipPath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pies-impr-')), 'pkg.zip')
    await downloadPackage(apiCtx.request, pid, ownerToken, zipPath)

    const context = await browser.newContext()
    const page = await context.newPage()
    await createSession(page, importerName, password)
    await page.goto('/')
    await openImportDialog(page)
    await page.locator('#import-package-file').setInputFiles(zipPath)
    await expect(page.getByRole('button', { name: '确认导入' })).toBeVisible({ timeout: 30_000 })
    // 首次确认请求模拟网络中断(路由 abort; 属测试模拟故障, 非业务失败)。
    await page.route('**/api/projects/import/*/confirm', (route) => route.abort('failed'))
    await page.getByRole('button', { name: '确认导入' }).click({ timeout: 30_000 })
    await expect(page.getByRole('dialog').getByText(/确认导入失败/)).toBeVisible({ timeout: 30_000 })
    // 恢复网络, 同一提案重试成功(不重新上传); 最终恰好一个项目, 失败未留半成品
    // (失败未建项目的行级证明由后端回滚回归覆盖, 此处重登会接管浏览器会话)。
    await page.unroute('**/api/projects/import/*/confirm')
    await page.getByRole('button', { name: '重试' }).click({ timeout: 30_000 })
    await expect(page.getByRole('dialog').getByText(/已导入为新项目/)).toBeVisible({ timeout: 60_000 })
    expect(await listProjectIds(apiCtx.request, await loginToken(apiCtx.request, importerName, password))).toHaveLength(1)
    await apiCtx.close()
    await context.close()
  })

  test('C1: 打开新项目后重开导入即全新流程, 无旧成功残留', async ({ browser }) => {
    const ownerName = uniqueName('es-impc1-own')
    const importerName = uniqueName('es-impc1-doer')
    const password = strongPassword('Init')
    const projectName = uniqueName('重开导入项目')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, ownerName, password)
    await createEngineer(apiCtx.request, importerName, password)
    const ownerToken = await loginToken(apiCtx.request, ownerName, password)
    const { pid } = await scaffoldSource(apiCtx.request, ownerToken, projectName, uniqueName('s4c-c1'))
    const zipPath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pies-impc1-')), 'pkg.zip')
    await downloadPackage(apiCtx.request, pid, ownerToken, zipPath)

    const context = await browser.newContext()
    const page = await context.newPage()
    await createSession(page, importerName, password)
    await page.goto('/')
    await openImportDialog(page)
    await page.locator('#import-package-file').setInputFiles(zipPath)
    await expect(page.getByRole('button', { name: '确认导入' })).toBeVisible({ timeout: 30_000 })
    await page.getByRole('button', { name: '确认导入' }).click({ timeout: 30_000 })
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByText(/已导入为新项目/)).toBeVisible({ timeout: 60_000 })
    await expect(dialog.getByText(/项目列表已刷新/)).toBeVisible({ timeout: 30_000 })
    // 经“打开新项目”离开(与普通关闭统一重置本地状态), 返回后再打开。
    await dialog.getByRole('button', { name: '打开新项目' }).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+/)
    await page.goto('/')
    await openImportDialog(page)
    const reopened = page.getByRole('dialog')
    // 全新流程: 选择文件入口可见, 无旧成功文案/旧打开按钮残留。
    await expect(reopened.getByText(/选择导出的项目包文件/)).toBeVisible({ timeout: 10_000 })
    await expect(reopened.getByText(/已导入为新项目/)).toBeHidden({ timeout: 10_000 })
    await expect(reopened.getByRole('button', { name: '打开新项目' })).toBeHidden({ timeout: 10_000 })
    await apiCtx.close()
    await context.close()
  })

  test('C2: 列表刷新失败仍导入成功, 单独重试只 GET 不重发确认', async ({ browser }) => {
    const ownerName = uniqueName('es-impc2-own')
    const importerName = uniqueName('es-impc2-doer')
    const password = strongPassword('Init')
    const projectName = uniqueName('刷新失败项目')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, ownerName, password)
    await createEngineer(apiCtx.request, importerName, password)
    const ownerToken = await loginToken(apiCtx.request, ownerName, password)
    const { pid } = await scaffoldSource(apiCtx.request, ownerToken, projectName, uniqueName('s4c-c2'))
    const zipPath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pies-impc2-')), 'pkg.zip')
    await downloadPackage(apiCtx.request, pid, ownerToken, zipPath)

    const context = await browser.newContext()
    const page = await context.newPage()
    let confirmPosts = 0
    let uploadPosts = 0
    page.on('request', (req) => {
      if (req.method() !== 'POST') return
      if (/\/api\/projects\/import\/\d+\/confirm/.test(req.url())) confirmPosts += 1
      else if (new URL(req.url()).pathname === '/api/projects/import') uploadPosts += 1
    })
    await createSession(page, importerName, password)
    await page.goto('/')
    await openImportDialog(page)
    await page.locator('#import-package-file').setInputFiles(zipPath)
    await expect(page.getByRole('button', { name: '确认导入' })).toBeVisible({ timeout: 30_000 })
    // 确认成功后的下一次项目列表 GET 定向失败(模拟故障, 非业务失败)。
    let failNextList = true
    await page.route(
      (url) => url.pathname === '/api/projects',
      (route) => {
        if (failNextList) {
          failNextList = false
          return route.abort('failed')
        }
        return route.continue()
      },
    )
    await page.getByRole('button', { name: '确认导入' }).click({ timeout: 30_000 })
    const dialog = page.getByRole('dialog')
    // 导入成功仍展示, 但“已刷新”不出现, 刷新失败可见。
    await expect(dialog.getByText(/已导入为新项目/)).toBeVisible({ timeout: 60_000 })
    await expect(dialog.getByText(/刷新失败/)).toBeVisible({ timeout: 30_000 })
    await expect(dialog.getByText(/项目列表已刷新/)).toBeHidden({ timeout: 10_000 })
    // 单独重试刷新: 只 GET, 不重发确认、不重传文件。
    await dialog.getByRole('button', { name: '重试刷新列表' }).click({ timeout: 30_000 })
    await expect(dialog.getByText(/项目列表已刷新/)).toBeVisible({ timeout: 30_000 })
    expect(confirmPosts).toBe(1)
    expect(uploadPosts).toBe(1)
    // 新项目可打开且正确(名称一致)。
    await dialog.getByRole('button', { name: '打开新项目' }).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+/)
    await expect(page.getByText(projectName).first()).toBeVisible({ timeout: 30_000 })
    await apiCtx.close()
    await context.close()
  })

  test('S4-D: 项目已保存模型随包导入, 副本画布消费副本自己的模型', async ({ browser }) => {
    const ownerName = uniqueName('es-impm-own')
    const importerName = uniqueName('es-impm-doer')
    const password = strongPassword('Init')
    const projectName = uniqueName('导入模型项目')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, ownerName, password)
    await createEngineer(apiCtx.request, importerName, password)
    const ownerToken = await loginToken(apiCtx.request, ownerName, password)
    // 源项目脚手架(公开 API): 保存项目模型 → 草稿设备实例引用该模型 → 导出包。
    const created = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2025,
      baseline_scenario_mode: 'single',
    }, ownerToken)
    const pid: number = created.project?.id ?? created.id
    const saved = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/models`, {
      model_yaml: SINK_YAML,
      expected_revision: 1,
    }, ownerToken)
    expect(saved.project_model.device_id).toBe(SINK_TYPE_FINAL)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: saved.project_revision,
      commands: [{
        id: 'imp-model-load',
        unit: 'model',
        type: 'model.upsert_device',
        payload: { name: SINK_NAME, device_type: SINK_TYPE_FINAL, kind: 'new', params: { rated_demand_kw: 20 } },
      }],
    }, ownerToken)
    const zipPath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pies-impm-')), 'pkg.zip')
    await downloadPackage(apiCtx.request, pid, ownerToken, zipPath)

    // 被测动作(真实 GUI, 不可替代): 上传 → 确认 → 打开新项目。
    const context = await browser.newContext()
    const page = await context.newPage()
    await createSession(page, importerName, password)
    await page.goto('/')
    await openImportDialog(page)
    await page.locator('#import-package-file').setInputFiles(zipPath)
    await page.getByRole('button', { name: '确认导入' }).click({ timeout: 30_000 })
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByText(/已导入为新项目/)).toBeVisible({ timeout: 60_000 })
    await dialog.getByRole('button', { name: '打开新项目' }).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+/)
    const newPid = Number(page.url().match(/\/projects\/(\d+)/)?.[1])
    expect(newPid).toBeTruthy()
    expect(newPid).not.toBe(pid)

    // 副本画布: 设备实例与端口来自副本自己的模型行; 类型串是副本新身份,
    // 参数默认值来自副本模型 schema(断链或消费源项目行时不会成立)。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('系统建模').click()
    await expect(page).toHaveURL(/\/model$/)
    const palette = page.locator('.mp-palette')
    await expect(palette).toBeVisible({ timeout: 30_000 })
    const sinkItem = palette.locator(`[aria-label*="${SINK_LABEL}"]`).first()
    await sinkItem.scrollIntoViewIfNeeded()
    await expect(sinkItem).toBeVisible({ timeout: 15_000 })
    const sinkNode = page.locator('.react-flow__node', { hasText: SINK_NAME }).first()
    await expect(sinkNode).toBeVisible({ timeout: 15_000 })
    await expect(sinkNode.locator('.mp-handle')).toHaveCount(1)
    await sinkNode.click()
    // 侧栏类型串是副本自己的身份; 参数节来自副本模型 schema(名称/范围),
    // 实例参数 20 随内容迁移; “恢复默认值”取到副本模型默认 10 并真实写草稿。
    await expect(page.locator('.mp-sidebar__section').first()).toHaveText(SINK_TYPE_FINAL)
    await expect(page.locator('.mp-param__name').first()).toHaveText('rated_demand_kw')
    await expect(page.locator('.mp-param__range').first()).toHaveText('0 ~ 100000')
    await expect(page.locator('input[aria-label="rated_demand_kw (kW)"]')).toHaveValue('20')
    await page.getByRole('button', { name: '恢复默认值' }).first().click({ timeout: 10_000 })
    await expect(page.locator('input[aria-label="rated_demand_kw (kW)"]')).toHaveValue('10')

    // 源项目不受影响: 源模型清单与草稿保持原身份(副本不复用源行身份)。
    const sourceModels = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/models`, undefined, ownerToken)
    expect(sourceModels.project_models).toHaveLength(1)
    expect(sourceModels.project_models[0].device_id).toBe(SINK_TYPE_FINAL)
    await apiCtx.close()
    await context.close()
  })
})
