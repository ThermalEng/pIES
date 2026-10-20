// QA-E2E-01 场景 4(13.3; 本文件仅保留场景 4):
//   4. 上传年度数据(内置样例 1h), 查看质量报告并绑定版本
//
// 场景 5(旧 1.0 财务配置控件)已删除, 由 config-revision.spec.ts 的
// 0.12-A1 新 UI 验收替代; 场景 6(真实计算/导出链)见本文件末尾 future skip。
//
// 前置: 独立用户(API 造数); 场景 4 的项目与一份占位配置由 API 造数
// (场景 6 同模式, 避免工作台落地时的 404-as-absence 噪声);
// 数据上传/质量报告/绑定业务流程全部走 UI。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { buildMinimalModel, createEngineer, loginToken, requestJson } from './setup/api'

test.describe('场景 4: 数据上传 / 质量报告 / 绑定', () => {
  test('内置样例生成并绑定到项目', async ({ browser }) => {
    const username = uniqueName('es-data')
    const password = strongPassword('Init')
    const projectName = uniqueName('QA 数据项目')
    // 造数阶段(UI 登录前, 场景 6 同模式): 建用户 → API 建项目 → API 保存
    // 一份配置。配置与数据验收无关, 仅使工作台落地(tasks 重定向)时的
    // 配置读取为 200, 避免 404-as-absence 的控制台噪声误触发断言;
    // 数据上传/质量报告/绑定全走 UI。
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    const setupToken = await loginToken(apiCtx.request, username, password)
    const createdProj = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2026,
      baseline_scenario_mode: 'single',
    }, setupToken)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${createdProj.project?.id ?? createdProj.id}/config`, {
      calculation_config: {
        schema: 'ies.calculation-config',
        schema_version: '2.0.0',
        mode: 'planning',
        predictions: [],
        generator: 'ies.generator.highs_milp@3.2.0',
        solver: 'ies.solver.highs@1.7.2',
        precision: 'medium',
        convergence: null,
        time_limit_seconds: 600,
        options: {},
        seed: null,
        outputs: ['solution'],
      },
      expected_revision: null,
    }, setupToken)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)
    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+$/)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click()
    await expect(page).toHaveURL(/\/data$/)

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

/* 场景 5(旧 1.0 财务配置控件: #cfg-discount / 保存默认 / 校验配置按钮)
 * 已删除: 0.12-A1 移除了自由形态配置与 /validate、/default 端点,
 * 对应验收由 config-revision.spec.ts 的新 UI 覆盖, 此处不再保留
 * 必然失败的旧选择器。
 */

// 场景 6 future skip: 真实计算/导出链属后续切片(A2 任务绑定 + Worker)。
// 本体保留待后续按新 UI 重写, 当前 A1 不声称验收计算链。
// 跳过原因: (1) 配置页已无"保存默认"(空白表单须逐项填写);
// (2) 计算类任务新建在 GUI 已暂停待 A2 绑定; (3) 结果/导出需真实执行。
test.describe.skip('场景 6: 任务提交 / 状态 / 结果 / 导出(后续切片)', () => {
  test('提交计算任务并观察状态与结果, 导出 Excel', async ({ browser }) => {
    const username = uniqueName('es-task')
    const password = strongPassword('Init')
    // 造数阶段(UI 登录前): 建用户(admin) → 建项目+模型(被测用户自己,
    // 否则 admin 无项目 edit 权限; 之后 UI 登录会自然顶掉 API 会话)
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    const userTok = await loginToken(apiCtx.request, username, password)
    const projectName = uniqueName('QA 任务项目')
    const createdProj = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2026,
      baseline_scenario_mode: 'single',
    }, userTok)
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
    // 评价确认(必填, 无默认值): 建设期 0 + 运营 1 年 + 折现率 0.08 + 付款/维护 JSON
    await page.locator('#task-construction-period').fill('0')
    await page.locator('#task-operation-years').fill('1')
    await page.locator('#task-discount-rate').fill('0.08')
    await page.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 1}]')
    await page.locator('#task-maintenance-expenses').fill('[]')
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
