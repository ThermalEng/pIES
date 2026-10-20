# 计算生成与求解

> 文档状态：生效目标蓝图；目标代码边界：`backend/iesplan/computation/`。

## 作用

计算模块把“一个已经证明合法的业务系统”交给具体求解器，并把原始求解输出恢复为统一结果。它不是一个同时理解项目、拼数学模型、运行进程和解释结果的“大引擎”，而是把序列物化与生成、执行、结果适配边界分开：

```text
ValidatedAssemblyArtifact + CalculationConfig
          ↓
constant/data_repeat/data_full/data_predict 序列物化
          ↓
GeneratorProvider → Solver Bundle → SolverRuntime
          ↓
ResultAdapter → ComputeResult
          │
          ├─ 独立预测：采用结果
          └─ 耦合预测：结果反馈 → 更新序列 → 重新生成并求解 → 判定收敛
```

这条边界使新增求解器不必修改项目、API 或 Worker 的业务逻辑，也使运行外部进程的安全规则不依赖某个算法实现。

## 子模块分工

| 子模块 | 输入 | 输出 | 不负责 |
|---|---|---|---|
| 序列物化 | 装配中的来源声明和固定输入、项目基线、计算配置 | 全周期连续序列与回执 | 改写装配、项目模型或数据版本 |
| [生成器](generators.md) | 装配产物、设备数学贡献、物化序列、有效财务快照、`CalculationConfig` 及冻结评价确认 | Solver Bundle | 启动求解器、读取数据库、提交结果 |
| [求解运行时](solver-runtime.md) | Solver Bundle、取消与资源上下文 | ExecutionReceipt、原始输出 | 解释设备、构造数学问题、财务分析 |
| Result Adapter | Bundle、回执、声明输出 | `ComputeResult` | 重试、重新求解、读取当前项目 |
| 计算公共契约 | provider descriptor、状态、回执与结果类型 | 稳定跨模块 DTO | provider 发现、HTTP、ORM |

文件交接格式见 [Solver Bundle](../formats/solver-bundle.md)。

## 输入

