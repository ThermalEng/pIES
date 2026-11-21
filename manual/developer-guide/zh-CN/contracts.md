# 公共契约

> 文档状态：生效蓝图；规范版本：1.0.0；上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)

本章规定跨进程、跨模块及前后端之间共享的基础传输与业务输入契约。具体 HTTP 路径与字段清单以运行版本的 OpenAPI 与 Schema 为准，手册不维护第二份接口表。

## JSON 与数据类型

- 编码与媒体类型：UTF-8、`application/json`。
- 命名约定：字段名与枚举值统一使用小写 `snake_case`。
- 标识符：对外实体 ID 为不透明十进制字符串（禁止算术运算）；扩展与 Provider ID 使用带版本号的命名空间字符串（如 `ies.generator.highs_milp@1.0.0`）。
- 数值表示：精确金额与结算费率使用十进制定点字符串；连续物理量使用有限 JSON number（禁止 `NaN`、`Infinity`）；比例统一使用 `0..1` 规范值；整数计数与年份使用 JSON integer。
- 时间戳：HTTP、事件与元数据统一使用带 `Z` 的 ISO 8601 UTC 字符串；计算序列与数据 CSV 使用从 `0` 开始的连续 `step` 序号，不引入时区语义。
- 长期结构：均声明 `schema_version`；缺失、`null` 与加载失败必须严格区分，不得用空数组或默认值掩盖错误。

## 成功与错误

### 响应信封

成功资源响应顶层采用**一级命名键包装**，以键名直接表达资源语义，禁止使用 `{data, meta}` 等通用包装，也不得在同一端点混用裸对象与包装对象：

- 单资源 / 复合视图：`{project, draft, versions, my_role}`、`{task, replayed, hint}`、`{result}`、`{assessment}`、`{report, stored}` 等；
- 列表与分页：`{items, total}` 或 `{items, next_cursor}`；
- 动作响应：`{ok, ...}`；无正文操作返回 HTTP `204 No Content`。

所有 JSON 端点的允许顶层键集在 API 层集中登记并受协议基线测试约束。

### 标准错误信封

所有错误响应使用统一结构：

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

- `code` 采用 `<DOMAIN>-<CATEGORY>-<NNN>`（如 `API` / `PROJ` / `TASK` / `DATA` / `CONFIG` / `OBJ` / `PERM` / `AUTH`），新诊断码须集中登记。
- 同一个 `code` 可对应多个 `message_key`，但每个 `message_key` 必须绑定唯一的 `code`。
- `fix_hint_key` 无建议时固定为空字符串 `""`；堆栈、SQL、宿主机路径和凭证禁止输出到客户端。

## HTTP 语义

- `GET` 只读且幂等；`POST` 用于创建资源或执行命令；`PUT` 完整替换；`PATCH` 显式部分更新；`DELETE` 执行生命周期删除。
- 异步受理返回 `202`；可重试写操作携带幂等键；并发编辑通过 `expected_revision` 实现乐观锁，冲突时返回 `409`。
- 上传端点必须声明媒体类型与大小上限；导出接口返回 `object_id` 与文件名，下载通过 `GET /api/projects/{id}/exports/{kind}/{object_id}/download` 校验当前会话与项目权限后返回受控字节流，客户端不拼接对象存储路径。

| 状态码 | 触发条件 | 典型错误码前缀 |
|---|---|---|
| `200 / 201 / 204` | 正常成功 / 创建成功 / 无正文成功 | — |
| `400` | 请求体业务内容校验失败（如 CSV 行数不足、时间戳乱序） | `*-VAL-*`、`DATA-VAL-*` |
| `401` | 未认证或会话已失效 | `AUTH-*-001` |
| `403` | 已认证但无权限 | `PERM-DENIED-*` |
| `404` | 资源或路由不存在 | `RES-MISS-*`、`API-NF-*` |
| `409` | 状态冲突（乐观锁修订不匹配、重复操作或确认缺失） | `*-CONFLICT-*`、`ADMIN-CONFIRM-*` |
| `413` | 上传文件过大 | 文件大小错误码 |
| `422` | 请求体结构/Schema 不可处理（类型错误、必填缺失、参数越界） | `API-REQ-001`、`CONFIG-VAL-*` |
| `500` | 未捕获内部异常 | `API-500-001` |

**400 与 422 的分工**：请求结构、类型或 Schema 校验失败使用 `422`；请求结构合法但业务内容不符合领域规则（如数据时轴冲突、引用语义不成立）使用 `400`。

