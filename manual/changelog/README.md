# 更新日志

本页记录已经实现的用户可感知行为、公共设计与迁移要求。新版本在上；尚未实现的开发顺序单独见 [Roadmap](roadmap.md)。

产品使用 `MAJOR.MINOR.PATCH` 三段式版本。当前产品版本为 `0.1.0`，当前开发目标为 `0.8.0`，首个正式稳定版本为 `1.0.0`。完整规则见[版本化与发布](../developer-guide/zh-CN/versioning-and-release.md)。

`Unreleased` 只保存已经完成并通过相应验收、但尚未正式发布的变化。一项工作写入这里时必须从 Roadmap 删除；部分完成只迁移已完成部分。这里不收录愿望、待办、目标状态或预计完成版本。

## Unreleased

### 规范装配产物(`ies.assembly` 1.0.0)

- 发布 `ies.assembly` `1.0.0` 机器可读 schema(`backend/iesplan/assembly/schema/assembly-1.0.0.schema.json`)：顶层 `schema`/`schema_version`/`assembly`/`time_axis`/`resources`/`devices`/`connections`/`constraints`/`calculation`/`outputs`/`extensions` 各节均必需（无内容写空 `{}`/`[]`），未知核心字段拒绝。
- 唯一规范化器(`iesplan.assembly.canonicalizer`，算法 `ies.assembly.canonical@1.0.0`)：固定顶层键序 + 嵌套键排序、时间统一换算为带 `Z` 的 UTC、`relative_file` 解析为内容寻址对象、数值唯一有限表示（整值浮点与整数同文本）、非有限值拒绝；规范文本为紧凑 JSON + LF，对规范字节计算 SHA-256。相同语义输入产生相同规范文本与摘要。
- 合法/非法手写样例(`backend/iesplan/assembly/samples/`)：合法样例覆盖全部节与相对文件资源；非法样例分别覆盖 schema 标识错误、未固定精确版本、宿主机路径、可执行字段、非法计算模式。
- 新增结构诊断码：`ASM-SYN-006`(schema 标识无法识别)、`ASM-SYN-007`(引用未固定精确版本，拒绝 latest/范围版本/未版本化别名)、`ASM-SYN-008`(禁止字段：shell/command/executable/函数模块路径/环境变量/凭证)、`ASM-SYN-009`(资源路径非法)；以及 `ASM-RES-001`/`ASM-CALC-001`/`ASM-CALC-002`/`ASM-OUT-001`/`ASM-ART-001`/`ASM-CONV-001`/`ASM-INPUT-006`。
- 成功产物为不可变 `ValidatedAssemblyArtifact`（规范文本 + `assembly_sha256` + 校验回执 `ValidationReceipt`）：回执记录校验器 ID/版本、schema、规范化算法 ID/版本、依赖锁、资源摘要与零阻断诊断；`verify()` 重算规范字节摘要核对三件套一致，不一致抛 `AssemblyValidationError`(422) 阻断计算。
- 统一校验入口(`iesplan.assembly.validator`)：四阶段校验（结构 → 模型与数据 → 图与系统 → 计算兼容），手写 `validate_assembly_text` 与 GUI 项目导出 `validate_project_export` 收敛到同一入口；成功只签发 `ValidatedAssemblyArtifact`，失败返回完整诊断列表且不产生任何 artifact。
- GUI 项目导出构造器(`iesplan.assembly.builder10`)：项目内容（设备/端口/连接/数据集绑定/计算配置）映射到 `ies.assembly` `1.0.0` 文档；`loss_rate > 0` 的连接自动包裹为 `ies.device.transport_pipe@<version>` 设备实例；计算字段显式（`mode`/`generator`/`solver`/`options`/`random_seed`），旧形态 `algorithm`/`tolerances` 通过推导与映射（legacy 求解器 `ies.solver.highs@1.7.2`）保持显式；`outputs` 派生为空列表（应用层可补）。
- 旧形态一次性迁移(`iesplan.assembly.migration`)：`FORMAT_VERSION = "1.0"` 的 `AssemblySpec` / 装配文本 → `ies.assembly` `1.0.0` 文档 + 迁移回执（`migration`/`from_format`/`to_schema`/`old_sha256`/`new_sha256`/`transformations`/`ok`/`diagnostics`）；迁移产物经同一 `validate_assembly_doc` 入口验证。旧形态无法唯一映射的字段（无模型精确版本、缺 `solver`、数据集缺 `sha256`/`media_type` 等）产生 `ASM-CONV-001` 阻断诊断，回执 `ok=False`；不发布半迁移状态。
- 生产计算入口收敛：任务下发在创建 `Task` / `CalcSnapshot` 前同步调用唯一 `validate_project_export` 闸门；校验失败返回阻断诊断且不留下任务或快照，不再旁路调用旧 `check_graph_inputs`。
- 持久输入收敛：`CalcSnapshot` 写入规范文本、`assembly_sha256` 和确定性校验回执三件套，旧 `assembly_text` 仅保留为历史审计列且新快照不再写入；Worker 执行前严格恢复并复验三件套，缺失、版本不匹配、含阻断诊断或摘要被篡改均拒绝执行。项目证据包同步导出三件套。
- 迁移边界：迁移后不再以可变 `AssemblySpec`/`CheckResult` 作为后续计算的持久输入；`MigrationResult.doc` 是不可变 `ies.assembly` `1.0.0` 文档，可经统一入口规范化为 `ValidatedAssemblyArtifact`。ASM 诊断码静态登记于核心注册表，装配模块不再在导入时修改核心状态。

