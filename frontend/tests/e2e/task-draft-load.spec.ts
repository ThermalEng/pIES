// 任务选择载入工作草稿：选择即把冻结输入载入本项目可编辑草稿。
//
// 裁决语义（用户已定；见 load-draft 端点与 workbench selectTaskWithDraft）：
// - 项目的工作区就是本项目草稿；显式选择任务（任务页行点击/结果页证据包选择）
//   即载入其冻结输入；初始化/重挂载/程序跟随不触发载入；
// - 覆盖全部分类：模型/装配内容、数据集版本绑定、规划与财务指针、计算配置、
//   评价与任务参数工作输入（后两者经提交对话框承载，不另造编辑器）；
// - 对话框是草稿工作输入的可编辑镜像：编辑边改边存入草稿，导航/重载不丢失；
// - 对话框不再做数据集选择（清单权威是草稿绑定）；
// - 载入只追加草稿 revision（与配置 revision），不创建任务与项目版本；
// - 失败可见且选择不变；提交冻结当时草稿工作区值为新任务。
// 被测选择/提交走 GUI；脚手架、 divergence 编辑与断言读数走公开 API。
// 单会话语义下 API 登录会顶掉 GUI 会话，因此按 API 块/GUI 块交替推进，
// 每个 GUI 块重新 UI 登录（同一 context，本地选择持久化保留）。
// 同一 GUI 会话内的只读轮询走 page.request（不新建登录，不触发接管）。
// 求解等待 T1/T2（结果切换与深链需真实完成）；T3 等到完成以固定默认最新；
// T4 只提交不等待。手动重试不在此覆盖：PG 终态触发器 tg_tasks_terminal
// 禁止终态→queued，重试终态任务 500，属任务域契约冲突（规格 6.4 vs 01 §7.2），
// 已报缺口，不私自改触发器/状态机；早创建晚完成的翻转由后端 12 任务回归覆盖。
// 不测画布拖放。

import { test, expect } from '@playwright/test'
import type { Page, BrowserContext, APIRequestContext } from '@playwright/test'
import { createSession, uniqueName, strongPassword } from './fixtures'
import type { SessionContext } from './fixtures'
import { createEngineer, loginToken, requestJson } from './setup/api'
import { APP, scaffoldProject } from './financial-scaffold'

type Json = Record<string, any>

async function api(apiCtx: APIRequestContext, method: string, url: string, body: unknown, token: string): Promise<any> {
  return requestJson(apiCtx, method, url, body, token)
}

/** 记录任务创建 POST（POST /projects/{id}/tasks，排除 /cancel 与 GET）。 */
function watchTaskCreates(page: Page): string[] {
  const urls: string[] = []
  page.on('request', (req) => {
    if (req.method() !== 'POST') return
    if (/\/projects\/\d+\/tasks$/.test(new URL(req.url()).pathname)) urls.push(req.url())
  })
  return urls
}

/** 任务列表卡片内的任务行（含表头行）。 */
function taskRows(page: Page) {
  return page
    .locator('section.ies-card')
    .filter({ has: page.getByRole('heading', { name: '任务列表' }) })
    .locator('.ies-table__row')
}

async function clickTaskRow(page: Page, taskId: number): Promise<void> {
  await taskRows(page)
    .filter({ hasText: new RegExp(`#${taskId}(?!\\d)`) })
    .first()
    .click({ timeout: 30_000 })
}

/** 结果页按任务 id 选择证据包（选项文本 `#{包} · {任务} · {状态}`），返回所选包 id。 */
async function selectEvidenceForTask(page: Page, tid: number): Promise<string> {
  // 用 has 过滤直接定位证据包选择器（自动等待其出现）。
  const sel = page
    .locator('select', { has: page.locator('option', { hasText: `· ${tid} ·` }) })
    .first()
  await expect(sel).toBeVisible({ timeout: 60_000 })
  const texts = await sel.locator('option').allTextContents()
  const values = await sel.locator('option').evaluateAll((els) =>
    els.map((el) => (el as HTMLOptionElement).value),
  )
  const idx = texts.findIndex((text) => text.includes(`· ${tid} ·`))
  if (idx < 0) throw new Error(`任务 ${tid} 无证据包选项`)
  await sel.selectOption(values[idx])
  // 等载入落定（选择器恢复可用）后再返回。
  await expect(sel).toBeEnabled({ timeout: 60_000 })
  return values[idx]
}

/** 从任务列表 UI 解析任务 id（表头行无 #id，不会被误中）。 */
async function taskIdsFromUI(page: Page): Promise<number[]> {
  const texts = await taskRows(page).allTextContents()
  const ids: number[] = []
  for (const text of texts) {
    const m = /#(\d+)(?!\d)/.exec(text)
    if (m) ids.push(Number(m[1]))
  }
  return ids.sort((a, b) => a - b)
}

async function projectView(apiCtx: APIRequestContext, pid: number, token: string): Promise<Json> {
  return api(apiCtx, 'GET', `/api/projects/${pid}`, undefined, token)
}

async function draftRevision(apiCtx: APIRequestContext, pid: number, token: string): Promise<number> {
  return Number((await projectView(apiCtx, pid, token)).draft.revision)
}

