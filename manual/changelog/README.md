# 更新日志

本页记录已经实现的用户可感知行为、公共设计与迁移要求。新版本在上；尚未实现的开发顺序单独见 [Roadmap](roadmap.md)。

产品使用 `MAJOR.MINOR.PATCH` 三段式版本。当前产品版本为 `0.1.0`，当前开发目标为 `0.2.0`，首个正式稳定版本为 `1.0.0`。完整规则见[版本化与发布](../developer-guide/zh-CN/versioning-and-release.md)。

`Unreleased` 只保存已经完成并通过相应验收、但尚未正式发布的变化。一项工作写入这里时必须从 Roadmap 删除；部分完成只迁移已完成部分。这里不收录愿望、待办、目标状态或预计完成版本。

## Unreleased

### 安全

- 为基于 Cookie 会话(`ies_session`, SameSite=Lax)的状态变更请求(POST/PUT/PATCH/DELETE)增加 CSRF 双源校验：浏览器请求优先校验 `Origin`、缺失回退 `Referer`，规范化后必须命中可信来源(`app_url` + `IESPLAN_CORS_ORIGINS` + 请求自身 Host 同源来源)，否则返回 403 `AUTH-CSRF-001`；Bearer 认证、无 Origin/Referer 的 API 客户端与只读请求不受影响。
- 为管理员危险维护操作增加二次确认与作用范围提示：`POST /api/admin/transfer-project`(所有权转移)与 `POST /api/admin/unlock-task`(任务解锁)请求须携带 `confirm=true` 才执行，未确认返回 409 `ADMIN-CONFIRM-REQUIRED` 并附影响范围(from_user/to_user/项目数)提示；所有权转移目标新增校验，必须是 active 且非管理员、非系统账号，避免把项目转给管理员/系统账号绕过"管理员维护只读"。不引入审批链。
- 管理员删除账号增加误操作防护（0.2.0 B1）：删除账号会级联软删其拥有的全部项目且不可恢复，现必须先调用 `POST /api/auth/users/{user_id}/delete-preview` 预览将受影响的项目清单（名称/ID/数量）并取得签名确认令牌，删除时携带 `{"confirm": true, "confirm_token": "..."}` 才会执行；缺少 confirm、令牌缺失/过期/伪造或预览后项目清单变化均返回 400 `AUTH-DEL-001`。前端账号管理在确认对话框中展示受影响项目清单。
- 项目删除确认强化(0.2.0 B4)：`DELETE /api/projects/{id}` 不再接受空布尔 `confirm: true` 单独确认，须提供与待删除项目名精确匹配的 `name` 或非空删除原因 `reason` 之一；`confirm` 字段仅为兼容旧调用方保留。审计记录确认方式与删除原因(脱敏)。前端删除确认对话框要求输入项目名才能确认。
- 部署不可变审计触发器(0.2.0 B4)：`init_db()` 在 PostgreSQL 下执行 `immutable_triggers.py` 的全部 DDL(`ALL_IMMUTABLE_TRIGGER_DDL` + `ALL_IMMUTABLE_REVOKE_DDL`)，为 12 张不可变表(含 `audit_log`/`auth_events`/`project_versions`/`calc_snapshots` 等)创建禁 UPDATE/DELETE 触发器与 `REVOKE UPDATE, DELETE ... FROM PUBLIC`，实现宪法 §16「关键变更保留不可变审计」；部署幂等(DROP FUNCTION IF EXISTS ... CASCADE 后重建)。SQLite 测试库跳过 PostgreSQL 触发器语法。
- 计算配置保存审计(0.2.0 B4)：`PUT /api/projects/{id}/config` 保存时写入 `config.saved` 审计记录(只含版本/变量数/目标/约束数/算法/随机种子等脱敏元数据，不复制完整配置)。

### 管理

- 对象清理引入软删/保留期（0.2.0-B3 恢复路径）：`POST /api/admin/objects/cleanup` 执行不再立即物理删除，而是把无引用的孤儿对象标记为“待物理回收”，默认保留 7 天；保留期内文件保留、内容可读，管理员可经 `POST /api/admin/objects/restore` 恢复误清理对象，对象重新获得 owner 引用时也会自动恢复。新增 `GET /api/admin/objects/pending` 查看“已删除待回收”清单，`POST /api/admin/objects/purge` 只对已过保留期的待回收对象执行物理回收（先 `dry_run` 预览再执行）；`reconcile` 巡检会兜底物理回收到期对象。

### 设备数据文件契约(`ies.device-data` 1.0.0)

