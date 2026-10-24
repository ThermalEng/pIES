// QA-E2E-01 场景 1-3(13.3):
//   1. 登录、首登强制改密、退出、会话失效(新窗口接管)
//   2. 创建项目并浏览全部工作台页面(模型/数据/配置/校验/任务/结果/导出)
//   3. 建模: 添加热泵/电池/电网/负荷, 真实端口合法连接 + 错误连接诊断
//
// 前置: global-setup 已确保 admin 可用并清理历史 es- 用户。
// 造数(API): 仅创建独立用户(engineer); 场景 3 使用 UI 完成建模。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword, loginViaUI } from './fixtures'
import { createEngineer } from './setup/api'

// ---------------------------------------------------------------------------
// 场景 1: 登录 / 首登改密 / 退出 / 会话失效
// ---------------------------------------------------------------------------

test.describe('场景 1: 认证与会话 @basic', () => {
  test('登录、强制改密、退出、新窗口会话失效', async ({ browser }) => {
    // 独立用户(API 造数, 与被测 UI 隔离); forceChange 使首登强制改密
    const username = uniqueName('es-auth')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    const created = await createEngineer(apiCtx.request, username, password, { forceChange: true })
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const runtimeErrors: string[] = []
    page.on('pageerror', error => runtimeErrors.push(error.message))
    // 首登: 用初始密码登录 → 强制改密表单
    await page.goto('/login')
    await expect(page.getByRole('heading', { name: /登录|Sign in/i })).toBeVisible()
    await page.getByLabel(/用户名|Username/i).fill(username)
    await page.getByLabel(/密码|Password/i).fill(password)
    await page.getByRole('button', { name: /登\s*录|Sign in/i }).click()

    // 1a. 首登强制改密表单出现
    await expect(page.getByText(/首次登录|must change the password/i)).toBeVisible()
    // 旧密码由后端预填(初始密码已自动填入当前密码框); 必填 label 带 (必填项),
    // 用 id 精确区分"新密码"与"确认新密码"
    await page.locator('#pwd-new').fill(strongPassword('Changed'))
    await page.locator('#pwd-confirm').fill('not-matching')
    await page.getByRole('button', { name: /^修改密码$|^Change password$/i }).click()
    await expect(page.getByText(/两次输入的密码不一致|passwords do not match/i)).toBeVisible()

    // 1b. 改密成功 → 回登录页, 用新密码重新登录
    const newPassword = strongPassword('Changed')
    await page.locator('#pwd-new').fill(newPassword)
    await page.locator('#pwd-confirm').fill(newPassword)
    await page.getByRole('button', { name: /^修改密码$|^Change password$/i }).click()
    await expect(page.getByText(/密码已修改|password changed/i)).toBeVisible()
    await expect(page).toHaveURL(/\/login/)

    // 1c. 退出登录
    await loginViaUI(page, username, newPassword)
    await expect(page).toHaveURL(/\/$/)
    await page.getByRole('button', { name: /退出登录|Logout/i }).click()
    await expect(page).toHaveURL(/\/login/)
    // 退出后再访问受保护页 → 跳登录
    await page.goto('/settings')
    await expect(page).toHaveURL(/\/login/)

    // 1d. 会话失效: 同一用户第二个窗口登录 → 原窗口会话被撤销
    // 注: 两个窗口必须使用独立 context(独立 cookie jar), 否则同 context
    // 的 Set-Cookie 会覆盖旧窗口凭证, 无法复现会话失效
    const context2: BrowserContext = await browser.newContext()
    const page2: Page = await context2.newPage()
    await loginViaUI(page2, username, newPassword)
    await expect(page2).toHaveURL(/\/$/)

    // 原窗口(已退出, localStorage 无会话标记)重新登录成功但保持退出前状态;
    // 换一种真实用户路径: 原窗口重新登录后, 新窗口的登录(同用户)会撤销其会话,
    // 原窗口页面内操作(非整页导航)触发 401 → 全局处理器带 reason=expired 跳登录
    await page.goto('/') // 未登录 → 跳 login
    await expect(page).toHaveURL(/\/login/)
    await loginViaUI(page, username, newPassword)
    await expect(page).toHaveURL(/\/$/)
    // 此时 page2 的会话已因 page 重新登录被撤销? 不: 后登录者(同凭证)撤销前者——
    // page2 已登录, page 重新登录会撤销 page2 会话并让自己成为 pending,
    // 前端 LoginPage 自动 confirm-takeover, page 会话激活、page2 被撤销
    // 因此 page2 页面内刷新列表(点击不同筛选按钮触发请求) → 401 → 跳登录页(带过期提示)
    await page2.bringToFront()
    await page2.getByRole('button', { name: /进行中|Active/i }).click()
    await expect(page2).toHaveURL(/\/login/)
    await expect(page2.getByText(/登录已过期|session has expired/i)).toBeVisible()

    // 新窗口正常使用(登录无接管卡死)
    await expect(page.getByRole('button', { name: /退出登录|Logout/i })).toBeVisible()
    await context2.close()

    // Monitor from before the first interaction, not after the tested actions.
    // The deliberate expired-session HTTP response is tested above.
    expect(runtimeErrors).toEqual([])
    await context.close()
  })
})

