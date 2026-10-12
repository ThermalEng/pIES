// 管理员只读审计 GUI 验收: 版本/恢复/应用/导入审计的行级核对、筛选、
// 翻页、读取失败恢复与非管理员隔离。
//
// 事件准备(建用户/项目/版本/求解/应用/恢复/导出/导入)走公开 API, 被测的
// 查看/筛选/翻页/重试动作必须经 GUI 完成, 不 mock、不注库、不伪造审计
// 响应。路由中断/GET 失败为测试模拟故障, 与真实业务链区分报告。

import { test, expect } from '@playwright/test'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import type { APIRequestContext, Page } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { adminToken, createEngineer, loginToken, requestJson } from './setup/api'
import { APP, financeProfile, scaffoldProject } from './financial-scaffold'

const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD ?? 'Iesplan-Admin-2026!'

const EVALUATION = {
  evaluation_years: 1,
  construction_period: 0,
  operation_years: 1,
  discount_rate: 0.08,
  construction_payments: [{ t: 0, ratio: 1 }],
  maintenance_expenses: [],
}

async function openAuditPage(page: Page): Promise<void> {
  await page.getByRole('navigation', { name: /项目列表|Projects/i }).getByText(/审计日志|Audit log/i).click()
  await expect(page).toHaveURL(/\/admin\/audit$/)
  await expect(page.getByRole('table')).toBeVisible({ timeout: 30_000 })
}

/** 表格所有行文本(跨翻页由调用方收集)。 */
async function tableRowTexts(page: Page): Promise<string[]> {
  return page.getByRole('table').locator('tbody tr').allInnerTexts()
}

interface AuditRow {
  text: string
  cells: string[]
  timeTitle: string | null
  entryId: string | null
}

/** 表格行文本/各单元格/时间 title(原始 occurred_at)/entry id。 */
async function tableRows(page: Page): Promise<AuditRow[]> {
  return page
    .getByRole('table')
    .locator('tbody tr')
    .evaluateAll((trs) =>
      trs.map((tr) => ({
        text: (tr as HTMLElement).innerText ?? '',
        cells: Array.from(tr.querySelectorAll('td')).map((td) => (td as HTMLElement).innerText ?? ''),
        timeTitle: tr.querySelector('td:first-child span')?.getAttribute('title') ?? null,
        entryId: tr.getAttribute('data-entry-id'),
      })),
    )
}

/** 点击“更多”直到没有下一页, 返回累积行数。 */
async function loadAllPages(page: Page): Promise<number> {
  for (;;) {
    const more = page.getByRole('button', { name: /^(更多|More)$/ })
    if ((await more.count()) === 0) break
    const before = await page.getByRole('table').locator('tbody tr').count()
    await more.click()
    await expect(async () => {
      expect(await page.getByRole('table').locator('tbody tr').count()).toBeGreaterThan(before)
    }).toPass({ timeout: 30_000 })
  }
  return page.getByRole('table').locator('tbody tr').count()
}

