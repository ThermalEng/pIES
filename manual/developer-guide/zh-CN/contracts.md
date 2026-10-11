# 公共契约

> 文档状态：生效蓝图；规范版本：1.0.0；上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)

本章定义跨进程、跨模块和扩展可依赖的稳定语义。具体 HTTP 路径与字段表由运行版本的 OpenAPI/schema 生成，不在手册中维护第二份易漂移副本。

## JSON 与数据类型

- UTF-8、`application/json`；
- 字段统一使用 `snake_case`；
- 枚举值使用小写 `snake_case`；
- 对外实体 ID 作为不透明十进制字符串传输，不能做算术；
- 稳定扩展 ID 使用命名空间字符串；
- 精确金额和费率结算值使用十进制定点字符串；
- 连续物理量使用 JSON number，拒绝 `NaN` 和无穷值；
- HTTP、事件、数据库和证据元数据中的时间戳使用带 `Z` 的 ISO 8601 UTC；项目计算序列与人工 CSV 按[设备数据 CSV](formats/device-data-csv.md)只使用 `step`、采样间隔和点数，不引入时区；
- 长期结构必须声明 `schema_version`。
- 计算与存储内部表示（`float64`、`Decimal`/`NUMERIC`、整数主键、时间戳存储类型）由实现决定，不得泄漏到公开 DTO。

一个字段只能有一种未标记形态。缺失、空值和加载失败必须区分，不能用默认值或空数组掩盖错误。

## 成功与错误

成功资源响应顶层使用**一级命名键包装**，每个键名直接表达资源语义：

- 单资源响应：顶层为 `{<resource_name>, ...}`，例如 `{project, draft, versions, my_role}` / `{task, replayed, hint}` / `{result}` / `{assessment}` / `{report, stored}`。
- 列表 + 分页：顶层固定为 `{items, total}` 或 `{items, next_cursor}` 二键（具体由端点选择一种）。
- 动作响应（确认/状态变更/校验触发）：顶层为 `{ok, ...}`，必要时附 message_key/expires_at/result 等。
- 必要时可嵌套（如 `{project, draft, versions, my_role}`），嵌套键名同样遵循自我文档化原则。

**每个路由端点的允许顶层键集必须登记**在 [`iesplan/api/wrapper_keys.py`](../../../backend/iesplan/api/wrapper_keys.py) 的 `WRAPPER_KEYS` 集中登记表（对应错误信封 `NEW_DIAG_CODES` 的管理模式）：新增/修改端点先登记再改代码，协议基线测试 `test_wrapper_keys_registered`（AST 全量扫描，含间接返回端点）强制校验，未登记或键集超出登记即失败。非 JSON 响应端点（302 重定向 / 二进制下载）豁免。

禁止：

- `{data, meta}` 这种通用包装（破坏键名自我文档化，分散前端适配层决策）；
- 同一资源端点既返回裸对象又返回包装对象（裸/包装两版并存）—— 协议基线测试 [`backend/tests/test_protocol_baseline.py`](../../../backend/tests/test_protocol_baseline.py) 的 AST 门禁禁止新增此类违规；
- `{**data, "data": data}` 双前缀；
- 204 No Content 返回伪造 JSON。

错误统一使用以下标准信封（参见宪法 §8，构造器权威源 `iesplan/core/errors.py:error_envelope`）：

```json
{
  "error": {
    "code": "DOMAIN-CATEGORY-NNN",
    "message_key": "ies.error.example",
    "severity": "error",
    "blocking": true,
    "params": {},
    "location": null,
    "fix_hint_key": "",
    "ref_ids": []
  }
}
```

`code` 用于稳定分类（域-类别-三位序号），`message_key + params` 用于本地化，`location` 用于定位资源或字段。客户端不得接收堆栈、SQL、宿主机路径或凭证。

**`code` 格式**：`DOMAIN-CATEGORY-NNN`。`DOMAIN` 是 API 子域（API/PROJ/TASK/DATA/CONFIG/OBJ/PERM/AUTH），`CATEGORY` 是错误类别（REQ/VAL/NF/CONFLICT/SEC/QUOTA/MISS 等）。**同 code 可跨 message_key 复用**（同语义不同文案，由前端按 message_key 渲染），但**禁止跨 code 共享 message_key**（每个 message_key 必须绑定唯一 code）。新码须在 `core/diagnostics.py NEW_DIAG_CODES` 集中登记，未登记即在 `Diagnostic` 构造时抛 ValueError（包络码从 `core/errors.py` 走时不受此约束，但建议同样登记）。

