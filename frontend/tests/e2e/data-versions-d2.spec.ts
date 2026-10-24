// D2 数据集版本历史失败不能当无版本。
//
// 成功路径真实不 mock: 数据集与版本经公开 API 创建/采样,
// 版本弹框内容与同一版本 GET 响应对照。failure 仅用定向 HTTP 500
// 拦截版本详情; 不注库、不注入页面状态、不 mock 成功正文。
// 无需求解与财务。经项目列表切项目会卸载 DataPage 再挂载, 不声称 state 沿用;
// 开读清旧 state 见源码, 浏览器只覆盖失败/恢复行为, 不夸大。

import { test, expect } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP } from './financial-scaffold'

test.describe('D2: 版本加载失败可见', () => {
  test.setTimeout(300_000)

  test('版本失败整组可见、恢复后版本与空态同源', async ({ browser }) => {
    const userName = uniqueName('es-d2-doer')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)

    // 项目 A: 公开 API 建两个数据集, 其中 A1 真实采样出版本, A2 为空。
    const projectA = uniqueName('D2项目A')
    const createdA = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectA,
      baseline_resolution: '1h',
      baseline_start_year: 2026,
      baseline_scenario_mode: 'single',
    }, userToken)
    const pidA: number = createdA.project?.id ?? createdA.id
    const dsA1Name = uniqueName('D2-A1有版本')
    const dsA1 = await requestJson(apiCtx.request, 'POST', `/api/projects/${pidA}/datasets`, {
      name: dsA1Name,
    }, userToken)
    const dsA1Id: number = dsA1.dataset?.id ?? dsA1.id
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pidA}/datasets/${dsA1Id}/sample?resolution=1h`, {}, userToken)
    const dsA2Name = uniqueName('D2-A2空')
    await requestJson(apiCtx.request, 'POST', `/api/projects/${pidA}/datasets`, {
      name: dsA2Name,
    }, userToken)
    // 同一真实版本的身份与关键元数据(弹框对照用, 不替代被测读取)。
    const detailA1 = await requestJson(apiCtx.request, 'GET', `/api/projects/${pidA}/datasets/${dsA1Id}`, undefined, userToken)
    const verA1 = (detailA1.versions ?? [])[0]
    expect(verA1?.id).toBeTruthy()
    const verBadge = `v${verA1.version_no}`
    const verResolution: string = verA1.resolution

    // 项目 B: 公开 API 建一个无版本数据集(切换目标, 无求解)。
    const projectB = uniqueName('D2项目B')
    const createdB = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectB,
      baseline_resolution: '1h',
      baseline_start_year: 2026,
      baseline_scenario_mode: 'single',
    }, userToken)
    const pidB: number = createdB.project?.id ?? createdB.id
    const dsBName = uniqueName('D2-B空')
    const dsB = await requestJson(apiCtx.request, 'POST', `/api/projects/${pidB}/datasets`, {
      name: dsBName,
    }, userToken)
    const dsBId: number = dsB.dataset?.id ?? dsB.id
    const a1VersionPath = `/api/projects/${pidA}/datasets/${dsA1Id}`
    const bVersionPath = `/api/projects/${pidB}/datasets/${dsBId}`

    const context = await browser.newContext()
    const page = await context.newPage()
    // 版本详情按标志失败/放行: A1 用于同项目失败场景, B 用于导航后场景。
    let failA1Version = false
    let failBVersion = false
    await context.route(
      (url) => url.pathname === a1VersionPath || url.pathname === bVersionPath,
      (route) => {
        const pathname = new URL(route.request().url()).pathname
        if ((pathname === a1VersionPath && failA1Version) || (pathname === bVersionPath && failBVersion)) {
          return route.fulfill({
            status: 500,
            contentType: 'application/json',
            body: '{"detail":"d2 injected versions failure"}',
          })
        }
        return route.fallback()
      },
    )
    // 全程写请求: 加载与重试只许 GET, 不许上传/绑定/采样 POST。
    const writePosts: string[] = []
    page.on('request', (req) => {
      if (req.method() === 'POST' && /\/(datasets|versions|draft|tasks|sample|bind|upload)/.test(req.url())) {
        writePosts.push(`${req.method()} ${req.url()}`)
      }
    })
    const session = await createSession(page, userName, password)

    // A 数据页成功基线: 两数据集、有版本徽章、合法空版本徽章。
    await page.getByText(projectA).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/data$/)
    await expect(page.getByText(dsA1Name)).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText(dsA2Name)).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText(verBadge).first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('该数据集暂无版本').first()).toBeVisible({ timeout: 30_000 })
    // A1 版本弹框内容与同一版本 GET 响应同源(编号/分辨率)。
    await page.locator('tr', { hasText: dsA1Name }).getByRole('button', { name: '版本列表' }).click({ timeout: 30_000 })
    const dialog = page.getByRole('dialog')
    await expect(dialog.getByText(verBadge)).toBeVisible({ timeout: 30_000 })
    await expect(dialog.getByText(verResolution)).toBeVisible({ timeout: 30_000 })
    // 页眉图标关闭与页脚文字关闭同名, 取页脚确认按钮。
    await dialog.getByRole('button', { name: '关闭' }).last().click({ timeout: 30_000 })
    await expect(dialog).toBeHidden({ timeout: 30_000 })

    // 同项目 A1 版本失败(reload 重进, 不声称 state 沿用):
    // 整组错误, A1/A2 行均不发布(A2 成功也不缩水), 无伪无版本/可绑定。
    failA1Version = true
    await page.reload()
    await expect(page.getByText('页面加载失败')).toBeVisible({ timeout: 60_000 })
    await expect(page.getByRole('button', { name: '重试', exact: true })).toBeVisible({ timeout: 30_000 })
    expect(await page.getByText(dsA1Name).count()).toBe(0)
    expect(await page.getByText(dsA2Name).count()).toBe(0)
    expect(await page.getByText('该数据集暂无版本').count()).toBe(0)
    expect(await page.getByRole('button', { name: '绑定到项目' }).count()).toBe(0)
    expect(writePosts).toEqual([])
    // 解除后重试: A1/A2 真实行恢复, 版本号/分辨率与已有 API 同源, A2 仍合法空。
    failA1Version = false
    await page.getByRole('button', { name: '重试', exact: true }).click({ timeout: 30_000 })
    await expect(page.getByText(dsA1Name)).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText(dsA2Name)).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText(verBadge).first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText(verResolution).first()).toBeVisible({ timeout: 30_000 })
    await expect(page.getByText('该数据集暂无版本').first()).toBeVisible({ timeout: 30_000 })
    expect(writePosts).toEqual([])

    // 经项目列表切到 B 会卸载 DataPage 再挂载, 不声称同组件 state 沿用;
    // 本段只验证导航后 B 失败/恢复, 开读清旧 state 见源码, 不夸大浏览器覆盖。
    // 路径: 列表 → B → 数据管理。
    await page.getByText('项目列表').first().click({ timeout: 30_000 })
    await page.getByText(projectB).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks/)
    failBVersion = true
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('数据管理').click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/data$/)

    // B 失败态只剩错误+重试(新挂载, 无旧行可滞留): 无伪“无版本”,
    // 无可交互绑定按钮。
    await expect(page.getByText('页面加载失败')).toBeVisible({ timeout: 60_000 })
    await expect(page.getByRole('button', { name: '重试', exact: true })).toBeVisible({ timeout: 30_000 })
    expect(await page.getByText(dsA1Name).count()).toBe(0)
    expect(await page.getByText(dsA2Name).count()).toBe(0)
    expect(await page.getByText('该数据集暂无版本').count()).toBe(0)
    expect(await page.getByRole('button', { name: '绑定到项目' }).count()).toBe(0)
    expect(await page.getByText(dsBName).count()).toBe(0)
    expect(writePosts).toEqual([])

    // 解除失败后重试: B 真实空态(合法空, 非恢复出版本)。
    failBVersion = false
    await page.getByRole('button', { name: '重试', exact: true }).click({ timeout: 30_000 })
    await expect(page.getByText(dsBName)).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText('该数据集暂无版本')).toBeVisible({ timeout: 30_000 })
    expect(writePosts).toEqual([])

    session.assertNoErrors([
      { method: 'GET', path: a1VersionPath, status: 500, times: 1 },
      { method: 'GET', path: bVersionPath, status: 500, times: 1 },
      // 两空项目读尚无保存的计算配置 404(D2 外既有行为, 计数锁定防漂移)。
      { method: 'GET', path: `/api/projects/${pidA}/config`, status: 404, times: 1 },
      { method: 'GET', path: `/api/projects/${pidB}/config`, status: 404, times: 1 },
    ])
    await apiCtx.close()
    await context.close()
  })
})