test.describe('审计日志只读查看', () => {
  test.setTimeout(900_000)

  test('管理员查看版本/恢复/应用/导入审计并翻页定位', async ({ browser }) => {
    const ownerName = uniqueName('es-aud-own')
    const importerName = uniqueName('es-aud-imp')
    const password = strongPassword('Init')
    const projectName = uniqueName('审计项目')
    const profileId = uniqueName('aud-tariff')
    const apiCtx = await browser.newContext({ baseURL: APP })
    const owner = await createEngineer(apiCtx.request, ownerName, password)
    const importer = await createEngineer(apiCtx.request, importerName, password)
    const ownerToken = await loginToken(apiCtx.request, ownerName, password)
    const importerToken = await loginToken(apiCtx.request, importerName, password)

    // 公开 API 准备事件: 项目 + 双泵规划 + 手动版本 + 真实求解 + 应用。
    const pid = await scaffoldProject(requestJson, apiCtx.request, ownerToken, projectName, profileId)
    const view0 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, ownerToken)
    const put2 = await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/draft`, {
      expected_revision: view0.draft.revision,
      commands: [{
        id: 'aud-dev-hp2', unit: 'model', type: 'model.upsert_device',
        payload: {
          name: 'hp2', device_type: 'ies.device.heat_pump', kind: 'new',
          params: { rated_heat_kw: 500, cop: 4.0 }, position: { x: 6.0, y: 6.0 },
        },
      }, {
        id: 'aud-conn-grid-hp2', unit: 'model', type: 'model.upsert_connection',
        payload: {
          name: 'c-grid-hp2', from_device: 'grid1', from_port: 'electricity_import',
          to_device: 'hp2', to_port: 'electricity_in',
        },
      }, {
        id: 'aud-finbind2', unit: 'assembly', type: 'assembly.set_finance_binding',
        payload: {
          instances: {
            grid1: { type: 'none' },
            load1: { type: 'none' },
            heatload: { type: 'none' },
            hp1: {
              type: 'costed', finance_type: 'heat_pump_system',
              drivers: {
                heat_kw: { property: 'rated_heat_kw' },
                consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
              },
            },
            hp2: {
              type: 'costed', finance_type: 'heat_pump_system',
              drivers: {
                heat_kw: { property: 'rated_heat_kw' },
                consumed_kwh: { interface: 'electricity_in', aggregation: 'integrate_positive' },
              },
            },
          },
        },
      }],
    }, ownerToken)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pid}/planning-config`, {
      planning_config: {
        objective: {
          sense: 'minimize',
          expression: 'hp1__upfront_capex + hp2__upfront_capex + 1 year * hp1__annual_fixed_om + 1 year * hp2__annual_fixed_om',
        },
        variables: {
          hp1_cap: { device_ref: 'hp1', parameter: 'rated_heat_kw', lower_bound: '50', upper_bound: '1000', unit: 'kW' },
          hp2_cap: { device_ref: 'hp2', parameter: 'rated_heat_kw', lower_bound: '200', upper_bound: '1000', unit: 'kW' },
        },
        constraints: {},
      },
      expected_revision: 1,
    }, ownerToken)
    const v1 = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/versions`, {
      name: 'aud-v1', reason: 'manual_save', expected_revision: put2.revision,
    }, ownerToken)
    const v1Id: number = v1.version.id
    const precheck = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/validation/run`, {}, ownerToken)
    expect((precheck.report?.diagnostics ?? []).filter((d: { blocking: boolean }) => d.blocking)).toEqual([])
    const cfg = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/config`, undefined, ownerToken)
    const task = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/tasks`, {
      task_type: 'optimization',
      config: { config_revision: cfg.revision },
      evaluation: EVALUATION,
      idempotency_key: `aud-${Date.now()}`,
      parent_task_id: null,
    }, ownerToken)
    const taskId: number = task.task.id
    await expect(async () => {
      const detail = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/tasks/${taskId}`, undefined, ownerToken)
      expect(detail.task.status).toBe('completed')
    }).toPass({ timeout: 420_000, intervals: [10_000] })
    const view1 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}`, undefined, ownerToken)
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/apply-result`, {
      task_id: taskId, solution_id: 0, expected_revision: view1.draft.revision,
    }, ownerToken)
    const v2 = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/versions`, {
      name: 'aud-v2', reason: 'manual_save',
    }, ownerToken)
    void v2
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/versions/${v1Id}/restore`, {}, ownerToken)
    // 导出 + 导入(导入者为另一账号, 审计操作者应为导入者)。
    const exported = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/exports/package`, undefined, ownerToken)
    const dl = await apiCtx.request.fetch(`${APP}/api/projects/${pid}/exports/package/download?token=${exported.token}`, {
      headers: { Authorization: `Bearer ${ownerToken}` },
    })
    if (!dl.ok()) throw new Error(`download package -> ${dl.status()}`)
    const zipPath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'pies-aud-')), 'pkg.zip')
    fs.writeFileSync(zipPath, await dl.body())
    const uploaded = await apiCtx.request.post(`${APP}/api/projects/import`, {
      headers: { Authorization: `Bearer ${importerToken}` },
      multipart: { file: { name: 'pkg.zip', mimeType: 'application/zip', buffer: fs.readFileSync(zipPath) } },
    })
    expect(uploaded.ok()).toBe(true)
    const proposal = (await uploaded.json()).proposal
    expect(proposal.project_id).toBeNull()
    const proposalId: number = proposal.id
    const confirmed = await requestJson(apiCtx.request, 'POST', `/api/projects/import/${proposalId}/confirm`, {}, importerToken)
    const importedPid: number = confirmed.project.id
    expect(importedPid).toBeTruthy()
    // 本轮已知版本集合: 11 个手动版本 + v1/应用/恢复版本(后续在全站
    // 筛选结果中按精确 entry ID 定位核对, 不依赖全站残留数量)。
    const bulkIds: number[] = []
    for (let i = 0; i < 11; i += 1) {
      const v = await requestJson(apiCtx.request, 'POST', `/api/projects/${pid}/versions`, {
        name: `aud-bulk-${i}`, reason: 'manual_save',
      }, ownerToken)
      bulkIds.push(v.version.id as number)
    }
    // 比较依据: 经管理员公开查询取本轮 owner/importer 已知事件的真实
    // audit entry(既有 actor_id 筛选, 不新增 UI 筛选或端点)。
    interface ExpectedAudit {
      id: number
      action: string
      entity: string
      actor: string
      occurredAt: string
    }
    // 版本/应用/恢复的真实版本 id(经公开列表复核, 不猜)。
    const versions = await requestJson(apiCtx.request, 'GET', `/api/projects/${pid}/versions`, undefined, ownerToken)
    const byReason = (reason: string) =>
      (versions.versions as Array<{ id: number; reason: string }>).filter((v) => v.reason === reason)
    const appliedId = byReason('apply_result')[0]?.id
    const restoredId = byReason('restore')[0]?.id
    expect(appliedId).toBeTruthy()
    expect(restoredId).toBeTruthy()
    const adminTok = await adminToken(apiCtx.request)
    async function groundTruth(actorId: number): Promise<ExpectedAudit[]> {
      const page = await requestJson(
        apiCtx.request, 'GET', `/api/admin/audit?actor_id=${actorId}&limit=200`, undefined, adminTok,
      )
      return (page.items as Array<Record<string, unknown>>).map((e) => ({
        id: e.id as number,
        action: e.action as string,
        entity: `${e.entity_type} #${e.entity_id}`,
        actor: `${e.actor_type} #${e.actor_id}`,
        occurredAt: e.occurred_at as string,
      }))
    }
    const ownerTruth = await groundTruth(owner.id)
    const importerTruth = await groundTruth(importer.id)
    const knownVersionEntities = new Set([...bulkIds, v1Id, appliedId, restoredId].map((v) => `project_version #${v}`))
    const expectedVersions = ownerTruth.filter(
      (e) => e.action === 'project.version_created' && knownVersionEntities.has(e.entity),
    )
    expect(expectedVersions.length).toBe(knownVersionEntities.size)
    const expectedRestored = ownerTruth.filter(
      (e) => e.action === 'project.version_restored' && e.entity === `project_version #${restoredId}`,
    )
    const expectedApplied = ownerTruth.filter(
      (e) => e.action === 'project.result_applied' && e.entity === `project_version #${appliedId}`,
    )
    expect(expectedRestored.length).toBe(1)
    expect(expectedApplied.length).toBe(1)
    const expectedImported = importerTruth.filter(
      (e) => e.action === 'project.imported' && e.entity === `project #${importedPid}`,
    )
    const expectedProposed = importerTruth.filter(
      (e) => e.action === 'import.proposal_created' && e.entity === `import_proposals #${proposalId}`,
    )
    expect(expectedImported.length).toBe(1)
    expect(expectedProposed.length).toBe(1)

    // 管理员 GUI 查看: 首屏恰一页(10 行)且有下一页(后端游标已修正, 详见后端回归)。
    // action 筛选仍是全站结果: 在其中按精确 entry ID 定位本轮已知事件,
    // 核对各列(对象/操作者/时间)等于真实 occurred_at, 不对他人历史行做假设。
    const context = await browser.newContext()
    const page = await context.newPage()
    await createSession(page, 'admin', ADMIN_PASSWORD)
    await page.goto('/')
    await openAuditPage(page)
    expect(await page.getByRole('table').locator('tbody tr').count()).toBe(10)
    await expect(page.getByRole('button', { name: /^(更多|More)$/ })).toBeVisible({ timeout: 10_000 })
    /** 全站筛选中按精确 entry ID 找到本轮行, 核对各列等于真实记录。 */
    async function expectKnownRows(action: string, expected: ExpectedAudit[]): Promise<void> {
      await page.locator('#audit-filter-action').fill(action)
      await page.locator('#audit-filter-entity-type').fill('')
      await page.locator('#audit-filter-entity-id').fill('')
      await page.getByRole('button', { name: /应用筛选|Apply filters/i }).click()
      await expect(async () => {
        expect(await page.getByRole('table').locator('tbody tr').count()).toBeGreaterThan(0)
      }).toPass({ timeout: 30_000 })
      await loadAllPages(page)
      const rows = await tableRows(page)
      const byEntry = new Map(rows.map((r) => [r.entryId, r]))
      // 本轮已知集合逐一核对完整性; 全站行 entry id 唯一。
      expect(new Set(rows.map((r) => r.entryId)).size).toBe(rows.length)
      for (const exp of expected) {
        const row = byEntry.get(String(exp.id))
        expect(row).toBeTruthy()
        // 列顺序: 时间/操作/操作者/对象/追溯, 单元格精确相等。
        expect(row?.cells[1]).toBe(exp.action)
        expect(row?.cells[2]).toBe(exp.actor)
        expect(row?.cells[3]).toBe(exp.entity)
        expect(row?.timeTitle).toBe(exp.occurredAt)
      }
    }
    await expectKnownRows('project.version_created', expectedVersions)
    await expectKnownRows('project.version_restored', expectedRestored)
    await expectKnownRows('project.result_applied', expectedApplied)
    await expectKnownRows('project.imported', expectedImported)
    await expectKnownRows('import.proposal_created', expectedProposed)
    // 对象筛选同样走 GUI: 定位到应用派生版本(精确 entity, 非前缀)。
    await page.locator('#audit-filter-action').fill('')
    await page.locator('#audit-filter-entity-type').fill('project_version')
    await page.locator('#audit-filter-entity-id').fill(String(appliedId))
    await page.getByRole('button', { name: /应用筛选|Apply filters/i }).click()
    await expect(async () => {
      const rows = await tableRows(page)
      expect(rows.length).toBeGreaterThan(0)
      for (const r of rows) {
        expect(r.cells[3]).toBe(`project_version #${appliedId}`)
      }
    }).toPass({ timeout: 30_000 })
    await apiCtx.close()
    await context.close()
  })

  test('对象标识无类型不发送并提示', async ({ browser }) => {
    const apiCtx = await browser.newContext({ baseURL: APP })
    let auditGets = 0
    const context = await browser.newContext()
    const page = await context.newPage()
    page.on('request', (req) => {
      if (req.method() === 'GET' && new URL(req.url()).pathname === '/api/admin/audit') auditGets += 1
    })
    await createSession(page, 'admin', ADMIN_PASSWORD)
    await page.goto('/')
    await openAuditPage(page)
    const before = auditGets
    await page.locator('#audit-filter-entity-id').fill('123')
    await page.getByRole('button', { name: /应用筛选|Apply filters/i }).click()
    await expect(page.getByText(/需与对象类型一起/)).toBeVisible({ timeout: 10_000 })
    expect(auditGets).toBe(before)
    await apiCtx.close()
    await context.close()
  })

  test('读取失败可见, 重试保留筛选恢复成功', async ({ browser }) => {
    const ownerName = uniqueName('es-audf-own')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, ownerName, password)
    const ownerToken = await loginToken(apiCtx.request, ownerName, password)
    const pid = await scaffoldProject(requestJson, apiCtx.request, ownerToken, uniqueName('审计失败项目'), uniqueName('audf-tariff'))
    void pid

    const context = await browser.newContext()
    const page = await context.newPage()
    await createSession(page, 'admin', ADMIN_PASSWORD)
    await page.goto('/')
    await page.getByRole('navigation', { name: /项目列表|Projects/i }).getByText(/审计日志|Audit log/i).click()
    await expect(page).toHaveURL(/\/admin\/audit$/)
    await expect(page.getByRole('table')).toBeVisible({ timeout: 30_000 })
    // 筛选后下一次查询定向失败(模拟故障, 非业务失败), 错误可见且可重试。
    await page.locator('#audit-filter-action').fill('project.version_created')
    let failOnce = true
    await page.route(
      (url) => url.pathname === '/api/admin/audit',
      (route) => {
        if (failOnce) {
          failOnce = false
          return route.abort('failed')
        }
        return route.continue()
      },
    )
    await page.getByRole('button', { name: /应用筛选|Apply filters/i }).click()
    await expect(page.getByRole('alert')).toBeVisible({ timeout: 30_000 })
    await page.getByRole('button', { name: /重试|Retry/i }).click()
    await expect(async () => {
      const filtered = await tableRowTexts(page)
      expect(filtered.length).toBeGreaterThan(0)
      for (const r of filtered) expect(r).toContain('project.version_created')
    }).toPass({ timeout: 30_000 })
    await apiCtx.close()
    await context.close()
  })

  test('非管理员无入口, 直访重定向, 直接 API 403', async ({ browser }) => {
    const userName = uniqueName('es-audn-doer')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)

    const context = await browser.newContext()
    const page = await context.newPage()
    await createSession(page, userName, password)
    await page.goto('/')
    await expect(page.getByRole('navigation', { name: /项目列表|Projects/i }).getByText(/审计日志|Audit log/i)).toBeHidden({
      timeout: 10_000,
    })
    await page.goto('/admin/audit')
    await expect(page).toHaveURL(/^.*\/$/)
    // GUI 登录已接管会话, API 再读前重登(此后无 GUI 动作)。
    const freshToken = await loginToken(apiCtx.request, userName, password)
    const res = await apiCtx.request.get(`${APP}/api/admin/audit`, {
      headers: { Authorization: `Bearer ${freshToken}` },
    })
    expect(res.status(), await res.text()).toBe(403)
    // 管理员同一端点可用(对照, 非伪造)。
    const adminRes = await apiCtx.request.get(`${APP}/api/admin/audit?limit=1`, {
      headers: { Authorization: `Bearer ${await adminToken(apiCtx.request)}` },
    })
    expect(adminRes.ok()).toBe(true)
    const adminBody = await adminRes.json()
    expect(Array.isArray(adminBody.items)).toBe(true)
    await apiCtx.close()
    await context.close()
  })
})
