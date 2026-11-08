# Solver Bundle

> 契约标识：`ies.solver-bundle`；目标 schema：`1.0.0`；生产者：GeneratorProvider；消费者：SolverRuntime。

Solver Bundle 是一次求解的可执行、不可变交接包。生成器把已校验装配转成求解器输入文件和结构化命令；通用运行时只核验并执行该命令。这样“如何构造数学问题”和“如何安全运行进程”成为两个可以独立开发、测试和替换的部分。

Solver Bundle 不由最终用户手写，也不能从未经校验的装配 YAML 直接拼接。

## manifest 与完整聚合

`bundle.yaml` 是 manifest 本体：只声明引用、输入清单、结构化命令、输出声明与结果适配器，不携带文件字节。完整聚合为 manifest 加按规范相对路径索引的只读输入内容映射；`manifest.inputs` 声明的路径与内容映射的键必须恰好对应。`outputs` 只是求解器允许写入的声明，生成包中不预建其内容。manifest 的 schema、规范化与校验只针对本体，不读取内容；两者关系唯一，不形成两套业务事实。

### 分块内容与生命周期

当前自有schema为`1.0.0`，不设置格式版本协商、执行模式开关或旧包回退。Python内容协议为 `Content.iter_chunks() -> Iterator[bytes]`。

- `files`的值统一为只读、可重复分块读取的内容协议。每次读取从头产生同一字节序列；块边界没有记录或UTF-8字符边界含义。消费者按块写出，不为了交接先汇集为完整文本或`bytes`。
- 小内容可使用该协议的不可变内存实现；生成期内容可由已冻结的紧凑数据确定性产生。两者使用同一消费入口，不构成“bytes包”和“文件包”两种执行路径。内容协议不携带数据库会话、裸文件句柄或宿主机路径，不允许消费者反解存储实现。
- 内容源可延迟产生实际字节；实际生成、编码或I/O失败发生时必须可见，不把创建了内容源等同于已经成功落盘。内容大小由写入时累计实际字节取得，不先完整遍历一遍计算大小，不增加hash或完整性复核。
- 内容协议不决定数学文件布局；MPS、参数、解和来源声明仍由配套生成器／适配器解释。分块交接不授权改变时域、关系、结果内容或浮点累加顺序。

| 内容阶段 | 当前所有者与消费方 | 释放／交接时点 |
| --- | --- | --- |
| 生成期内容 | 生成器产生确定性只读内容，application消费并持久化 | 生成检查点及全部对象引用提交成功后，application以对象内容引用替换生成期内容，并释放不再需要的矩阵和生成缓冲 |
| 已提交Bundle | storage拥有字节与生命周期，运行时按对象内容分块复制输入 | 恢复使用已提交manifest和对象引用，不整包读回，不重新构模推测原内容 |
| 执行原始输出 | runtime管理只读运行期内容，application接收并保存 | 解与原回执的执行检查点提交成功后，返回持久化内容引用，再清理运行目录；不能先删文件再返回悬空内容 |
| 结果适配与导出 | 消费同次执行的已提交内容 | 沿同一内容协议读取；最终证据增加引用并复用对象，不再次封存相同大文件 |

生成器不访问storage或自行管理临时目录；实际持久化归application协调。持久化接收失败时不发布阶段成功，未交付的运行期产物仍归runtime管理；对象元数据、owner引用和Blob的失败语义分别遵守[对象存储](../modules/storage.md)与[Worker](../modules/worker.md)。独立runtime调用没有持久化接收方时，调用方持有只读输出的生命周期，并在读取完成后显式释放，不能清理后才尝试适配或导出。

## 目录结构

```text
solver-bundle/
├── bundle.yaml
├── input/
│   ├── problem.mps
│   ├── parameters.json
│   └── parameters.jsonl
└── output/
```

`output/` 在生成时为空。求解器只能写入 manifest 声明的相对输出路径。Bundle 可以存为目录或不可变归档；解包后保持相同规范路径。

## `bundle.yaml` 示例

