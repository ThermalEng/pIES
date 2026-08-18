export const meta = {
  name: 'iesplan-frontend',
  description: '前端: React 主界面(画布/表单/任务/结果/导出) + 教程页 + i18n',
  phases: [
    { title: 'Scaffold', detail: 'API 客户端与设计系统' },
    { title: 'Features', detail: '并行实现页面功能' },
    { title: 'Build', detail: '构建验证' },
  ],
}

const SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    files: { type: 'array', items: { type: 'string' } },
    issues: { type: 'array', items: { type: 'string' } },
  },
  required: ['ok'],
}

const SCAFFOLD = `你是 IES Plan 前端的脚手架实现者。工作目录 /home/mc/Documents/工作文档/IES_Plan/frontend。

## 任务: 只写以下文件(不得写其他路径)
1. src/main.tsx: React 入口 (BrowserRouter)
2. src/App.tsx: 路由表: /login, / (项目列表), /projects/:id (工作台), /tutorial (独立教程页), /settings(管理员)
3. src/api/client.ts: fetch 封装: 自动带凭证(Cookie), 统一错误处理(解析后端 error 信封 {error:{code,message_key,params,...}}), 401 跳登录, 类型化方法集:
   - auth: login/logout/changePassword/confirmTakeover/register
   - projects: list/create/get/updateDraft/versions/archive/unarchive/delete/duplicate/transfer/viewers
   - model: getGraph/addDevice/updateDevice/deleteDevice/connect/disconnect/validate/deviceTypes
   - datasets: list/template/upload/versions/sample
   - config: get/save/validate/default/algorithms
   - validation: run/baselineConfirm
   - tasks: list/create/get/cancel/retry
   - results: result/assessments/assess/select/diff/hourly
   - exports: excel/package/download
   - admin: users/storage/health/audit
4. src/i18n/index.ts: 中英双语消息表(键结构 ies.*), 默认 zh; 提供 useI18n() hook (t(key, params))
5. src/i18n/messages_zh.ts + messages_en.ts: 完整键表(登录/导航/建模/数据/配置/任务/结果/导出/帮助/诊断), 至少 150 键; 诊断消息键对应后端消息键
6. src/components/ui.tsx: 基础组件(按钮/输入框/选择器/表格/标签/卡片/对话框/表单字段/状态徽章) 带 aria 属性与键盘支持, 状态不只靠颜色(文字+图标+形状)
7. src/styles.css: 设计系统(变量/布局/组件样式), WCAG 2.2 AA 对比度, 支持 1920x1080 到 4K
8. src/types.ts: 领域类型(Project, Device, Port, Connection, DatasetVersion, CalcConfig, Task, TaskState, Evidence, Assessment, Diagnostic...)
9. src/lib/format.ts: 单位格式化(kWh/MW/°C/金额, 按 i18n), 日期时间格式
10. tsconfig.node.json: vite 配置的 tsconfig

## 注意
- 只写上面列出的文件; 页面组件由其他 agent 并行实现
- 类型标注完整; strict TS
- 中文注释; 完成后报告文件清单`

