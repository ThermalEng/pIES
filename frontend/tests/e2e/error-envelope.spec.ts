// 从真实模型页触发校验；仅把该请求改为服务端不支持的方法，
// 使用真实后端的 405 信封验证共享文案，不伪造响应或注入组件状态。
import { test, expect } from '@playwright/test'
import { createSession, strongPassword, uniqueName } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'

test('模型页展示真实 HTTP 异常状态码而非插值占位符', async ({ browser }) => {
  const username = uniqueName('es-error-envelope')
  const password = strongPassword('Init')
  const setup = await browser.newContext()
  await createEngineer(setup.request, username, password)
  const token = await loginToken(setup.request, username, password)
  const created = await requestJson(setup.request, 'POST', '/api/projects', {
    name: uniqueName('错误信封'),
    baseline_resolution: '1h',
    baseline_start_year: 2026,
    baseline_scenario_mode: 'single',
  }, token)
  const project = created.project
  await setup.close()

  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  const path = `/api/projects/${project.id}/validation/run`
  await context.route((url) => url.pathname === path, async (route) => {
    const response = await route.fetch({ method: 'GET' })
    expect(response.status()).toBe(405)
    expect((await response.json()).error.code).toBe('API-REQ-003')
    await route.fulfill({ response })
  })
  try {
    await page.getByText(project.name, { exact: true }).click()
    await page.getByRole('navigation', { name: /工作台|Workspace/i })
      .getByText(/系统建模|System Modeling/i).click()
    await page.getByRole('button', { name: '校验系统图', exact: true }).click()
    await expect(page.getByText('请求失败(HTTP 405)', { exact: true })).toBeVisible()
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${project.id}/config`, status: 404, times: 1 },
      { method: 'POST', path, status: 405, times: 1 },
    ])
  } finally {
    await context.close()
  }
})
