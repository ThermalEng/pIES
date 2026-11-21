import { expect, test } from '@playwright/test'

import { loginViaUI, uniqueName } from './fixtures'
import { APP } from './financial-scaffold'
import { createEngineer, createProject, loginToken } from './setup/api'

test('项目普通确认删除与管理员预览后删除账号', async ({ browser }) => {
  const setup = await browser.newContext({ baseURL: APP })
  const username = uniqueName('es-delete')
  const account = await createEngineer(setup.request, username, 'simple')
  const ownerToken = await loginToken(setup.request, username, 'simple')
  const removed = await createProject(setup.request, ownerToken, uniqueName('待删除项目'))
  const cascaded = await createProject(setup.request, ownerToken, uniqueName('待级联项目'))
  await setup.close()

  const owner = await browser.newContext()
  const ownerPage = await owner.newPage()
  await loginViaUI(ownerPage, username, 'simple')
  const row = ownerPage.getByRole('row').filter({ hasText: removed.name })
  await row.getByRole('button', { name: '删除项目', exact: true }).click()
  const projectDialog = ownerPage.getByRole('dialog')
  await expect(projectDialog.getByText(removed.name, { exact: false })).toBeVisible()
  await projectDialog.getByRole('button', { name: '删除', exact: true }).click()
  await expect(row).toHaveCount(0)

  const admin = await browser.newContext()
  const adminPage = await admin.newPage()
  await loginViaUI(adminPage, 'admin', process.env.E2E_ADMIN_PASSWORD ?? 'Iesplan-Admin-2026!')
  await adminPage.getByRole('link', { name: '账号管理' }).click()
  await adminPage.getByRole('row').filter({ hasText: username })
    .getByRole('button', { name: '删除账号', exact: true }).click()
  const accountDialog = adminPage.getByRole('dialog')
  await expect(accountDialog.getByText(cascaded.name, { exact: true })).toBeVisible()
  await accountDialog.getByRole('button', { name: /确认删除/ }).click()
  await expect(adminPage.getByText(/已删除账号/)).toBeVisible()
  expect((await ownerPage.request.get('/api/auth/me')).status()).toBe(401)
  const projects = await adminPage.request.get('/api/projects/admin-visible')
  expect(projects.ok()).toBe(true)
  expect((await projects.json()).projects).toEqual(expect.arrayContaining([
    expect.objectContaining({ id: cascaded.id, status: 'deleted' }),
  ]))
  expect(account.id).toBeGreaterThan(0)
  await owner.close()
  await admin.close()
})