```yaml
schema: ies.solver-bundle
schema_version: "1.0.0"

bundle:
  generator: acme.generator.highs_milp@1.0.0
  solver: ies.solver.highs@1.7.2

inputs:
  - path: input/problem.mps
    media_type: application/mps
  - path: input/parameters.json
    media_type: application/json
  - path: input/parameters.jsonl
    media_type: application/x-ndjson

command:
  executor: ies.executor.local_process@1.0.0
  executable: ies.solver.highs@1.7.2
  arguments:
    - input/problem.mps
    - --solution_file
    - output/solution.json
  working_directory: .
  environment: {}
  timeout_seconds: 43200
  memory_mib: 4096
  cpu_cores: 4
  network: false
  success_exit_codes:
    - 0

outputs:
  - path: output/solution.json
    media_type: application/json
    required: true

result_adapter: acme.result-adapter.highs@1.0.0
extensions: {}
```

## 生成器职责

一个 GeneratorProvider 分别接收已签发的 `ValidatedAssemblyArtifact`、按装配实例固定的设备数学贡献、已物化的全周期输入序列、不可变 `EffectiveFinanceConfig`、已校验的 `CalculationConfig` 及冻结评价确认，负责：

1. 直接消费已签发的装配产物，并验证计算配置中的 generator、solver、精度、算法选项、种子和输出请求能力匹配；
2. 消费已规范化的设备方程贡献，并结合装配中的规划配置与 `EffectiveFinanceConfig` 建立变量、目标、约束和索引；
3. 在唯一边界把业务单位转换为求解器内部单位；
4. 生成 MPS、LP、JSON 或特定求解器需要的输入文件；
5. 声明受控命令、资源限制、预期输出和结果适配器；
6. 对 manifest 与全部输入原子发布 Bundle。

生成器应是确定性的纯转换：不得连接数据库、对象服务或网络，不得启动求解器，不得读环境变量，不得修改项目或任务状态。资源由调用方显式传入。

同一组直接输入与生成器版本必须产生相同输入字节；评价确认也属于生成输入。若底层文件格式本身含非确定字段，生成器必须移除或固定它们。

## 内置配套参数记录

内置生成器与适配器使用一个小JSON头部 `input/parameters.json` 和有序UTF-8记录文件 `input/parameters.jsonl`。头部包含 `assembly`、`generator`、`solver`、`result_adapter`、`seed`、`precision`、`outputs`、`objective_offset`、`objective_unit`、`column_count` 和 `records`。`records` 是manifest已声明的记录文件相对路径；头部不包含大数组、全量单位表或财务表达式。目标单位由生成器确定，不由适配器猜测。

记录文件每行是一个JSON数组，第一个元素为以下标签；换行只在文件布局中分隔记录，内容块可以切在任意UTF-8或记录位置。所有位置、表达式编号均从0开始，是本次Bundle内部槽位，不作为公开业务ID。除小型字面量对象外，大列表拆成多个连续记录：

| 标签及后续元素 | 含义与顺序 |
| --- | --- |
| `column_names, [name,…]`；`row_names, [name,…]` | 按实际求解行列顺序保存一次名称表；分批记录拼接为同一表，不重扫MPS、不从名称反解身份 |
| `expression, id, constant`；`terms, [[column_position,coefficient],…]`；`expression_end` | 一个冻结仿射式；项记录可重复，按记录和项原顺序从常数开始累加，不能重排、合并或重新结合浮点运算 |
| `units, [[column_position,unit],…]` | 可发布变量及业务单位，保持原有稳定发布顺序，内部辅助列没有伪业务单位 |
| `component, identity, unit, caliber, expression_id` | 财务分量消费共享表达式的同次解值 |
| `facts, object` | 结构化财务事实的字面量骨架；金额节点为 `{expression: id}`，引用已经声明的表达式 |
| `series, object`；`positions, [column_position,…]`；`series_end` | 一条设备或系统序列的身份／单位元数据，后接按完整step排序的实际列位置 |
| `sources, object` | 来源schema、来源表和结果字段关联，不含groups |
| `source_group, object, indices`；`targets, [position,…]`；`indices, [index,…]`；`source_group_end` | 一个来源组的元数据及求解目标位置；role决定行或列。等差索引直接在头记录使用 `{start,stride,count}`；非等差时头值为null并接分批indices记录 |

