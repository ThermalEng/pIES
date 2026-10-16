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
    // 创建响应精确信封 {project, my_role}: 直接消费具名键, 不做兼容猜测。
    const pid: number = created.project.id
    expect(pid).toBeTruthy()
    // 公开 API 预置非空模型(电网→负荷/热泵等 6 设备); 成功后按真实设备名对照。
    await buildMinimalModel(apiCtx.request, pid, userToken)
    await apiCtx.close()

    const projectPath = `/api/projects/${pid}`
    // 定向故障: 仅模型页内的目标项目视图 GET 可 500。WorkbenchProvider 跨
    // 页签常驻不重发, 模型页只新增一次 getModel 读取, 故故障恰好命中该读
    // 1 次(计数锁定防漂移); 进项目主页的同路径请求真实放行。
    // 重试阶段对放行响应加定向延迟, 验证 error→loading 真过渡与单飞。
    let failProject = true
    let passDelayMs = 0
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
        if (passDelayMs > 0 && page.url().includes('/model')) {
          await new Promise((r) => setTimeout(r, passDelayMs))
        }
        await route.continue()
      },
    )
    // 目标读计数: 断言重试只派发一次模型读(无重叠重复请求)。
    let projectGets = 0
    page.on('request', (req) => {
      if (req.method() === 'GET' && new URL(req.url()).pathname === projectPath) projectGets += 1
    })
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

    // 阶段二: 解除故障后点正文重试 → 先进入真实加载态(旧错清除、重试按钮
    // 卸载, 不可重复派发), 再只读恢复, 图与真实预置一致且标已保存。
    failProject = false
    passDelayMs = 3000
    const getsBeforeRetry = projectGets
    await page.getByRole('button', { name: /^重试$|^Retry$/ }).click({ timeout: 30_000 })
    await expect(page.getByText(/模型加载中|Loading model/)).toBeVisible({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: /^重试$|^Retry$/ })).toHaveCount(0, { timeout: 30_000 })
    await expect(page.locator('.react-flow__node', { hasText: 'E2E heat_load' })).toBeVisible({ timeout: 60_000 })
    // 重试只派发一次模型读: 重复点击不可能(按钮在途卸载), 无重叠请求。
    expect(projectGets).toBe(getsBeforeRetry + 1)
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
    // 先登记响应等待再触发失焦保存, 快响应也不漏检。
    const putWait = page.waitForResponse((res) => res.request().method() === 'PUT' && res.url().includes('/draft'), { timeout: 60_000 })
    await page.locator('#mp-device-name').press('Tab')
    const putRes = await putWait
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
    // 创建响应精确信封 {project, my_role}: 直接消费具名键, 不做兼容猜测。
    const pid: number = created.project.id
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

  test('已加载图刷新在途不可写, 失败保留旧图并持久提示, 恢复后可写', async ({ browser }) => {
    const userName = uniqueName('es-modelload')
    const password = strongPassword('Init')
    const projectName = uniqueName('模型刷新项目')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)
    const created = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2026,
      baseline_scenario_mode: 'single',
    }, userToken)
    // 创建响应精确信封 {project, my_role}: 直接消费具名键, 不做兼容猜测。
    const pid: number = created.project.id
    expect(pid).toBeTruthy()
    await buildMinimalModel(apiCtx.request, pid, userToken)
    await apiCtx.close()

    const projectPath = `/api/projects/${pid}`
    // 刷新故障: 模型页内的目标项目视图 GET 可延迟、可 500; 其余真实放行。
    // (初次加载与主页请求全部放行, 只拦截手动刷新那一次)。
    let armRefreshFault = false
    let refreshDelayMs = 0
    let refreshFail = false
    const context = await browser.newContext()
    const page = await context.newPage()
    await context.route(
      (url) => url.pathname === projectPath,
      async (route) => {
        if (route.request().method() !== 'GET') {
          await route.continue()
          return
        }
        if (armRefreshFault && page.url().includes('/model')) {
          if (refreshDelayMs > 0) {
            await new Promise((r) => setTimeout(r, refreshDelayMs))
          }
          if (refreshFail) {
            await route.fulfill({
              status: 500,
              contentType: 'application/json',
              body: '{"detail":"modelload injected refresh failure"}',
            })
            return
          }
        }
        await route.continue()
      },
    )
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
    await expect(page.locator('.react-flow__node', { hasText: 'E2E heat_load' })).toBeVisible({ timeout: 60_000 })
    await expect(page.getByText(/^已保存$|^Saved$/)).toBeVisible({ timeout: 30_000 })
    expect(writeReqs).toEqual([])

    // 阶段一: 刷新在途(延迟 3s) → 旧图仍在但不可写: 校验禁用、点节点无侧栏。
    armRefreshFault = true
    refreshDelayMs = 3000
    refreshFail = false
    await page.getByRole('button', { name: /^刷新$|^Refresh$/ }).click({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: /校验系统图|Validate graph/ })).toBeDisabled({ timeout: 30_000 })
    await page.locator('.react-flow__node', { hasText: 'E2E heat_load' }).first().click()
    expect(await page.locator('#mp-device-name').count()).toBe(0)
    // 面板条目在途只读(不可拖拽)。
    expect(await page.locator('.mp-palette-item[aria-disabled="true"]').count()).toBeGreaterThan(0)
    expect(writeReqs).toEqual([])
    // 在途刷新成功落定(旧修订一致), 再进入失败阶段。
    refreshDelayMs = 0
    await expect(page.getByRole('button', { name: /校验系统图|Validate graph/ })).toBeEnabled({ timeout: 60_000 })

    // 阶段二: 再次刷新失败 → 旧图仍在(非空、非成功态), 陈旧提示持久,
    // 写操作仍被阻断, 无草稿写请求, 保存/冲突态未被重置。
    refreshFail = true
    await page.getByRole('button', { name: /^刷新$|^Refresh$/ }).click({ timeout: 30_000 })
    await expect(page.getByText(/模型加载失败|Failed to load model/)).toBeVisible({ timeout: 60_000 })
    expect(await page.locator('.react-flow__node').count()).toBeGreaterThanOrEqual(6)
    expect(await page.getByText(/从左侧面板拖拽设备|Drag devices from the left panel/).count()).toBe(0)
    await expect(page.getByRole('button', { name: /校验系统图|Validate graph/ })).toBeDisabled()
    await page.locator('.react-flow__node', { hasText: 'E2E heat_load' }).first().click()
    expect(await page.locator('#mp-device-name').count()).toBe(0)
    expect(writeReqs).toEqual([])

    // 阶段三: 故障解除后显式恢复 → 当前内容/修订一致, 编辑重新可用。
    refreshFail = false
    refreshDelayMs = 0
    await page.getByRole('button', { name: /^重试$|^Retry$/ }).click({ timeout: 30_000 })
    await expect(page.getByText(/^已保存$|^Saved$/)).toBeVisible({ timeout: 60_000 })
    await expect(page.getByRole('button', { name: /校验系统图|Validate graph/ })).toBeEnabled()
    const newName = uniqueName('E2E refresh')
    await page.locator('.react-flow__node', { hasText: 'E2E heat_load' }).first().click()
    await expect(page.locator('#mp-device-name')).toBeVisible({ timeout: 30_000 })
    await page.locator('#mp-device-name').fill(newName)
    const putWait = page.waitForResponse((res) => res.request().method() === 'PUT' && res.url().includes('/draft'), { timeout: 60_000 })
    await page.locator('#mp-device-name').press('Tab')
    expect((await putWait).status()).toBe(200)
    await expect(page.locator('.react-flow__node', { hasText: newName })).toBeVisible({ timeout: 60_000 })

    session.assertNoErrors([
      { method: 'GET', path: projectPath, status: 500, times: 1 },
      // 进项目读尚无保存的计算配置 404(D3 外既有行为, 计数锁定防漂移)。
      { method: 'GET', path: `/api/projects/${pid}/config`, status: 404, times: 1 },
    ])
    await context.close()
  })

  test('描述符失败图不就绪, 重试重取注册表后恢复', async ({ browser }) => {
    const userName = uniqueName('es-modelload')
    const password = strongPassword('Init')
    const projectName = uniqueName('模型描述符项目')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)
    const created = await requestJson(apiCtx.request, 'POST', '/api/projects', {
      name: projectName,
      baseline_resolution: '1h',
      baseline_start_year: 2026,
      baseline_scenario_mode: 'single',
    }, userToken)
    // 创建响应精确信封 {project, my_role}: 直接消费具名键, 不做兼容猜测。
    const pid: number = created.project.id
    expect(pid).toBeTruthy()
    await apiCtx.close()

    const typesPath = '/api/registry/device-types'
    // 定向故障: 仅注册表 GET 可 500 一次, 其余真实放行。
    let failTypes = true
    const context = await browser.newContext()
    const page = await context.newPage()
    await context.route(
      (url) => url.pathname === typesPath,
      async (route) => {
        if (route.request().method() !== 'GET') {
          await route.continue()
          return
        }
        if (failTypes) {
          failTypes = false
          await route.fulfill({
            status: 500,
            contentType: 'application/json',
            body: '{"detail":"modelload injected types failure"}',
          })
          return
        }
        await route.continue()
      },
    )
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

    // 描述符失败 → 图不就绪: 可见错误 + 重试, 无面板/节点, 无写请求。
    await expect(page.getByText(/模型加载失败|Failed to load model/)).toBeVisible({ timeout: 60_000 })
    await expect(page.getByRole('button', { name: /^重试$|^Retry$/ })).toBeVisible({ timeout: 30_000 })
    expect(await page.getByLabel(/设备面板|Device palette/).count()).toBe(0)
    expect(await page.locator('.react-flow__node').count()).toBe(0)
    expect(writeReqs).toEqual([])

    // 重试一并重取注册表 → 真实恢复为空图可编辑态。
    await page.getByRole('button', { name: /^重试$|^Retry$/ }).click({ timeout: 30_000 })
    await expect(page.getByText(/从左侧面板拖拽设备|Drag devices from the left panel/)).toBeVisible({ timeout: 60_000 })
    await expect(page.getByLabel(/设备面板|Device palette/)).toBeVisible({ timeout: 30_000 })
    expect(writeReqs).toEqual([])

    session.assertNoErrors([
      { method: 'GET', path: typesPath, status: 500, times: 1 },
      // 进项目读尚无保存的计算配置 404(D3 外既有行为, 计数锁定防漂移)。
      { method: 'GET', path: `/api/projects/${pid}/config`, status: 404, times: 1 },
    ])
    await context.close()
  })

  test('项目切换不压住新读, 旧读回包不污染新项目', async ({ browser }) => {
    // 生命周期回归: A 读挂起时切到 B, B 必须真实读出(画布节点 + 工作台头部
    // 项目名均为 B), 随后放行 A 也不得把 A 内容/修订/就绪/错误、共享状态
    // 或工作台项目摘要发布到 B。
    // 说明: 工作台内无跨项目切换 UI(仅列表进项目, 会重挂整树), 此处用路由
    // 本身支持的同树 :id 切换(与路由参数变更同一生命周期, 不新增产品特性):
    // history 入栈 + popstate 由真实路由消费, 组件/网络均为真实。
    const userName = uniqueName('es-modelload')
    const password = strongPassword('Init')
    const projectA = uniqueName('模型A项目')
    const projectB = uniqueName('模型B项目')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, userName, password)
    const userToken = await loginToken(apiCtx.request, userName, password)
    const mkProject = async (name: string): Promise<number> => {
      const created = await requestJson(apiCtx.request, 'POST', '/api/projects', {
        name,
        baseline_resolution: '1h',
        baseline_start_year: 2026,
        baseline_scenario_mode: 'single',
      }, userToken)
      // 创建响应精确信封 {project, my_role}: 直接消费具名键, 不做兼容猜测。
      const pid: number = created.project.id
      expect(pid).toBeTruthy()
      return pid
    }
    const pidA = await mkProject(projectA)
    await buildMinimalModel(apiCtx.request, pidA, userToken)
    const pidB = await mkProject(projectB)
    // B 仅放一个同名绝不重复的设备, 与 A 的 6 节点明确区分。
    const bDevice = uniqueName('E2E-B-lifecycle')
    const deviceTypes = await requestJson(apiCtx.request, 'GET', '/api/registry/device-types', undefined, userToken)
    const heatType = (deviceTypes.items as Array<{ type_id: string }>).find((it) => it.type_id.includes('heat_load'))
    expect(heatType).toBeTruthy()
    const viewB = await requestJson(apiCtx.request, 'GET', `/api/projects/${pidB}`, undefined, userToken)
    await requestJson(apiCtx.request, 'PUT', `/api/projects/${pidB}/draft`, {
      expected_revision: viewB.draft.revision,
      commands: [{
        id: `e2e-lifecycle-${pidB}`,
        unit: 'model',
        type: 'model.upsert_device',
        payload: { name: bDevice, device_type: heatType!.type_id, kind: 'new', params: {} },
      }],
    }, userToken)
    await apiCtx.close()

    const pathA = `/api/projects/${pidA}`
    // 定向挂起: 仅 A 项目视图 GET 取回真实响应后挂起, B 与其余真实放行。
    // A 读取精确到 3 次(工作台主体 + 任务页刷新 + 模型读取各 1 次): 发起计数
    // 与挂起计数必须在切换前都到达 3, 证明模型旧读已真实发出并卡在门禁,
    // 而不只是看到加载文案; 多一次少一次都超时失败, 不静默吸收噪声。
    let releaseA!: () => void
    const gateA = new Promise<void>((resolve) => { releaseA = resolve })
    let aHeld = 0
    let aReqTotal = 0
    const context = await browser.newContext()
    const page = await context.newPage()
    await context.route(
      (url) => url.pathname === pathA,
      async (route) => {
        if (route.request().method() !== 'GET') {
          await route.continue()
          return
        }
        aReqTotal += 1
        const res = await route.fetch()
        aHeld += 1
        await gateA
        await route.fulfill({ response: res })
      },
    )
    let bGets = 0
    page.on('request', (req) => {
      const u = new URL(req.url())
      if (req.method() === 'GET' && u.pathname === `/api/projects/${pidB}`) bGets += 1
    })
    // A 旧响应收集(按响应事件计数, 不共用同一 waitForResponse)。
    const aLateStatuses: number[] = []
    page.on('response', (res) => {
      const req = res.request()
      if (req.method() === 'GET' && new URL(req.url()).pathname === pathA) aLateStatuses.push(res.status())
    })
    const session = await createSession(page, userName, password)

    try {
      // A 页加载中(A 读挂起)。
      await page.getByText(projectA).click({ timeout: 30_000 })
      await expect(page).toHaveURL(/\/projects\/\d+$/)
      await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText(/系统建模|System Modeling/i).click({ timeout: 30_000 })
      await expect(page).toHaveURL(/\/model$/)
      await expect(page.getByText(/模型加载中|Loading model/)).toBeVisible({ timeout: 30_000 })
      // 三次 A 读取(工作台主体/任务页刷新/模型读取)全部发出并卡在门禁,
      // 其中第 3 次即模型旧读: 到达门禁才切换, 不依赖加载文案时序。
      await expect.poll(async () => aReqTotal, { timeout: 30_000 }).toBe(3)
      await expect.poll(async () => aHeld, { timeout: 30_000 }).toBe(3)

      // 同树切到 B(路由 :id 变更, 画布按 projectId 键控重挂)。
      await page.evaluate((url) => {
        window.history.pushState({}, '', url)
        window.dispatchEvent(new PopStateEvent('popstate'))
      }, `/projects/${pidB}/model`)
      // B 真实读出: 唯一节点 + 已保存, 且 B 确实发起了读取。
      await expect(page.locator('.react-flow__node', { hasText: bDevice })).toBeVisible({ timeout: 60_000 })
      expect(await page.locator('.react-flow__node', { hasText: 'E2E grid_connection' }).count()).toBe(0)
      await expect(page.getByText(/^已保存$|^Saved$/)).toBeVisible({ timeout: 30_000 })
      expect(bGets).toBeGreaterThanOrEqual(1)
      // 工作台摘要归属 B: 头部标题为 B 项目名, A 项目名不得作为当前状态出现。
      await expect(page.locator('.wb-header__title', { hasText: projectB })).toBeVisible({ timeout: 30_000 })
      expect(await page.getByText(projectA, { exact: false }).count()).toBe(0)

      // 放行 A 的旧读: 三个挂起的 A 响应都必须真实到达(响应事件计数,
      // 每个旧请求独立事件, 不共用同一 waitForResponse),
      // 但 B 内容/就绪/共享状态不得被覆盖, 也不得立错。
      releaseA()
      await expect.poll(async () => aLateStatuses.length, { timeout: 30_000 }).toBe(3)
      expect(aLateStatuses).toEqual([200, 200, 200])
      // 旧 A 摘要回包不得覆盖 B: 标题仍为 B, A 名仍不出现(画布断言在后)。
      await expect(page.locator('.wb-header__title', { hasText: projectB })).toBeVisible({ timeout: 30_000 })
      expect(await page.getByText(projectA, { exact: false }).count()).toBe(0)
      await expect(page.locator('.react-flow__node', { hasText: bDevice })).toBeVisible({ timeout: 30_000 })
      expect(await page.locator('.react-flow__node', { hasText: 'E2E grid_connection' }).count()).toBe(0)
      expect(await page.locator('.react-flow__node').count()).toBe(1)
      await expect(page.getByText(/^已保存$|^Saved$/)).toBeVisible({ timeout: 30_000 })
      expect(await page.getByText(/模型加载失败|Failed to load model/).count()).toBe(0)
    } finally {
      releaseA()
    }

    session.assertNoErrors([
      // 进项目读尚无保存的计算配置 404(D3 外既有行为, 计数锁定防漂移)。
      { method: 'GET', path: `/api/projects/${pidA}/config`, status: 404, times: 1 },
    ])
    await context.close()
  })
})
