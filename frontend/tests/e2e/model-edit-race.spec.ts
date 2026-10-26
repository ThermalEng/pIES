// Account creation alone uses the public setup API. Every tested graph edit uses UI.
import { test, expect, type Page, type Locator } from '@playwright/test'
import { createEngineer } from './setup/api'
import { createSession, uniqueName, strongPassword } from './fixtures'

async function dragTo(page: Page, source: Locator, target: Locator, fx: number, fy: number) {
  await source.scrollIntoViewIfNeeded()
  const from = await source.boundingBox(), to = await target.boundingBox()
  expect(from && to).toBeTruthy()
  await page.mouse.move(from!.x + from!.width / 2, from!.y + from!.height / 2)
  await page.mouse.down()
  await page.mouse.move(to!.x + to!.width * fx, to!.y + to!.height * fy, { steps: 12 })
  await page.mouse.up()
}

// The response is real; only its delivery is gated. No sleeps or forged graph state.
async function holdSave(page: Page, path: string, commandType?: string) {
  let release!: () => void, received!: () => void
  const gate = new Promise<void>(r => { release = r })
  const ready = new Promise<void>(r => { received = r })
  await page.route(`**${path}`, async route => {
    if (route.request().method() !== 'PUT') return route.continue()
    const response = await route.fetch()
    if (commandType && !route.request().postDataJSON().commands.some((c: any) => c.type === commandType)) {
      await route.fulfill({ response })
      return
    }
    received()
    await gate
    await route.fulfill({ response })
  })
  return { ready, release, clear: () => page.unroute(`**${path}`) }
}