`fix_hint_key` 字段：值为 `""` 表示无修复建议（非 null），与前端 `client.ts:217 ?? null` 容错路径兼容。

## HTTP 语义

- GET 只读且可重试；
- POST 创建资源或执行命令；
- PUT 完整替换，PATCH 显式部分更新；
- DELETE 执行明确生命周期操作；
- 创建使用 201，异步接受使用 202，无正文使用 204；
- 认证、授权、不存在、冲突和校验失败使用一致的标准状态码；
- 可重试写操作使用作用域明确的幂等键；
- 并发编辑使用 revision、ETag 或等价乐观锁。

**状态码全局选择规则**（ADR-0005）：

| 状态码 | 触发条件 | 典型码前缀 |
|---|---|---|
| 200 / 201 / 204 | 正常成功 / 创建成功 / 无正文成功 | — |
| 400 | 请求体**业务内容**校验失败（数据集行数/时间戳/缺失、配置业务字段越界） | `*-VAL-*` / `DATA-VAL-*` |
| 401 | 未认证 | `AUTH-*-001` |
| 403 | 已认证但无权限 / CSRF / 跨源 | `PERM-DENIED-*` / `AUTH-CSRF-*` |
| 404 | 资源不存在 / 路由不存在 | `RES-MISS-*` / `API-NF-*` |
| 409 | 资源状态冲突（重复/乐观锁失败/确认缺失） | `*-CONFLICT-*` / `ADMIN-CONFIRM-*` |
| 413 | 上传文件过大 / 配额超限 | `API-QUOTA-*` |
| 422 | 请求体**结构/语义**不可处理（Pydantic schema 错、配置变量类型错/算法参数越界） | `API-REQ-001` / `CONFIG-VAL-*` |
| 429 | 限流触发 | `API-RL-001` |
| 500 | 未捕获异常 | `API-500-001` |

**400 与 422 的核心区分**：400 用于**业务内容**校验失败（数据本身有问题，如上传 CSV 行数不够、时间戳乱序）；422 用于**请求体结构/语义**不可处理（schema 不匹配、配置变量类型错、算法参数越界、必填字段缺失）。同一域内选择必须保持一致；端点设计时参考本表并查阅同类端点历史示例。

**上传与下载错误**：413 用于文件大小/配额超限（封顶）；429 用于限流（重试导向）。两者都不视为校验错误，前端按 message_key 渲染。

上传必须声明媒体类型和大小约束。下载返回受控资源或短期授权，客户端不拼接存储路径。

**应用方案 POST /api/projects/{id}/apply-result（来源版本基线）**：请求 `{task_id, solution_id, name?, description?, expected_revision?}`，只接受任务引用与解标识，不接受浏览器侧参数补丁；服务端从任务快照解析来源版本与封存规划参数，在来源版本工作状态上写回并派生新版本（切换为当前），不叠加当前草稿；`expected_revision` 为打开确认框时捕获的当前草稿修订，发送前不重读，不符 → 409（不创建版本、不替换新编辑）；响应唯一 `{version, draft}`。具体字段表以运行版本 OpenAPI 为准。

## 单位边界

系统区分：

1. GUI 展示单位；
2. HTTP/业务契约的规范单位；
3. 生成器/求解器内部单位。

前端 mapper 完成表单字符串、百分比和简单展示换算；后端校验单位、量纲、范围和领域规则；装配阶段证明单位兼容并保留明确业务单位，业务单位到求解器内部单位只在 GeneratorProvider 边界发生，结果反向换算只在 ResultAdapter 边界发生。设备方程解析、运行时和业务服务中不得散落隐式换算常量。

## 时间与集合

项目计算不建立时区语义。项目创建时固定时间分辨率、单场景模式与运行起始年（无闰年字段，闰年逐年按公历推导）；计算序列使用从零开始的连续 `step`，并声明分辨率、点数和单位。`data_repeat` 的完整来源序列整体作为重复基线，可为完整日、周或年且不另设周期字段；`data_full` 必须原样覆盖全周期全部点数；`data_predict` 的输入覆盖范围由预测目标和算法契约规定，输出覆盖完整运营期。各来源必须与项目基线分辨率一致并保持自身 `step` 连续。装配前不做重采样、插值、聚合、融合或补齐；计算阶段物化后，所有计算序列的点数和 `step` 必须完全一致（全周期点数由分辨率、起始年与已确认运营年限 `operation_years` 推导）。

