// Real Worker observations, real task and browser. No response or state injection.
import { test, expect } from '@playwright/test'
import { createSession, strongPassword, uniqueName } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

test('任务详情公开共享资源实际限额，不把观测当作独享资源保证', async ({ browser }) => {
  test.setTimeout(420_000)
  const username = uniqueName('es_resources')
  const password = strongPassword('Resources')
  const setup = await browser.newContext({ baseURL: APP })
  await createEngineer(setup.request, username, password)
  const token = await loginToken(setup.request, username, password)
  const pid = await scaffoldProject(requestJson, setup.request, token,
    uniqueName('运行资源验收'), uniqueName('resources-finance'))
  const config = await requestJson(setup.request, 'GET', `/api/projects/${pid}/config`, undefined, token)
  const created = await requestJson(setup.request, 'POST', `/api/projects/${pid}/tasks`, {
    task_type: 'optimization', config: { config_revision: config.revision },
    evaluation: { evaluation_years: 1, construction_period: 0, operation_years: 1,
      discount_rate: 0.08, construction_payments: [{ t: 0, ratio: 1 }], maintenance_expenses: [] },
    idempotency_key: uniqueName('resource-task'), parent_task_id: null,
  }, token)
  const taskId = created.task.id
  let facts: any
  await expect(async () => {
    const response = await requestJson(setup.request, 'GET',
      `/api/projects/${pid}/tasks/${taskId}`, undefined, token)
    expect(response.task.status).toBe('completed')
    facts = response.task.progress.detail.worker_resources
  }).toPass({ timeout: 300_000, intervals: [2000] })
  expect(facts.scope).toBe('worker_cgroup')
  if (process.env.E2E_WORKER_MEMORY_BYTES) {
    expect(facts.memory_limit).toEqual({ status: 'limited', bytes: Number(process.env.E2E_WORKER_MEMORY_BYTES) })
    expect(facts.swap_limit).toEqual({ status: 'limited', bytes: 0 })
  }
  const context = await browser.newContext()
  const page = await context.newPage()
  await createSession(page, username, password)
  await page.goto(`/projects/${pid}/tasks`)
  await page.locator('section.ies-card')
    .filter({ has: page.getByRole('heading', { name: '任务列表' }) })
    .locator('.ies-table__row').filter({ hasText: new RegExp(`#${taskId}(?!\\d)`) }).click()
  const resources = page.getByRole('region', { name: '运行资源（最近阶段观测）' })
  await expect(resources).toBeVisible()
  await expect(resources).toContainText('不是任务独享额度')
  await expect(resources).toContainText('不代表本任务峰值')
  const memory = resources.locator('dt').filter({ hasText: '共享内存硬限额' }).locator('xpath=following-sibling::dd[1]')
  if (facts.memory_limit.status === 'limited') {
    const value = (facts.memory_limit.bytes / 1024 ** 3).toLocaleString(undefined, { maximumFractionDigits: 2 })
    await expect(memory).toHaveText(`${value} GiB`)
  } else {
    await expect(memory).toContainText(facts.memory_limit.status === 'unlimited' ? '未设置' : '未知')
  }
  await page.getByRole('button', { name: '语言', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Runtime resources (latest stage observation)' }))
    .toContainText('not reserved for this task')
  await context.close()
  await setup.close()
})