// GUI 会话直读项目草稿（不接管会话，供选择/导航后立即断言服务端值）。
async function guiDraft(page: Page, pid: number): Promise<{ revision: number; content: Json; working: Json }> {
  const body = (await (await page.request.get(`/api/projects/${pid}`)).json()) as Json
  const draft = (body?.draft ?? {}) as Json
  return {
    revision: Number(draft?.revision),
    content: (draft?.content ?? {}) as Json,
    working: (draft?.content?.task_working_inputs ?? {}) as Json,
  }
}

async function taskIds(apiCtx: APIRequestContext, pid: number, token: string): Promise<number[]> {
  const list = await api(apiCtx, 'GET', `/api/projects/${pid}/tasks`, undefined, token)
  return (list.items as Json[]).map((t) => Number(t.id)).sort((a, b) => a - b)
}

async function versionCount(apiCtx: APIRequestContext, pid: number, token: string): Promise<number> {
  const list = await api(apiCtx, 'GET', `/api/projects/${pid}/versions`, undefined, token)
  return (list.versions as Json[]).length
}

async function taskInputs(apiCtx: APIRequestContext, pid: number, tid: number, token: string): Promise<Json> {
  const res = await api(apiCtx, 'GET', `/api/projects/${pid}/tasks/${tid}/inputs`, undefined, token)
  return res.inputs
}

async function putDraft(apiCtx: APIRequestContext, pid: number, token: string, commands: Json[]): Promise<number> {
  const view = await projectView(apiCtx, pid, token)
  const res = await api(apiCtx, 'PUT', `/api/projects/${pid}/draft`, {
    expected_revision: Number(view.draft.revision),
    commands,
  }, token)
  return Number(res.revision ?? (await projectView(apiCtx, pid, token)).draft.revision)
}

/**
 * 将页面停到空白页后再做 API 登录（单会话语义顶掉 GUI 会话）。
 * about:blank 卸载应用代码并中止在途轮询：导航取消已发出的 fetch，
 * 中止的请求只记 ERR_ABORTED（监控已过滤），不产生 401 回显；空白页
 * 自身零请求，随后的 API 接管无页面请求可被失效。停 /login 不可靠：
 * 会话有效时会跳转项目列表，列表轮询必在接管后刷出 401 噪声。
 * 本块后若还有 GUI 动作，必须经 guiLoginTasks（其先回 /login 再碰
 * localStorage，about:blank 下直接碰存储会抛 opaque-origin 异常）。
 */
async function parkForApi(page: Page): Promise<void> {
  await page.goto('about:blank')
  await expect(page).not.toHaveURL(/\/projects\/\d+/, { timeout: 30_000 })
}

/** UI 登录并进入指定项目的任务中心（同一 page 可重复调用）。 */
async function guiLoginTasks(
  page: Page,
  projectName: string,
  username: string,
  password: string,
  monitor: SessionContext,
): Promise<void> {
  // 先清失效会话标记：前一 API 登录已顶掉 GUI 会话；残留 cookie/localStorage
  // 会话标记会使路由守卫在 / 与 /login 间循环并刷出 401 噪声。
  // 任务选择键（iesplan.tasks.selected.*）保留，不受影响。
  // 停放页可能是 about:blank（其源上直接碰 localStorage 会抛异常），
  // 先回应用源再清理存储。回源期间残留标记会使路由守卫短暂进列表并轮询，
  // 而 cookie 已清、会话已接管，轮询必 401；回源期的轮询在路由回调内
  // 按实际请求逐条登记再中止（method + 路径精确登记，一条只核销一条；
  // 登记期内的其他 URL/方法失败仍判失败）。监听挂在首个会话对象上
  // （同页监听不移除，累计全程；只在最终断言消费；新会话对象的监听
  // 仅追加，不影响登记）。
  await page.context().clearCookies()
  await page.route('**/api/**', (route) => {
    monitor.registerExpectedAbort(route.request().method(), route.request().url())
    route.abort()
  })
  try {
    await page.goto('/login')
    await page.evaluate(() => window.localStorage.removeItem('iesplan.session'))
  } finally {
    await page.unroute('**/api/**')
  }
  await createSession(page, username, password)
  await page.getByText(projectName).click({ timeout: 30_000 })
  // 重登录可能恢复上次停留的子页（/tasks 后缀），仅断言项目作用域。
  await expect(page).toHaveURL(/\/projects\/\d+/)
  await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
  await expect(taskRows(page).first()).toBeVisible({ timeout: 60_000 })
}

function hpCop(draftContent: Json): unknown {
  const devices = draftContent?.model?.devices as Json[] | undefined
  return devices?.find((d) => d.name === 'hp1')?.params?.cop
}

function bindingVids(draftContent: Json): number[] {
  const bindings = (draftContent?.dataset_bindings as Json[] | undefined) ?? []
  return bindings.map((b) => Number(b.dataset_version_id))
}