### 设备模型与建模命令契约

- 发布 `ies.device-model` `1.0.0` 契约：机器可读 JSON Schema、唯一规范化规则（稳定键排序 + 规范字节 SHA-256 摘要）、定位到文件/字段/稳定诊断码的诊断，以及合法/非法手写样例。
- 设备 YAML 由旧 `type_id`/`function` 格式一次性迁移为 `schema`/`device`/`parameters`/`ports`/`data_inputs`/`states`/`model_commands`/`extensions` 结构；`function.package/function.entry` 替换为稳定 ModelCommand ID + 精确版本（`<command-id>@<exact-version>`），命令 ID 到实现的解析只存在于组合根与 modeling provider 内部。
- 设备公开 descriptor 收敛为深度不可变值对象（list→tuple、dict→MappingProxyType）；公开面不再暴露函数、包、模块或宿主机路径（`standard_csv_path` 移除，标准 csv 经 `get_profile_columns(type_id)` 门面读取）。
- 提供内置目录 YAML 的一次性迁移回执（文件清单 + 新旧规范摘要 + 校验结果）。

### 安全

- 为基于 Cookie 会话(`ies_session`, SameSite=Lax)的状态变更请求(POST/PUT/PATCH/DELETE)增加 CSRF 双源校验：浏览器请求优先校验 `Origin`、缺失回退 `Referer`，规范化后必须命中可信来源(`app_url` + `IESPLAN_CORS_ORIGINS` + 请求自身 Host 同源来源)，否则返回 403 `AUTH-CSRF-001`；Bearer 认证、无 Origin/Referer 的 API 客户端与只读请求不受影响。
- 为管理员危险维护操作增加二次确认与作用范围提示：`POST /api/admin/transfer-project`(所有权转移)与 `POST /api/admin/unlock-task`(任务解锁)请求须携带 `confirm=true` 才执行，未确认返回 409 `ADMIN-CONFIRM-REQUIRED` 并附影响范围(from_user/to_user/项目数)提示；所有权转移目标新增校验，必须是 active 且非管理员、非系统账号，避免把项目转给管理员/系统账号绕过"管理员维护只读"。不引入审批链。
- 管理员删除账号增加误操作防护（0.2.0 B1）：删除账号会级联软删其拥有的全部项目且不可恢复，现必须先调用 `POST /api/auth/users/{user_id}/delete-preview` 预览将受影响的项目清单（名称/ID/数量）并取得签名确认令牌，删除时携带 `{"confirm": true, "confirm_token": "..."}` 才会执行；缺少 confirm、令牌缺失/过期/伪造或预览后项目清单变化均返回 400 `AUTH-DEL-001`。前端账号管理在确认对话框中展示受影响项目清单。
- 项目删除确认强化(0.2.0 B4)：`DELETE /api/projects/{id}` 不再接受空布尔 `confirm: true` 单独确认，须提供与待删除项目名精确匹配的 `name` 或非空删除原因 `reason` 之一；`confirm` 字段仅为兼容旧调用方保留。审计记录确认方式与删除原因(脱敏)。前端删除确认对话框要求输入项目名才能确认。
- 部署不可变审计触发器(0.2.0 B4)：`init_db()` 在 PostgreSQL 下执行 `immutable_triggers.py` 的全部 DDL(`ALL_IMMUTABLE_TRIGGER_DDL` + `ALL_IMMUTABLE_REVOKE_DDL`)，为 12 张不可变表(含 `audit_log`/`auth_events`/`project_versions`/`calc_snapshots` 等)创建禁 UPDATE/DELETE 触发器与 `REVOKE UPDATE, DELETE ... FROM PUBLIC`，实现宪法 §16「关键变更保留不可变审计」；部署幂等(DROP FUNCTION IF EXISTS ... CASCADE 后重建)。SQLite 测试库跳过 PostgreSQL 触发器语法。
- 计算配置保存审计(0.2.0 B4)：`PUT /api/projects/{id}/config` 保存时写入 `config.saved` 审计记录(只含版本/变量数/目标/约束数/算法/随机种子等脱敏元数据，不复制完整配置)。

