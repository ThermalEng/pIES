// Basic user operations, not financial acceptance or visual snapshots.
// Account setup alone uses API; all tested edits and navigation use real UI.
import { test, expect } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer } from './setup/api'

test('calculation settings survive save and browser reload @basic', async ({ browser }) => {
  const username = uniqueName('es-basic-config')
  const password = strongPassword('Init')
  const setup = await browser.newContext()
  await createEngineer(setup.request, username, password)
  await setup.close()
  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  const name = uniqueName('basic-project')
  await page.getByRole('button', { name: /新建项目|New project/i }).first().click()
  await page.getByLabel(/名称|Name/i).fill(name)
  await page.locator('#pp-create-start-year').fill('2026')
  await page.getByRole('button', { name: /确认|Confirm/i }).click()
  await page.getByText(name, { exact: true }).click()
  await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
  const projectId = page.url().match(/\/projects\/(\d+)/)![1]
  await page.getByRole('navigation', { name: /工作台|Workspace/i })
    .getByText('计算配置', { exact: true }).click()
  await expect(page.locator('#calc-mode')).toBeEditable()
  await page.locator('#calc-mode').fill('planning')
  await page.locator('#calc-generator').fill('ies.generator.highs_milp@3.4.0')
  await page.locator('#calc-solver').fill('ies.solver.highs@1.7.2')
  await page.locator('#calc-precision').selectOption('medium')
  await page.locator('#calc-time-limit').fill('43200')
  await page.locator('#calc-outputs').fill('["solution"]')
  const saved = page.waitForResponse(r => r.request().method() === 'PUT' &&
    new URL(r.url()).pathname === `/api/projects/${projectId}/config`)
  await page.getByRole('button', { name: /保存计算配置|Save calculation config/i }).click()
  expect((await saved).ok()).toBe(true)
  await page.reload()
  await expect(page.locator('#calc-mode')).toHaveValue('planning')
  await expect(page.locator('#calc-precision')).toHaveValue('medium')
  await expect(page.locator('#calc-time-limit')).toHaveValue('43200')
  // JSON formatting is presentation, not part of the saved value contract.
  expect(JSON.parse(await page.locator('#calc-outputs').inputValue())).toEqual(['solution'])
  session.assertNoErrors([
    { method: 'GET', path: `/api/projects/${projectId}/config`, status: 404, times: 2 },
  ])
  await context.close()
})

test('help chapter can be opened and reloaded @basic', async ({ browser }) => {
  const username = uniqueName('es-basic-help')
  const password = strongPassword('Init')
  const setup = await browser.newContext()
  await createEngineer(setup.request, username, password)
  await setup.close()
  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  await page.getByRole('navigation', { name: /项目列表|Projects/i })
    .getByText('帮助中心', { exact: true }).click()
  await page.getByText('快速开始', { exact: true }).first().click()
  await expect(page).toHaveURL(/\/help\/zh-CN\//)
  const chapterUrl = page.url()
  const title = page.locator('.ies-help__title').first()
  await expect(title).toContainText('快速开始')
  await page.reload()
  await expect(page).toHaveURL(chapterUrl)
  await expect(title).toContainText('快速开始')
  session.assertNoErrors()
  await context.close()
})
