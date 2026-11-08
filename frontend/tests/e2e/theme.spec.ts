// Appearance is browser-local, not an account/project field. All theme changes
// use the real button; API calls below only prepare unrelated model fixtures.
import { chromium, test, expect, type Page } from '@playwright/test'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { APP_URL } from './fixtures/env'
import { buildMinimalModel, createEngineer, createProject, loginToken } from './setup/api'

const themeButton = (page: Page) => page.getByRole('button', { name: /^(深色模式|Dark mode)$/ })

async function expectTheme(page: Page, theme: 'light' | 'dark') {
  await expect(themeButton(page)).toHaveAttribute('aria-pressed', String(theme === 'dark'))
  await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
  await expect(page.locator('html')).toHaveCSS('color-scheme', theme)
  await expect(page.locator('body')).toHaveCSS(
    'background-color', theme === 'dark' ? 'rgb(16, 23, 32)' : 'rgb(243, 245, 248)',
  )
}

async function expectAdjacentToLanguage(page: Page) {
  const language = page.getByRole('button', { name: /^(语言|Language)$/ })
  await expect(language).toBeVisible()
  expect(await language.evaluate((el) => el.nextElementSibling?.classList.contains('ies-theme-toggle'))).toBe(true)
}

test('语言与外观独立保存，关闭浏览器后保留中英文及深浅两种选择', async ({}, testInfo) => {
  // Real persistent Chromium profile, without injecting storageState/preferences.
  // Chromium creates mode-0700 profile directories. Keep the disposable profile
  // outside uploaded artifacts so the host CI artifact collector can read them.
  const profile = await mkdtemp(join(tmpdir(), 'pies-preferences-'))
  const launch = () => chromium.launchPersistentContext(profile, {
    baseURL: APP_URL, viewport: { width: 1280, height: 800 },
  })
  const errors: string[] = []
  const monitor = (page: Page) => page.on('pageerror', (error) => errors.push(error.message))
  let context = await launch()
  try {
    let page = context.pages()[0]
    monitor(page)
    await page.goto('/login')
    await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN')
    await expectTheme(page, 'light')
    await expectAdjacentToLanguage(page)
    await themeButton(page).focus()
    await themeButton(page).press('Space')
    await expectTheme(page, 'dark')
    await expect(themeButton(page)).toHaveAttribute('title', '切换到浅色模式')
    await expect(page.locator('.ies-login__card')).toHaveCSS('background-color', 'rgb(24, 34, 46)')
    await expect(page.getByLabel(/用户名/)).toHaveCSS('color', 'rgb(233, 238, 245)')
    await page.getByRole('button', { name: '语言', exact: true }).click()
    await expect(page.locator('html')).toHaveAttribute('lang', 'en')
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible()
    await expectTheme(page, 'dark')
    await page.reload()
    await expectTheme(page, 'dark')
    await page.setViewportSize({ width: 375, height: 812 })
    await expect(themeButton(page)).toBeInViewport()
    await expectAdjacentToLanguage(page)
    await page.screenshot({ path: testInfo.outputPath('login-dark-mobile.png') })
    await context.close()

    context = await launch()
    page = context.pages()[0]
    monitor(page)
    await page.goto('/login')
    await expectTheme(page, 'dark')
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible()
    await expect(page.locator('html')).toHaveAttribute('lang', 'en')
    await themeButton(page).click()
    await expectTheme(page, 'light')
    await page.getByRole('button', { name: 'Language', exact: true }).click()
    await expect(page.getByRole('heading', { name: '登录', exact: true })).toBeVisible()
    await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN')
    await expectTheme(page, 'light')
    await page.reload()
    await expectTheme(page, 'light')
    await context.close()

    context = await launch()
    page = context.pages()[0]
    monitor(page)
    await page.goto('/login')
    await expectTheme(page, 'light')
    await expect(page.getByRole('heading', { name: '登录', exact: true })).toBeVisible()
    await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN')
    expect(errors).toEqual([])
  } finally {
    await context.close()
    await rm(profile, { recursive: true })
  }
})

test('外壳、表单弹窗、帮助、画布及连线跟随同一主题，退出后仍保留', async ({ browser, page }, testInfo) => {
  const username = uniqueName('es-theme')
  const password = strongPassword('Theme')
  const setup = await browser.newContext({ baseURL: APP_URL })
  await createEngineer(setup.request, username, password)
  const token = await loginToken(setup.request, username, password)
  const project = await createProject(setup.request, token, uniqueName('Theme project'))
  await buildMinimalModel(setup.request, project.id, token)
  await setup.close()

  const session = await createSession(page, username, password)
  await expectAdjacentToLanguage(page)
  await themeButton(page).click()
  await expectTheme(page, 'dark')
  await page.getByRole('button', { name: /新建项目|New project/ }).first().click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toHaveCSS('background-color', 'rgb(24, 34, 46)')
  await expect(dialog.getByLabel(/名称|Name/)).toHaveCSS('color', 'rgb(233, 238, 245)')
  await dialog.getByRole('button', { name: /取消|Cancel/ }).click()

  await page.getByText(project.name, { exact: true }).click()
  await page.getByRole('navigation', { name: /工作台|Workspace/ }).getByText('系统建模', { exact: true }).click()
  await expect(page.locator('.react-flow__node')).toHaveCount(6)
  await expect(page.locator('.react-flow')).toHaveCSS('background-color', 'rgb(24, 34, 46)')
  const zoomIn = page.locator('.react-flow__controls-zoomin')
  await expect(zoomIn).toHaveCSS('background-color', 'rgb(24, 34, 46)')
  await expect(zoomIn).toHaveCSS('color', 'rgb(233, 238, 245)')
  const edge = page.locator('.react-flow__edge-path').first()
  await expect(edge).toHaveCSS('stroke', 'rgb(143, 198, 255)')
  await page.screenshot({ path: testInfo.outputPath('canvas-dark.png') })
  await themeButton(page).click()
  await expectTheme(page, 'light')
  await expect(edge).toHaveCSS('stroke', 'rgb(14, 92, 173)')
  await themeButton(page).click()

  await page.getByRole('navigation', { name: /项目列表|Projects/ }).getByText('帮助中心', { exact: true }).click()
  await expect(page.locator('.ies-help__title')).toBeVisible()
  await expect(page.locator('.ies-help__content')).toHaveCSS('color', 'rgb(233, 238, 245)')
  await page.getByRole('button', { name: '语言', exact: true }).click()
  await expect(themeButton(page)).toHaveAccessibleName('Dark mode')
  await expectTheme(page, 'dark')
  await page.reload()
  await expectTheme(page, 'dark')
  await page.setViewportSize({ width: 375, height: 812 })
  await expect(themeButton(page)).toBeInViewport()
  await expectAdjacentToLanguage(page)
  await themeButton(page).press('Enter')
  await expectTheme(page, 'light')
  await themeButton(page).click()
  await page.screenshot({ path: testInfo.outputPath('shell-dark-mobile.png') })
  await page.getByRole('button', { name: /Sign out|退出登录/ }).click()
  await expect(page).toHaveURL(/\/login/)
  await expectTheme(page, 'dark')
  session.assertNoErrors([
    // Fixture has no calculation config; tasks page reports its exact absence.
    { method: 'GET', path: `/api/projects/${project.id}/config`, status: 404 },
  ])
})
