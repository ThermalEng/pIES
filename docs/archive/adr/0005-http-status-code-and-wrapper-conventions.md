# ADR-0005：HTTP 状态码、响应包装与错误码全局统一规则

> 状态：已批准、文档已实施  
> 决策日期：2026-08-23  
> 批准依据：项目所有者明确同意  
> 修订条款：架构宪法 §8.2/§8.3/§8.4；开发者指南 contracts.md / development.md；roadmap 0.3.0/0.3.x  

## 问题

0.2.0-0.4.0 批次落地错误信封与成功响应包装后，现状存在三类**字面 vs 实际**的不一致，已记录在终检报告中：

1. **§8.2 成功响应**：宪法字面要求 `{"data": {}, "meta": {}}`，实际全 API 用**命名键包装**（`{project}` / `{items, next_cursor}` / `{ok}` / `{task, replayed, duplicate, hint}` 等），C4 协议基线测试已锁定现状形状；objects 域有一处 `{data, meta}` 孤例。
2. **状态码选择**：config 语义校验用 422 `CONFIG-VAL-001`、dataset 内容校验用 400 `DATA-VAL-001`、FastAPI 请求体结构校验曾用 422 裸 `{"detail": [...]}`（0.3.0 收口后已转标准信封，仍为 422），分域并存规则未在 manual 中成文。
3. **error_envelope.code 复用**：同一码 `API-REQ-001` 同时用于业务"请求无效"与 FastAPI 请求体结构校验，分歧依赖 message_key；规则未在 manual 中成文。

这些不一致若继续悬置，后果是：(a) 后续开发者读宪法会以为代码违宪；(b) 新端点选择状态码与包装键时无统一依据，分散决策造成新偏差；(c) ADR/roadmap/宪法/代码四处口径不一。

## 决策

**D1 成功响应一律使用一级命名键包装**：
- 成功资源响应顶层只允许 1-3 个键，每个键名**必须直接表达资源语义**（如 `{project}`/`{task}`/`{items, next_cursor}`/`{ok, ...}`）；禁止 `{data, meta}` 这种"通用包装"。
- 一级键名集合**一域一约定**：列表 + 分页用 `{items, total}` 或 `{items, next_cursor}`，单资源用 `{<resource_name>, ...}`，动作响应（OK/确认/状态变更）用 `{ok, ...}`。
- 必要时可嵌套（如 `{project, draft, versions, my_role}`），嵌套键名同样遵循"自我文档化"原则。
- 宪法 §8.2 字面要求 `{"data", "meta"}` 视为**已被本 ADR 替代**，宪法该节改为指向 contracts.md。

**D2 HTTP 状态码全局统一规则**（写入 contracts.md "HTTP 语义"节）：

| 状态码 | 触发条件 | 码前缀 |
|---|---|---|
| 200/201/204 | 正常成功 / 创建成功 / 无正文成功 | — |
| 400 | 请求体**业务内容**校验失败（数据集/上传/导入等） | `*-VAL-*` / `DATA-VAL-*` |
| 401 | 未认证 | `AUTH-*-001` |
| 403 | 已认证但无权限 / CSRF / 跨源 | `PERM-DENIED-*` / `AUTH-CSRF-*` |
| 404 | 资源不存在 / 路由不存在 | `RES-MISS-*` / `API-NF-*` |
| 409 | 资源状态冲突（重复/乐观锁失败/确认缺失） | `*-CONFLICT-*` / `ADMIN-CONFIRM-*` |
| 413 | 上传文件过大 / 配额超限 | `API-QUOTA-*` |
| 422 | 请求体**结构/语义**不可处理（Pydantic schema 错、配置变量越界/类型错/算法不兼容） | `API-REQ-001` / `CONFIG-VAL-*` |
| 429 | 限流触发 | `API-RL-001` |
| 500 | 未捕获异常 | `API-500-001` |

关键区分：**400 = 业务内容校验失败**（上传的 CSV 行数不够、时间戳乱序），**422 = 请求体结构/语义不可处理**（schema 不匹配、配置变量类型错、算法参数越界）。两个状态码语义不重叠，由具体 domain 选其一，写入 contracts.md。

**D3 error_envelope.code 三段式稳定约束**：
- 格式 `DOMAIN-CATEGORY-NNN`：`DOMAIN` 是 API 子域（API/项目 PROJ/任务 TASK/数据集 DATA/计算 CONFIG/对象 OBJ/权限 PERM/认证 AUTH）；`CATEGORY` 是错误类别（REQ 请求无效/VAL 校验/NF 路由未找到/CONFLICT 冲突/SEC 安全/QUOTA 配额/MISS 资源缺失）；`NNN` 是同域同类下的三位序号。
- 同一 `code` **允许跨 message_key 复用**（如 `API-REQ-001` 既用于"业务请求无效"也用于"请求体结构错"，靠 message_key 区分文案），但**禁止跨 code 共享 message_key**（一个 message_key 必须绑定唯一 code）。
- 新码须在 `core/diagnostics.py NEW_DIAG_CODES` 登记（已存在的强制约束）。

**D4 宪法 §8.2/§8.3/§8.4 简化**：
- §8.2 字面要求改为指针，详细规则指向 contracts.md。
- §8.3 信封 8 字段不变（已正确），增补 D3 的 code 格式约束指针。
- §8.4 状态码语义改为指针，详细表格在 contracts.md。

## 影响

- **宪法**：§8.2 改为指针；§8.3/§8.4 增补指针。无需新增条款。
- **contracts.md**：扩展"成功与错误"节为命名键规则；扩展"HTTP 语义"节为 D2 状态码表格。
- **development.md**：在"架构门禁"或"实现节奏"节补"选择状态码与包装键"流程，开发者实现端点前先查 contracts.md。
- **roadmap**：0.3.0 第 2 条"将现有 API 依次迁移到唯一成功包装"已完成（错误信封部分），成功包装部分移入"后续版本段"，注明 ADR-0005 决策与代码现状。
- **代码**：0.3.0 收口后无新代码改动；C4 协议基线测试保持不变（已锁定 D1 现状）；终检报告中的"RequestValidationError 422"项关闭。
- **C4 test_protocol_baseline**：原"已知偏差"测试已改写为 `test_pydantic_request_validation_422_uses_envelope`，验证 D2 表格中 422 路径。

## 被否决方案

- **回滚代码至 `{"data", "meta"}` 通用包装**：破坏性变更，影响前端 asItems/oneOf 适配层、证据包、快照、配置等所有 C4 锁定的端点；前端一次性解包、meta 天然承载分页等论点在项目当前规模下不构成足够收益。
- **保持 §8.2 字面要求与代码现状并列**：违反 §17 "ADR 与宪法/开发者指南必须一致"原则，后续开发者读法分歧。
- **把所有错误都归到 400**：会丢失 422 语义信号，集成方无法按状态码分支（重试/修复请求结构）。
- **为每种新错误类型新增独立 code**（避免 D3 复用）：码空间膨胀到难以维护，且 message_key 才是用户文案的唯一依据。

## 回滚方案

回滚 ADR-0005 = 撤销 contracts.md/development.md/roadmap.md 改动 + 宪法 §8.2 还原字面 + C4 协议基线测试从命名键断言改为 `{data, meta}` 断言（约 28 处）+ 全量回归。前端 client.ts asItems/oneOf 适配层可保留不动（其 `oneOf(body, key)` 在两种形状下都能工作，只是需要重新适配）。预计工作量 1-2 个工作批次。