const FEATURES = [
  {
    key: 'login',
    prompt: `你是 IES Plan 前端的登录与会话界面实现者。工作目录 /home/mc/Documents/工作文档/IES_Plan/frontend。

## 先读
- src/api/client.ts, src/i18n/index.ts, src/i18n/messages_zh.ts, src/components/ui.tsx, src/styles.css, src/App.tsx (脚手架已实现)

## 任务: 只写 src/pages/LoginPage.tsx (及必要小文件)
- 登录表单(用户名/密码, 键盘可操作, 错误显示诊断消息键对应文案)
- 首次登录强制改密流程(force_password_change 时显示改密表单)
- 窗口接管确认流程(服务端返回 needs_takeover_confirm 时, 显示确认页: 旧窗口已接管提示, 确认后拿新凭证)
- 会话过期处理(401 全局跳转登录)
- 登录限速提示(后端返回错误码时显示对应消息)
- 离线状态提示(网络失败时显示离线徽章, 提示本地局域网可用性)

## 注意
- 只写 src/pages/LoginPage.tsx 与必要辅助文件
- 完成后报告文件清单`,
  },
  {
    key: 'projectlist',
    prompt: `你是 IES Plan 前端的项目列表页实现者。工作目录 /home/mc/Documents/工作文档/IES_Plan/frontend。

## 先读
- src/api/client.ts, src/i18n/index.ts, src/components/ui.tsx, src/styles.css, src/App.tsx

## 任务: 只写 src/pages/ProjectsPage.tsx (及必要小文件)
- 项目列表: 名称/角色(所有者/查看者)/状态(活跃/归档)/更新时间/最近任务结局
- 创建项目对话框: 名称/基准币种(CNY/USD 默认 CNY)/UTC 偏移(默认 +08:00)
- 项目操作: 打开/归档/撤销归档/复制(候选方案)/删除(明确确认对话框, 提示不可恢复)/转移所有权(选择目标工程师)/管理查看者(添加/移除)
- 每个操作后刷新; 权限不足时操作禁用并显示原因
- 键盘可达性: 列表行可 Tab 聚焦, Enter 打开

## 注意
- 只写 src/pages/ProjectsPage.tsx 与必要辅助文件
- 完成后报告文件清单`,
  },
  {
    key: 'canvas',
    prompt: `你是 IES Plan 前端的建模画布实现者。工作目录 /home/mc/Documents/工作文档/IES_Plan/frontend。

## 先读
- src/api/client.ts, src/i18n/index.ts, src/components/ui.tsx, src/styles.css, src/types.ts
- 设计输入 RPD 第7节(建模与设备): 拖拽构建设备系统图, 设备类型: 电网连接/光伏/电池/电负荷/热负荷/冷负荷(冷热组合)/热泵/燃气锅炉/电制冷机; 端口能源类型(电/热/冷/气/太阳辐射)与方向(out 源/in 汇); 连接须类型方向兼容

## 任务: 只写 src/pages/ModelPage.tsx (及必要小文件, 可用 @xyflow/react)
- 画布: 左侧设备面板(从 /api/registry/device-types 拉取), 拖拽到画布添加设备; 设备节点显示名称/类型/存量或新增徽章/容量
- 连接: 端口间连线(兼容校验: 能源类型+方向), 不兼容时显示可定位诊断
- 设备参数编辑侧栏: 按设备类型 schema 渲染表单(单位/范围/默认值/帮助键), 存量/新增切换, 模型精度选择(1简化/2标准/3详细)
- 保存: 语义命令提交(updateDraft), 自动保存(防抖)+手动保存按钮; 冲突提示(修订号过期)
- 布局与拓扑分离: 布局变化不影响工程语义
- 工具栏: 缩放/适应视图/验证(调用 validate 显示诊断列表)
- 节点自定义渲染(ReactFlow Node), 状态不只靠颜色

## 注意
- 只写 src/pages/ModelPage.tsx 与必要辅助文件
- 完成后报告文件清单`,
  },
  {
    key: 'dataconfig',
    prompt: `你是 IES Plan 前端的数据与配置页实现者。工作目录 /home/mc/Documents/工作文档/IES_Plan/frontend。

## 先读
- src/api/client.ts, src/i18n/index.ts, src/components/ui.tsx, src/styles.css, src/types.ts
- 设计输入 RPD 第8节(数据约束)/9.2(配置): 分辨率 15/30/60 分钟, 365 天非闰年, 固定 UTC 偏移, 标准 CSV 模板导入, 阻断性错误定位到字段行

## 任务: 只写 src/pages/DataPage.tsx, src/pages/ConfigPage.tsx (及必要小文件)
**DataPage**:
- 数据集列表(名称/版本/质量报告摘要/溯源/许可证)
- 上传: 选择分辨率+UTC 偏移+字段描述, 文件上传; 后端返回质量报告与诊断列表(阻断/警告 分级, 定位字段/行); 阻断错误未修复不可绑定
- 内置样例数据按钮(生成合成数据)
- 模板下载按钮
- 数据集版本历史与质量报告详情
**ConfigPage**:
- 经济参数: 评价周期/折现率/最低可接受 IRR(与折现率独立)/税率/折旧年限/币种显示
- 变量配置: 每类新建设备容量变量(类型/初值/上下界), 存量设备固定显示
- 目标: 默认税后项目投资 IRR 最大化; 可选 NPV/资本金 IRR; 碳排放目标/约束
- 约束: 预定义约束开关列表 + 高级模式表达式输入(受限语法提示); 最低 IRR 硬约束显著显示(不可被权重抵消)
- 算法选择: auto/手动(算法能力列表), 不兼容算法禁止提交并说明原因
- 保存: 校验诊断显示(定位配置项); 保存成功提示

## 注意
- 只写上面文件与必要辅助文件
- 完成后报告文件清单`,
  },
  {
    key: 'tasksresults',
    prompt: `你是 IES Plan 前端的任务与结果页实现者。工作目录 /home/mc/Documents/工作文档/IES_Plan/frontend。

## 先读
- src/api/client.ts, src/i18n/index.ts, src/components/ui.tsx, src/styles.css, src/types.ts
- 设计输入 RPD 第9/10/11节: 任务状态机(queued/running/completed/cancelling/cancelled/timed_out/failed)与业务结局(normal_completion/no_recommendation/no_feasible_multi_objective/partial_batch/restricted_results/insufficient_evidence)分别展示; 四维结果(物理/最优性/财务/可靠性)独立展示, 摘要不得掩盖任一维度失败; 技术状态≠业务结局; 结果应用前显示参数差异并确认

## 任务: 只写 src/pages/ProjectPage.tsx(工作台框架), src/pages/TasksPage.tsx, src/pages/ResultsPage.tsx (及必要小文件)
**ProjectPage**: 工作台框架: 侧边导航(模型/数据/配置/校验/任务/结果/导出), 项目信息头(版本/货币/UTC), 自动保存状态指示, 离线状态徽章
**TasksPage**:
- 任务列表(类型/状态徽章/业务结局徽章/进度条/创建时间/发起人)
- 提交新任务: 类型(方案评价/规划/不确定性分析), 配置摘要, 重复提交提示(已有相同任务)
- 任务详情: 状态/结局/进度/诊断列表(定位/修复建议)/取消/重试
- 轮询(5s)自动刷新; 浏览器关闭后重开可恢复视图
- 不确定性分析: 模式选择(固定方案可靠性/重规划敏感性, 明确区分不可合并)/样本数/种子/分布参数
**ResultsPage**:
- 结果摘要: 版本/数据版本/计算配置/状态/业务结局/四维结论卡片(物理/最优性/财务/可靠性, 各自 passed/restricted/failed/na/insufficient 状态, 颜色+文字+图标)
- 财务: 投资/运行成本/收益/现金流表/IRR 状态(唯一/无解/多解/退化/域外/数值失败)
- 环境: 碳排放/边界/因子版本
- 工程: 能源平衡表(电/热/冷)/设备容量/逐时曲线(简单折线图: 购电/负荷/SOC)/峰值/购售电/需量
- Pareto: 候选点图(简单 SVG 散点, 目标轴), 用户选择候选 → 显示参数差异预览 → 确认应用(创建新版本提示)
- 评估历史(不可变列表)与重新评估按钮
- 导出: Excel 按钮(选择语言 zh/en), 完整项目包按钮(仅所有者可见)

## 注意
- 图表用原生 SVG, 不引入图表库
- 只写上面文件与必要辅助文件
- 完成后报告文件清单`,
  },
  {
    key: 'tutorial',
    prompt: `你是 IES Plan 前端的独立教程页实现者。工作目录 /home/mc/Documents/工作文档/IES_Plan/frontend。

## 先读
- src/i18n/index.ts, src/styles.css, src/App.tsx
- 设计输入 RPD 第14节(帮助/教程)/17.8 REQ-HELP-002: 教程是独立静态页面, 不接收项目数据, 不拥有业务数据; 计算服务不可用时仍可读; 声明适用程序版本; 与快速帮助共享术语

## 任务: 只写 src/pages/TutorialPage.tsx 与 src/data/tutorial.ts (及必要小文件)
- 教程内容(中英双语, 静态数据驱动, 无任何 API 调用):
  1. 快速开始: 创建项目→建模→数据→配置→校验→提交任务→查看结果→导出
  2. 建模指南: 设备类型/端口/连接规则/存量与新增
  3. 数据指南: 模板/分辨率/校验错误修复/样例数据
  4. 规划配置: 变量/目标/约束/最低 IRR 硬约束/多目标
  5. 结果解读: 四维有效性/业务结局/Pareto 选择/结果应用
  6. 不确定性: 固定方案可靠性 vs 重规划敏感性
  7. 故障恢复: 离线模式/任务中断恢复/常见错误修复
  8. 术语表: 项目版本/计算快照/证据/评估/所有者/查看者/内容寻址对象
- 布局: 左侧目录导航(可滚动), 右侧内容, 顶部显示程序版本号
- 离线说明: 页面纯静态, 无需后端

## 注意
- 不 import src/api/client.ts, 不调用任何后端
- 只写上面文件与必要辅助文件
- 完成后报告文件清单`,
  },
]

