import { expect, test } from '@playwright/test'

import { createSession, strongPassword, uniqueName } from './fixtures'
import { APP, scaffoldProject } from './financial-scaffold'
import { createEngineer, loginToken, requestJson } from './setup/api'

test('从任务中心提交并取消排队任务', async ({ browser }) => {
  test.skip(process.env.E2E_QUEUED_CANCEL !== '1', '需要暂未启动计算 Worker 的独立测试栈')
  const setup = await browser.newContext({ baseURL: APP })
  const username = uniqueName('es-queued')
  const password = strongPassword('Queued')
  await createEngineer(setup.request, username, password)
  const token = await loginToken(setup.request, username, password)
  const pid = await scaffoldProject(
    requestJson, setup.request, token, uniqueName('queued-project'), uniqueName('queued-profile'),
  )
  await setup.close()

  const context = await browser.newContext()
  const page = await context.newPage()
  await createSession(page, username, password)
  await page.goto(`/projects/${pid}/tasks`)
  await page.getByRole('button', { name: '提交任务', exact: true }).first().click()
  const submit = page.getByRole('dialog')
  await submit.locator('#task-type').selectOption('optimization')
  await submit.locator('#task-construction-period').fill('0')
  await submit.locator('#task-operation-years').fill('1')
  await submit.locator('#task-discount-rate').fill('0.08')
  await submit.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 1}]')
  await submit.locator('#task-maintenance-expenses').fill('[]')

  const submitted = page.waitForResponse((response) =>
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/projects/${pid}/tasks`,
  )
  await submit.getByRole('button', { name: '提交任务', exact: true }).click()
  const response = await submitted
  expect(response.status()).toBe(201)
  const { task } = await response.json()
  expect(task.status).toBe('queued')
  await expect(submit).toBeHidden()

  const row = page.getByRole('row').filter({ hasText: `#${task.id}` })
  await row.click()
  await page.getByRole('button', { name: '取消任务', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: '确认', exact: true }).click()
  await expect(row.getByText('已取消', { exact: true })).toBeVisible()
  const view = await page.request.get(`/api/projects/${pid}/tasks/${task.id}`)
  expect((await view.json()).task.status).toBe('cancelled')
  await context.close()
})
