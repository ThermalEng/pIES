# Solver Bundle

> 契约标识：`ies.solver-bundle`；目标 `schema_version`：`"1.0.0"`；生产者：`GeneratorProvider`；消费者：`SolverRuntime` 与 `ResultAdapter`

Solver Bundle 是计算生成与进程隔离执行之间的不可变交接包：生成器将已校验的装配、配置、物化序列与财务规则编译为求解器输入文件和结构化运行命令；运行时只负责按命令安全执行并采集结果，使“数学构模”和“安全进程控制”彻底解耦。

## 目录结构与 Manifest

```text
solver-bundle/
├── bundle.yaml
├── input/
│   ├── problem.mps
│   ├── parameters.json
│   └── parameters.jsonl
└── output/
```

`bundle.yaml` 声明包元数据、产出的公开求解契约标识、规范输入相对路径清单与允许写入的输出声明（`output/` 在生成期为空目录，任意声明消费该公开契约的 `solver` 容器均可接入执行，不再写死特定求解器或适配器 ID）：

```yaml
schema: ies.solver-bundle
schema_version: "1.0.0"

bundle:
  generator: ies.generator.highs_milp@1.0.0
  contract: ies.solver-bundle.mps@1.0.0

inputs:
  - path: input/problem.mps
    media_type: application/mps
  - path: input/parameters.json
    media_type: application/json
  - path: input/parameters.jsonl
    media_type: application/x-ndjson

outputs:
  - path: output/primal_solution.json
    media_type: application/json
    required: true

extensions: {}
```

### 分块内容与生命周期

Bundle 内输入与输出文件统一采用可重复按块读取的只读内容协议（`Content.iter_chunks() -> Iterator[bytes]`），避免在生成、检查点持久化、运行时复制与结果适配过程中把大文件整包拼装进内存：

| 阶段 | 所有权与交接语义 |
|---|---|
| 生成期 | 生成器产出确定性只读内容；生成检查点与对象引用提交完成后，应用层以持久化对象引用替换生成期来源并释放构模缓冲 |
| 运行时 | 按块将输入从对象存储复制到隔离目录；进程结束后先通过持久化接收接口落盘执行检查点，确认后再清理临时目录 |
| 结果适配与导出 | 通过同一只读内容协议读取解与回执；发布最终证据时复用既有对象引用 |

## 容器执行隔离与职责边界

