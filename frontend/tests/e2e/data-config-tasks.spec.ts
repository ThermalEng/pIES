// QA-E2E-01 场景 4-6(13.3):
//   4. 上传年度数据(内置样例 1h), 查看质量报告并绑定版本
//   5. 保存非默认财务配置, 重新读取、确认基准并通过校验
//   6. 提交任务, 观察状态变化, 查看结果并下载导出
//
// 前置: 独立用户(API 造数); 项目由 UI 创建。
// 造数(API): 仅创建用户; 业务流程全部走 UI。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { buildMinimalModel, createEngineer, loginToken, requestJson } from './setup/api'

/** 通过 UI 创建项目并进入数据页。 */
async function createProjectEnterData(page: Page, projectName: string): Promise<void> {
  await page.getByRole('button', { name: /新建项目|New project/i }).first().click()
  await page.getByLabel(/名称|Name/i).fill(projectName)
  await page.getByRole('button', { name: /确认|Confirm/i }).click()
  await expect(page.getByText(projectName)).toBeVisible()
  await page.getByText(projectName).click()
  await expect(page).toHaveURL(/\/projects\/\d+$/)
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click()
  await expect(page).toHaveURL(/\/data$/)
}

test.describe('场景 4: 数据上传 / 质量报告 / 绑定', () => {
  test('内置样例生成并绑定到项目', async ({ browser }) => {
    const username = uniqueName('es-data')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await createProjectEnterData(page, uniqueName('QA 数据项目'))

    // 4a. 打开"内置样例数据"对话框, 生成 1h 合成数据
    await page.getByRole('button', { name: /内置样例数据|Built-in sample data/i }).click()
    await expect(page.getByText(/生成合成数据|Generate synthetic data/i)).toBeVisible()
    await page.getByRole('button', { name: /生成并上传|Generate & upload/i }).click()
    // 等待上传完成(质量报告面板出现: 上传结果 Alert 文案为"已通过校验")
    await expect(page.getByText(/已通过校验|Validation passed|Good quality/i).first()).toBeVisible({ timeout: 30_000 })
    // 关闭对话框(确定按钮)
    await page.getByRole('button', { name: /^确定$|^OK$/i }).click()

    // 4b. 数据集列表出现, 绑定最新版本到项目草稿
    await expect(page.getByText(/样例|sample/i).first()).toBeVisible()
    const bindBtn = page.getByRole('button', { name: /绑定到项目|Bind to project/i }).first()
    await bindBtn.click()
    // 绑定后按钮变为"解绑" 或状态徽章变化
    await expect(page.getByText(/已绑定|Bound/i).first()).toBeVisible({ timeout: 10_000 })

    session.assertNoErrors()
    await context.close()
  })
})

test.describe('场景 5: 财务配置往返与校验', () => {
  test('保存非默认配置, 重新读取, 确认基准, 校验通过', async ({ browser }) => {
    const username = uniqueName('es-config')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    const projectName = uniqueName('QA 配置项目')
    await page.getByRole('button', { name: /新建项目|New project/i }).first().click()
    await page.getByLabel(/名称|Name/i).fill(projectName)
    await page.getByRole('button', { name: /确认|Confirm/i }).click()
    await expect(page.getByText(projectName)).toBeVisible()
    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+$/)

    // 进入配置页
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('计算配置').click()
    await expect(page).toHaveURL(/\/config$/)

    // 5a. 修改经济参数为非默认值(折现率 8→10, 税率 25→20, 评价周期 20→15)
    const discount = page.locator('#cfg-discount')
    const tax = page.locator('#cfg-tax')
    const period = page.locator('#cfg-period')
    await expect(discount).toBeVisible()
    await discount.fill('10')
    await tax.fill('20')
    await period.fill('15')

    // 5b. 保存配置
    await page.getByRole('button', { name: /^保存$/ }).click()
    await expect(page.getByText(/已保存|Saved/i).first()).toBeVisible({ timeout: 10_000 })

    // 5c. 重新读取(刷新页面)验证值保持
    await page.reload()
    await expect(page.locator('#cfg-discount')).toHaveValue('10')
    await expect(page.locator('#cfg-tax')).toHaveValue('20')
    await expect(page.locator('#cfg-period')).toHaveValue('15')

    // 5d. 校验配置
    await page.getByRole('button', { name: /校验配置|Validate config/i }).click()
    await expect(page.getByText(/配置校验通过|Config validation passed/i).first()).toBeVisible({ timeout: 20_000 })

    // 5e. 进入校验页: 确认财务基准
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('校验').click()
    await expect(page).toHaveURL(/\/validation$/)
    await page.getByRole('button', { name: /确认财务基准|Confirm financial baseline/i }).click()
    await expect(page.getByText(/财务基准已确认|baseline confirmed/i).first()).toBeVisible({ timeout: 10_000 })

    session.assertNoErrors()
    await context.close()
  })
})