// ---------------------------------------------------------------------------
// 场景 2: 创建项目并浏览全部工作台页面
// ---------------------------------------------------------------------------

test.describe('场景 2: 项目与页面导航 @basic', () => {
  test('创建项目并逐个访问工作台 7 个子页面', async ({ browser }) => {
    const username = uniqueName('es-proj')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    // 2a. 新建项目(UI)
    const projectName = uniqueName('QA 验收项目')
    await page.getByRole('button', { name: /新建项目|New project/i }).first().click()
    await page.getByLabel(/名称|Name/i).fill(projectName)
    await page.locator('#pp-create-start-year').fill('2026')
    await page.getByRole('button', { name: /确认|Confirm/i }).click()
    await expect(page.getByText(projectName)).toBeVisible()

    // 2b. 打开项目 → 默认落在任务中心
    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()

    // 2c. 遍历 7 个子页面: 模型/数据/配置/校验/任务/结果/导出
    // 各页标题结构不同(tasks/results 用 Card 标题, 无 h1), 统一断言:
    // 页面已渲染且非"占位组件"(.ies-page-placeholder)
    const navItems: Array<[string, RegExp]> = [
      ['系统建模', /model/],
      ['数据管理', /data/],
      ['计算配置', /config/],
      ['校验', /validation/],
      ['任务中心', /tasks/],
      ['结果分析', /results/],
      ['导出', /export/],
    ]
    for (const [label, pathRe] of navItems) {
      await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText(label).click()
      await expect(page).toHaveURL(pathRe)
      // 页面已渲染(非占位组件)
      await expect(page.locator('.wb-content .ies-page-placeholder')).not.toBeVisible({ timeout: 10_000 })
    }

    const projectId = page.url().match(/\/projects\/(\d+)/)![1]
    session.assertNoErrors([
      // 空项目未保存配置时允许精确资源缺席, 不依赖页面缓存/请求合并的次数。
      { method: 'GET', path: `/api/projects/${projectId}/config`, status: 404 },
    ])
    await context.close()
  })
})

// ---------------------------------------------------------------------------
// 场景 3: 建模 - 真实端口连接 + 错误连接诊断
// ---------------------------------------------------------------------------

