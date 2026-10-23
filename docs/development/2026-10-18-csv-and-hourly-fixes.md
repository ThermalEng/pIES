# CSV 预检与结果曲线：两个独立缺陷修复

日期：2026-10-18；基线：`576858e`；临时实施指南，非产品规范。
授权：用户明确批准两个新 OpenCode 分别修复这两个缺口。宪法及正式契约不变，不重开原 0.14 四批，也不展开 0.15 的其他扩展类别。

## 共同背景

主线已合入 CSV 公开消费验收 `dea669e` 与精确身份断言 `9a7c6de`。实际上传、公开 API 提交、真实求解与结果摘要/封存财务卡可用；这不代表 GUI 提交和曲线已正常。

原证据在主工作树 `.claude/worktrees/pies-014-b3-project-boundaries/docs/reviews/2026-10-18-external-csv-identity-fix.md` 及同目录日志。预检反例在 `/tmp/opencode/csv-evidence/evidence/`（可能被临时清理，不得依赖其永久存在）。`docs/` 被忽略，各新树须通过主工作树绝对路径阅读本指南与已有证据，不强制提交 docs。

开始前完整读取本树 AGENTS、宪法及相关正式契约；理解或定位代码先用 CodeGraph（仅主树存在索引时可在主树查询，再核对自己树的源码）。只有确需改变规范时先报告，由 Codex 裁决，不能擅改宪法。

## A：CSV 来源声明预检误拒

- 树：`.claude/worktrees/pies-csv-source-precheck`；分支：`fix/csv-source-precheck`。
- 原因：builder 接受 predefined 接口的数据来源映射，但 application 的参数预检仍对同一值执行数值 constant 检查，将合法 dict 报成 PARAM-UNIT-002，任务中心据此阻断提交。
- 结果：既有合法 constant 与 data_repeat/data_full/data_predict 声明按其真实公开语义通过，不再把来源映射当物理数值。非法输入、未知键、错误接口和确有业务约束的值仍应在权威入口明确报错。
- 核对已有来源声明解析和装配闸门，复用公开能力；不穿透私有 helper，不复制一套 CSV/装配校验器，不以“任意 dict 都放行”冒充修复，也不在内部快照/读取/运行时新增重复校验。必要的最小公开纯能力提取允许，但不能做跨模块大重构。
- 归属：后端来源准入/预检及其定向测试；不改 ResultsPage、结果映射、运行时、项目模型消费者或 schema。另一个模型实例也修改了 precheck 的项目文档解析，属于已知集成交叉点；不可复制、合并或覆盖其分支，由 Codex 后续集成。
- 验收：先红后绿；定向覆盖来源形态与既有 constant 拒绝规则。用 Docker Playwright 在合法 CSV 输入下通过真正的任务中心提交动作产生任务，再由真实 Worker 完成并核对输入对应的序列值。外围模型/财务准备可复用公开 API，但不得用 API 代替本轮被测 GUI 提交；不依赖曲线显示，关键求解值可从公开结果核对。

## B：逐时曲线错误依赖指标摘要

- 树：`.claude/worktrees/pies-result-hourly-visibility`；分支：`fix/result-hourly-visibility`。
- 原因：ResultsPage 的曲线区块嵌在 `metrics` 条件内；当前真实链顶层 metrics_summary 可以合法为 null，但独立的 hourly_refs 与逐时读取已有真实数据。
- 结果：曲线按自身数据/加载/缺席/失败状态独立展示，不依赖无关指标摘要；财务卡、结果摘要与曲线相互不遮蔽。保留现有单位分组、step 横轴、任务身份、读取失败可见与显式重试、切换任务不显示旧结果的行为。
- 归属：前端结果页及必要定向测试；不改后端预检/worker/结果载荷，不伪造空 metrics，不把 solver 的原始统计硬转为 MetricValue，不添加兼容 fallback 或新摘要框架。
- 验收：先红后绿；Docker Playwright 在真实求解、HTTP metrics_summary=null 且 hourly_refs 非空时确认曲线可见，身份/单位/step 与公开结果一致。复用或修正现有 hourly-steps 规格；按改动风险定向覆盖合法缺席、读取失败/重试及任务切换，不重跑所有前后端测试。任务准备/提交可走现有公开 API，不能篡改 DB/证据或注入页面状态来制造结果；不修 A 的 GUI 提交。

## 并行、交付和停止

- 两个进程虽然启动于主树，所有修改/git/构建上下文均须显式使用各自树；不能编辑主树或对方树。原项目模型进程保持其独立范围，不拉入本轮。
- A 使用 Compose 项目 `pies-csv-precheck`、候选端口 18088；B 使用 `pies-hourly-visible`、候选端口 18089。启动前检查实际端口与 compose 挂载，使用各自可丢弃测试数据，绝不挂主树 ./data 或复用其他栈的 DB/Redis/卷。
- 仅 Docker 编译/测试/格式化，真实 GUI 用 Docker Playwright。按修改风险验证，保留实际命令、测试基线、完整日志和关键值，不用构建成功冒充浏览器通过，不为小修改重复全仓，也不额外增加文件摘要比对。
- 功能完成后在自己分支独立提交；必要使用说明/更新日志须与实际行为同步，中央文档冲突由 Codex 集成。不改版本完成状态、宪法或原业务契约；历史记录不重写。新发现无关缺口只记录，不自动转为前置开发。
- 清理仅限自己明确创建的测试容器/非基础镜像，保留证据、基础镜像及归属不明资源。不迁移/删除开发库、用户资源或旧工作树。
- 中文报告提交号、实际改动、先红后绿证据、定向命令/结果与未覆盖项；提交后停下，由 Codex 独立复审，不 merge/push。
