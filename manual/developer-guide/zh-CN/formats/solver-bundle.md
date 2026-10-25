# Solver Bundle

> 契约标识：`ies.solver-bundle`；目标 schema：`1.0.0`；生产者：GeneratorProvider；消费者：SolverRuntime。

Solver Bundle 是一次求解的可执行、不可变交接包。生成器把已校验装配转成求解器输入文件和结构化命令；通用运行时只核验并执行该命令。这样“如何构造数学问题”和“如何安全运行进程”成为两个可以独立开发、测试和替换的部分。

Solver Bundle 不由最终用户手写，也不能从未经校验的装配 YAML 直接拼接。

## manifest 与完整聚合

`bundle.yaml` 是 manifest 本体：只声明引用、输入清单、结构化命令、输出声明与结果适配器，不携带文件字节。运行时可执行的唯一完整形态是 manifest 加按规范相对路径索引的只读输入文件字节映射；`manifest.inputs` 声明的路径与文件映射的键必须恰好对应。`outputs` 只是求解器允许写入的声明，生成包中不预建其内容。manifest 的 schema、规范化与校验只针对本体，不触及文件字节；两者关系唯一，不形成两套业务事实。

## 目录结构

```text
solver-bundle/
├── bundle.yaml
├── input/
│   ├── problem.mps
│   └── parameters.json
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

## 步序列结果映射

以下为生效目标契约，不表示当前 provider 已实现。配套生成器在 `input/parameters.json` 的 `step_series` 数组声明可发布的求解步序列，每条包含：

| 字段 | 语义 |
| --- | --- |
| `series_id` | 此 Bundle 内稳定且唯一的不透明序列标识，不由消费者拆解 |
| `instance_id` | 公开装配实例 ID |
| `variable_id` | 该实例设备数学贡献中已声明的变量身份，包含接口变量，不猜设备类型 |
| `unit` | 业务单位，与生成器单位映射同源 |
| `columns` | 按连续 `step=0..n-1` 排列的具体求解列名，不是让下游重构列名的模板 |

声明由生成器在建立对应变量时形成，与真实求解列同源，覆盖该序列的完整运营时域；不复制装配或以物化输入冒充求解结果。无可发布步变量时声明空数组。ResultAdapter 直接使用明确列引用恢复同一次求解的结果，application、Worker 和前端不得按求解变量名反解实例或时步。

声明属于配套 generator/result adapter 的版本化协议，不改变通用 Bundle manifest 或封闭 CalculationConfig。若新版本将该声明改为必需，必须提升配套 provider 协议主版本并同步精确引用；不得以旧解析器忽略未知键证明兼容，也不实现旧声明回退。统一结果形状见 [计算生成与求解](../modules/engines.md)。

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

配套生成器在 `parameters.json` 的 `financial_facts` 声明结构化财务事实映射，ResultAdapter 使用同一次主解恢复为 `ComputeResult.outputs.financial_facts`。声明复用既有仿射求值方式，包含明确字段、业务身份、期间、单位与求解列引用；消费者不拆解分量键、序列 ID 或列名。现金流声明与规划目标中的现金表达式同源，不重新建立税务模型。声明不复制完整财务配置，也不是新的用户公式语言。

以下为配套生成器 `ies.generator.highs_milp@4.0.0` 与适配器 `ies.result-adapter.highs@4.0.0` 的目标业务输出：

| 字段 | 语义 |
| --- | --- |
| `currency` | 冻结财务币种，所有金额使用该单位和十进制字符串，不作展示舍入 |
| `construction_total`、`annual_fixed_om` | 实际含税建设总额与年度固定运维金额，按本课题增量范围计取 |
| `payments_applied` | `[{t, ratio, amount}]`，明确建设付款时点、比例与实际含税付款；不合并或丢失同一时点的多笔付款 |
| `annual_operation` | `[{financial_year, energy_net, variable_om, fixed_om, maintenance, demand_charge, net}]`，实际含税收付；`demand_charge` 为当年需量费正金额合计，无费用为 `"0"`；`net` 扣除所列费用各一次，尚未扣税款结算或折旧 |
| `monthly_demand` | `[{billing_id, charge_id, calendar_year, calendar_month, financial_year, peak_power, peak_unit, rate, charge}]`；每个需量绑定、每个运营自然月一项。`peak_power` 为实际月峰十进制字符串，`peak_unit` 为 `kW`；`rate` 是冻结费率 Money，`charge` 是实际含税支付金额字符串；币种沿顶层。无绑定时显式 `[]`，零费率仍保留真实峰值与零账单 |
| `tax_cash.vat` | `[{pool, month, paid, refund_claimed, refund_arrived, carry}]`，每池每评价月一行；`carry` 为扣除当月确认退款后的期末可结转留抵，实缴与到账退款才影响现金 |
| `tax_cash.refunds` | `[{pool, period, arrival_period, amount}]`，明确确认和到账期间；保留评价期后到账的已确认款项，期后款不计入期内现金 |
| `tax_cash.income` | `[{rule, year, taxable_base, loss_used, loss_generated, loss_expired, tax}]`，每所得税规则每评价年一行，基础为当年抵扣亏损前的应税基础 |
| `tax_cash.depreciation` | `[{rule, asset, year, amount}]`，该年计税折旧；只解释税基，不作为额外现金支出 |
| `tax_cash.loss_balances` | `[{rule, vintage_id, year, last_usable_year, amount}]`，按规则及不透明亏损批次标识记录年末余额，批次标识不得由消费者拆解 |
| `annual_net_flows` | 按 `t=0..evaluation_years` 排列的完整税后现金流；包含明确建设付款、运行收支、VAT实缴／退款到账与所得税，余额及折旧不额外计入现金 |

`t`、`month`、`year`、`period`、`arrival_period`、`last_usable_year` 及日历／评价年、日历月均为整数；`ratio` 与金额均为十进制字符串。期间映射以财务契约为准，池与规则保持显式身份，共享池不得按规则重复计入。没有适用规则时对应数组为空，含义为不适用；适用规则的零税额、零余额显式发布，不省略为未知。无税则的完整现金流仍须声明并恢复，不以缺映射冒充零税。

需量费与能源电量费分列，不重复包含于 `energy_net` 或 O&M。月明细按 `(billing_id, calendar_year, calendar_month)` 排序，年度正费用与净额在模型中同源产生；适配器只恢复声明，不重新取 max、重新计税或扣费。此目标随 `ies.financial-evaluation@2.0.0` 封存（新增必填月明细与年度分量），生成器／适配器为本节列出的 `4.0.0` 配套；不改通用 Bundle、ComputeResult 或快照顶层版本，不在旧结果上注入新字段。

该块随已请求的 `solution` 且存在可用数学解时发布；未请求solution或无可用解时缺席。它不增加 CalculationConfig 输出词汇，不改变 ComputeResult 顶层身份与状态或通用 Bundle manifest。适配器只恢复业务事实，不重算税务、猜列名或改写期间；内部可信声明不增加重复结构、摘要或完整性复核。

### 关联维护金额协议

以下为生效目标契约，尚未实现。配套 `ies.generator.highs_milp@5.0.0` 与 `ies.result-adapter.highs@5.0.0` 将内部 `financial_facts.annual_operation[].maintenance` 声明从固定金额字符串改为既有 `{constant, terms}` 仿射节点；即使是确定金额也使用常数节点。适配器按同一次主解求值，公开输出仍为十进制金额字符串。该必需声明形态的破坏性变化提升配套主版本，不保留字符串／节点双轨解释。

维护现金、进项税与所得税扣除均来自同一逐笔金额表达式，年度维护与净额声明保持同源。公开财务结果不增加逐笔来源字段，`ies.financial-evaluation` 仍为 `2.0.0`；通用 Bundle、ComputeResult、solver 与 executor 协议不变。此目标不改写上文 `4.0.0` 的既有声明语义。

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