test.describe('场景 3: 建模与连线 @basic', () => {
  test('拖拽添加热泵/电池/电网/负荷, 合法连接, 错误连接给出诊断', async ({ browser }) => {
    const username = uniqueName('es-model')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    // 创建项目(起始年为服务端必填, 按当前产品要求如实填写, 不弱化校验)
    const projectName = uniqueName('QA 建模项目')
    await page.getByRole('button', { name: /新建项目|New project/i }).first().click()
    await page.getByLabel(/名称|Name/i).fill(projectName)
    await page.locator('#pp-create-start-year').fill('2026')
    await page.getByRole('button', { name: /确认|Confirm/i }).click()
    await expect(page.getByText(projectName)).toBeVisible()
    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()

    // 进入模型页
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('系统建模').click()
    await expect(page).toHaveURL(/\/model$/)

    const projectId = page.url().match(/\/projects\/(\d+)/)![1]
    // 3a. 拖拽添加设备: 电网 / 电负荷 / 热泵 / 电池
    // 固定网格坐标避免节点重叠(随机坐标曾导致拖放落点重叠未添加)
    const canvas = page.locator('.mp-canvas-wrap')
    const palette = page.locator('.mp-palette')
    await expect(palette).toBeVisible({ timeout: 10_000 })
    const spots: Array<[number, number]> = [
      [0.15, 0.15], // 电网连接(左上)
      [0.75, 0.15], // 电负荷(右上)
      [0.15, 0.75], // 热泵(左下)
      [0.75, 0.75], // 电池储能(右下)
      [0.45, 0.75], // 热负荷(中下)
    ]
    let spotIdx = 0
    const addDevice = async (typeName: string) => {
      // 设备面板可滚动(9 类设备, 视口内仅显示前 6 项): 先滚动到目标项
      const item = palette.locator(`[aria-label*="${typeName}"]`).first()
      await item.scrollIntoViewIfNeeded()
      await expect(item).toBeVisible()
      const box = await item.boundingBox()
      const canvasBox = await canvas.boundingBox()
      expect(box && canvasBox).toBeTruthy()
      const [fx, fy] = spots[spotIdx++ % spots.length]
      await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2)
      await page.mouse.down()
      await page.mouse.move(canvasBox!.x + canvasBox!.width * fx, canvasBox!.y + canvasBox!.height * fy, { steps: 8 })
      await page.mouse.up()
      // UI 节点可见且实际草稿已写入服务器, 不检查保存图标 CSS 或固定 sleep。
      const node = page.locator('.react-flow__node', { hasText: typeName }).first()
      await expect(node).toBeVisible({ timeout: 10_000 })
      await expect.poll(async () => {
        const response = await page.request.get(`/api/projects/${projectId}`)
        if (!response.ok()) return -1
        const view = await response.json()
        return view.draft?.content?.model?.devices?.length ?? -1
      }, { timeout: 15_000 }).toBe(spotIdx)
      // ReactFlow 的 fitView 可能移动节点; 等几何稳定后再取连接端口位置。
      const nodeHandle = await node.elementHandle()
      expect(nodeHandle).toBeTruthy()
      await nodeHandle!.waitForElementState('stable', { timeout: 10_000 })
    }
    await addDevice('电网连接')
    await addDevice('电负荷')
    await addDevice('热泵')
    await addDevice('电池储能')

    // 3b. 非法连接: 把热泵热输出连到电负荷(电气) → 类型不兼容诊断
    // 设备已保存落库(上一步等待), 直接连线
    // 通过画布节点句柄尝试连接(热泵 thermal out → 电负荷 electric in)
    const hpNode = page.locator('.react-flow__node', { hasText: '热泵' }).first()
    const loadNode = page.locator('.react-flow__node', { hasText: '电负荷' }).first()
    await expect(hpNode).toBeVisible()
    await expect(loadNode).toBeVisible()
    // xyflow 连接操作: 拖拽源句柄到目标句柄
    // (handle 是 React Flow 的 div; 用 class 定位热泵 heat 输出与电负荷 electric 输入)
    const hpOut = hpNode.locator('.mp-handle--heat').last() // thermal out
    const loadIn = loadNode.locator('.mp-handle--electricity').first() // electricity in
    await expect(hpOut).toBeVisible({ timeout: 5_000 })
    await expect(loadIn).toBeVisible({ timeout: 5_000 })
    const src = await hpOut.boundingBox()
    const dst = await loadIn.boundingBox()
    expect(src && dst).toBeTruthy()
    await page.mouse.move(src!.x + src!.width / 2, src!.y + src!.height / 2)
    await page.mouse.down()
    await page.mouse.move(dst!.x + dst!.width / 2, dst!.y + dst!.height / 2, { steps: 12 })
    await page.mouse.up()
    // 错误诊断出现(能源类型不兼容): 画布下方 .mp-issues 诊断条
    await expect(page.locator('.mp-issues').getByText(/能源类型不兼容|Energy type mismatch/i).first()).toBeVisible({ timeout: 10_000 })

    // 3c. 合法连接: 热泵热输出 → 热负荷热输入
    // 项目模型里没有热负荷节点(只加了 4 类); 先补加热负荷
    await addDevice('热负荷')
    // addDevice 已等待服务端实际保存及节点几何稳定, 无需样式态二次等待。

    const hpOut2 = hpNode.locator('.mp-handle--heat').last()
    const heatLoadNode = page.locator('.react-flow__node', { hasText: '热负荷' }).first()
    const hlIn = heatLoadNode.locator('.mp-handle--heat').first()
    const s2 = await hpOut2.boundingBox()
    const d2 = await hlIn.boundingBox()
    expect(s2 && d2).toBeTruthy()
    await page.mouse.move(s2!.x + s2!.width / 2, s2!.y + s2!.height / 2)
    await page.mouse.down()
    await page.mouse.move(d2!.x + d2!.width / 2, d2!.y + d2!.height / 2, { steps: 12 })
    await page.mouse.up()
    // 合法连接成功: 画布出现连线
    await expect(page.locator('.react-flow__edge').first()).toBeVisible({ timeout: 10_000 })

    // 3d. 服务端一致性(RR-P1-04): 2.0 连接只表达真实端点(设备名 + YAML 真实端口名),
    // 热泵 heat_out 输出 → 热负荷 heat_in 输入 —— 断言名称而非仅类型, 杜绝"端口猜错"
    const viewResp = await page.request.get(`/api/projects/${projectId}`)
    expect(viewResp.ok()).toBeTruthy()
    const view = (await viewResp.json()) as {
      draft: { content: { model: { connections: Array<{
        name: string
        from_device: string
        from_port: string
        to_device: string
        to_port: string
      }> } } }
    }
    const connections = view.draft.content.model.connections
    expect(connections.length).toBeGreaterThan(0)
    const conn = connections[connections.length - 1]
    expect(conn.from_port).toBe('heat_out')
    expect(conn.to_port).toBe('heat_in')
    expect(typeof conn.from_device).toBe('string')
    expect(conn.from_device.length).toBeGreaterThan(0)
    expect(typeof conn.to_device).toBe('string')
    expect(conn.to_device.length).toBeGreaterThan(0)

    session.assertNoErrors([
      // 未保存的计算配置可以被请求零次或多次; 其他 404 仍然严格报错。
      { method: 'GET', path: `/api/projects/${projectId}/config`, status: 404 },
    ])
    await context.close()
  })
})