test.describe('任务选择载入草稿：切换恢复全部分类、编辑保持、提交冻结、无杂散写入', () => {
  test.setTimeout(1_800_000)

  test('双任务输入差异下切换/编辑/提交全链路', async ({ browser }) => {
    const projectName = uniqueName('草稿载入项目')
    const tag = `draft-${Date.now()}`
    const soloUser = uniqueName('es-draft')
    const soloPass = strongPassword('Init')
    const soloCtx = await browser.newContext({ baseURL: APP })
    await createEngineer(soloCtx.request, soloUser, soloPass)
    const req = soloCtx.request

    // ---- API 块 A：脚手架 + 基线读数（GUI 尚未登录，会话独占无冲突）。
    let token = await loginToken(req, soloUser, soloPass)
    const pid = await scaffoldProject(requestJson, req, token, projectName, tag)
    const dsList = await api(req, 'GET', `/api/projects/${pid}/datasets`, undefined, token)
    const ds1 = (dsList.datasets as Json[]).find((d) => d.dataset?.name === 'e2e-sample')
    const ds1id = Number(ds1.dataset.id)
    const ds1detail = await api(req, 'GET', `/api/projects/${pid}/datasets/${ds1id}`, undefined, token)
    const v1 = Number(ds1detail.versions[0].id)
    const cfg1 = await api(req, 'GET', `/api/projects/${pid}/config`, undefined, token)
    expect(cfg1.calculation_config.seed).toBeNull()
    const plan1 = await api(req, 'GET', `/api/projects/${pid}/planning-config`, undefined, token)
    const bound1 = plan1.planning_config.variables.hp_cap.upper_bound
    const baseView = await projectView(req, pid, token)
    expect(hpCop(baseView.draft.content)).toBe(3.5)

    const context: BrowserContext = await browser.newContext()
    const page: Page = await context.newPage()
    const session = await createSession(page, soloUser, soloPass)
    const creates = watchTaskCreates(page)

    // ---- GUI 块 1：T1 提交（评价 K1N2；数据集清单来自脚手架绑定 v1，对话框无选择）。
    await page.getByText(projectName).click({ timeout: 30_000 })
    await expect(page).toHaveURL(/\/projects\/\d+\/tasks$/)
    await expect(page.getByRole('button', { name: /提交任务|Submit task/i }).first()).toBeVisible()
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await page.locator('#task-type').selectOption('optimization')
    await page.locator('#task-construction-period').fill('1')
    await page.locator('#task-operation-years').fill('2')
    await page.locator('#task-discount-rate').fill('0.08')
    await page.locator('#task-construction-payments').fill('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
    await page.locator('#task-maintenance-expenses').fill('[]')
    // 对话框不再做数据集选择（旧勾选后端从未消费，已删除）。
    await expect(page.getByRole('checkbox', { name: 'e2e-sample' })).toHaveCount(0)
    // 关闭对话框（脏输入落草稿），等防抖保存落定：草稿工作输入即刚才的编辑。
    await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 10_000 })
    await expect
      .poll(
        async () =>
          (await (await page.request.get(`/api/projects/${pid}`)).json() as Json)
            .draft?.content?.task_working_inputs?.operation_years ?? null,
        { timeout: 30_000 },
      )
      .toBe('2')
    // 离开再回来 + 重载：工作输入不丢失（草稿权威，非页面本地态）。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.reload()
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 30_000 })
    await expect
      .poll(async () => page.locator('#task-operation-years').inputValue(), { timeout: 30_000 })
      .toBe('2')
    await expect
      .poll(async () => page.locator('#task-construction-payments').inputValue(), { timeout: 30_000 })
      .toBe('[{"t": 0, "ratio": 0.6}, {"t": 1, "ratio": 0.4}]')
    await page.getByRole('dialog').getByRole('button', { name: /提交任务|Submit task/i }).click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 60_000 })
    expect(creates.length).toBe(1)
    let uiIds1: number[] = []
    await expect
      .poll(async () => {
        uiIds1 = await taskIdsFromUI(page)
        return uiIds1.length
      }, { timeout: 60_000 })
      .toBe(1)
    const [t1] = uiIds1

    // ---- API 块 B：工作区分歧（模型 cop→4.0、新数据集绑定、配置 seed→7、规划 bound→800）。
    await parkForApi(page)
    token = await loginToken(req, soloUser, soloPass)
    await putDraft(req, pid, token, [{
      id: 'e2e-edit-cop', unit: 'model', type: 'model.upsert_device',
      payload: { name: 'hp1', params: { rated_heat_kw: 600, cop: 4.0 }, position: { x: 1.0, y: 1.0 } },
    }])
    const ds2 = await api(req, 'POST', `/api/projects/${pid}/datasets`, { name: 'e2e-second' }, token)
    const ds2id = Number(ds2.dataset?.id ?? ds2.id)
    const sampled2 = await api(req, 'POST', `/api/projects/${pid}/datasets/${ds2id}/sample?resolution=1h`, {}, token)
    const v2 = Number(sampled2.dataset_version?.id)
    await putDraft(req, pid, token, [{
      id: 'e2e-bind-second', unit: 'dataset', type: 'dataset.bind',
      payload: { dataset_version_id: v2, dataset_id: ds2id, role: 'annual' },
    }])
    await putDraft(req, pid, token, [{
      id: 'e2e-unbind-first', unit: 'dataset', type: 'dataset.unbind',
      payload: { dataset_version_id: v1 },
    }])
    const cfgLive = await api(req, 'GET', `/api/projects/${pid}/config`, undefined, token)
    await api(req, 'PUT', `/api/projects/${pid}/config`, {
      calculation_config: { ...cfgLive.calculation_config, seed: 7 },
      expected_revision: Number(cfgLive.revision),
    }, token)
    const planLive = await api(req, 'GET', `/api/projects/${pid}/planning-config`, undefined, token)
    await api(req, 'PUT', `/api/projects/${pid}/planning-config`, {
      planning_config: {
        ...planLive.planning_config,
        variables: {
          ...planLive.planning_config.variables,
          hp_cap: { ...planLive.planning_config.variables.hp_cap, upper_bound: '800' },
        },
      },
      expected_revision: Number(planLive.revision),
    }, token)
    const revDiverged = await draftRevision(req, pid, token)

    // ---- GUI 块 2：T2 提交（评价 K1N3，选择跟随 T2）。
    await guiLoginTasks(page, projectName, soloUser, soloPass, session)
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await page.locator('#task-operation-years').fill('3')
    await page.getByRole('dialog').getByRole('button', { name: /提交任务|Submit task/i }).click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 60_000 })
    expect(creates.length).toBe(2)
    let uiIds2: number[] = []
    await expect
      .poll(async () => {
        uiIds2 = await taskIdsFromUI(page)
        return uiIds2.length
      }, { timeout: 60_000 })
      .toBe(2)
    const [, t2] = uiIds2

    // ---- API 块 C：T2 快照断言 + T1/T2 快照全文基线（为后续“快照不变”留证，
    // 不只比快照 id：评价确认块与任务参数逐值比对）。
    await parkForApi(page)
    token = await loginToken(req, soloUser, soloPass)
    const versionsAfterT2 = await versionCount(req, pid, token)
    const t2inputs = await taskInputs(req, pid, t2, token)
    expect(t2inputs.snapshot.evaluation.operation_years).toBe(3)
    expect(t2inputs.snapshot.dataset_version_ids.map(Number)).toEqual([v2])
    // T2 对话框承接 T1 落草稿的工作输入（仅改 N=3）：其余评价逐值一致
    // （付款比例冻结为定点字符串，见任务契约）。
    expect(t2inputs.snapshot.evaluation.construction_period).toBe(1)
    expect(t2inputs.snapshot.evaluation.discount_rate).toBe(0.08)
    expect(t2inputs.snapshot.evaluation.construction_payments).toEqual([
      { t: 0, ratio: '0.6' },
      { t: 1, ratio: '0.4' },
    ])
    expect(t2inputs.snapshot.evaluation.maintenance_expenses).toEqual([])
    const t1json = JSON.stringify((await taskInputs(req, pid, t1, token)).snapshot)
    const t2json = JSON.stringify(t2inputs.snapshot)

    // ---- GUI 块 3：点选 T1 行 → 载入（flash 为载入完成的唯一 UI 信号）。
    await guiLoginTasks(page, projectName, soloUser, soloPass, session)
    await clickTaskRow(page, t1)
    await expect(page.getByRole('status').filter({ hasText: /载入工作草稿/ }).last()).toContainText(new RegExp(`#${t1}.*载入工作草稿`), {
      timeout: 60_000,
    })

    // ---- API 块 D：T1 全部分类恢复；无任务/版本新增；快照不变。
    await parkForApi(page)
    token = await loginToken(req, soloUser, soloPass)
    const afterT1 = await projectView(req, pid, token)
    // +2 = T2 对话框编辑 N=3 的工作输入落草稿(+1) + 切 T1 载入(+1)；均为草稿修订，
    // 无任务/版本新增（下断言）。
    expect(Number(afterT1.draft.revision)).toBe(revDiverged + 2)
    expect(hpCop(afterT1.draft.content)).toBe(3.5)
    expect(bindingVids(afterT1.draft.content)).toEqual([v1])
    const cfgRestored = await api(req, 'GET', `/api/projects/${pid}/config`, undefined, token)
    expect(cfgRestored.calculation_config.seed).toBeNull()
    const planRestored = await api(req, 'GET', `/api/projects/${pid}/planning-config`, undefined, token)
    expect(planRestored.planning_config.variables.hp_cap.upper_bound).toBe(bound1)
    expect(await taskIds(req, pid, token)).toEqual([t1, t2])
    expect(await versionCount(req, pid, token)).toBe(versionsAfterT2)
    // T1 快照全文不变（评价确认块与任务参数逐值比对，不只比快照 id）。
    expect(JSON.stringify((await taskInputs(req, pid, t1, token)).snapshot)).toBe(t1json)
    // 草稿工作输入整体替换为 T1 冻结业务值原样（snake_case 业务键，非文本）。
    expect(afterT1.draft.content.task_working_inputs).toEqual({
      task_type: 'optimization',
      construction_period: 1,
      operation_years: 2,
      discount_rate: 0.08,
      construction_payments: [
        { t: 0, ratio: '0.6' },
        { t: 1, ratio: '0.4' },
      ],
      maintenance_expenses: [],
    })
    expect(creates.length).toBe(2)
    const revLoaded = Number(afterT1.draft.revision)

    // ---- GUI 块 4：重进任务中心（挂载/持久化恢复，不得重放载入）。
    await guiLoginTasks(page, projectName, soloUser, soloPass, session)

    // ---- API 块 E：revision 未变；随后保存编辑仅改草稿（绑定 v2），版本不动。
    await parkForApi(page)
    token = await loginToken(req, soloUser, soloPass)
    expect(await draftRevision(req, pid, token)).toBe(revLoaded)
    await putDraft(req, pid, token, [{
      id: 'e2e-edit-postswitch', unit: 'dataset', type: 'dataset.bind',
      payload: { dataset_version_id: v2, dataset_id: ds2id, role: 'annual' },
    }])
    expect(await draftRevision(req, pid, token)).toBe(revLoaded + 1)
    expect(bindingVids((await projectView(req, pid, token)).draft.content)).toEqual([v1, v2])
    expect(await versionCount(req, pid, token)).toBe(versionsAfterT2)
    const versionsBeforeT3 = versionsAfterT2

    // ---- GUI 块 5：显式提交 T3（评价 N4）：恰好一个新任务，冻结当时草稿为新版本。
    await guiLoginTasks(page, projectName, soloUser, soloPass, session)
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await page.locator('#task-operation-years').fill('4')
    await page.getByRole('dialog').getByRole('button', { name: /提交任务|Submit task/i }).click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 60_000 })
    expect(creates.length).toBe(3)
    let uiIds3: number[] = []
    await expect
      .poll(async () => {
        uiIds3 = await taskIdsFromUI(page)
        return uiIds3.length
      }, { timeout: 60_000 })
      .toBe(3)
    const t3 = uiIds3.find((id) => id !== t1 && id !== t2) as number

    // ---- API 块 F：T3 快照 = 当时草稿（编辑后绑定 + 载入来源配置 + 对话框评价）。
    await parkForApi(page)
    token = await loginToken(req, soloUser, soloPass)
    expect(await taskIds(req, pid, token)).toEqual([t1, t2, t3])
    expect(await versionCount(req, pid, token)).toBe(versionsBeforeT3 + 1)
    const t3inputs = await taskInputs(req, pid, t3, token)
    // T3 冻结当时草稿工作区值（T1 载入值 + N=4 编辑 + 编辑后绑定 [v1, v2]）。
    expect(t3inputs.snapshot.evaluation).toMatchObject({
      evaluation_years: 5,
      construction_period: 1,
      operation_years: 4,
      discount_rate: 0.08,
      construction_payments: [
        { t: 0, ratio: '0.6' },
        { t: 1, ratio: '0.4' },
      ],
      maintenance_expenses: [],
    })
    expect(typeof t3inputs.snapshot.evaluation.confirmed_by).toBe('number')
    expect(typeof t3inputs.snapshot.evaluation.confirmed_at).toBe('string')
    expect(t3inputs.snapshot.dataset_version_ids.map(Number)).toEqual([v1, v2])
    expect(t3inputs.snapshot.calc_config_snapshot.seed).toBeNull()
    expect(Number(t3inputs.snapshot.calc_config_snapshot.config_ref.revision)).toBe(
      Number(cfgRestored.revision),
    )
    // T1/T2 快照全文仍不变（提交新任务不碰历史快照）。
    expect(JSON.stringify((await taskInputs(req, pid, t1, token)).snapshot)).toBe(t1json)
    expect(JSON.stringify((await taskInputs(req, pid, t2, token)).snapshot)).toBe(t2json)

    // ---- GUI 块 6：点选 T2 行 → 草稿替换为 T2 全套输入。
    await guiLoginTasks(page, projectName, soloUser, soloPass, session)
    await clickTaskRow(page, t2)
    await expect(page.getByRole('status').filter({ hasText: /载入工作草稿/ }).last()).toContainText(new RegExp(`#${t2}.*载入工作草稿`), {
      timeout: 60_000,
    })

    // ---- API 块 G：T2 全部分类断言。
    await parkForApi(page)
    token = await loginToken(req, soloUser, soloPass)
    const afterT2 = await projectView(req, pid, token)
    expect(hpCop(afterT2.draft.content)).toBe(4.0)
    expect(bindingVids(afterT2.draft.content)).toEqual([v2])
    const cfgT2 = await api(req, 'GET', `/api/projects/${pid}/config`, undefined, token)
    expect(cfgT2.calculation_config.seed).toBe(7)
    const planT2 = await api(req, 'GET', `/api/projects/${pid}/planning-config`, undefined, token)
    expect(planT2.planning_config.variables.hp_cap.upper_bound).toBe('800')
    // 草稿工作输入整体替换为 T2 冻结业务值原样（K=1/E=4/N=3）。
    expect(afterT2.draft.content.task_working_inputs).toEqual({
      task_type: 'optimization',
      construction_period: 1,
      operation_years: 3,
      discount_rate: 0.08,
      construction_payments: [
        { t: 0, ratio: '0.6' },
        { t: 1, ratio: '0.4' },
      ],
      maintenance_expenses: [],
    })
    expect(await taskIds(req, pid, token)).toEqual([t1, t2, t3])
    expect(creates.length).toBe(3)
    const revAfterT2Load = Number(afterT2.draft.revision)

    // ---- GUI 块 7：等待 T1/T2 完成，经结果页证据包选择器切换（同一载入边界）。
    // 选项文本形如 #{包} · {任务} · {状态}，据所属任务 id 精确选择。
    await guiLoginTasks(page, projectName, soloUser, soloPass, session)
    for (const tid of [t1, t2]) {
      await expect
        .poll(
          async () =>
            (await taskRows(page)
              .filter({ hasText: new RegExp(`#${tid}(?!\\d)`) })
              .first()
              .textContent()) ?? '',
          { timeout: 600_000, intervals: [10_000] },
        )
        .toMatch(/已完成|Completed/)
    }
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    const header = page.getByText(/任务\s*#\d+\s*·\s*证据包\s*#\d+/).first()
    await expect(header).toBeVisible({ timeout: 60_000 })
    let evidenceSelect = null as unknown as ReturnType<Page['locator']>
    const selectCount = await page.locator('select').count()
    for (let i = 0; i < selectCount; i += 1) {
      const texts = await page.locator('select').nth(i).locator('option').allTextContents()
      if (texts.length > 1 && texts.some((x) => /#\d+ · \d+ ·/.test(x))) {
        evidenceSelect = page.locator('select').nth(i)
        break
      }
    }
    expect(evidenceSelect).not.toBeNull()
    const optionTexts = await evidenceSelect.locator('option').allTextContents()
    const optionValues = await evidenceSelect.locator('option').evaluateAll((els) =>
      els.map((el) => (el as HTMLOptionElement).value),
    )
    const pkgFor = (tid: number): string => {
      const idx = optionTexts.findIndex((text) => text.includes(`· ${tid} ·`))
      expect(idx).toBeGreaterThanOrEqual(0)
      return optionValues[idx]
    }
    // 选 T1 证据包：结果绑定 T1；再选 T2：绑定跟随且导出可用。
    // 每次选择后等选择器恢复可用（上一次载入完全落定），再做下一次选择。
    await evidenceSelect.selectOption(pkgFor(t1))
    await expect(evidenceSelect).toBeEnabled({ timeout: 60_000 })
    await expect(page.getByText(new RegExp(`任务\\s*#${t1}\\s*·\\s*证据包`)).first()).toBeVisible({
      timeout: 60_000,
    })
    await expect(page.getByText(/尚未提交证据包/)).toHaveCount(0)
    await evidenceSelect.selectOption(pkgFor(t2))
    await expect(evidenceSelect).toBeEnabled({ timeout: 60_000 })
    await expect(page.getByText(new RegExp(`任务\\s*#${t2}\\s*·\\s*证据包`)).first()).toBeVisible({
      timeout: 60_000,
    })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    await expect(page.getByRole('button', { name: '导出 Excel 报告', exact: true }).first()).toBeEnabled({
      timeout: 60_000,
    })

    // ---- API 块 H：两次结果页选择各追加恰好一个 revision，最终草稿为 T2 全套。
    await parkForApi(page)
    token = await loginToken(req, soloUser, soloPass)
    const finalView = await projectView(req, pid, token)
    expect(Number(finalView.draft.revision)).toBe(revAfterT2Load + 2)
    expect(hpCop(finalView.draft.content)).toBe(4.0)
    expect(bindingVids(finalView.draft.content)).toEqual([v2])
    expect(await taskIds(req, pid, token)).toEqual([t1, t2, t3])
    expect(creates.length).toBe(3)

    // ---- GUI 块 A1：旧保存与新载入排序（在途旧表单保存不得盖住新载入）。
    // 仅首个草稿 PUT 延迟 15s（修订读取不受影响）：对话框编辑 N=9 后
    // 防抖保存必在途，此时取消（落定排队）并立即到结果页选 T1 证据包。
    // 有协调时：选择 supersede 排队旧保存（取消落定/卸载兜底写前丢弃）
    // 并等在途（防抖保存）落定后再载入，最终为 T1 业务值（operation_years
    // 数字 2），修订 +2（防抖保存/载入各一）。无协调时：排队旧保存
    // （取消落定）在载入后读到新修订并落地，盖住载入，最终为旧 N=9
    // 文本（修订同为 +2，以内容区分）。
    await guiLoginTasks(page, projectName, soloUser, soloPass, session)
    const revBeforeA1 = (await guiDraft(page, pid)).revision
    let delayedFirstPut = false
    await page.route('**/api/projects/*/draft', async (route) => {
      if (route.request().method() !== 'PUT' || delayedFirstPut) return route.continue()
      delayedFirstPut = true
      await new Promise((res) => setTimeout(res, 15000))
      await route.continue()
    })
    try {
      await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
      await page.locator('#task-operation-years').fill('9')
      // 防抖 700ms 已触发、在途 PUT 被延迟卡住时取消（落定排队）并立即
      // 到结果页选 T1 证据包（对话框模态挡住导航，必须先取消）。
      await page.waitForTimeout(1000)
      await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
      await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
      const pkgA1 = await selectEvidenceForTask(page, t1)
      await expect(page.getByText(new RegExp(`任务\\s*#${t1}\\s*·\\s*证据包\\s*#${pkgA1}`)).first()).toBeVisible({
        timeout: 60_000,
      })
      await expect
        .poll(async () => (await guiDraft(page, pid)).working, { timeout: 60_000 })
        .toEqual({
          task_type: 'optimization',
          construction_period: 1,
          operation_years: 2,
          discount_rate: 0.08,
          construction_payments: [
            { t: 0, ratio: '0.6' },
            { t: 1, ratio: '0.4' },
          ],
          maintenance_expenses: [],
        })
      const viewA1 = await guiDraft(page, pid)
      expect(hpCop(viewA1.content)).toBe(3.5)
      expect(bindingVids(viewA1.content)).toEqual([v1])
      expect(viewA1.revision).toBe(revBeforeA1 + 2)
      expect(creates.length).toBe(3)
    } finally {
      await page.unroute('**/api/projects/*/draft')
    }

    // ---- GUI 块 A2：编辑后立即导航保留最终编辑（卸载兜底用新修订写）。
    // 对话框编辑 N=9 后防抖在途，再改 N=8 并立即取消+导航：最终服务端
    // 为 N=8 文本（修订 +2）。本块是已定行为的端到端刻画（导航前最后
    // 一次编辑不丢失）；排序竞争的红绿判定由块 A1 承担。
    await guiLoginTasks(page, projectName, soloUser, soloPass, session)
    const revBeforeA2 = (await guiDraft(page, pid)).revision
    await page.route('**/api/projects/*/draft', async (route) => {
      if (route.request().method() !== 'PUT') return route.continue()
      await new Promise((res) => setTimeout(res, 3000))
      await route.continue()
    })
    try {
      await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
      await page.locator('#task-operation-years').fill('9')
      await page.waitForTimeout(1000)
      await page.locator('#task-operation-years').fill('8')
      // 取消即落定（落定失败保持打开）；随后立即导航触发卸载。
      await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
      await expect(page.getByRole('dialog')).toBeHidden({ timeout: 30_000 })
      await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
      await expect
        .poll(async () => (await guiDraft(page, pid)).working?.operation_years ?? null, { timeout: 60_000 })
        .toBe('8')
      const viewA2 = await guiDraft(page, pid)
      expect(viewA2.revision).toBe(revBeforeA2 + 2)
      expect(creates.length).toBe(3)
      // 恢复 T2 选择（后续块从 T2 出发；重载入 +1 修订，不影响后续相对断言）。
      await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
      await clickTaskRow(page, t2)
      await expect(page.getByRole('status').filter({ hasText: /载入工作草稿/ }).last()).toContainText(new RegExp(`#${t2}.*载入工作草稿`), {
        timeout: 60_000,
      })
    } finally {
      await page.unroute('**/api/projects/*/draft')
    }

    // ---- API 块 I：等 T3 完成（T1/T2 已在块 7 完成；T3 完成时刻最晚，
    // 后续全新上下文默认即 T3，顺序确定不靠猜）。
    await parkForApi(page)
    token = await loginToken(req, soloUser, soloPass)
    await expect
      .poll(
        async () =>
          (await api(req, 'GET', `/api/projects/${pid}/tasks/${t3}`, undefined, token)).task
            .status ?? '',
        { timeout: 600_000, intervals: [10_000] },
      )
      .toBe('completed')

    // ---- API 块 J：经公开结果端点取 T1/T2/T3 证据包 id（任务详情无 evidence 数组，
    // 不臆造形状），供选择绑定、默认最新与深链断言。
    async function pkgOf(tid: number): Promise<number> {
      const res = await api(req, 'GET', `/api/projects/${pid}/tasks/${tid}/result`, undefined, token)
      if (res.result?.evidence_status !== 'available') throw new Error(`任务 ${tid} 尚无可用结果`)
      return Number(res.result.evidence.id)
    }
    const pkg1 = await pkgOf(t1)
    const pkg2 = await pkgOf(t2)
    const pkg3 = await pkgOf(t3)

    // ---- GUI 块 8：显式选中历史任务 T1 即精确绑定其原包（不回退、不重解）。
    await guiLoginTasks(page, projectName, soloUser, soloPass, session)
    await clickTaskRow(page, t1)
    await expect(page.getByRole('status').filter({ hasText: /载入工作草稿/ }).last()).toContainText(new RegExp(`#${t1}.*载入工作草稿`), {
      timeout: 60_000,
    })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    const t1Header = page.getByText(/任务\s*#\d+\s*·\s*证据包\s*#\d+/).first()
    await expect(t1Header).toBeVisible({ timeout: 60_000 })
    await expect(t1Header).toContainText(new RegExp(`任务\\s*#${t1}\\s*·\\s*证据包\\s*#${pkg1}`))

    // ---- GUI 块 9：无显式选择（全新上下文）默认完成最晚 → T3；默认跟随只读不写草稿。
    await parkForApi(page)
    token = await loginToken(req, soloUser, soloPass)
    const revAfterT1Select = await draftRevision(req, pid, token)
    const freshCtx: BrowserContext = await browser.newContext()
    const freshPage: Page = await freshCtx.newPage()
    const freshSession = await createSession(freshPage, soloUser, soloPass)
    await freshPage.getByText(projectName).click({ timeout: 30_000 })
    await freshPage.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    const freshHeader = freshPage.getByText(/任务\s*#\d+\s*·\s*证据包\s*#\d+/).first()
    await expect(freshHeader).toBeVisible({ timeout: 60_000 })
    await expect(freshHeader).toContainText(new RegExp(`任务\\s*#${t3}\\s*·\\s*证据包\\s*#${pkg3}`))
    await parkForApi(freshPage)
    token = await loginToken(req, soloUser, soloPass)
    expect(await draftRevision(req, pid, token)).toBe(revAfterT1Select)
    await freshSession.assertNoErrors()
    await freshCtx.close()

    // ---- GUI 块 10：结果深链 ?package=T1包 → 绑定 T1，随后用户选择 T2 接管一切。
    await guiLoginTasks(page, projectName, soloUser, soloPass, session)
    await page.goto(`${APP}/projects/${pid}/results?package=${pkg1}`)
    const linkHeader = page.getByText(/任务\s*#\d+\s*·\s*证据包\s*#\d+/).first()
    await expect(linkHeader).toBeVisible({ timeout: 60_000 })
    await expect(linkHeader).toContainText(new RegExp(`任务\\s*#${t1}\\s*·\\s*证据包\\s*#${pkg1}`))
    // 初始化完成后深链参数被清除（一次性权威），重载不再重拷快照。
    await expect(page).not.toHaveURL(/package=/, { timeout: 30_000 })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await clickTaskRow(page, t2)
    await expect(page.getByRole('status').filter({ hasText: /载入工作草稿/ }).last()).toContainText(new RegExp(`#${t2}.*载入工作草稿`), {
      timeout: 60_000,
    })
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('结果分析').click({ timeout: 30_000 })
    const t2Header = page.getByText(/任务\s*#\d+\s*·\s*证据包\s*#\d+/).first()
    await expect(t2Header).toBeVisible({ timeout: 60_000 })
    await expect(t2Header).toContainText(new RegExp(`任务\\s*#${t2}\\s*·\\s*证据包\\s*#${pkg2}`))
    // 导出入口同绑定 T2：Excel 按钮可用（T2 有证据包）；真实下载的文件名
    // report-{项目}-{包} 即 T2 包 id（完成链已备好系统评估，导出成功）。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('导出').click({ timeout: 30_000 })
    const excelBtn = page.getByRole('button', { name: '导出 Excel 报告', exact: true }).first()
    await expect(excelBtn).toBeEnabled({ timeout: 60_000 })
    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 120_000 }),
      excelBtn.click({ timeout: 30_000 }),
    ])
    expect(download.suggestedFilename()).toBe(`report-${pid}-${pkg2}.xlsx`)
    await expect(page.getByText(/报告已生成/).first()).toBeVisible({ timeout: 60_000 })

    // ---- GUI 块 11：任务级参数（不确定性 n=1/seed=11）随工作区编辑暂存、重载不丢、提交冻结。
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 30_000 })
    await page.locator('#task-type').selectOption('uncertainty')
    await page.locator('#u-samples').fill('1')
    await page.locator('#u-seed').fill('11')
    await page.getByRole('dialog').getByRole('button', { name: /取消|Cancel/i }).click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 10_000 })
    await expect
      .poll(
        async () =>
          (await (await page.request.get(`/api/projects/${pid}`)).json() as Json)
            .draft?.content?.task_working_inputs?.task_params?.params?.n_samples ?? null,
        { timeout: 30_000 },
      )
      .toBe(1)
    await page.reload()
    await page.getByRole('navigation', { name: /工作台|Workspace/i }).getByText('任务中心').click({ timeout: 30_000 })
    await page.getByRole('button', { name: /提交任务|Submit task/i }).first().click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeVisible({ timeout: 30_000 })
    await expect.poll(async () => page.locator('#u-samples').inputValue(), { timeout: 30_000 }).toBe('1')
    await expect.poll(async () => page.locator('#u-seed').inputValue(), { timeout: 30_000 }).toBe('11')
    await page.getByRole('dialog').getByRole('button', { name: /提交任务|Submit task/i }).click({ timeout: 30_000 })
    await expect(page.getByRole('dialog')).toBeHidden({ timeout: 60_000 })
    expect(creates.length).toBe(4)
    let uiIds4: number[] = []
    await expect
      .poll(async () => {
        uiIds4 = await taskIdsFromUI(page)
        return uiIds4.length
      }, { timeout: 60_000 })
      .toBe(4)
    const t4 = uiIds4.find((id) => id !== t1 && id !== t2 && id !== t3) as number

    // ---- API 块 K：T4 快照 = 任务级参数 + 当时评价（T2 载入值）；T1/T2 快照全文仍不变。
    // T4 求解在后台继续，不等待（快照断言不需要完成）。
    await parkForApi(page)
    token = await loginToken(req, soloUser, soloPass)
    expect(await taskIds(req, pid, token)).toEqual([t1, t2, t3, t4])
    const t4inputs = await taskInputs(req, pid, t4, token)
    // 任务级参数与提交装配同形（{params: 内层}，见 submissions 任务级 config 并入）。
    expect(t4inputs.snapshot.calc_config_snapshot.task_params).toEqual({
      params: {
        mode: 'fixed_reliability',
        n_samples: 1,
        distribution: { type: 'normal', noise_pct: 5 },
        seed: 11,
      },
    })
    expect(t4inputs.snapshot.evaluation).toMatchObject({
      construction_period: 1,
      operation_years: 3,
      discount_rate: 0.08,
    })
    expect(JSON.stringify((await taskInputs(req, pid, t1, token)).snapshot)).toBe(t1json)
    expect(JSON.stringify((await taskInputs(req, pid, t2, token)).snapshot)).toBe(t2json)

    await soloCtx.close()
    session.assertNoErrors()
    await context.close()
  })
})