## 单位、时间与集合

- **三层单位边界**：前端 mapper 仅负责表单字符串、百分比与简单展示单位换算；后端及装配证明规范业务单位与量纲兼容；业务单位到求解器内部单位（SI 等）的换算只在 `GeneratorProvider` 边界发生，反向换算只在 `ResultAdapter` 边界发生。公共表达式支持 `能量 = 功率 × 时间` 的自动量纲约简。
- **时间基准**：项目不设立时区；创建项目时固定的 `resolution`、`scenario_mode` 与 `start_year` 唯一确定全周期点数（闰年逐年按公历推导）。`data_repeat` 将完整日、周或年序列作为重复基线，`data_full` 必须原样覆盖全运营期，`data_predict` 按预测目标生成全周期序列。所有输入在物化前均保持各自来源分辨率并从 `step=0` 连续递增。
- **集合与大数组**：需确定性输出的集合按稳定业务键排序；大型逐步计算曲线通过独立分页接口读取，不无条件嵌入摘要 DTO。

### 求解步序列读取

结果视图中的 `hourly_refs`（每组 `{rows, series}`）与结果对象 `meta.series` 采用以下判别联合区分设备序列与系统方程辅助序列，`series_id` 在同一次结果中共享唯一命名空间：

| `kind` | 完整目录条目字段 | 说明 |
|---|---|---|
| `device` | `{kind: "device", series_id, instance_id, variable_id, unit}` | 身份对应装配实例与设备变量 |
| `system` | `{kind: "system", series_id, block, variable, unit}` | 身份对应 `system_equations` 块与辅助变量，不含实例字段 |

逐时读取端点 `GET /api/projects/{project_id}/tasks/{task_id}/result/hourly` 通过 `field`（即 `series_id`）与步号窗口 `[start, end)` 分页拉取，返回 `{field, unit, start, end, values, next_start, total_rows}` 及对应 `kind` 身份字段；当 `next_start` 为 `null` 时表示读取完整。Excel 导出中的步序列目录沿用 `series_id, kind, instance_id, variable_id, block, variable, unit, rows` 统一列头。

## 快照与异步契约

计算类任务提交时在快照 `CalcSnapshot@1.0.0` 中冻结以下独立事实：

1. 已校验的规范装配产物、按实例冻结的设备数学贡献、绑定数据集与价格对象引用；
2. 所选已保存计算配置 revision 及其指向的 `Generator`、`Solver`、`Executor` 与 `ResultAdapter` 精确版本；
3. **显式评价确认 `evaluation`**：作为任务提交的顶层结构化字段，以下各字段均无默认值：

| 字段 | 类型与约束 |
|---|---|
| `evaluation_years` | 正整数，财务评价总年限，必须等于 `construction_period + operation_years` |
| `construction_period` | 非负整数年，不得为浮点数 |
| `operation_years` | 正整数，投产后的运营年限 |
| `discount_rate` | 无量纲比例（`0..1`，`0` 合法） |
| `construction_payments` | 建设付款比例列表 `[{t, ratio}]`；`t` 为自开工起的整数年 `0..construction_period`，`ratio` 按十进制精确求和须为 `1.0` |
| `maintenance_expenses` | 年度维护/换新清单 `[{year, currency, amount, terms?}]`；`year` 为评价年（`construction_period+1..evaluation_years`），支持空列表 `[]` |
| `comparison` | 可选评价比较输入，省略等价于 `{mode: "absolute"}` |

服务端提交时记录 `confirmed_by` 与 `confirmed_at`，并在提交边界一次性核对财务规则与生成器能力声明。应用方案（`POST /api/projects/{id}/apply-result`）基于任务快照对应的来源版本工作状态派生新版本，携带打开确认弹窗时捕获的 `expected_revision` 检查并发冲突。

任务结果查询（`GET /projects/{id}/tasks/{task_id}/result`）直接投影同一冻结证据：

