// 路由分块加载失败可见性：已发版页面的缺失/损坏分块不再是永久"加载中"。
// 用真实后端 + 真实构建产物，仅把目标分块请求改成失败；不注入组件状态，
// 不伪造成功模块响应。主动中止的分块请求按实际 method + URL 精确登记。
import { test, expect } from '@playwright/test'
import { createSession, strongPassword, uniqueName } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'

async function setupProject(browser: import('@playwright/test').Browser, tag: string) {
  const username = uniqueName(`pl-${tag}`)
  const password = strongPassword('Init')
  const setup = await browser.newContext()
  await createEngineer(setup.request, username, password)
  const token = await loginToken(setup.request, username, password)
  const created = await requestJson(setup.request, 'POST', '/api/projects', {
    name: uniqueName('分块加载'),
    baseline_resolution: '1h',
    baseline_start_year: 2026,
    baseline_scenario_mode: 'single',
  }, token)
  await setup.close()
  return { username, password, project: created.project }
}

async function openWorkbench(
  browser: import('@playwright/test').Browser,
  username: string,
  password: string,
  projectName: string,
) {
  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  // 等待默认任务页的配置读取完成再离开，避免导航竞态决定已知 404 是否发生。
  const configRead = page.waitForResponse((response) =>
    response.request().method() === 'GET' &&
    /^\/api\/projects\/\d+\/config$/.test(new URL(response.url()).pathname) &&
    response.status() === 404,
  )
  await page.getByText(projectName, { exact: true }).click()
  await expect(page).toHaveURL(/\/projects\/\d+\/tasks/)
  await configRead
  return { context, page, session }
}

test('已发版数据页正常导航可用', async ({ browser }) => {
  const { username, password, project } = await setupProject(browser, 'ok')
  const { context, page, session } = await openWorkbench(browser, username, password, project.name)
  try {
    await page.getByRole('link', { name: '数据管理' }).click()
    await expect(page).toHaveURL(/\/projects\/\d+\/data/)
    await expect(page.getByRole('heading', { name: '数据管理' })).toBeVisible()
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${project.id}/config`, status: 404, times: 1 },
    ])
  } finally {
    await context.close()
  }
})

test('嵌套分块失败显示可见错误而非永久加载', async ({ browser }) => {
  const { username, password, project } = await setupProject(browser, 'nested')
  const { context, page, session } = await openWorkbench(browser, username, password, project.name)
  try {
    let aborted = 0
    await context.route(/\/assets\/DataPage-[^/]+\.js$/, async (route) => {
      const req = route.request()
      aborted++
      session.registerExpectedAbort(req.method(), new URL(req.url()).pathname)
      await route.abort()
    })
    await page.getByRole('link', { name: '数据管理' }).click()
    await expect(page).toHaveURL(/\/projects\/\d+\/data/)
    await expect(page.getByRole('alert')).toBeVisible()
    await expect(page.getByText('页面加载失败', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: '重试' })).toBeVisible()
    expect(aborted).toBe(1)
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${project.id}/config`, status: 404, times: 1 },
    ])
  } finally {
    await context.close()
  }
})

test('解除失败后显式重试到达目标页', async ({ browser }) => {
  const { username, password, project } = await setupProject(browser, 'recover')
  const { context, page, session } = await openWorkbench(browser, username, password, project.name)
  try {
    let aborted = 0
    await context.route(/\/assets\/DataPage-[^/]+\.js$/, async (route) => {
      const req = route.request()
      aborted++
      session.registerExpectedAbort(req.method(), new URL(req.url()).pathname)
      await route.abort()
    })
    await page.getByRole('link', { name: '数据管理' }).click()
    await expect(page.getByRole('alert')).toBeVisible()
    expect(aborted).toBe(1)
    await context.unrouteAll({ behavior: 'wait' })
    await page.getByRole('button', { name: '重试' }).click()
    await expect(page).toHaveURL(/\/projects\/\d+\/data/)
    await expect(page.getByRole('heading', { name: '数据管理' })).toBeVisible()
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${project.id}/config`, status: 404, times: 1 },
    ])
  } finally {
    await context.close()
  }
})

test('顶层分块失败同样可见错误且重试恢复', async ({ browser }) => {
  const { username, password, project } = await setupProject(browser, 'top')
  const { context, page, session } = await openWorkbench(browser, username, password, project.name)
  try {
    let aborted = 0
    await context.route(/\/assets\/HelpPage-[^/]+\.js$/, async (route) => {
      const req = route.request()
      aborted++
      session.registerExpectedAbort(req.method(), new URL(req.url()).pathname)
      await route.abort()
    })
    await page.getByRole('link', { name: /帮助|Help/ }).click()
    await expect(page.getByRole('alert')).toBeVisible()
    await expect(page.getByText('页面加载失败', { exact: true })).toBeVisible()
    expect(aborted).toBe(1)
    await context.unrouteAll({ behavior: 'wait' })
    await page.getByRole('button', { name: '重试' }).click()
    await expect(page.locator('.ies-help').first()).toBeVisible()
    session.assertNoErrors([
      { method: 'GET', path: `/api/projects/${project.id}/config`, status: 404, times: 1 },
    ])
  } finally {
    await context.close()
  }
})