### 管理

- 对象清理引入软删/保留期（0.2.0-B3 恢复路径）：`POST /api/admin/objects/cleanup` 执行不再立即物理删除，而是把无引用的孤儿对象标记为“待物理回收”，默认保留 7 天；保留期内文件保留、内容可读，管理员可经 `POST /api/admin/objects/restore` 恢复误清理对象，对象重新获得 owner 引用时也会自动恢复。新增 `GET /api/admin/objects/pending` 查看“已删除待回收”清单，`POST /api/admin/objects/purge` 只对已过保留期的待回收对象执行物理回收（先 `dry_run` 预览再执行）；`reconcile` 巡检会兜底物理回收到期对象。
- 资源使用边界（0.2.0 A4）：新增全局 IP 限流（按进程内存滑动窗口，Redis 可用时跨 Worker 原子计数）与按用户/项目的上传配额门禁，超限返回 429 `API-RL-001` / 413 `API-QUOTA-001`；dataset 上传的 `meta`/`fields`/`provenance` 字段经白名单校验，未知键或畸形结构返回 400 `API-META-001`。本地开发与 e2e 默认宽松阈值（限流 120 次/分钟，配额 0 = 不限），通过 `IESPLAN_RATE_LIMIT_MAX_REQUESTS` / `IESPLAN_RATE_LIMIT_WINDOW_SECONDS` / `IESPLAN_UPLOAD_QUOTA_BYTES` / `IESPLAN_PROJECT_QUOTA_BYTES` 配置。
- 下载授权加固（0.2.0 A3）：`/api/exports` 的下载令牌除原 HMAC 签名外，新增对象归属校验——令牌载荷 `object_id` 必须真实存在，且对象必须被当前会话用户有权限访问的项目引用，否则 403 `AUTH-OBJ-001`；伪造 weak-secret 签发的令牌不再能越权下载。
- 任务结果查询 IDOR 修复（0.2.0）：`select_result_endpoint` 与 `results.py` `read_hourly` 增加 `ensure_task_belongs(db, project_id, task_id)` 校验，未授权项目任务返回 404 `RES-MISS-003`，阻断"猜测 task_id 跨项目读取结果"的路径。
- `/api/readyz` 脱敏（0.2.0 A3）：注册表初始化失败的原始异常串（可能含内部路径/凭证）只进日志，探针响应只给 `modeling_registry / detail: unavailable`，避免 503 响应泄露内部堆栈。
- 对象存储公开面收尾（0.4.0）：`ObjectHandle` 公开字段移除 `storage_path` / `ref_count`，业务模块只能经 `iesplan.storage` 公开门面传递 `ObjectId/ObjectHandle/ObjectOwner`；包导入的 `ImportProposal.source_path` 停止写值（可追溯性由 `source_object_id` 与 `source_hash` 承担）；reconcile 审计与 dry-run 报告不再含内部路径，主机绝对路径不进错误信封（§11/§16 路径泄漏清零）。替换存储适配器不再改变业务契约。

