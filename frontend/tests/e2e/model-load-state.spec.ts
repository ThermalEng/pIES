// 模型页读取状态: 必需读取失败不得呈现可编辑空图, 不得误标已保存;
// 未读到真实修订前不开放写操作; 成功空模型仍是合法可编辑空态。
//
// 成功内容真实不 mock: 非空图以公开 API 预置(buildMinimalModel), 重试后
// 按真实设备名逐字对照; 唯一故障注入是目标 GET /projects/{id} 的定向
// 延迟/500(方法精确限定 GET, 其余请求真实放行)。不注库、不注入页面状态,
// 不做画布拖放(按 AGENTS 约束, 只用节点点击 + 侧栏改名等非拖拽控件)。

import { test, expect } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer, loginToken, buildMinimalModel, requestJson } from './setup/api'
import { APP } from './financial-scaffold'

test.describe('模型页读取状态', () => {
  test.setTimeout(300_000)

  test('读取失败无可写空图、无伪已保存, 重试后真实修订可写', async ({ browser }) => {
    const userName = uniqueName('es-modelload')
    const password = strongPassword('Init')
    const projectName = uniqueName('模型读取项目')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)
    const created = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2026,
      baseline_scenario_mode: 'single',
    }, userToken)
    const pid: number = created.project?.id ?? created.id
    expect(pid).toBeTruthy()
    // 公开 API 预置非空模型(电网→负荷/热泵等 6 设备); 成功后按真实设备名对照。
    await buildMinimalModel(apiCtx.request, pid, userToken)
    await apiCtx.close()

    const projectPath = `/api/projects/${pid}`
    // 定向故障: 仅模型页内的目标项目视图 GET 可 500。WorkbenchProvider 跨
    // 页签常驻不重发, 模型页只新增一次 getModel 读取, 故故障恰好命中该读
    // 1 次(计数锁定防漂移); 进项目主页的同路径请求真实放行。
    let failProject = true
    const context = await browser.newContext()
    const page = await context.newPage()
    await context.route(
      (url) => url.pathname === projectPath,
      async (route) => {
        if (route.request().method() !== 'GET') {
          await route.continue()
          return
        }
        if (failProject && page.url().includes('/model')) {
          await route.fulfill({
            status: 500,
            contentType: 'application/json',
            body: '{"detail":"modelload injected project failure"}',
          })
          return
        }
        await route.continue()
      },
    )
    // 全程业务写请求追踪: 失败/加载态只许读, 重试恢复后改名才允许草稿写。
    // 只覆盖业务写路径(draft/tasks/models/datasets 等), 登录等认证请求除外。
    const writeReqs: string[] = []
    page.on('request', (req) => {
      if (req.method() !== 'GET' && /\/(draft|tasks|models|datasets|sample|bind|upload|versions)\b/.test(req.url())) {
        writeReqs.push(`${req.method()} ${new URL(req.url()).pathname}`)
      }
    })
    const session = await createSession(page, userName, password)

    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+$/)
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText(/系统建模|System Modeling/i).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/model$/)

    // 阶段一: 必需读取失败 → 可见错误 + 重试; 无可写画布/面板, 不标已保存。
    await expect(page.getByText(/模型加载失败|Failed to load model/)).toBeVisible({ timeout: 60_000 })
    await expect(page.getByRole('button', { name: /^重试$|^Retry$/ })).toBeVisible({ timeout: 30_000 })
    // 未就绪: 设备面板与画布节点均不渲染, 校验按钮不可用。
    expect(await page.getByLabel(/设备面板|Device palette/).count()).toBe(0)
    expect(await page.locator('.react-flow__node').count()).toBe(0)
    await expect(page.getByRole('button', { name: /校验系统图|Validate graph/ })).toBeDisabled()
    // 未读到真实修订: 不得出现“已保存”, 也不得有任何模型写请求。
    expect(await page.getByText(/^已保存$|^Saved$/).count()).toBe(0)
    expect(writeReqs).toEqual([])

    // 阶段二: 解除故障后点正文重试 → 只读恢复, 图与真实预置一致且标已保存。
    failProject = false
    await page.getByRole('button', { name: /^重试$|^Retry$/ }).click({ timeout: 30_000 })
    await expect(page.locator('.react-flow__node', { hasText: 'E2E heat_load' })).toBeVisible({ timeout: 60_000 })
    expect(await page.locator('.react-flow__node').count()).toBeGreaterThanOrEqual(6)
    await expect(page.getByText(/^已保存$|^Saved$/)).toBeVisible({ timeout: 30_000 })
    await expect(page.getByLabel(/设备面板|Device palette/)).toBeVisible()
    await expect(page.getByRole('button', { name: /校验系统图|Validate graph/ })).toBeEnabled()
    expect(writeReqs).toEqual([])

    // 阶段三: 真实修订可写 —— 点节点经侧栏改名(非拖拽), PUT 成功且回到已保存。
    // 注: 用 heat 口设备改名; electric 口设备改名因画布 ENERGY_CARRIERS 未跟
    // 注册表 electricity 更名导致句柄解析失败而 400, 属既有缺陷(见报告), 本片不碰。
    const newName = uniqueName('E2E rename')
    await page.locator('.react-flow__node', { hasText: 'E2E heat_load' }).first().click()
    await expect(page.locator('#mp-device-name')).toBeVisible({ timeout: 30_000 })
    await page.locator('#mp-device-name').fill(newName)
    await page.locator('#mp-device-name').press('Tab')
    const putRes = await page.waitForResponse((res) => res.request().method() === 'PUT' && res.url().includes('/draft'), { timeout: 60_000 })
    expect(putRes.status()).toBe(200)
    await expect(page.locator('.react-flow__node', { hasText: newName })).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText(/^已保存$|^Saved$/)).toBeVisible({ timeout: 30_000 })
    expect(writeReqs.filter((w) => w.includes('/draft'))).not.toEqual([])

    session.assertNoErrors([
      { method: 'GET', path: projectPath, status: 500, times: 1 },
      // 进项目读尚无保存的计算配置 404(D3 外既有行为, 计数锁定防漂移)。
      { method: 'GET', path: `/api/projects/${pid}/config`, status: 404, times: 1 },
    ])
    await context.close()
  })

  test('延迟读取不呈现可写空图, 成功空模型仍可编辑', async ({ browser }) => {
    const userName = uniqueName('es-modelload')
    const password = strongPassword('Init')
    const projectName = uniqueName('模型空图项目')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)
    const created = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2026,
      baseline_scenario_mode: 'single',
    }, userToken)
    const pid: number = created.project?.id ?? created.id
    expect(pid).toBeTruthy()
    await apiCtx.close()

    const projectPath = `/api/projects/${pid}`
    // 定向延迟: 目标项目视图 GET 真实响应挂起 2s, 其余放行。
    let releaseProject!: () => void
    const gate = new Promise<void>((resolve) => { releaseProject = resolve })
    const context = await browser.newContext()
    const page = await context.newPage()
    await context.route(
      (url) => url.pathname === projectPath,
      async (route) => {
        if (route.request().method() !== 'GET') {
          await route.continue()
          return
        }
        const res = await route.fetch()
        await gate
        await route.fulfill({ response: res })
      },
    )
    const writeReqs: string[] = []
    page.on('request', (req) => {
      if (req.method() !== 'GET' && /\/(draft|tasks|models|datasets|sample|bind|upload|versions)\b/.test(req.url())) {
        writeReqs.push(`${req.method()} ${new URL(req.url()).pathname}`)
      }
    })
    const session = await createSession(page, userName, password)

    try {
      await page.getByText(projectName).click({ timeout: 30_000 })
      await expect(page).toHaveURL(/\/projects\/\d+$/)
      await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText(/系统建模|System Modeling/i).click({ timeout: 30_000 })
      await expect(page).toHaveURL(/\/model$/)

      // 延迟期间: 只显示加载中, 无面板/节点/可用校验, 无写请求。
      await expect(page.getByText(/模型加载中|Loading model/)).toBeVisible({ timeout: 30_000 })
      expect(await page.getByLabel(/设备面板|Device palette/).count()).toBe(0)
      expect(await page.locator('.react-flow__node').count()).toBe(0)
      await expect(page.getByRole('button', { name: /校验系统图|Validate graph/ })).toBeDisabled()
      expect(writeReqs).toEqual([])

      // 真实成功且为空模型: 占位 + 面板出现(合法可编辑空态, 非失败)。
      releaseProject()
      await expect(page.getByText(/从左侧面板拖拽设备|Drag devices from the left panel/)).toBeVisible({ timeout: 60_000 })
      await expect(page.getByLabel(/设备面板|Device palette/)).toBeVisible({ timeout: 30_000 })
      await expect(page.getByRole('button', { name: /校验系统图|Validate graph/ })).toBeEnabled()
      await expect(page.getByText(/^已保存$|^Saved$/)).toBeVisible({ timeout: 30_000 })
      expect(writeReqs).toEqual([])
    } finally {
      releaseProject()
    }

    session.assertNoErrors([
      // 进项目读尚无保存的计算配置 404(D3 外既有行为, 计数锁定防漂移)。
      { method: 'GET', path: `/api/projects/${pid}/config`, status: 404, times: 1 },
    ])
    await context.close()
  })
})