- **断网容器与无 Shell 约束**：`solver` 容器经 `job_runner` 以 `--network=none --read-only --cap-drop=ALL` 非 root 身份拉起，禁止 Shell 字符串拼接、`..` 路径逃逸与任意命令注入。
- **执行回执（`ExecutionReceipt`）与 0-based 列序契约**：无论成功或失败均记录尝试、求解插件版本、起止时间、退出码或信号及 cgroup 资源指标；进程退出码为 `0` 仅代表容器正常退出，求解器输出的 `primal_solution.json` 列值数组必须严格按 `input/problem.mps` 的 **0-based 原始列顺序**排列。
- **内置 HiGHS 结构加速与通用还原边界区分**：内置 `ies.generator.highs_milp@1.0.0` 默认开启 `on_demand_mutex` 结构加速（可通过 [CalculationConfig](calculation-config.md) `options.generator.acceleration.mode = "off"` 显式关闭）；**松弛互斥后的原模型约束容差核查仅属于官方内置 `ies.solver.highs`（`ies-highs-solver`）自身的内部求解质量控制**（详见[计算生成与求解](../modules/engines.md#求解器内建结构加速)）。`backend` 受限子进程中的通用 `ResultAdapter` 只负责按 `variable_map`（`input/parameters.json` + `input/parameters.jsonl`）忠实还原业务单位标准工程解，**不对第三方 `solver` 执行重新验算**。

## 通用变量映射与参数记录

`generator` 与 `backend` 受限子进程还原层通过轻量元数据头 `input/parameters.json`（保存汇编标识、目标偏移与单位、列数、请求输出、种子及记录文件相对路径）和有序 NDJSON 记录文件 `input/parameters.jsonl` 传递 0-based 求解列位置与变量/来源声明。

`input/parameters.jsonl` 按以下顺序写出 JSON 数组记录（所有位置与表达式 ID 从 `0` 起按内部槽位索引，同类记录不可交换顺序）：

| 记录标签与元素 | 含义 |
|---|---|
| `column_names, [name, ...]` / `row_names, [name, ...]` | 按实际求解列/行顺序分批记录名称表 |
| `epigraph, peak_position` / `epigraph_flows, [col, ...]` / `epigraph_end` | 仅在启用连续需量上包络时输出，记录月峰与同月流量求解列位置供紧度校验 |
| `expression, id, constant` / `terms, [[col, coeff], ...]` / `expression_end` | 冻结仿射表达式（适配器按项原顺序求值，一份结果供多处复用） |
| `units, [[col, unit], ...]` | 可发布变量的业务单位与求解列位置 |
| `component, identity, unit, caliber, expression_id` | 财务分量与共享表达式关联 |
| `facts, object` | 结构化财务事实骨架（金额节点以 `{expression: id}` 引用） |
| `series, object` / `positions, [col, ...]` / `series_end` | 设备或系统步序列元数据与完整连续 `step` 对应列位置 |
| `sources, object` / `source_group, ...` / `targets` / `indices` / `source_group_end` | 模型来源表与按分组压缩的行列索引映射 |

## 步序列结果映射

配套生成器在建立变量时登记设备序列元数据 `{series_id, instance_id, variable_id, unit}` 及按 `step=0..n-1` 排列的实际求解列位置。`series_id` 为本次 Bundle 共享的唯一不透明标识；`ResultAdapter` 据此恢复业务单位的 `ComputeResult.outputs.series`，不按列名反向猜测实例或时步。

## 系统序列映射

装配声明 `outputs.system_series` 时，生成器通过相同记录机制声明 `{series_id, block, variable, unit}` 及列位置（与设备序列共享 `series_id` 唯一命名空间）；适配器恢复为 `ComputeResult.outputs.system_series`，在公共目录及导出中通过 `kind: "system"` 明确区分（参见[求解步序列读取](../contracts.md#求解步序列读取)）。

## 同次解财务事实映射

在启用 `solution` 输出且取得可用主解时，适配器结合冻结财务表达式恢复 `ComputeResult.outputs.financial_facts`（配套当前契约版本规则见[单一当前契约](../file-formats.md#单一当前契约与项目包依赖)）：

| 字段 | 完整结构与业务语义 |
|---|---|
| `currency` | 冻结财务币种，所有金额为十进制字符串 |
| `construction_total` / `annual_fixed_om` | 实际含税建设总额与年度固定运维金额 |
| `construction_units` | `[{unit_id, instance_id, mode, built, scales: [{planning_variable_id, value, unit}]}]`；`built` 为同次解布尔建设状态，`scales[].value` 为业务单位数值（JSON number） |
| `payments_applied` | `[{t, ratio, amount}]`（保留同 `t` 多笔付款） |
| `net_cashflows` | `[{id, t, direction, amount}]`，按 `(t, id)` 排序 |
| `annual_operation` | `[{financial_year, energy_net, variable_om, fixed_om, maintenance, demand_charge, net}]`；`net` 已一次性扣除当年各费用 |
| `monthly_demand` | `[{billing_id, charge_id, calendar_year, calendar_month, financial_year, peak_power, peak_unit, rate, charge}]`，按 `(billing_id, calendar_year, calendar_month)` 排序 |
| `tax_cash.vat` | `[{pool, month, paid, refund_claimed, refund_arrived, carry, receivable}]`（按池及月 `1..12E` 记录） |
| `tax_cash.vat_rules` | `[{rule, pool, month, output_tax, deductible_input}]`（按规则记录确认额，不拆摊池实缴） |
| `tax_cash.refunds` | `[{pool, period, arrival_period, amount}]` |
| `tax_cash.income` | `[{rule, year, taxable_base, loss_used, loss_generated, loss_expired, assessed, tax}]`（每所得税规则每年 `1..E` 一行） |
| `tax_cash.depreciation` | `[{rule, base_from, year, amount}]`；`base_from` 保留来源判别联合 |
| `tax_cash.loss_balances` | `[{rule, vintage_id, year, last_usable_year, amount}]` |
| `annual_net_flows` | 按 `t=0..evaluation_years` 排列的完整税后现金流 |

### 关联维护金额协议

维护支出中的确定金额与规模/建设成本关联项均通过冻结仿射表达式节点在 `annual_operation[].maintenance` 求值恢复为十进制字符串；`net_cashflows[].amount` 同样复用冻结表达式节点，保证与主规划模型同源一致。

## 关系事实映射

扩展财务关系及[评价比较](../contracts.md#评价范围与冻结基准)在 `financial_facts` 中封存以下同次解结构（按身份与期间排序，未启用项输出 `[]`）：

| 字段 | 结构与语义 |
|---|---|
| `cost_items` | `[{instance_id, finance_type, component, item_id, axis, period, raw, gross, deductible_input, net_basis, unit}]`（已含于总分量金额中，不额外重复扣减现金） |
| `amounts` | `[{id, axis, unit, values: [{period, value}]}]`（`scalar` 的 `period` 为 `0`） |
| `cash_movements` | `[{id, axis, period, direction, price_basis, raw, gross, deductible_input, net_basis, output_tax, net_revenue, payments: [{at, ratio, amount}], end_unpaid}]`（期内付款进入 `annual_net_flows`，期外归入 `end_unpaid`） |
| `assets` | `[{id, basis, capitalized_at, available_from, book_values: [{year, value}]}]`（折旧明细归入 `tax_cash.depreciation`） |
| `tax_cash.derived` | `[{rule, axis, period, assessed, payment_period, paid}]`（实际支付按 `payment_period` 进入现金流） |
| `comparison` | `{mode}`；`reference` 附 `{baseline_source, comparability, facts}`；`difference` 另附 `baseline_net_flows` 与 `evaluation_net_flows`（保持候选 `annual_net_flows` 不被覆盖） |

### 税额引用与封存字段

财务 `kind: tax` 引用及基准 `kind: tax` 选择器严格按照以下映射读取同次封存 `financial_facts`：

| `source` 类别 | `measure` | 封存路径与字段 | 轴与取值期间 |
|---|---|---|---|
| VAT 规则 | `output_tax` / `deductible_input` | `tax_cash.vat_rules[rule=id, month=p].output_tax` / `.deductible_input` | `tax_month`（确认月 `p`） |
| VAT 抵扣池 | `paid` / `refund_confirmed` / `refund_received` / `credit_balance` / `receivable` | `tax_cash.vat[pool=id, month=p].paid` / `.refund_claimed` / `.refund_arrived` / `.carry` / `.receivable` | `tax_month`（月 `p`） |
| 所得税规则 | `taxable_base` / `loss_used` / `assessed` / `paid` | `tax_cash.income[rule=id, year=y].taxable_base` / `.loss_used` / `.assessed` / `.tax` | `financial_year`（评价年 `y`） |
| 派生税规则 | `assessed` | `tax_cash.derived[rule=id, period=p].assessed` | 本规则轴（应计期 `p`） |
| 派生税规则 | `paid` | `tax_cash.derived[rule=id, payment_period=p].paid` | 本规则轴（支付期 `p`） |

## 构模来源映射

内置生成器与适配器通过 `schema: ies.model-sources`（`schema_version: "1.0.0"`）传递模型构模溯源：

| 字段 | 含义 |
|---|---|
| `sources` | 不透明来源 ID → `{kind, location, domain: {kind, count, index?}, inputs, outputs, depends_on}` |
| `groups` | `[{source, role, axis, targets, indices}]`（`role` 以 `row:` / `column:` 区分，等差索引压缩为 `{start, stride, count}`） |
| `outputs` | `[{path, sources}]`，关联结果字段路径与上游来源 ID |

请求 `solution` 且有解时发布为 `ComputeResult.outputs.model_sources`；构模转换阶段发生预期限制或失败时，通过 `TASK-SOLVE-001` 的 `context.model_source` 携带定位上下文。
