export const meta = {
  name: 'iesplan-browser-minimal',
  description: 'browser-harness 真实浏览器跑最小案例闭环, 发现并修复 bug 直至通过',
  phases: [
    { title: 'Browser', detail: '浏览器执行最小案例 UI 闭环' },
    { title: 'Fix', detail: '并行修复发现的 bug' },
    { title: 'Verify', detail: '重建并浏览器回归验证' },
  ],
}

const BUGS_SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    bugs: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          area: { type: 'string', description: 'frontend | backend | env' },
          step: { type: 'string' },
          title: { type: 'string' },
          detail: { type: 'string' },
          evidence: { type: 'string' },
          severity: { type: 'string' },
        },
        required: ['id', 'area', 'title', 'detail'],
      },
    },
    summary: { type: 'string' },
  },
  required: ['ok'],
}

function browserPrompt(extra) {
  return `你是 pIES 的浏览器端到端测试员。使用 browser-harness CLI(命令名 browser-harness, 不是 browser-use)在真实 Chrome 中执行最小案例完整闭环, 发现并报告所有 bug。

## 环境
- 应用: http://localhost:8080 (全栈已运行)
- 账号: admin / Iesplan-Admin#Verify2026 (若登录 401/429, 报告并尝试等待 60s 重试)
- 命令: browser-harness --doctor 先检查; 用法示例:
  browser-harness <<'PY'
  new_tab("http://localhost:8080")
  print(page_info())
  capture_screenshot()
  PY
- 交互技巧: 截图看像素 → 优先用 js("...") 定位并点击按钮(document.querySelector(...).click()) → 截图确认; wait_for_load() 等待导航
- 收集前端错误: 每步后执行 js("window.__errs ? window.__errs : (window.__errs=[], window.addEventListener('error', e => window.__errs.push(String(e.message))), [])") 查看累计错误
- 若页面显示"接管旧窗口会话"确认页 → 点击"确认接管"继续
- 截图存 /tmp/browser_min/ 目录(每步一张, 命名 step_编号.png)

${extra}

## 最小案例步骤(UI 操作, 每步截图+记录结果)
1. 打开应用, 登录 admin(处理接管确认) → 应进入项目列表页
2. 点击"新建项目"→ 填写名称(如 "浏览器最小案例 {时间戳}")→ 创建 → 项目出现在列表
3. 点击项目"打开" → 工作台(侧边导航: 模型/数据/配置/校验/任务/结果/导出)
4. 模型页: 从左侧设备面板添加 4 类设备: 电网连接、电负荷、热负荷、热泵。操作: 点击面板中的设备类型(或拖拽到画布)。若添加后出现参数表单/侧栏, 填写必要参数(热泵 mode 选 heating)。截图设备列表/画布
5. 模型页: 建立连接(电网→电负荷, 电网→热泵, 热泵→热负荷)。若画布支持端口连线, 尝试操作; 若 UI 不支持或操作失败, 记录为 bug(说明 UI 上连接功能的现状)
6. 数据页: 点击"内置样例数据"或类似按钮生成样例数据(分辨率 1h); 若需先创建数据集, 按 UI 流程操作。截图结果
7. 配置页: 点击"保存"(默认配置); 若有经济参数表单无需改动。截图保存结果
8. 校验页: 点击"运行校验"→ 记录状态(通过/阻断)与诊断列表; 点击"财务基准确认"。截图
9. 任务页: 点击"提交任务"或"新建任务"→ 选择类型(计算/方案评价)→ 提交; 轮询等待任务完成(刷新页面或等待自动刷新), 记录最终状态
10. 结果页: 查看结果(四维评估/指标摘要/候选)。截图
11. 导出页: 点击"Excel 导出"(选择中文)→ 观察下载行为(浏览器下载或提示)。截图

## 输出要求
按 JSON 输出(必须):
- ok: 是否全部步骤完成且无 bug
- bugs: 数组, 每个 bug: {id, area(frontend/backend/env), step, title, detail(具体现象+复现操作+console错误), evidence(截图路径或API响应), severity(critical/high/medium/low)}
- summary: 每步结果简述(通过/失败)

注意: 只测试不修改代码; 若某步 UI 操作不确定, 先用 js 检查页面结构再操作。`
}