test('parameter and rename save responses preserve the next input; graph survives re-entry', async ({ browser }) => {
  const username = uniqueName('es-ui-canvas'), password = strongPassword('Init')
  const setup = await browser.newContext()
  await createEngineer(setup.request, username, password)
  await setup.close()
  const context = await browser.newContext()
  const page = await context.newPage()
  const session = await createSession(page, username, password)
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  const name = uniqueName('UI canvas')
  await page.getByLabel(/名称/).fill(name)
  await page.getByLabel(/运行起始年/).fill('2026')
  await page.getByRole('button', { name: '确认', exact: true }).click()
  await page.getByText(name, { exact: true }).click()
  const pid = page.url().match(/projects\/(\d+)/)![1]
  const nav = page.getByRole('navigation', { name: '工作台' })
  await nav.getByText('系统建模', { exact: true }).click()
  const canvas = page.locator('.mp-canvas-wrap')
  const saved = () => expect(page.getByText('已保存', { exact: true })).toBeVisible()
  await dragTo(page, page.getByLabel('放置 电池储能 设备'), canvas, .25, .35)
  const battery = page.getByRole('button', { name: '电池储能: 电池储能 1', exact: true })
  await expect(battery).toBeVisible()
  await saved()
  const capacity = page.getByRole('spinbutton', { name: 'capacity_kwh (kWh)', exact: true })
  const power = page.getByRole('spinbutton', { name: 'rated_power_kw (kW)', exact: true })
  let gate = await holdSave(page, `/api/projects/${pid}/draft`)
  try {
    await capacity.fill('1200')
    await power.fill('650') // blur capacity, then start editing the next field
    await gate.ready
    await expect(page.getByRole('button', { name: '刷新', exact: true })).toBeDisabled()
    gate.release()
    await saved()
    await expect(power).toHaveValue('650')

  } finally { gate.release(); await gate.clear() }
  await page.getByLabel('设备名称', { exact: true }).click()
  await saved()
  await expect(capacity).toHaveValue('1200')
  // Now submit both edits before the first response: each field must survive.
  gate = await holdSave(page, `/api/projects/${pid}/draft`)
  try {
    await capacity.fill('1250')
    await power.fill('675')
    await gate.ready
    await page.getByLabel('设备名称', { exact: true }).click()
    gate.release()
    await expect.poll(async () => {
      const response = await page.request.get(`/api/projects/${pid}`)
      const devices = (await response.json()).draft.content.model.devices
      return devices.find((d: any) => d.name === '电池储能 1')?.params
    }).toMatchObject({ capacity_kwh: 1250, rated_power_kw: 675 })
    await saved()
  } finally { gate.release(); await gate.clear() }
  // Reverting the same field before its first response must also persist.
  gate = await holdSave(page, `/api/projects/${pid}/draft`)
  try {
    await capacity.fill('1400')
    await page.getByLabel('设备名称', { exact: true }).click()
    await gate.ready
    await capacity.fill('1250')
    await page.getByLabel('设备名称', { exact: true }).click()
    gate.release()
    await expect.poll(async () => {
      const response = await page.request.get(`/api/projects/${pid}`)
      const devices = (await response.json()).draft.content.model.devices
      return devices.find((d: any) => d.name === '电池储能 1')?.params.capacity_kwh
    }).toBe(1250)
    await saved()
    await expect(capacity).toHaveValue('1250')
  } finally { gate.release(); await gate.clear() }
  // Start from the actual catalog default: the old switchKind only overwrote defaults.
  const defaultSaved = page.waitForResponse(r => r.request().method() === 'PUT' &&
    new URL(r.url()).pathname === `/api/projects/${pid}/draft`)
  await capacity.fill('1000')
  await page.getByLabel('设备名称', { exact: true }).click()
  expect((await defaultSaved).ok()).toBe(true)
  await saved()
  // Switching kind while the edited parameter is saving keeps the explicit value.
  gate = await holdSave(page, `/api/projects/${pid}/draft`)
  try {
    await capacity.fill('1300')
    await page.getByRole('radio', { name: '存量设备', exact: true }).click()
    await gate.ready
    await expect(capacity).toHaveValue('1300')
    gate.release()
    await expect.poll(async () => {
      const response = await page.request.get(`/api/projects/${pid}`)
      const devices = (await response.json()).draft.content.model.devices
      const device = devices.find((d: any) => d.name === '电池储能 1')
      return { kind: device.kind, capacity: device.params.capacity_kwh }
    }).toEqual({ kind: 'existing', capacity: 1300 })
    await saved()
  } finally { gate.release(); await gate.clear() }
  // Rename changes the server name-key, but must not replace an ongoing edit.
  gate = await holdSave(page, `/api/projects/${pid}/draft`, 'model.rename_device')
  try {
    await page.getByLabel('设备名称', { exact: true }).fill('Storage edited')
    await power.fill('700')
    await gate.ready
    await capacity.click()
    gate.release()
    await saved()
    await expect(power).toHaveValue('700')
    await expect.poll(async () => {
      const response = await page.request.get(`/api/projects/${pid}`)
      const model = (await response.json()).draft.content.model
      return model.devices.find((d: any) => d.name === 'Storage edited')?.params.rated_power_kw
    }).toBe(700)
    await expect(page.getByLabel('设备名称', { exact: true })).toHaveValue('Storage edited')
  } finally { gate.release(); await gate.clear() }
  // A rename can be reverted while its response is pending, just like a parameter.
  gate = await holdSave(page, `/api/projects/${pid}/draft`, 'model.rename_device')
  try {
    await page.getByLabel('设备名称', { exact: true }).fill('Temporary name')
    await capacity.click()
    await gate.ready
    await page.getByLabel('设备名称', { exact: true }).fill('Storage edited')
    await capacity.click()
    gate.release()
    await expect.poll(async () => {
      const response = await page.request.get(`/api/projects/${pid}`)
      return (await response.json()).draft.content.model.devices.map((d: any) => d.name)
    }).toEqual(['Storage edited'])
    await saved()
    await expect(page.getByLabel('设备名称', { exact: true })).toHaveValue('Storage edited')
  } finally { gate.release(); await gate.clear() }
  await capacity.click()
  await saved()
  await page.getByRole('region', { name: '编辑设备' }).getByRole('button', { name: '关闭', exact: true }).click()
  await dragTo(page, page.getByLabel('放置 电负荷 设备'), canvas, .75, .65)
  await saved()
  // Use actual endpoint labels and current geometry, not screen coordinates.
  const renamed = page.getByRole('button', { name: '电池储能: Storage edited', exact: true })
  const load = page.getByRole('button', { name: '电负荷: 电负荷 1', exact: true })
  await (await renamed.elementHandle())!.waitForElementState('stable')
  await (await load.elementHandle())!.waitForElementState('stable')
  await dragTo(page, renamed.getByLabel('电气端口 输出', { exact: true }), load.getByLabel('电气端口 输入', { exact: true }), .5, .5)
  await expect(page.locator('.react-flow__edge')).toHaveCount(1)
  await saved()
  const read = async () => {
    const response = await page.request.get(`/api/projects/${pid}`)
    expect(response.ok()).toBe(true)
    return (await response.json()).draft.content.model
  }
  const model = await read()
  expect(model.devices.find((d: any) => d.name === 'Storage edited').params).toMatchObject({ capacity_kwh: 1300, rated_power_kw: 700 })
  expect(model.connections).toHaveLength(1)
  expect(model.connections[0]).toMatchObject({ from_device: 'Storage edited', from_port: 'electricity', to_device: '电负荷 1', to_port: 'electricity_in' })
  await page.getByRole('button', { name: '刷新', exact: true }).click()
  await expect(renamed).toBeVisible()
  await page.reload()
  await expect(renamed).toBeVisible()
  await expect(page.locator('.react-flow__edge')).toHaveCount(1)
  await nav.getByText('任务中心', { exact: true }).click()
  await nav.getByText('系统建模', { exact: true }).click()
  await renamed.click()
  await expect(capacity).toHaveValue('1300')
  await expect(power).toHaveValue('700')
  expect(await read()).toEqual(model)
  // The normalized unique name must be reflected in the sidebar and connections.
  const renameSaved = page.waitForResponse(r => r.request().method() === 'PUT' &&
    new URL(r.url()).pathname === `/api/projects/${pid}/draft`)
  await page.getByLabel('设备名称', { exact: true }).fill('电负荷 1')
  await capacity.click()
  expect((await renameSaved).ok()).toBe(true)
  await expect(page.getByLabel('设备名称', { exact: true })).toHaveValue('电负荷 1 2')
  await expect(page.getByRole('button', { name: '电池储能: 电负荷 1 2', exact: true })).toBeVisible()
  const normalized = await read()
  expect(normalized.connections[0].from_device).toBe('电负荷 1 2')
  expect(normalized.devices.find((d: any) => d.name === '电负荷 1 2').params)
    .toMatchObject({ capacity_kwh: 1300, rated_power_kw: 700 })

  session.assertNoErrors([{ method: 'GET', path: `/api/projects/${pid}/config`, status: 404 }])
  await context.close()
})