- `solver_summary`：包含 `termination_reason`、`math_status`、`has_feasible_solution`、`objective_value`、`objective_unit`、`best_bound` 与 `relative_gap`。
- `financial_evaluation`：存在可行解且已评价时按[封存评价块](modules/finance.md#封存评价块-financial_evaluation)结构返回，否则为 `null` 并通过 `financial_absence` 明确说明原因（`outcome_not_normal` / `solution_not_requested` / `missing_unknown`）。

### 评价范围与冻结基准

`evaluation.comparison` 用于声明有/无项目比较边界与外部基准事实，为判别联合：

| `mode` | 完整字段 | 语义 |
|---|---|---|
| `absolute` | `{mode: "absolute"}` | 评价当前完整项目绝对现金流 |
| `difference` | `{mode: "difference", baseline, facts, comparability}` | 双方按各自冻结税务规则分别计税形成税后现金流后，计算 `CF_c(x) - CF_b` |
| `reference` | `{mode: "reference", baseline, facts, comparability}` | 仅向财务金额或规划 `amount_relation` 提供基准常量，最终仍评价候选自身现金流 |

- **`baseline` 来源**：恰选其一：
  - `{kind: "task_result", task_id, evidence_id}`：引用已完成且含有效评价的已封存结果，禁止指向运行中任务或当前草稿；提交时从该证据的[查询投影](modules/worker.md#阶段检查点与恢复)一次性解析并冻结财务事实与来源（无索引时按现有投影规则流式读取），项目包导入时自动转为 `declared` 形态。
  - `{kind: "declared", currency, evaluation_years, annual_net_flows, facts}`：人工显式声明基准；`difference` 模式下 `annual_net_flows` 须提供 `E+1` 个金额字符串，`reference` 模式下固定为 `null`。
- **`comparability`**：必填 `{service_basis, differences}`，均为非空字符串，由确认人明确服务范围与已知差异。
- **`facts` 选择器**：用于为财务公式的 `kind: baseline` 引用提供局部别名。`task_result` 来源支持以下类型化选择器（`declared` 来源对应 `declared_fact` 引用其 `facts` 中 `{unit, axis, values}` 声明）：

| `kind` | 选择器结构与取值 |
|---|---|
| `operation` | `{kind: "operation", measure}`，`measure` 为 `energy_net` / `fixed_om` / `variable_om` / `maintenance` / `demand_charge` |
| `cashflow` | `{kind: "cashflow", measure: "net"}` |
| `amount` | `{kind: "amount", id}` |
| `movement` | `{kind: "movement", id, measure}`，`measure` 为 `gross` / `net_basis` / `net_revenue` / `paid` |
| `asset` | `{kind: "asset", id, measure}`，`measure` 为 `basis` / `depreciation` / `book_value` |
| `tax` | `{kind: "tax", source, measure}`，映射规范见[税额输出引用](formats/finance-relations.md#税额输出引用)与[封存对应表](formats/solver-bundle.md#税额引用与封存字段) |

**重要财务语义**：`difference` 模式固定采用**分别计税后求差**（`Tax(c) - Tax(b)` 通常不等于 `Tax(c - b)`），不提供“对现金差额重新套税率”的开关。使用 `comparison` 非 `absolute` 时，所选生成器须具备 `finance.frozen_baseline`（`difference` 另需 `finance.cashflow_difference` 及 `finance.financial_facts`）。

### 年度维护金额来源

`evaluation.maintenance_expenses` 中每笔条目为 `{year, currency, amount, terms?}`：

- `amount` 为非负十进制确定金额（纯关联费用填 `"0"`）；`terms` 为可选有序关联加项列表，省略或 `[]` 表示仅有确定金额。
- 关联项仅支持以下两种 `kind`（引用范围、量纲及生成器 `finance.affine_maintenance` 能力在提交边界检查）：

| `kind` | 必填字段与金额含义 |
|---|---|
| `scale_unit_cost` | `instance_id`、`property`、`unit_cost: {value, unit}`；按实例冻结属性或规划变量规模乘单位费用计取 |
| `cost_component_share` | `instance_id`、`component: "upfront_capex"`、`ratio`（`0..1`）；按该实例本课题实际建设总分量乘比例计取（不可引用 `existing` 沉没投资） |

每笔维护按冻结清单中的 `(year, position)`（同评价年内从 1 起顺序编号）作为唯一税务与折旧引用身份；组合项仍视为同一笔支付。资本化与费用扣除冲突规则见[财务金额来源](formats/finance-yaml.md#金额来源与折旧期间)。

## 契约演进原则

- 2.0 正式发布前，自有文件与内部扩展契约版本统一固定为 `1.0.0`，变更时直接同步生产者、消费者、样例与测试，不保留多版本兼容矩阵或旧字段别名。
- 正式发布后，向后兼容新增提升次版本（MINOR），缺陷修复提升修订版本（PATCH）；破坏性变更须先经 ADR 批准并提升主版本（MAJOR）。
- 前后端及模块契约测试必须覆盖序列化字节到正式后端解析入口的完整往返，验证关键业务数值、单位、ID 与标准错误信封。