生成器将税务关系与技术关系纳入同一次求解，财务评价使用同次解。关系分类与现金流口径见[财务计算](finance.md#税务参与规划与现金流)。运行时负责执行 Bundle。

计算入口只接受：

- 由装配模块签发的 `ValidatedAssemblyArtifact`；
- 与装配回执一致的预定义来源声明和对象输入资源；
- 预测目标类型及其所需时间、自回归和外生协变量绑定；
- 精确 generator、solver、executor 和 result adapter 版本；
- 固定预测算法与参数、耦合迭代的收敛条件、生成选项、求解选项、随机种子和资源上限；
- Worker 提供的 attempt、取消、租约和证据写入上下文。

不接受项目草稿、前端表单、ORM 对象、未经校验的 YAML/CSV、宿主机路径和动态函数入口。

## 输出

对外统一 `ComputeResult` 至少区分：

- 技术执行状态：完成、策略拒绝、普通进程失败、超时、取消、租约隔离、资源不足、执行协议失败；
- 数学状态：有解、不可行、无界、数值失败或未求解；
- 业务候选：容量、运行轨迹、目标值和约束余量；
- 算法、生成器、求解器、结果适配器、版本、种子和容差；
- assembly、Bundle、ExecutionReceipt 和原始输出；
- 可定位且不泄露内部路径的结构化诊断。

进程退出码为零不等于存在可推荐方案；不可行也不是内部异常。结果适配器必须完整映射这些语义。

## 开发思路

### 按预测与计算变量的依赖关系选择流程

计算阶段按同一快照展开 `constant`、重复 `data_repeat`、原样使用覆盖全期的 `data_full`。`data_predict` 按以下两类处理：

- **独立预测**：历史数据和未来已知条件足以确定全期序列，求解前生成一次，随后保持固定。
- **耦合预测**：序列依赖待求的配置或运行结果。先按算法规定的初始条件生成全期序列并求解，再用求解结果更新序列、重建完整模型并重新求解，直到满足收敛条件或达到轮数上限。

预测能力声明其依赖的计算输出，流程据此选择：一个耦合目标也进入迭代，多个独立目标仍直接求解。两类共存时，独立序列固定，耦合序列随计算更新。固定单位出力乘待求容量、储能 SOC 等可由技术方程表达的关系直接进入模型。

公开 `PredictorDescriptor` 使用必填布尔字段 `depends_on_solve_result` 声明依赖：`false` 为独立预测，`true` 为耦合预测；各 provider 显式填写，内置 naive 为 `false`。目录查询保留该声明，提交与运行按选中能力的声明分流。

`predict(previous=...)` 首轮接收 `None`，耦合轮次接收上一轮 `ComputeResult.outputs` 的只读业务结果，复用已有解与统一序列契约。算法的公开输入约定明确其消费的输出及初始参数；初始条件放入已有 `parameters`，按算法输入规则校验。

每轮使用相同冻结输入和完整运营时域；上一轮结果用于更新耦合预测，不改变项目初始条件。收敛定义由所选耦合算法提供，按下述公共接口返回无量纲指标；`convergence` 提供容差与总求解轮数上限（首次求解计一轮）。每轮先完成求解，再记录收敛指标与状态；达到上限但未满足容差时记录未收敛。最终结果与其使用的预测序列、回执和收敛状态一起封存，求解器最优性与外层收敛状态分别记录。

#### 预测参数与收敛能力

provider 通过公开 `validate_parameters(parameters)` 在计算配置输入边界校验自己的参数，说明初始预测的来源和所需结果。执行阶段直接使用已固定的参数，不重复校验。

耦合 provider 另提供纯函数式公开调用 `measure_convergence(previous_prediction, current_prediction, previous_outputs, current_outputs, parameters)`：预测产物复用 `PredictedTarget`，计算输出复用相邻两轮只读 `ComputeResult.outputs`。返回不可变 `PredictionConvergence`，包含 `metric`（指标标识）、`value`（非负有限数）、`unit`（固定为 `1`，无量纲）。独立 provider 不承担该调用。耦合能力注册时确认该公共调用可用，其返回值在自定义能力输出边界校验。

归一化公式及必要尺度属于算法的版本化输入规则；需要用户给定尺度时放入该算法的 `parameters`，平台不统一增加 `scale` 参数或推算尺度。多个耦合目标各自计算无量纲收敛量，全部不大于同一个 `tolerance` 才收敛，汇总值为各目标值的最大值。现有轮次证据记录目标、算法精确引用、指标、值、单位和汇总值；首轮没有相邻轮比较，不填写虚构的零差值。

算法精确版本固定其依赖声明、参数规则与收敛定义，同版本内容不可替换。任务沿已冻结配置中的精确引用解析这些能力，提交与执行使用同一版本，不另存可独立漂移的依赖标志。混合目标首轮全部生成，后续只调用耦合 provider；独立预测产物和序列复用首轮结果。

### 先冻结业务输入

装配模块只负责证明业务输入合法并产生规范装配。它不提前构造某个求解器的私有对象，也不提供命令字符串。

### 再由生成器形成可执行交接包

不同 GeneratorProvider 可以面向 HiGHS、Pyomo、专用仿真器或其他受支持求解器。每个生成器独立负责问题构造、内部单位和输入文件，但都产出同一 Solver Bundle 契约。

### 运行时只执行

SolverRuntime 根据 allowlist 解析结构化命令，在隔离目录执行，采集退出状态、日志和资源。它不根据求解器名称增加业务判断。

### 结果适配器只解释

与生成器配套的 ResultAdapter 将求解器状态、变量和指标映射回统一结果及业务单位。它不能重新读取最新设备目录，也不能对失败结果伪造空成功结果。

## Provider 关系

计算扩展至少包含以下可独立版本化的角色：

- `GeneratorProvider`：规范装配到 Solver Bundle；
- `ExecutorProvider`：受控 executable ID 到隔离运行方式；
- `ResultAdapterProvider`：声明输出到统一结果；
- 可选 `SolverCapabilityProvider`：求解器版本、能力与运行约束描述。

组合根负责发现、校验依赖并原子发布。装配阶段根据 descriptor 校验能力；Worker 按任务快照使用精确版本。任一角色缺失时在执行前失败，不回退到内置实现。

### 结果适配器契约与注册

- `ComputeResult` 分两层表达状态：`execution_status` 只解读回执执行事实（`completed`/`rejected`/`failed`/`timed_out`/`cancelled`/`fenced`/`resource_exhausted`/`protocol_failed`，七种回执状态各有准确归属：策略拒绝归 `rejected` 而非协议失败，租约隔离归 `fenced` 而非取消，`process_failed` 按终止原因拆为 OOM 的 `resource_exhausted` 与其余的 `failed`），`math_status`（`solved`/`infeasible`/`unbounded`/`numeric_failure`/`not_solved`）只解读求解器数学结局；`outputs` 为统一业务输出容器。本层不设业务结局字段，不依赖任务域；首个具体求解器语义见下（`ies.result-adapter.highs@2.0.0`）。
- 追溯经 `receipt_id` 唯一定位一张回执（同一 bundle + attempt 可有多张回执），`bundle_id` 指向 Bundle，不复制回执内容。
- 扩展适配器只需实现 `ResultAdapter` 公开身份（`descriptor`/`ref`/`available`）与唯一业务调用 `adapt(Bundle, receipt, outputs)`，经 `build_result_adapter_registry` 原子发布，按 manifest/回执固定的精确引用解析。
- 首个内置 `ies.result-adapter.highs@2.0.0`（配 `ies.generator.highs_milp@3.0.0`）解释求解 executable 写出的 `HIGHS_SOLUTION_V1` 文本（状态、目标值、列数与按索引的主解）。`kOptimal` 有完整主解时记 `solved`：变量按 MPS 列序对 `unit_map` 键反向换算到业务单位；目标值先加回 `objective_offset`（只加一次），再按 `objective_unit` 恢复业务单位——金额量纲守币种原值（财务币种，含 USD），物理量反向换算，单位由生成器确定，适配器不猜。声明分量按 `components` 用同一次主解求值发布到 `solution.components`（每分量 `{value, unit, caliber}`，年值按币种/年换算；声明缺失或引用未知 LP 变量即协议失败，不补零）。步序列按 `step_series` 声明用同一次主解恢复业务单位序列到 `outputs.series`（随 `solution` 请求发布，`metrics-only` 缺席）。只发布 parameters 已请求的 `solution`/`metrics`。`kInfeasible`/`kUnbounded` 映射同名数学状态且无业务输出；`kUnboundedOrInfeasible` 记 `not_solved` 且无输出。文件自报时间限制时明确报错（部分解不当成功解释）；垫片超时以非零退出且不写文件，走回执失败路径，监督超时走回执 `timed_out`，此时不读 solution 文件。其余非最优状态、未知状态、格式损坏、缺失输出、发布值非有限一律结果协议失败。结果填 `receipt_id` 与 `bundle_id` 追溯，不读取当前项目或最新 provider。

### 统一求解步序列

以下为生效目标契约，不表示当前链已实现。ResultAdapter 按 [Bundle 步序列映射](../formats/solver-bundle.md#步序列结果映射) 与同次主解恢复 `ComputeResult.outputs.series`：以不透明 `series_id` 为键，每项为 `{instance_id, variable_id, unit, values}`；`values` 顺序对应完整运营时域的连续 `step=0..n-1`，使用业务单位。不得由消费者拆解列名或序列 ID，不补零、不循环输入、不重新求解；实际结果解释失败沿已有结果协议错误处理。

步序列随已请求的 `solution` 发布，不增加 CalculationConfig 输出词汇；未请求 solution、无可用数学解时不发布 series。候选归属复用同一结果的已有 solution/bundle/receipt 身份，不为曲线另造候选。该新增输出不改变 ComputeResult 顶层状态及追溯字段；配套 provider 的必需映射变更按其协议版本处理。

application 只将已恢复统一序列序列化为结果对象，经 storage 落盘，并在已有封存事务内提交对象元数据、引用、证据和任务终态；外部 Blob 不参与数据库原子提交。序列身份、单位和顺序保留到公开读取 DTO，禁止 application 再解释 Bundle 列或做内部长度/hash 复核。Worker 只调用用例，不解释求解输出。读取协议见 [公共契约](../contracts.md#求解步序列读取)。

## 失败语义

| 阶段 | 示例 | 结果 |
|---|---|---|
| 生成前 | 装配产物不完整、generator 能力不足 | 不生成 Bundle，任务阻断 |
| 生成中 | 输入构造失败、非有限值、写文件中断 | 不发布部分 Bundle |
| 执行前 | Bundle schema/manifest 错误、命令越权、路径逃逸 | 不启动进程 |
| 执行中 | 超时、取消、OOM、求解器崩溃 | 保留失败回执与受控日志 |
| 解释时 | 缺输出、格式损坏、状态未知 | 结果协议失败，不发布业务结果 |
| 数学结局 | 不可行、无界、数值不稳定 | 发布明确数学状态与诊断 |

## 必须遵循的规范

- 业务单位到求解内部单位只在 GeneratorProvider 边界发生；反向换算只在 ResultAdapter；
- 生成器不访问网络、数据库、对象服务或环境变量，也不启动子进程；
- 运行时不读取装配语义，不补数据，不切换 solver；
- 命令使用参数数组和受信任 ID，禁止 shell 字符串；
- 所有输入、输出、manifest 和回执均按对象引用管理；
- 随机过程固定并回传种子；数值比较使用公开容差；
- 私有求解器对象、进程句柄和绝对路径不得跨公共边界；
- 重试由 Worker 按相同快照创建新 attempt，不在 generator/runtime 内隐式循环。

## 增加计算能力

1. 判断需求是新的公共方程能力、GeneratorProvider、ExecutorProvider 还是 ResultAdapter；
2. 先定义 descriptor、支持的 schema/mode、版本兼容和失败语义；
3. 准备最小规范装配与已知答案；
4. 实现纯生成并固定 Bundle；
5. 用通用 runtime 执行，补齐成功、不可行、超时和异常输出；
6. 通过 result adapter 映射统一状态和单位；
7. 在组合根原子注册并验证 readiness；
8. 证明 API、项目、装配和 GUI 无需增加具体求解器名称分支。

## 完成标准

- 生成器、运行时和结果适配器可以分别测试和替换；
- 仅凭 Bundle 可复现一次进程执行，凭装配和固定依赖可重建同一 Bundle；
- 非法装配、Bundle 和输出分别在所属边界失败；
- attempt 的输入、命令、日志、原始输出和统一结果形成连续证据链；
- 新生成器接入不修改通用 runtime，新 solver 接入不修改上游业务模块。

代码阅读顺序以目标结构为准：先读 computation 公共 contract，再读 [generators](generators.md)、[solver runtime](solver-runtime.md) 和 result adapters。迁移期间阅读现有 `engines` 时，要把“构造、执行、解释”分别标记并逐步移入对应边界，不能把现状当成长期 contract。