顺序为名称表、全部表达式、单位、分量、财务骨架、全部序列、来源表及来源组；同种分批记录顺序不可交换。表达式只共享同一已冻结来源及常数，不按近似值合并不同金额，不符号化改写。适配器按紧凑主解的实际列位置逐记录求值，一份表达式值供各消费处复用；无需恢复整份参数对象图。生成器的时变系数、独立实例／时步、跨年状态、财务期间与结果业务语义不变。

这些记录由配套provider直接解释，不是通用Runtime的数学协议，也不是用户手写的新语言。内部已类型化声明不重复schema或完整性校验；实际生成、运算、读取或外部解解释失败仍可见。没有旧整包参数解析器或扫MPS回退路径。

## 步序列结果映射

配套生成器通过有序 `series`／`positions` 记录声明设备求解步序列，元数据包含：

| 字段 | 语义 |
| --- | --- |
| `series_id` | 此 Bundle 全部设备与系统序列共享的唯一不透明标识；同一封存结果/候选的所有引用组中唯一，不由消费者拆解；不同任务不作全局唯一承诺 |
| `instance_id` | 公开装配实例 ID |
| `variable_id` | 该实例设备数学贡献中已声明的变量身份，包含接口变量，不猜设备类型 |
| `unit` | 业务单位，与生成器单位映射同源 |
| 后接的 `positions` 记录 | 按连续 `step=0..n-1` 排列的实际求解列位置，不是列名模板 |

声明由生成器在建立对应变量时形成，与真实求解列同源，覆盖该序列的完整运营时域；不复制装配或以物化输入冒充求解结果。无可发布步变量时声明空数组。ResultAdapter 直接使用明确列引用恢复同一次求解的结果，application、Worker 和前端不得按求解变量名反解实例或时步。

声明由当前 generator/result adapter 配套解释，不改变通用 Bundle manifest 或封闭 CalculationConfig。2.0 正式发布前统一固定 `1.0.0`，声明变化直接同步生成与恢复，不保留旧声明回退。统一结果形状见 [计算生成与求解](../modules/engines.md)。

## 系统序列映射

配套生成器通过相同记录声明[系统方程](network-relations.md)输出；没有声明时不产生伪设备序列。系统 `series` 元数据恰为 `{series_id, block, variable, unit}`，后接完整运营step顺序的实际列位置。series_id 为不透明唯一身份，block/variable 为冻结系统块及辅助变量ID，unit 为业务单位。