- 发布 `ies.device-data` `1.0.0` 机器可读 schema(`backend/iesplan/devices/schema/device-data-v1.0.0.schema.json`)与唯一纯函数规范化器(`iesplan.devices.datacontract.canonicalize_device_data`)。
- CSV 元数据(`# schema`/`# schema_version`/`# dataset_id`/`# device_model`/`# series_mode`/`# resolution`/`# timestamp_mode`/`# unit.<column>`)与方言(UTF-8/LF/英文逗号/RFC 4180/小数 `.`/布尔 `true|false`/禁 NaN/Inf/公式/区域化数字/千位分隔符)校验。
- `timestamp_mode=fixed_offset` 必须声明 `fixed_utc_offset_minutes`(-840..840)，不依赖机器时区/夏令时；`series_mode=periodic` 必须声明 `period`(day|week|year)。
- 列声明与设备模型核对：未声明列/重复列拒绝、必需列缺失拒绝、列单位量纲不一致拒绝；规范输出按模型声明顺序排列。
- 时间轴：timeline 时间戳严格递增无重复、与分辨率对齐、同文件不混用带Z/带偏移/无偏移；utc 用 RFC 3339 带 Z，fixed_offset 由文件级偏移唯一换算 UTC。
- 数值按设备模型 value_type/范围/有限性校验：超范围阻断不截断；缺值未在模型中声明阻断；不静默删行/补零/前值填充/解析失败变空集。
- 规范化产物保留原始文件 SHA-256 与规范表格 SHA-256；同一语义输入得到同一规范摘要。
- 新增 DATA-META-* / DATA-DIAL-* / DATA-COL-003..006 / DATA-VAL-* / DATA-TIME-* / DATA-ARR-001 / DATA-SUM-001 诊断码。
- 包内设备 CSV 与 GUI 上传共用同一 `ies.device-data` 规范化流程(`datacontract.normalize_upload_csv` / `datacontract.canonical_table_bytes`)：时区(UTC 带 Z)、时间轴(严格递增/步长对齐)、单位(量纲一致)、缺失值(模型策略)、数组长度(行数一致)统一校验；`devices.profile.load_profile_columns` 与 GUI 上传均经同一规范化器，手写 CSV 与上传对同一内容产生同一规范摘要。
- 迁移内置设备目录 CSV(`electric_load/heat_load/cooling_load`)到 `ies.device-data` `1.0.0` 格式：数据行原样保留、前插标准元数据头、迁移后全量校验通过才写回；迁移回执(`catalog/migration-receipt-0.6.0.json`)记录迁移文件、旧/新 SHA-256、行数、列声明与校验结果；后续装配只持有已校验的内容引用(`dataset_version_id`/`content_hash`)，不依赖上传文件名。

## 0.1.0 — 开发基线

> 发布日期：2026-08-22；发布状态：正式稳定版之前的开发版本

### 新增

- 建立“使用者指南 / 开发者指南 / 更新日志”三个并列正式文档入口。
- 新增面向零基础 GUI 用户的项目、建模、数据、配置、任务、结果和管理手册。
- 新增系统架构、领域追溯、公共契约、扩展、前端、部署、贡献和发布蓝图。
- 新增按三段式版本组织的 Roadmap 与发布门禁。
- 新增可直接手写的设备模型 YAML、设备数据 CSV 和装配 YAML `1.0.0` 目标标准，以及生成器产出的 Solver Bundle 标准。
- 新增计算生成器与求解运行时模块手册，分别说明问题构造、结构化命令、隔离执行和结果适配。

### 变更

- 重构项目 README，以简明列表突出多能模型、边—端物理语义、模块化组合和规划分析等特点，并用“待实现”标记 Roadmap 亮点。
- 将本地 AI 定位为智能化数据准备和一键生成完整规划报告的实现方式，不再作为独立 Roadmap 功能。
- 明确 `backend/pyproject.toml` 是产品版本唯一权威源。
- 正式文档统一围绕 `manual/` 维护，帮助中心直接发布同一份正文。
- `docs/` 只保留 Review 导航；旧规格、合同、路线图、设计输入和开发工作流移入只读归档。
- 架构审查的稳定意图被提炼进开发者指南；与架构宪法冲突的旧定案不再作为开发输入。
- 开发者指南由原则汇总扩展为分模块开发手册；组合根、Core、设备、建模、装配、计算、财务、分析、存储、应用、API、Worker 和持久化均明确作用、输入输出、开发流程、失败语义与完成标准。
- 经 ADR-0003 将目标计算边界调整为 `ValidatedAssemblyArtifact → GeneratorProvider → Solver Bundle → SolverRuntime → ResultAdapter → ComputeResult`；装配文件不包含命令，单位转换由 GeneratorProvider 承担。
- Roadmap 改为只保留尚未实现的工作；完成并验收的事项立即移入 `Unreleased`，不在 Roadmap 保留完成标记或历史副本。
- `0.1.0` 完成后，Roadmap 从下一功能版本 `0.2.0` 开始；合并 AI 能力时不提前后续功能的既定版本号。

### 当前实现说明

- 本次只修改文档和开发蓝图，没有修改设备加载、CSV 导入、装配、计算或 Worker 代码。
- 四种 `1.0.0` 文件契约与 generator/runtime 边界属于生效目标契约；当前目录文件和计算路径尚不能据此宣称兼容，代码迁移按 Roadmap `0.x` 顺序分步进行。

### 修复

- 修正中文使用者指南的失效相对链接。
- 删除或改正超过当前 GUI 能力的说明，包括独立项目版本发布、项目包导入、人工评估和未提供的管理操作。
- 区分产品版本、项目版本、数据版本、配置版本、计算快照和结果证据。

### 移除

- 移除活跃规格、实施计划和历史工作流作为当前权威开发文档的地位；内容仍保存在归档中。

### 已有能力

- 用户登录、首次改密、项目创建与访问控制；
- 图形化系统建模、数据上传与绑定、计算配置和完整校验；
- 方案计算、规划优化、不确定性分析和批量分析任务；
- 结果、四维系统评估、Excel 报告和完整项目包导出；
- 中文和英文界面、简体中文帮助中心；
- Docker 化 API、Worker、前端、数据库、缓存和对象存储运行环境。

### 当前边界

该版本用于建立版本化开发基线。安全 Review、公开契约收敛、历史结果追溯、候选方案应用、项目版本 GUI、导入恢复和全链路验收仍需按 Roadmap 完成，不能视为 `1.0.0` 发布候选。
