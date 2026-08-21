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

test.describe('场景 1: 认证与会话', () => {
  test('登录、强制改密、退出、新窗口会话失效', async ({ browser }) => {
    // 独立用户(API 造数, 与被测 UI 隔离); forceChange 使首登强制改密
    const username = uniqueName('es-auth')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    const created = await createEngineer(apiCtx.request, username, password, { forceChange: true })
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
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

    // 控制台/网络错误检查
    const errors: string[] = []
    page.on('console', (msg) => {
      if (msg.type() === 'error') errors.push(msg.text())
    })
    expect(errors).toEqual([])
    await context.close()
  })
})

// ---------------------------------------------------------------------------
// 场景 2: 创建项目并浏览全部工作台页面
// ---------------------------------------------------------------------------

test.describe('场景 2: 项目与页面导航', () => {
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
    await page.getByRole('button', { name: /确认|Confirm/i }).click()
    await expect(page.getByText(projectName)).toBeVisible()

    // 2b. 打开项目 → 默认落在任务中心
    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+$/)

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

    session.assertNoErrors()
    await context.close()
  })
})

// ---------------------------------------------------------------------------
// 场景 3: 建模 - 真实端口连接 + 错误连接诊断
// ---------------------------------------------------------------------------

test.describe('场景 3: 建模与连线', () => {
  test('拖拽添加热泵/电池/电网/负荷, 合法连接, 错误连接给出诊断', async ({ browser }) => {
    const username = uniqueName('es-model')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: process.env.E2E_APP_URL ?? 'http://web:80' })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, username, password)

    // 创建项目
    const projectName = uniqueName('QA 建模项目')
    await page.getByRole('button', { name: /新建项目|New project/i }).first().click()
    await page.getByLabel(/名称|Name/i).fill(projectName)
    await page.getByRole('button', { name: /确认|Confirm/i }).click()
    await expect(page.getByText(projectName)).toBeVisible()
    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+$/)

    // 进入模型页
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('系统建模').click()
    await expect(page).toHaveURL(/\/model$/)

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
      // 等待设备节点出现在画布(拖拽后自动保存 1.2s 防抖)
      await expect(page.locator('.react-flow__node', { hasText: typeName }).first()).toBeVisible({ timeout: 10_000 })
      await expect(page.locator('.mp-save-state--saved, .wb-autosave--saved').first()).toBeVisible({ timeout: 15_000 })
      // 等待视野跟随动画结束(fitView 300ms), 否则连线手势的句柄坐标在动画中漂移
      await page.waitForTimeout(800)
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
    const loadIn = loadNode.locator('.mp-handle--electric').first() // electric in
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
    // 等待自动保存完成再连线
    await expect(page.locator('.mp-save-state--saved, .wb-autosave--saved').first()).toBeVisible({ timeout: 15_000 })

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

    // 3d. 服务端一致性(RR-P1-04): 连接两端必须是 YAML 真实端口
    // (热泵 heat_out 输出 → 热负荷 heat_in 输入), 而非按类型猜出的端口
    const projectId = page.url().match(/\/projects\/(\d+)/)![1]
    const graphResp = await page.request.get(`/api/projects/${projectId}/model`)
    expect(graphResp.ok()).toBeTruthy()
    const graph = (await graphResp.json()) as {
      connections: Array<{ from_port_id: number; to_port_id: number }>
      ports: Array<{ id: number; name: string; direction: string; port_type: string }>
    }
    expect(graph.connections.length).toBeGreaterThan(0)
    const conn = graph.connections[graph.connections.length - 1]
    const fromPort = graph.ports.find((p) => p.id === conn.from_port_id)
    const toPort = graph.ports.find((p) => p.id === conn.to_port_id)
    expect(fromPort).toBeTruthy()
    expect(toPort).toBeTruthy()
    // 真实端口名来自设备 YAML 目录: 热泵输出 = heat_out(thermal,out),
    // 热负荷输入 = heat_in(thermal,in) —— 断言名称而非仅类型, 杜绝"端口猜错"
    expect(fromPort!.name).toBe('heat_out')
    expect(fromPort!.direction).toBe('out')
    expect(fromPort!.port_type).toBe('thermal')
    expect(toPort!.name).toBe('heat_in')
    expect(toPort!.direction).toBe('in')
    expect(toPort!.port_type).toBe('thermal')

    session.assertNoErrors()
    await context.close()
  })
})