function fixPrompt(bugs, target) {
  const items = bugs.filter((b) => b.area === target)
  if (!items.length) return null
  return `你是 pIES 的修复工程师。修复以下浏览器测试发现的 ${target} 侧 bug。工作目录 /home/mc/Documents/工作文档/pIES。

## Bug 清单
${items.map((b) => `### ${b.id} [${b.severity}] ${b.step}: ${b.title}\n${b.detail}\n证据: ${b.evidence || '无'}`).join('\n\n')}

## 任务
1. 逐个阅读相关代码(前端: frontend/src/, 后端: backend/iesplan/)定位根因
2. 修复 bug(最小改动, 保持既有风格)
3. 验证:
   - 前端: docker compose build web(tsc+vite 编译通过即可)
   - 后端: docker compose build backend && docker compose run --rm backend pytest -q 2>&1 | tail -5(全部通过)
4. 不要重建部署(verify 阶段统一重建), 只改代码并确认编译/测试通过

## 注意
- 只改 ${target === 'frontend' ? 'frontend/' : 'backend/'} 下文件
- 修复后报告: 每个 bug 的根因与修复内容, 验证结果`
}

phase('Browser')
let bugs = []
let round = 0
let lastRun = null

const first = await agent(browserPrompt(''), { label: 'browser:minimal-run', phase: 'Browser', effort: 'high', schema: BUGS_SCHEMA })
bugs = (first && first.bugs) || []
log('首轮浏览器测试: ' + bugs.length + ' 个 bug')

while (bugs.length && round < 3) {
  round += 1
  phase('Fix' + round)
  const fe = fixPrompt(bugs, 'frontend')
  const be = fixPrompt(bugs, 'backend')
  const fixers = []
  if (fe) fixers.push(() => agent(fe, { label: 'fix:frontend-' + round, phase: 'Fix' + round, effort: 'high' }))
  if (be) fixers.push(() => agent(be, { label: 'fix:backend-' + round, phase: 'Fix' + round, effort: 'high' }))
  await parallel(fixers)
  log('修复轮 ' + round + ' 完成')

  phase('Verify' + round)
  const verify = await agent(
    `你是 pIES 的部署与回归验证员。

## 步骤
1. 重建并重启全栈: cd /home/mc/Documents/工作文档/pIES && docker compose build web backend 2>&1 | tail -3 && docker compose up -d web backend worker io_worker 2>&1 | tail -5
2. 等待就绪: 循环 curl http://localhost:8080/api/healthz 直到 200(最多 60s)
3. 运行后端测试确认无回归: docker compose run --rm backend pytest -q 2>&1 | tail -3

## 注意
- 不要修改任何代码, 只部署与验证
- 输出: 重建是否成功, 服务是否就绪, 测试通过数`,
    { label: 'deploy:verify-' + round, phase: 'Verify' + round, effort: 'medium' }
  )

  const re = await agent(browserPrompt('本轮修复了之前发现的 bug, 请重新执行完整最小案例流程, 重点验证之前失败的步骤是否已修复, 并报告任何新 bug。'), {
    label: 'browser:verify-' + round,
    phase: 'Verify' + round,
    effort: 'high',
    schema: BUGS_SCHEMA,
  })
  bugs = (re && re.bugs) || []
  lastRun = re
  log('验证轮 ' + round + ': 剩余 ' + bugs.length + ' 个 bug')
}

if (bugs.length) {
  log('达到最大修复轮数, 仍有 ' + bugs.length + ' 个 bug 未修复')
}
return { ok: bugs.length === 0, rounds: round, remainingBugs: bugs, lastSummary: (lastRun && lastRun.summary) || (first && first.summary) }