### 错误契约

- 错误响应统一为标准 8 字段信封 `{"error": {code, message_key, severity, blocking, params, location, fix_hint_key, ref_ids}}`（0.3.0）：所有非 2xx 响应（404 路由未找到、403 权限/CSRF、429 限流、413 配额、500 未未捕获）走 `iesplan/core/errors.py:error_envelope` 唯一权威构造器；`code` 格式 `DOMAIN-CATEGORY-NNN`，新码须在 `core/diagnostics.py NEW_DIAG_CODES` 登记。前端 `client.ts` 按 `parseErrorEnvelope` 解析后透传 `params.diagnostics` 等明细到 `ApiError.params`，页面不再特判裸 `{"detail"}`。
- `Diagnostic` 收敛为深度不可变公共类型（0.3.0 C1）：`@dataclass(frozen=True, slots=True)` + `object.__setattr__` 冻结，params/location 经递归只读包装（MappingProxyType + tuple），`ref_ids` 转 tuple；`to_dict()` 递归解冻为普通 dict/list 供 JSON 序列化；新增 `replace()` 与 `with_context(project_id/task_id/trace_id/source)` 派生方法。
- 删除空数据降级（0.3.0 C2）：系统模型域返回 `has_graph` 显式字段标识"未建模/已建模"；结果域新增 `evidence_status: no_evidence|available` 显式区分"任务未完成/已提交证据包"，无证据包时 `hourly_refs/metrics` 等字段为 null（前端不再猜测）；任务结果域缺证据包返回 404 `RES-MISS-003`，前端 `no_evidence` 时不渲染空卡片。
- 配置/数据集校验失败统一信封 + 阻塞语义（0.3.0 C3 + 收口）：`PUT /config` 校验失败返回 422 `CONFIG-VAL-001`，dataset 上传校验失败返回 400 `DATA-VAL-001`，诊断明细进 `params.diagnostics`；`DataValidationError` 显式 `blocking=True` 与配置 422 一致。
- FastAPI 请求体校验走标准信封（0.3.0 收口）：`RequestValidationError` 注册全局处理器，422 + `error_envelope(code=API-REQ-001, message_key=ies.error.invalid_request, params.errors=...)`，前端无需特判裸 `{"detail"}`。

### 协议门禁

- 公共协议测试基线（0.3.0 C4）：`backend/tests/test_protocol_baseline.py`（628 行）锁定 12 条错误路径的 8 字段信封 + 28 个端点的成功包装键集（AST 门禁禁止裸/包装并存）；新增 / 修改端点必须维持基线。
- 静态架构门禁（0.3.0 C5）：`backend/tests/test_architecture_gates.py` 白名单基线三门禁——`core` 不依赖业务模块、禁止跨模块私有符号导入、禁止 API 直接导入 ORM；新增违规直接断言失败，存量违规在白名单带 TODO，后续按宪法 §14.3 逐步整改。

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
