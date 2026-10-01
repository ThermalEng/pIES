// QA-E2E-01 0.12-A1 配置收口验收(13.3 范围外新增聚焦场景):
//   计算配置 revision 编辑器: 空白新建无预填 / 填写保存 / 重读 /
//   409 保留表单 + 用户主动重载。
//
// 前置: 独立用户(API 造数); 项目由 UI 创建; 并发修改由同一用户的
// 第二个标签页经 UI 保存产生(真实用户行为, 无 API 并发写)。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, attachTab, uniqueName, strongPassword } from './fixtures'
import { createEngineer } from './setup/api'

const SAVE_BTN = /保存计算配置|Save calculation config/i
const SAVED_OK = /已保存 revision v\d+|Saved revision v\d+/i
const CONFLICT = /当前表单编辑已保留|your edits are kept/i
const RELOAD_BTN = /重新加载最新配置|Reload latest config/i

/** UI 创建项目并进入计算配置页, 返回项目 id。 */
async function createProjectEnterConfig(page: Page, projectName: string): Promise<string> {
  await page.getByRole('button', { name: /新建项目|New project/i }).first().click()
  await page.getByLabel(/名称|Name/i).fill(projectName)
  await page.getByRole('button', { name: /确认|Confirm/i }).click()
  await expect(page.getByText(projectName)).toBeVisible()
  await page.getByText(projectName).click()
  await expect(page).toHaveURL(/\/projects\/\d+$/)
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('计算配置').click()
  await expect(page).toHaveURL(/\/config$/)
  const m = page.url().match(/\/projects\/(\d+)\/config$/)
  if (!m) throw new Error(`未解析出项目 id: ${page.url()}`)
  return m[1]
}

/** 填写全部必填字段(精度由用户明确选择 medium)。 */
async function fillConfig(page: Page, mode: string, timeLimit: string): Promise<void> {
  await page.locator('#calc-mode').fill(mode)
  await page.locator('#calc-generator').fill('ies.algo.milp_hybrid@1.0.0')
  await page.locator('#calc-solver').fill('ies.solver.highs@1.7.2')
  await page.locator('#calc-precision').selectOption('medium')
  await page.locator('#calc-time-limit').fill(timeLimit)
  await page.locator('#calc-outputs').fill('["solution"]')
}

test.describe('0.12-A1: 计算配置 revision 编辑器', () => {
  test('空白无预填 / 保存 / 重读 / 409 保留表单并主动重载', async ({ browser }) => {
    const username = uniqueName('es-cfgrev')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    const projectId = await createProjectEnterConfig(page, uniqueName('QA 配置收口项目'))

    // 1) 空白新建: 无已保存配置, 精度/时间限制不得预填
    await expect(page.getByText(/尚未保存|Not saved yet/i)).toBeVisible()
    await expect(page.locator('#calc-precision')).toHaveValue('')
    await expect(page.locator('#calc-time-limit')).toHaveValue('')
    await expect(page.locator('#calc-mode')).toHaveValue('')

    // 2) 填写并保存 → v1
    await fillConfig(page, 'planning', '600')
    await page.getByRole('button', { name: SAVE_BTN }).click()
    await expect(page.getByText(SAVED_OK).first()).toBeVisible({ timeout: 10_000 })
    await expect(page.getByText(/当前 revision: v1|Current revision.*v1/i)).toBeVisible()

    // 3) 重读(刷新) → 值保持
    await page.reload()
    await expect(page.locator('#calc-mode')).toHaveValue('planning')
    await expect(page.locator('#calc-precision')).toHaveValue('medium')
    await expect(page.locator('#calc-time-limit')).toHaveValue('600')
    // 重读经 pretty() 美化缩进，内容一致即可
    await expect(page.locator('#calc-outputs')).toHaveValue('[\n  "solution"\n]')

    // 4) 第二标签页保存 v2(时间限制 600→900)
    const tabB = await context.newPage()
    attachTab(session, tabB)
    await tabB.goto(`/projects/${projectId}/config`)
    await expect(tabB.locator('#calc-mode')).toHaveValue('planning')
    await tabB.locator('#calc-time-limit').fill('900')
    await tabB.getByRole('button', { name: SAVE_BTN }).click()
    await expect(tabB.getByText(/已保存 revision v2|Saved revision v2/i).first()).toBeVisible({ timeout: 10_000 })
    await tabB.close()

    // 5) 原页(仍基于 v1)修改并保存 → 409, 表单保留未提交编辑
    await page.locator('#calc-mode').fill('planning-local')
    await page.getByRole('button', { name: SAVE_BTN }).click()
    await expect(page.getByText(CONFLICT).first()).toBeVisible({ timeout: 10_000 })
    await expect(page.getByRole('button', { name: RELOAD_BTN })).toBeVisible()
    // 未提交编辑未被覆盖, 远端 v2 也未自动灌入
    await expect(page.locator('#calc-mode')).toHaveValue('planning-local')
    await expect(page.locator('#calc-time-limit')).toHaveValue('600')

    // 6) 用户主动重载 → 表单切换为 v2 内容
    await page.getByRole('button', { name: RELOAD_BTN }).click()
    await expect(page.locator('#calc-mode')).toHaveValue('planning')
    await expect(page.locator('#calc-time-limit')).toHaveValue('900')
    await expect(page.getByText(/当前 revision: v2|Current revision.*v2/i)).toBeVisible()

    session.assertNoErrors()
    await context.close()
  })
})