test.describe('场景 6: 任务提交 / 状态 / 结果 / 导出', () => {
  test('提交计算任务并观察状态与结果, 导出 Excel', async ({ browser }) => {
    const username = uniqueName('es-task')
    const password = strongPassword('Init')
    // 造数阶段(UI 登录前): 建用户(admin) → 建项目+模型(被测用户自己,
    // 否则 admin 无项目 edit 权限; 之后 UI 登录会自然顶掉 API 会话)
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    const userTok = await loginToken(apiCtx.request, username, password)
    const projectName = uniqueName('QA 任务项目')
    const createdProj = await requestJson(apiCtx.request, 'POST', '/api/projects', { name: projectName }, userTok)
    const projectId = createdProj.project?.id ?? createdProj.id
    await buildMinimalModel(apiCtx.request, projectId, userTok)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    // 打开项目(已由 API 造数)
    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+$/)

    // 数据: 生成样例并绑定
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click()
    await page.getByRole('button', { name: /内置样例数据|Built-in sample data/i }).click()
    await page.getByRole('button', { name: /生成并上传|Generate & upload/i }).click()
    await expect(page.getByText(/已通过校验|Validation passed|Good quality/i).first()).toBeVisible({ timeout: 30_000 })
    await page.getByRole('button', { name: /^确定$|^OK$/i }).click()
    await expect(page.getByText(/样例|sample/i).first()).toBeVisible()
    await page.getByRole('button', { name: /绑定到项目|Bind to project/i }).first().click()
    await expect(page.getByText(/已绑定|Bound/i).first()).toBeVisible({ timeout: 10_000 })

    // 配置: 保存默认(直接保存当前默认值)并确认基准
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('计算配置').click()
    await page.getByRole('button', { name: /^保存$/ }).click()
    await expect(page.getByText(/已保存|Saved/i).first()).toBeVisible({ timeout: 10_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('校验').click()
    await page.getByRole('button', { name: /确认财务基准|Confirm financial baseline/i }).click()
    await expect(page.getByText(/财务基准已确认|baseline confirmed/i).first()).toBeVisible({ timeout: 10_000 })

    // 6b. 提交任务: 方案评价(calc)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click()
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click()
    // 提交对话框: 校验门禁预检不应有阻断
    await expect(page.getByText(/任务由服务器端执行|runs on the server/i).first()).toBeVisible()
    // 默认类型 calc, 点击对话框底部提交按钮
    await page.getByRole('button', { name: /提交任务|Submit task/i }).last().click()
    // 任务出现在列表
    await expect(page.locator('.ies-table__row').first()).toBeVisible({ timeout: 15_000 })

    // 6c. 等待任务完成(轮询详情状态)
    await page.locator('.ies-table__row').first().click()
    await expect(page.getByText(/在左侧选择一个任务查看详情|Select a task/i)).not.toBeVisible({ timeout: 10_000 })
    await expect(page.locator('.ies-task-detail')).toBeVisible({ timeout: 10_000 })
    // 轮询直到任务状态为"已完成"(worker 计算, 最长 180s)
    const detailBadge = page.locator('.ies-task-detail .ies-badge__label').first()
    await expect(async () => {
      const text = (await detailBadge.textContent()) ?? ''
      expect(text).toMatch(/已完成|Completed/i)
    }).toPass({ timeout: 180_000, intervals: [5_000] })

    // 6d. 结果页(通过任务详情"查看结果"链接进入)
    const viewResults = page.getByRole('link', { name: /查看结果|View results/i })
    await expect(viewResults).toBeVisible({ timeout: 10_000 })
    await viewResults.click()
    await expect(page).toHaveURL(/\/results/)

    // 6e. 导出页: Excel 报告(下载自动开始)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click()
    await expect(page).toHaveURL(/\/export$/)
    const downloadPromise = page.waitForEvent('download', { timeout: 60_000 })
    await page.getByRole('button', { name: /导出 Excel 报告|Export Excel report/i }).click()
    const download = await downloadPromise
    expect(download.suggestedFilename()).toMatch(/\.xlsx$|\.xls$/)

    session.assertNoErrors()
    await context.close()
  })
})