列表是否有序必须显式。需稳定排序的集合按稳定键规范化；JSON 字段顺序没有业务语义。大型逐时数组通过字段、时间范围或对象引用读取，不无条件嵌入普通 DTO。

## 快照与异步契约

长时间计算、分析、导入和报告生成使用任务。任务请求携带幂等信息，消费不可变快照，并以不可变证据或对象提交结果。

计算类任务必须明确引用本项目已保存的计算配置 revision；提交时冻结该 revision 的完整配置与来源，后续保存新 revision 不改变已提交任务。缺失或不存在的引用不得回退到当前草稿、最新配置或默认配置；任务级参数不得覆盖计算配置正文。具体 HTTP 字段和错误信封以运行版本的 OpenAPI 为准。

计算类任务提交的类型化输入包含显式评价确认 `evaluation`，以任务提交顶层字段提交，不在宽泛任务参数字典内，以下字段均无业务默认值：

| 字段 | 公开输入语义 |
| --- | --- |
| `evaluation_years` | 正整数，财务评价总年限，包含建设期 |
| `construction_period` | 非负整数年，不接受浮点年或起止月 |
| `operation_years` | 正整数，投产后的运营年限；必须满足 `evaluation_years = construction_period + operation_years` |
| `discount_rate` | 无量纲比例，API 规范值 `0..1`，零合法 |
| `construction_payments` | 显式付款比例清单 `[{t: integer, ratio: number}]`；`t` 为自开工起的整数年时点，`0..construction_period`；比例各在 `0..1`，按十进制语义求和须精确等于 `1.0`，不猜金额、不自动均摊 |
| `maintenance_expenses` | 显式维护／换新清单 `[{year: integer, amount: Decimal, currency: string}]`；`year` 为从开工起的财务发生年（对应 `CF[year]`，范围 `construction_period+1..evaluation_years`），不是公历年份；金额为非负十进制定点字符串，币种与有效财务配置一致；空清单明确表示无额外支出 |

服务端记录 `confirmed_by`/`confirmed_at`，将上述确认及运行年度边界随任务冻结。无确认、时间关系不成立、付款比例不成立或必要财务参数未确定，在公开提交边界阻断；自由字典式 `baseline-confirm` 审计记录不作为参数来源。冻结块仍为 `evaluation`，目标快照 schema 升为 `4.0.0`，不保留旧字段语义或双轨读取；评价确认属财务输入，不新增第六类输入，Generator 五项直接输入不变。建设支出与运营净流的时点映射以[财务计算](modules/finance.md)为唯一语义正文。

Worker 只有在命令可解析、租约有效且写入资格仍成立时才能提交进度和结果。失败保留结构化诊断；取消、超时和重试遵守公开状态转换。

任务结果读取（`GET /projects/{id}/tasks/{task_id}/result` 的 `result` 视图）复用同一证据内容，不另建财务结果存储、不重新运行评价：有证据时原样投影 `financial_evaluation`（形态见[财务计算](modules/finance.md)“封存评价块”，无块时为 null）；同时给出缺块原因 `financial_absence`（有块时恒为 null）：`outcome_not_normal` 表示任务业务结局非正常完成（未求解或无可行方案）；`solution_not_requested` 表示快照 `outputs` 未请求 `solution`（解数据未发布）；`missing_unknown` 表示请求了 `solution` 但该证据未封存财务评价（如历史证据）。展示与导出不得把缺块猜为未请求、前两种原因伪装为评价通过。

## 兼容与演进

产品正式发布前，错误的旧字段、响应并集和静默 fallback 应一次性删除。正式发布后：

- 兼容新增按次版本演进；
- 缺陷修复按修订版本演进；
- 不兼容公开契约变化提升主版本，并提供迁移说明；
- schema、文件格式、provider 和产品版本各自拥有独立版本，不能机械同步。

版本计划、发布操作与完成状态不属于本公共契约；它们由独立的版本与更新记录维护。

