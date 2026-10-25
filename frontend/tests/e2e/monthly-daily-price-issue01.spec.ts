/** ISSUE-01 real browser: upload daily sources, author 12-month override,
 * validate feedback, reload, submit, solve, check financial charge.
 * Synthetic data only; GUI interactions are not replaced with API mocks.
 */
import { test, expect } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

function dayCsv(price: string): Buffer {
  return Buffer.from([
    '# schema: ies.price-series',
    '# schema_version: 1.0.0',
    '# resolution: 1h',
    '# unit: CNY/kWh',
    'step,price',
    ...Array.from({ length: 24 }, (_, i) => `${i},${price}`),
    '',
  ].join('\n'), 'utf8')
}

test.describe('ISSUE-01 月份日曲线财务页面与真实计费', () => {
  test.setTimeout(900_000)
  test('配置完整12月、共享对象、错误反馈、保存读回及真实求解', async ({ browser }) => {
    const username = uniqueName('issue01-user')
    const password = strongPassword('Init')
    const projectName = uniqueName('ISSUE-01 月份价格')
    const profileId = uniqueName('issue01-profile')
    const api = await browser.newContext({ baseURL: APP })
    await createEngineer(api.request, username, password)
    const token = await loginToken(api.request, username, password)
    const pid = await scaffoldProject(requestJson, api.request, token, projectName, profileId)
    const ds = await requestJson(api.request, 'POST', `/api/projects/${pid}/datasets`, {
      name: 'synthetic-daily-prices',
    }, token)
    const dsId: number = ds.dataset?.id ?? ds.id

    async function upload(price: string, filename: string): Promise<string> {
      const response = await api.request.post(
        `${APP}/api/projects/${pid}/datasets/${dsId}/versions`, {
          headers: { Authorization: `Bearer ${token}` },
          multipart: {
            file: { name: filename, mimeType: 'text/csv', buffer: dayCsv(price) },
            resolution: '1h',
          },
        },
      )
      expect(response.status(), await response.text()).toBe(201)
      const body = await response.json()
      const oid: string = body.quality_report?.price_object_id
      expect(typeof oid).toBe('string')
      expect(oid.length).toBeGreaterThan(0)
      return oid
    }
    const janRef = await upload('0.2', 'jan.price.csv')
    const febRef = await upload('0.7', 'feb.price.csv')
    const restRef = await upload('0.3', 'other.price.csv')

    const context = await browser.newContext()
    const page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.goto(`${APP}/projects/${pid}/finance`)
    const kind = page.locator('#overrides-price-grid_import-kind')
    await expect(kind).toBeVisible({ timeout: 30_000 })
    await kind.selectOption('monthly_daily')
    const month = (m: number) => page.locator(`#overrides-price-grid_import-month-${m}`)
    await expect(month(12)).toBeVisible()
    await month(1).fill(restRef)
    await page.getByRole('button', { name: /全部月份引用一月曲线/ }).click()
    expect((await Promise.all(Array.from({ length: 12 }, (_, i) => month(i + 1).inputValue()))))
      .toEqual(Array(12).fill(restRef))
    await month(1).fill(janRef)
    await month(2).fill(febRef)

    // Show actual backend feedback when the 12-month source is incomplete.
    await month(12).fill('')
    const save = page.getByRole('button', { name: /保存覆盖|保存|Save/i }).first()
    const badReply = page.waitForResponse(r =>
      r.url().includes(`/projects/${pid}/finance-overrides`) && r.request().method() === 'PUT')
    await save.click()
    expect((await badReply).status()).toBe(400)
    await expect(page.getByRole('alert').filter({ hasText: /ref|不能为空|非法|必需|invalid|错误/i }).first())
      .toBeVisible({ timeout: 15_000 })

    await month(12).fill(restRef)
    const goodReply = page.waitForResponse(r =>
      r.url().includes(`/projects/${pid}/finance-overrides`) && r.request().method() === 'PUT')
    await save.click()
    expect((await goodReply).status()).toBe(200)
    await page.reload()
    await expect(kind).toHaveValue('monthly_daily')
    await expect(month(1)).toHaveValue(janRef)
    await expect(month(2)).toHaveValue(febRef)
    for (let m = 3; m <= 12; m++) await expect(month(m)).toHaveValue(restRef)
    const savedText = await page.locator('#finance-overrides-doc').inputValue()
    const saved = JSON.parse(savedText).energy_prices.grid_import
    expect(saved.kind).toBe('monthly_daily')
    expect(Object.keys(saved.months).sort((a, b) => Number(a) - Number(b)))
      .toEqual(Array.from({ length: 12 }, (_, i) => String(i + 1)))
    expect(saved.months['2'].ref).toBe(febRef)

    // Actual public submit and Worker path: K=0, N=1, 2025 non-leap.
    await page.goto(`${APP}/projects/${pid}/tasks`)
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click()
    await page.locator('#task-type').selectOption('optimization')
    await page.locator('#task-construction-period').fill('0')
    await page.locator('#task-operation-years').fill('1')
    await page.locator('#task-discount-rate').fill('0.08')
    await page.locator('#task-construction-payments').fill('[{"t":0,"ratio":1}]')
    await page.locator('#task-maintenance-expenses').fill('[]')
    await page.getByRole('button', { name: /提交任务|Submit task/i }).last().click()
    await page.locator('.ies-table__row').first().click({ timeout: 30_000 })
    const badge = page.locator('.ies-task-detail .ies-badge__label').first()
    await expect(async () => {
      expect(await badge.textContent()).toMatch(/已完成|Completed/i)
    }).toPass({ timeout: 240_000, intervals: [10_000] })

    const result = await page.evaluate(async projectId => {
      const listing = await fetch(`/api/projects/${projectId}/tasks?limit=10`, {
        credentials: 'include',
      })
      if (!listing.ok) throw new Error(`list tasks ${listing.status}`)
      const items = (await listing.json()).items
      const res = await fetch(`/api/projects/${projectId}/tasks/${items[0].id}/result`, {
        credentials: 'include',
      })
      if (!res.ok) throw new Error(`task result ${res.status}`)
      return res.json()
    }, pid)
    const fin = result.result.financial_evaluation
    expect(fin.status).toBe('evaluated')
    expect(fin.annual_operation).toHaveLength(1)
    // 2025: 31 Jan + 28 Feb + remaining 306 days; all 24h prices.
    const dailyPriceSum = 31 * 0.2 + 28 * 0.7 + 306 * 0.3
    const expectedEnergyCost = dailyPriceSum * 24 * (50 + 30 / 3.5)
    expect(Number(fin.annual_operation[0].energy_net)).toBeCloseTo(
      -expectedEnergyCost, 0,
    )
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${pid}/finance-overrides`, status: 404 },
      { method: 'PUT', path: `/api/projects/${pid}/finance-overrides`, status: 400 },
    ])
    await context.close()
    await api.close()
  })
})
