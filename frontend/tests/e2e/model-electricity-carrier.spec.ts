import { test, expect } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import { createEngineer } from './setup/api'
import { APP } from './financial-scaffold'

// 0.14 载能身份收口: 前端载能须与注册表 descriptor 同一身份(electricity,
// 非 electric), 否则电气连接在画布静默丢失、改名重建连接报 400。
// 本用例走真实 GUI: 拖设备 → 连 electricity 接口 → 改名已连接设备 →
// 重载, 断言真实端点与保存名一致, 不 mock API 正文。
test.describe('电力载能连接与改名', () => {
  test('拖电网/电负荷, 连 electricity 接口, 改名后重载端点与名称一致', async ({ browser }) => {
    const username = uniqueName('es-electric')
    const password = strongPassword('Init')
    const apiCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(apiCtx.request, username, password)
    await apiCtx.close()

    const context = await browser.newContext()
    const page = await context.newPage()
    const session = await createSession(page, username, password)

    // UI 建项目(起始年为服务端必填, 如实填写)。
    const projectName = uniqueName('电力载能项目')
    await page.getByRole('button', { name: /新建项目|New project/i }).first().click()
    await page.getByLabel(/名称|Name/i).fill(projectName)
    await page.locator('#pp-create-start-year').fill('2026')
    await page.getByRole('button', { name: /确认|Confirm/i }).click()
    await expect(page.getByText(projectName)).toBeVisible({ timeout: 30_000 })
    await page.getByText(projectName).click()
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()
    const projectId = page.url().match(/\/projects\/(\d+)/)![1]

    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('系统建模').click()
    await expect(page).toHaveURL(/\/model$/)

    const canvas = page.locator('.mp-canvas-wrap')
    const palette = page.locator('.mp-palette')
    await expect(palette).toBeVisible({ timeout: 30_000 })
    const addDevice = async (typeName: string, fx: number, fy: number) => {
      const item = palette.locator(`[aria-label*="${typeName}"]`).first()
      await item.scrollIntoViewIfNeeded()
      await expect(item).toBeVisible()
      const box = await item.boundingBox()
      const canvasBox = await canvas.boundingBox()
      expect(box && canvasBox).toBeTruthy()
      await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2)
      await page.mouse.down()
      await page.mouse.move(canvasBox!.x + canvasBox!.width * fx, canvasBox!.y + canvasBox!.height * fy, { steps: 8 })
      await page.mouse.up()
      await expect(page.locator('.react-flow__node', { hasText: typeName }).first()).toBeVisible({ timeout: 15_000 })
      await expect(page.locator('.mp-save-state--saved, .wb-autosave--saved').first()).toBeVisible({ timeout: 15_000 })
      await page.waitForTimeout(800)
    }
    await addDevice('电网连接', 0.15, 0.3)
    await addDevice('电负荷', 0.75, 0.3)

    // electricity 接口相连: 电网 electricity_import(out) → 电负荷 electricity_in(in)。
    // 旧 electric 身份下此步静默无事发生(句柄解析失败直接 return), 不得回退。
    const gridNode = page.locator('.react-flow__node', { hasText: '电网连接' }).first()
    const loadNode = page.locator('.react-flow__node', { hasText: '电负荷' }).first()

    // 接口语义验证: 电负荷声明了 electricity_in(in) 与 electricity_demand(predefined);
    // predefined 接口严禁作为连接句柄暴露, 电负荷节点上仅有单一 in 句柄, 零 out 句柄
    await expect(loadNode.locator('.mp-handle')).toHaveCount(1)
    await expect(loadNode.locator('.react-flow__handle-left.mp-handle--electricity')).toHaveCount(1)
    await expect(loadNode.locator('.react-flow__handle-right')).toHaveCount(0)

    // 电网连接声明了 electricity_import(out) 与 electricity_export(in), 两连接句柄均正常呈现
    await expect(gridNode.locator('.mp-handle')).toHaveCount(2)
    await expect(gridNode.locator('.react-flow__handle-left')).toHaveCount(1)
    await expect(gridNode.locator('.react-flow__handle-right')).toHaveCount(1)
    const gridOut = gridNode.locator('.mp-handle--electricity').last()
    const loadIn = loadNode.locator('.mp-handle--electricity').first()
    await expect(gridOut).toBeVisible({ timeout: 10_000 })
    await expect(loadIn).toBeVisible({ timeout: 10_000 })
    const connectSave = page.waitForResponse(
      (res) => res.request().method() === 'PUT' && new URL(res.url()).pathname === `/api/projects/${projectId}/draft` && res.status() === 200,
      { timeout: 30_000 },
    )
    const src = await gridOut.boundingBox()
    const dst = await loadIn.boundingBox()
    expect(src && dst).toBeTruthy()
    await page.mouse.move(src!.x + src!.width / 2, src!.y + src!.height / 2)
    await page.mouse.down()
    await page.mouse.move(dst!.x + dst!.width / 2, dst!.y + dst!.height / 2, { steps: 12 })
    await page.mouse.up()
    await connectSave
    await expect(page.locator('.react-flow__edge').first()).toBeVisible({ timeout: 10_000 })

    // 改名已连接的电负荷: 改名即换键, 受影响连接随批重建, 旧 electric 身份下报 400。
    const newLoadName = uniqueName('改名后负荷')
    await loadNode.click()
    const nameInput = page.locator('#mp-device-name')
    await expect(nameInput).toBeVisible({ timeout: 10_000 })
    const renameSave = page.waitForResponse(
      (res) => res.request().method() === 'PUT' && new URL(res.url()).pathname === `/api/projects/${projectId}/draft` && res.status() === 200,
      { timeout: 30_000 },
    )
    await nameInput.fill(newLoadName)
    await nameInput.press('Tab')
    await renameSave
    await expect(page.locator('.react-flow__node', { hasText: newLoadName }).first()).toBeVisible({ timeout: 15_000 })

    // 服务端草稿即真实端点(设备名 + YAML 真实端口名), 非类型猜测。
    const viewResp = await page.request.get(`/api/projects/${projectId}`)
    expect(viewResp.ok()).toBeTruthy()
    const view = (await viewResp.json()) as {
      draft: { content: { model: {
        devices: Array<{ name: string }>
        connections: Array<{ name: string; from_device: string; from_port: string; to_device: string; to_port: string }>
      } } }
    }
    const conns = view.draft.content.model.connections
    expect(conns.length).toBe(1)
    expect(conns[0].from_port).toBe('electricity_import')
    expect(conns[0].to_port).toBe('electricity_in')
    expect(conns[0].to_device).toBe(newLoadName)
    const names = view.draft.content.model.devices.map((d) => d.name)
    expect(names).toContain(newLoadName)

    // 重载: 同一连接与保存名必须从服务端读回, 非本地残留。
    await page.reload()
    await expect(page.locator('.react-flow__node', { hasText: newLoadName }).first()).toBeVisible({ timeout: 30_000 })
    await expect(page.locator('.react-flow__edge').first()).toBeVisible({ timeout: 15_000 })
    const viewResp2 = await page.request.get(`/api/projects/${projectId}`)
    expect(viewResp2.ok()).toBeTruthy()
    const view2 = (await viewResp2.json()) as typeof view
    expect(view2.draft.content.model.connections.length).toBe(1)
    expect(view2.draft.content.model.connections[0].from_port).toBe('electricity_import')
    expect(view2.draft.content.model.connections[0].to_port).toBe('electricity_in')
    expect(view2.draft.content.model.connections[0].to_device).toBe(newLoadName)

    session.assertNoErrors([
      // 进项目读尚无保存的计算配置 404(既有行为, 计数锁定防漂移)。
      { method: 'GET', path: `/api/projects/${projectId}/config`, status: 404, times: 1 },
    ])
    await context.close()
  })
})