## 公共文件契约

核心计算和离线交换继续使用四种版本化契约：设备模型 YAML、设备数据 CSV、装配 YAML 和 Solver Bundle。用户算法扩展另使用[算法插件包](formats/algorithm-plugin-package.md)交付契约；它由通用计算 provider 在隔离环境解释，不能代替 Solver Bundle。完整标准见[文件格式标准](file-formats.md)。

- 前三种允许人工编写，但必须先经过对应 schema 校验和规范化；
- 装配成功产出 `ValidatedAssemblyArtifact`，不能把原始 YAML `dict` 直接交给计算；
- GeneratorProvider 只从规范装配生成 Solver Bundle；
- SolverRuntime 只按 Bundle 的结构化命令执行，不解释业务字段；
- 设备、数据和装配文件禁止实现模块路径、shell 字符串、宿主机绝对路径和凭证；算法插件入口只能出现在插件 manifest 的受控字段中，并且只由隔离运行器解释。

四种核心 schema 与算法插件包 schema 分别独立版本化。不能识别的 MAJOR 必须拒绝，不得以“尽可能解析”方式继续任务。

前端生成文件的契约测试必须把实际序列化字节提交给正式后端文件入口，并验证关键业务值完整往返。解析或领域错误统一进入标准错误信封；测试专用解析器、前端预解析对象和直接构造领域模型都不能替代这条链路。

## 设计一个新 contract

不要从现有 ORM 或页面表单直接复制字段。按下面顺序设计：

1. 写出调用方要完成的动作和接收方承诺的结果；
2. 为 contract 指定唯一所有者和版本；
3. 区分命令、查询、领域结果、应用结果和 HTTP DTO；
4. 对每个字段说明类型、单位、空值、顺序和稳定性；
5. 定义成功、业务失败、冲突和内部失败；
6. 决定幂等、revision、分页或异步语义；
7. 用前后端/模块协议测试证明关键值完整往返；
8. 再从权威 schema 生成具体字段文档。

如果同一字段需要“旧字符串或新对象都能用”，应先完成调用方迁移，再一次性采用唯一形态，而不是把并集写进正式 contract。

## Contract 在各层的变化

| 边界 | 输入 | 输出 | 不应跨越的内容 |
|---|---|---|---|
| GUI ↔ feature | FormState | FrontendModel | 原始 HTTP 与业务数据库概念 |
| feature ↔ API | Request/Response Contract | JSON/文件 | React 状态和显示单位 |
| API ↔ application | Actor + Command | ApplicationResult | Cookie、Response、ORM |
| application ↔ domain | 领域命令/值对象 | 领域结果/诊断 | 跨模块私有实现 |
| assembly/modeling/finance/computation → generator | 装配产物、设备数学贡献、物化序列、有效财务快照、`CalculationConfig` 五项直接输入 | Solver Bundle | 原始 YAML、项目草稿、ORM、存储路径 |
| generator ↔ runtime | Solver Bundle | `ExecutionReceipt` + 原始输出 | 设备私有实现、数据库、shell 字符串 |
| runtime ↔ result adapter | 只读 Bundle、回执、声明输出 | `ComputeResult` | 进程句柄、当前项目、隐式重试 |
| Worker ↔ results | attempt + evidence contract | 写入回执 | 无 fencing 的裸结果 |

同一个业务概念可以在不同边界有不同类型，但转换必须存在于明确 mapper 中，不能依靠字段名碰巧相同。

## 示例：保存计算配置

前端 FormState 可保存百分数文本和未完成输入；提交 mapper 把它变为规范比例与明确枚举的请求 DTO。API 只校验传输结构并形成应用命令；application 校验权限与 revision；配置领域校验目标、变量和算法兼容性；成功后返回新的配置修订和诊断摘要。任一失败都保持原表单，并通过标准错误或诊断回到对应字段。

这个流程中不存在“后端替前端除以 100”、路由直接写 ORM、页面接受两种响应形状或配置失败后返回旧数据。

## 完成标准

- contract 的所有者、调用方和版本明确；
- 输入输出、单位、空值、失败和并发语义完整；
- 具体字段由 schema 生成，不在多份手册重复；
- 关键 ID、金额、单位、枚举和错误参数有跨边界往返测试；
- 旧形态已迁移删除，没有静默 fallback。