适配器由同次解发布 `ComputeResult.outputs.system_series`，以series_id为键，每项为 `{block, variable, unit, values}`；values为业务单位有限数值，顺序与完整运营时域一致。它与设备 outputs.series 分别拥有真实来源，不能复制一份设备序列再改名，也不在适配时重新求分配或来源比例。两张映射共享series_id命名空间；公共目录、分页和导出按[求解步序列读取](../contracts.md#求解步序列读取)的device/system判别联合发布，设备序列保留真实instance_id/variable_id，系统序列不填“假实例”。

仅在请求solution并取得可用主解时发布；实际选择项为装配outputs.system_series。源汇关闭行没有额外的伪求解输出，所需端口序列仍按原设备映射发布。两类序列和模型来源共同固定于同一证据，不读取当前草稿补名称或单位。

## 结构化命令

`command` 是数据结构，不是 shell 字符串：

- `executor`：受信任运行方式的稳定 ID 与精确版本；
- `executable`：由运行时根据 allowlist 解析的求解器 ID，不是文件路径；
- `arguments`：一个参数一个字符串，禁止拼成命令行；
- `working_directory`：只能是 Bundle 内的规范相对目录；
- `environment`：仅包含 executor contract 允许的变量；
- 资源与网络字段：声明硬限制，`network` 默认并通常固定为 `false`；
- `success_exit_codes`：只判断进程是否按协议结束，不能替代求解状态解析。

禁止 `sh -c`、`bash -c`、管道、重定向、命令替换、通配符展开和由参数触发的任意插件加载。命令参数中的路径同样必须在 Bundle 内，不能含绝对路径或 `..`。

## 同次解财务事实映射

配套生成器在有序 `facts` 记录声明结构化财务事实映射，金额引用先前冻结表达式，ResultAdapter使用同一次主解恢复为 `ComputeResult.outputs.financial_facts`。声明保留明确字段、业务身份、期间、单位与实际求解列引用；消费者不拆解分量键、序列ID或列名。现金流声明与规划目标中的现金表达式同源，不重新建立税务模型，不复制完整财务配置，也不是新的用户公式语言。

以下为建设激活目标生成器 `ies.generator.highs_milp@1.0.0` 与适配器 `ies.result-adapter.highs@1.0.0` 的业务输出；版本发布及迁移见[格式版本表](../file-formats.md#建设激活配套版本与迁移)：

| 字段 | 语义 |
| --- | --- |
| `currency` | 冻结财务币种，所有金额使用该单位和十进制字符串，不作展示舍入 |
| `construction_total`、`annual_fixed_om` | 实际含税建设总额与年度固定运维金额，按本课题增量范围计取 |
| `construction_units` | `[{unit_id, instance_id, mode, built, scales}]`，每个冻结建设单元一项；mode 原样为 mandatory/optional，built 为同次解的布尔建设状态。scales 为 `[{planning_variable_id, value, unit}]`，value 是反向换算后的有限业务数值（JSON number，不是金额字符串），unit 沿用规划变量。不按规模是否大于零重算 built，无单元时为 `[]` |
| `payments_applied` | `[{t, ratio, amount}]`，明确建设付款时点、比例与实际含税付款；不合并或丢失同一时点的多笔付款 |
| `net_cashflows` | `[{id, t, direction, amount}]`，显式净现金项目的同次解金额；amount 为非负金额字符串，direction 保留 inflow／outflow，t 为实际财务时点；无项目为 `[]`，按 `(t,id)` 排序 |
| `annual_operation` | `[{financial_year, energy_net, variable_om, fixed_om, maintenance, demand_charge, net}]`，实际含税收付；`demand_charge` 为当年需量费正金额合计，无费用为 `"0"`；`net` 扣除所列费用各一次，尚未扣税款结算或折旧 |
| `monthly_demand` | `[{billing_id, charge_id, calendar_year, calendar_month, financial_year, peak_power, peak_unit, rate, charge}]`；每个需量绑定、每个运营自然月一项。`peak_power` 为实际月峰十进制字符串，`peak_unit` 为 `kW`；`rate` 是冻结费率 Money，`charge` 是实际含税支付金额字符串；币种沿顶层。无绑定时显式 `[]`，零费率仍保留真实峰值与零账单 |
| `tax_cash.vat` | `[{pool, month, paid, refund_claimed, refund_arrived, carry, receivable}]`，每池每评价月一行；`carry` 为扣除当月确认退款后的期末可结转留抵，`receivable`为当月到账后尚未到账的期末已确认退款应收，实缴与到账退款才影响现金 |
| `tax_cash.vat_rules` | `[{rule, pool, month, output_tax, deductible_input}]`，每VAT规则每评价月一行；规则实际没有的销项/进项方向明确为零，两个规则共享池仍分别保留确认税额，不把池实缴分摊给规则 |
| `tax_cash.refunds` | `[{pool, period, arrival_period, amount}]`，明确确认和到账期间；保留评价期后到账的已确认款项，期后款不计入期内现金 |
| `tax_cash.income` | `[{rule, year, taxable_base, loss_used, loss_generated, loss_expired, assessed, tax}]`，每所得税规则每评价年一行，基础为当年抵扣亏损前的应税基础；`assessed`为该年应计所得税，原`tax`字段仍表示该年实缴，当前无支付滞后时两者来自同一金额表达式 |
| `tax_cash.depreciation` | `[{rule, base_from, year, amount}]`，该年计税折旧；base_from 原样保留[财务金额来源](finance-yaml.md#金额来源与折旧期间)判别联合，区分初建与同年多笔换新，不压成 asset 类别；只解释税基，不作为额外现金支出 |
| `tax_cash.loss_balances` | `[{rule, vintage_id, year, last_usable_year, amount}]`，按规则及不透明亏损批次标识记录年末余额，批次标识不得由消费者拆解 |
| `annual_net_flows` | 按 `t=0..evaluation_years` 排列的完整税后现金流；包含明确建设付款、运行收支、VAT实缴／退款到账、所得税及显式净现金项目，余额及折旧不额外计入现金 |

`t`、`month`、`year`、`period`、`arrival_period`、`last_usable_year` 及日历／评价年、日历月均为整数；`ratio` 与金额均为十进制字符串。期间映射以财务契约为准，池与规则保持显式身份，共享池不得按规则重复计入。没有适用规则时对应数组为空，含义为不适用；适用规则的零税额、零余额显式发布，不省略为未知。无税则的完整现金流仍须声明并恢复，不以缺映射冒充零税。

需量费与能源电量费分列，不重复包含于 `energy_net` 或 O&M；显式净现金项目也不重复写入 `annual_operation.net`，只在其时点进入完整现金流。月明细按 `(billing_id, calendar_year, calendar_month)` 排序，年度正费用与净额在模型中同源产生；适配器只恢复声明，不重新取 max、重新计税或扣费。目标财务结果按 `ies.financial-evaluation@1.0.0` 封存，不在旧结果上注入新字段。

建设声明的 unit_id、instance_id、mode 及规划变量 ID/单位是冻结字面量；built 和各规模的数值复用既有仿射求值节点，引用实际建设决策列和规划列，必建状态可为常数 1。ResultAdapter 由声明的二元状态恢复布尔值，不解析列名或新增容量阈值。单元按 unit_id、规模按 planning_variable_id 排序。Worker、结果读取、页面及 Excel 原样消费同次解状态与业务单位；不能用 SI 内部值冒充 kW/kWh，也不能用当前草稿补单位或重判选建。

该块随已请求的 `solution` 且存在可用数学解时发布；未请求solution或无可用解时缺席。它不增加 CalculationConfig 输出词汇，不改变 ComputeResult 顶层身份与状态或通用 Bundle manifest。适配器只恢复业务事实，不重算税务、猜列名或改写期间；内部可信声明不增加重复结构、摘要或完整性复核。

### 关联维护金额协议

内部 `financial_facts.annual_operation[].maintenance` 引用有序记录中的冻结仿射式，即使确定金额也使用常数表达式。适配器按同一次主解求值，公开输出仍为十进制金额字符串，不保留字符串／节点双轨解释。

维护现金、进项税与所得税处理均来自同一逐笔金额表达式，年度维护与净额声明保持同源；资本化项由折旧关系扣税，不再当期重复扣除。维护年度合计仍为金额字符串；来源身份保留在折旧明细，不另建全量维护支付结果副本。通用 Bundle、ComputeResult、solver 与 executor 协议不变。

同次解声明中的 `net_cashflows[].amount` 复用金额仿射节点，base_from、项目 ID 与期间是冻结字面量。适配器求值而不重建来源金额；application 原样封存项目明细与完整现金流。

## 关系事实映射

以下为[命名金额关系](finance-relations.md)及[评价比较](../contracts.md#评价范围与冻结基准)的输出约定。适配器由同次主解恢复 `cash_movements`、`assets`、规则级税额、派生税及比较现金流。所有字段沿用同一 `financial_facts`、仿射求值节点与币种，不另建财务结果对象或税务后处理器：

| 新字段 | 完整公开结构与含义 |
| --- | --- |
| `cost_items` | `[{instance_id, finance_type, component, item_id, axis, period, raw, gross, deductible_input, net_basis, unit}]`；每个实际命名项、实际期间一行，含零值；年率unit为币种/年，逐年付款额另按显式年度关系恢复 |
| `amounts` | `[{id, axis, unit, values: [{period, value}]}]`；scalar的period为0，其余为真实评价年/月；保留命名配方输出，不自动作为现金 |
| `cash_movements` | `[{id, axis, period, direction, price_basis, raw, gross, deductible_input, net_basis, output_tax, net_revenue, payments, end_unpaid}]`；period为发生期，payments为`[{at, ratio, amount}]`，at保留输入TimePoint形态；包含期外约定支付但不将其加入期内现金，end_unpaid为评价期末未付/未收金额。非适用方向的税额为0，对应净额仍按其明确算式恢复，不把它作为另一种收入/费用使用 |
| `assets` | `[{id, basis, capitalized_at, available_from, book_values: [{year, value}]}]`；basis为实际非负净基础，book_values来自同次资产关系，不自动作为残值现金 |
| `tax_cash.derived` | `[{rule, axis, period, assessed, payment_period, paid}]`；应计与实际约定支付分开，期外payment_period保留但不进入期内现金 |

新资产的折旧仍只在原tax_cash.depreciation声明，base_from增加`{kind: asset, id}`，不复制第二份资产折旧数组。资产book_values为基础减截至该期实际计提的累计折旧，不因收取一笔net_cashflow就自动作资产处置。所得税assessed/tax及规则级VAT、池应收的引用使用下节明确映射，不让调用方按列名或税种推断。

所有金额/比例为十进制字符串；period/year为整数；TimePoint中年/月字段为整数。cost_items按实例、component、item_id、period排序，其余按id/rule及period排序，payments保留输入顺序。未适用目录发布空数组；适用零值不省略。命名项已包含于原分量总额，不能再把cost_items逐项另扣现金。原annual_operation保持既有经营字段与合计；movement支付另从cash_movements进入annual_net_flows一次，不暗塞进maintenance或fixed_om再扣。派生税费实付由tax_cash.derived进入现金，资产基础/折旧不产生第二笔支付。

财务事实包含 `comparison`；其 `baseline_source.selectors` 保留冻结输入中的原业务选择器，仅用于追溯：

- absolute为`{mode: absolute}`。
- reference为`{mode: reference, baseline_source, comparability, facts}`；facts是已冻结的基准别名到`{unit,axis,values}`的映射，不是对当前草稿的引用；原annual_net_flows仍为候选现金流。
- difference在reference字段之外增加 `baseline_net_flows` 与 `evaluation_net_flows`。前者为冻结CF_b[0..E]，后者为主模型声明的CF_c-CF_b；原annual_net_flows始终保留CF_c，不被差额覆盖。baseline_source保留来源kind及可用的task/evidence/对象身份或declared来源标记；不复写完整基准模型。

`financial_evaluation`原样封存这些关系事实及comparison；difference的指标rows/NPV/IRR/回收期使用evaluation_net_flows，其余使用annual_net_flows。优化目标也使用同一选定现金流；不在报告阶段才换评价口径。适配器只恢复，财务评价只执行一次既有指标计算，读取/页面/Excel不得再次扣基准、重算税或将余额当现金。

模型来源映射同时覆盖命名成本、amount、movement发生与支付、asset基础/计提、derived_tax和baseline别名；amount_relation保留planning.constraints中的约束ID、references别名、明确期间及所绑定原金额/基准来源。保留原字段、作用期间及依赖。新增字段不改变Bundle manifest、Runtime、solver软件版本、ComputeResult顶层状态或solution输出词汇；仅有输入而没有这些同次恢复声明时不得发布支持能力。

### 税额引用与封存字段

本表是[税额输出引用](finance-relations.md#税额输出引用)及基准`kind: tax`选择器的唯一输出对应表。路径均相对同一封存`financial_facts`；声明阶段就用原税务关系登记下列字段的仿射节点，ResultAdapter按同次解恢复，financial_evaluation原样投影。表中数组条件是稳定业务身份匹配，不是用户可传的任意JSON路径。

| source类别 | measure | 封存行与值字段 | 轴及取值期间 |
| --- | --- | --- | --- |
| VAT rule | `output_tax` | `tax_cash.vat_rules[rule=id, month=p].output_tax` | tax_month，确认月p |
| VAT rule | `deductible_input` | `tax_cash.vat_rules[rule=id, month=p].deductible_input` | tax_month，确认月p |
| pool | `paid` | `tax_cash.vat[pool=id, month=p].paid` | tax_month，实缴月p |
| pool | `refund_confirmed` | `tax_cash.vat[pool=id, month=p].refund_claimed` | tax_month，确认月p |
| pool | `refund_received` | `tax_cash.vat[pool=id, month=p].refund_arrived` | tax_month，到账月p |
| pool | `credit_balance` | `tax_cash.vat[pool=id, month=p].carry` | tax_month，月p期末存量 |
| pool | `receivable` | `tax_cash.vat[pool=id, month=p].receivable` | tax_month，月p期末存量 |
| income rule | `taxable_base` | `tax_cash.income[rule=id, year=y].taxable_base` | financial_year，评价年y |
| income rule | `loss_used` | `tax_cash.income[rule=id, year=y].loss_used` | financial_year，评价年y |
| income rule | `assessed` | `tax_cash.income[rule=id, year=y].assessed` | financial_year，应计年y |
| income rule | `paid` | `tax_cash.income[rule=id, year=y].tax` | financial_year，实缴年y；不另造paid别名字段 |
| derived rule | `assessed` | `tax_cash.derived[rule=id, period=p].assessed` | 本规则axis，应计期p |
| derived rule | `paid` | `tax_cash.derived[rule=id, payment_period=p].paid` | 本规则axis，支付期p；不是period=p的行 |

各金额字段为本币十进制字符串；rule、pool为冻结的规则/池ID。VAT rule包括kind为vat与construction_input_vat的规则，后者output_tax为零；共享池的每条规则只报告自己的确认额。月行覆盖1..12E，所得税年行覆盖1..E；适用身份的零值行必须存在，数组按身份及实际期间排序。derived每个应计期一行，payment_period即使超出评价期也保留，但只在其实际支付期才可被paid选择器读到，不进入期内现金。其支付滞后为固定值，支付期无对应行时表示该规则本期无支付，取零；不能以缺少整个规则目录冒充零。所有存量均按其期末定义封存，不在消费时从其他结果重建。

基准税额选择器按该表产生带unit/axis的期间值：月轴依次为月1..12E，年轴依次为年0..E，年0为明确不适用的结构性零；派生税的paid按payment_period放入同轴位置，期外款不放入期内数组。未知rule/池、对应字段未发布或期间事实缺失必须报告选择器及缺失事实，不从当前规则、共享池或候选解补算。新任务内的税额引用直接绑定同一原表达式，基准选择器只读取已封存值，两者语义相同但不互相启动求解。

## 构模来源映射

内置生成器 `ies.generator.highs_milp@1.0.0` 与适配器 `ies.result-adapter.highs@1.0.0` 通过有序来源记录传递以下映射；行列名各保存一次，组内以实际位置引用，恢复后公开结果仍使用真实名称。该表示不改变现有解与财务字段、六项生成输入或通用Bundle manifest；不以新适配器补写历史格式。

来源映射为 `schema: ies.model-sources`、`schema_version: 1.0.0` 的对象：

| 字段 | 含义 |
| --- | --- |
| `sources` | 不透明来源 ID 到 `{kind, location, domain, inputs, outputs, depends_on}` 的表；location 保留冻结字段路径及已有实例、模型、revision、关系、绑定或数据对象身份，无值项省略；domain 为 `{kind,count}`，单一财务期间另带实际 `index`；inputs／outputs 为已绑定的符号身份，depends_on 引用本表中的上游关系 |
| `groups` | `[{source, role, axis, targets, indices}]`；source 引用来源表，role 区分原关系与转换辅助角色，axis 为 scalar/step/month/year，targets 为实际求解行或列名；indices 为与 targets 对齐的实际索引，连续等差时用 `{start,stride,count}`，否则为整数数组；标量索引为0 |
| `outputs` | `[{path,sources}]`，path 为既有结果字段的键／数组下标路径，sources 引用来源表；目标、分量、步序列和财务明细由同次构模登记，不由消费者拆名称推断 |

组的 role 以 `row:` 或 `column:` 区分行列，后缀表示转换角色。组内 targets 保留生成顺序，来源表和组按稳定身份排序。来源表保存关系及其依赖一次，组只记录实际索引和目标，不复制每步公式、完整 YAML 或求解值；同一关系可产生多组，多个来源通过 inputs／depends_on 保留。没有生成行的常量关系仍可保留来源，没有输出值时不伪造主解。

请求 solution 且取得可用主解时，适配器将同一映射发布为 `ComputeResult.outputs.model_sources`，与既有结果一起封存；不重建模型、不重新读取项目、不以来源映射再次验证可信矩阵。求解不可行时映射仍保存在 Bundle 证据，不声称已找到最小冲突集。无 solution 请求时不新增来源输出。来源内容按配套 provider 版本解释，ComputeResult 顶层、财务评价、装配和 solver 版本不随之机械升级。

生成中已识别的数学转换失败沿现有 `TASK-SOLVE-001` 诊断链发布 `context.model_source`：`{source,role,axis,index,location}`，并可附 `coefficients` 保存该关系已知系数的来源位置。该模型定位可由有权查看任务的项目成员读取，堆栈仍按原权限规则处理。位置来自当前绑定关系，index 为真实 step／评价月／评价年（标量为0）；物化系数沿用冻结对象身份，月日价格可保留按1～12月排列的 `data_object_refs`。缺有限界、不支持的数学形式仍是转换能力不足，不改称输入非法或数学无界。只有预期转换错误携带该定位；取消、隔离及内部程序异常保持各自失败语义，不吞掉或统一包装为模型错误。

## 运行时职责

SolverRuntime 是求解器无关的受控执行层，只负责：

1. 校验 Bundle schema、清单结构和规范路径；
2. 确认 executor、executable、参数、环境和资源策略在部署 allowlist 内；
3. 建立隔离工作目录，以最小权限运行一个命令；
4. 执行超时、取消、CPU、内存、文件和网络限制；
5. 分别捕获 stdout、stderr、退出码、信号、开始/结束时间和资源统计；
6. 核对声明输出，生成不可变 `ExecutionReceipt`。

运行时不读取 AssemblySpec、不构造数学问题、不解释设备类型、不修正输入文件，也不把非零退出码自动重试成另一个求解器。部署对可执行文件 ID 的解析属于组合根和 executor provider，不进入 Bundle。

## 输出和结果适配

每个输出都必须预先声明相对路径、媒体类型和是否必需。求解后：

- 缺少必需输出、输出越界或非预期文件视为执行协议失败；
- 原始输出保存并写入回执；
- stdout/stderr 单独保存，不能混入业务结果；
- `success_exit_codes` 只表示进程协议成功；不可行、无界等业务状态由结果适配器读取求解器输出后映射。

`result_adapter` 接收只读 Bundle、ExecutionReceipt 和声明输出，产出统一 `ComputeResult`。它不得重新运行求解器、访问项目草稿或根据当前最新 provider 改写历史结果。

## `ExecutionReceipt` 最小内容

回执至少固定：

- Bundle 标识、assembly 标识、`CalculationConfig` 标识、attempt ID；
- executor、solver、generator、result adapter 的 ID 与版本；
- 实际开始/结束时间、退出码或终止信号；
- 资源限制和可用的使用统计；
- stdout、stderr 和每个输出；
- 取消、超时、实际资源失败和输出协议错误的结构化状态。

回执是证据，不是可变运行日志索引。迟到 attempt 的回执可以保留审计，但不能覆盖当前有效任务结果。

## 失败边界

| 失败 | 所属边界 | 处理 |
|---|---|---|
| 未签发或非法的装配输入 | 装配/application 边界 | 在进入生成器前阻断，不产生部分 Bundle |
| 方程 contract 或生成器能力缺失 | generator | 任务运行前阻断 |
| 输入文件生成中断 | generator | 临时产物不发布 |
| 路径或 allowlist 不合法 | runtime 入口 | 不启动进程 |
| 超时、取消、OOM、非零退出 | runtime | 形成失败 ExecutionReceipt |
| 不可行、无界、数值异常 | result adapter | 映射为明确 ComputeResult 状态 |
| 结果格式损坏或非有限值 | result adapter | 结果协议失败，不发布伪成功 |

## 增加一个生成器

1. 声明稳定 generator ID、版本、支持的装配 schema、mode、方程 contract 能力和 solver；
2. 定义生成器选项 schema、内部单位、确定性和资源上界；
3. 实现纯转换和配套 result adapter；
4. 提供最小装配、生成后 Bundle 和已知求解结果；
5. 测试能力不匹配、路径、非确定性和中途失败（非法装配由上游装配/application 边界阻断，不进入生成）；
6. 通过 provider 原子注册，由组合根显式选择；
7. 证明通用 runtime 无需为该生成器或求解器增加业务分支。

## 完成标准

- 生成、执行、结果适配可分别进行契约测试；
- runtime 测试只靠 Bundle 即可执行，不需要项目数据库和设备目录；
- generator 测试不启动外部进程；
- 相同输入的 Bundle 稳定，任一输入变化都能被追溯；
- 任意命令注入、路径逃逸、未声明输出和网络访问默认被拒绝；
- 新求解器主要新增 generator/result-adapter/executor provider，不修改装配、API 或 GUI 类型映射。