phase('Scaffold')
const scaffold = await agent(SCAFFOLD, { label: 'scaffold:frontend', phase: 'Scaffold', effort: 'high', schema: SCHEMA })

phase('Features')
const feats = await parallel(FEATURES.map((f) => () =>
  agent(f.prompt, { label: 'feat:' + f.key, phase: 'Features', effort: 'high' })
))

phase('Build')
const featsDone = feats.filter(Boolean)
const build = await agent(
  `你是 IES Plan 前端的构建集成者。工作目录 /home/mc/Documents/工作文档/IES_Plan/frontend。

脚手架与 6 个页面 agent 已完成。现在:

## 步骤
1. 检查 src/ 完整性: App.tsx 路由引用所有页面组件, 缺失文件或导出名不符处修复
2. 修复 TypeScript 错误: 在 docker 内构建:
   - cd /home/mc/Documents/工作文档/IES_Plan && docker compose build frontend
   - docker compose run --rm frontend-builder sh -c 'cd /app && npm run build' (或直接用 docker compose build frontend 观察输出)
   - 反复修复到构建通过
3. 检查 i18n: 页面引用的消息键都存在于 messages_zh/en; 缺失键补齐
4. 可访问性抽查: 主要表单有 label, 状态组件有 aria/文本, 颜色外有图标
5. 输出报告: 页面清单, 构建是否通过, 遗留问题

## 注意
- 只改 frontend/ 下文件
- 构建在 docker 内(node 镜像), 不在主机跑 npm
- 中文输出最终报告`,
  { label: 'build:frontend', phase: 'Build', effort: 'high' }
)

log('前端完成')
return { scaffold: !!scaffold, features: featsDone.length, built: !!build }
