# 计算生成与求解

> 代码边界：`backend/iesplan/computation/`；相关子模块：[计算生成器](generators.md)、[求解运行时](solver-runtime.md)、[Solver Bundle](../formats/solver-bundle.md)

`computation` 将已校验的业务系统转换为具体求解器计算包并执行，再将原始求解结果还原为统一的业务输出，使求解器扩展与进程安全控制解耦于项目与界面逻辑。

## 计算全链路与子模块分工

```text
ValidatedAssemblyArtifact + CalculationConfig
  ↓ 序列物化（constant / data_repeat / data_full / data_predict）
GeneratorProvider ──→ Solver Bundle ──→ SolverRuntime
                                             ↓
ComputeResult ←── ResultAdapter ←── ExecutionReceipt + 原始输出
  │
  ├─ 独立预测：直接采用结果
  └─ 耦合预测：按上一轮输出更新预测序列 → 重建并求解 → 判定收敛
```

| 子模块 | 职责 | 不负责 |
|---|---|---|
| 序列物化 | 按项目基线将四类来源物化为全周期对齐 `MaterializedSet` | 修改装配或数据版本 |
| [生成器](generators.md) | 数学降级、单位换算与生成 Solver Bundle | 启动进程、访问数据库 |
| [求解运行时](solver-runtime.md) | 隔离执行 Bundle 命令并生成 `ExecutionReceipt` | 解释设备语义或求解结果 |
| 结果适配器 | 将原始解与回执映射为规范单位的 `ComputeResult` | 重跑求解、读取当前草稿 |

## 预测调度与收敛能力

任务提交按选中预测算法描述符的公开字段 `depends_on_solve_result` 自动选择执行路径：

- **独立预测（`false`）**：在首次生成前执行一次训练与序列生成，全过程保持固定；
- **耦合预测（`true`）**：`predict(previous=...)` 首轮接收 `None`，后续轮次接收上一轮只读 `ComputeResult.outputs` 并重新物化、构模与求解，直至所有耦合目标均满足 `CalculationConfig.convergence.tolerance` 或达到 `max_iterations` 上限（不收敛时保留对应收敛状态与最终解）。

### 预测参数与收敛能力

- **参数校验**：预测 Provider 通过公开 `validate_parameters(parameters)` 在计算配置保存或任务提交边界完成参数校验，执行阶段直接消费已冻结参数。
- **收敛度量**：耦合算法 Provider 必须提供纯函数 `measure_convergence(previous_prediction, current_prediction, previous_outputs, current_outputs, parameters)`，返回不可变的 `PredictionConvergence`（含指标标识 `metric`、非负有限值 `value` 与无量纲单位 `"1"`）；多目标共存时汇总值为各目标收敛量最大值。首轮求解无前一轮对比，不生成零差值。

## 通用前置启发式加速与两级漏斗协同

面向大规模长周期规划算例（如 25 年半小时级 `438,288` 步），可选前置启发式加速插件通过抽象高层决策结构为任意后端正规求解器提供前置缩域与可选热启动引导，而不改变[计算生成](generators.md)与[求解运行时](solver-runtime.md)的三段式隔离边界，也不由 Worker 跨 Bundle 改写装配约束。

### 同格式主契约、适配器边界与可选热启动

1. **“同格式模型进、同格式缩域模型出”主契约**：前置缩域插件以正式求解器的模型输入契约为准（例如 MPS 输入产出同一后端可直接读取的派生 MPS；接入其他模型格式时由对应格式适配器读写）。通用缩域逻辑处理变量、约束与随 Bundle 导出的领域元数据，各格式适配器显式声明支持的问题类型（纯 LP、MILP、QP/SOCP 或 NLP/MINLP）及可保真变换范围。
2. **仅靠派生模型即可独立工作，热启动单独解耦**：插件通过改写变量上下界、固定零容量或冗余离散变量及联动更新 Big-M 系数输出同格式缩域派生模型，无需强绑定特定求解器内存句柄即可工作；初解热启动（`incumbent_hint`，可由第二级全周期松弛与修复生成）作为可移植的可选增强，仅在目标后端具备对应初解注入接口且经对照证实存在净收益时传递。
3. **跨问题谱系加速机制与泛用结构**：pIES 各数学形式均具备**“上层低维规划决策（`planning.variables` 与 `planning.construction_units`）+ 下层海量高维运行与税务时步变量”**的主从结构，对应前置引导契约在派生模型中体现为三类动作：

| 问题谱系 | 主要性能瓶颈 | 同格式派生模型与可选热启动加速机制 |
|---|---|---|
| 纯 LP | 百万级行列稀疏矩阵分解或迭代慢 | 通过固定零容量冗余设备触发求解器预处理整块消元减维；后端支持时附带初始点 |
| MILP（主力） | 海量伪分数互斥 0-1 变量、过松 Big-M 与缺乏剪枝初解 | 经 `{property_ref: ...}` 联动收紧全周期 Big-M 系数并固定冗余开关；支持时注入 `x_init*` |
| QP / SOCP | 锥约束与长时域耦合导致内点分解开销大 | 固定零容量候选设备减少活跃锥约束；支持时提供热启动中心点 |
| NLP / MINLP | 非凸多峰、McCormick 包络过松及对初始点高度敏感 | 依托收紧后的箱界压缩松弛包络；有初解接口时注入可行 `x_init*` 稳定收敛 |

### 完整时域保留与结果列映射不变量

- **不改变正式模型的全周期口径**：代表日、极值窗口或代理降维只允许在插件内部用于决定缩域方向；交给正式求解器的同格式派生模型**必须保留完整全周期时步约束、公历自然月最大需量、跨期储能状态及全部税务关系**。
- **优先原位保留原列身份与映射**：缩界、更新 Big-M 或将冗余变量固定为 `0` 时，应优先保留原变量身份与列序号不变（例如修改列上下界或标为固定界），交由正式求解器内部预处理消元，使 `ResultAdapter` 可直接按 `parameters.jsonl` 原列索引恢复设备与系统步序列及同次解财务事实；若适配器主动删列，则**必须在交接给 `ResultAdapter` 前将主解向量无损还原为原模型完整列向量**。
- **领域元数据补足契约缺口**：因通用模型文件（如原始 MPS）未必原生携带 `{property_ref: ...}`、公历月界和建设激活关联，由[生成器](generators.md#高层规划结构导出与前置缩域配合)随 Bundle 导出这类结构化元数据供缩域与 Big-M 联动使用。

### 开关冻结、受控执行流与两级漏斗算法

- **开关冻结与共享总预算**：前置加速开关与参数冻结于[计算配置](../formats/calculation-config.md)（`CalcSnapshot`）中，并在配置提交阶段按模型格式、问题类型与后端能力完成静态匹配。开关关闭时，正式求解器直接读取只读原始模型；开关开启时，同一受控执行流程在任务隔离工作目录读取只读原始模型，生成同格式派生模型后再交给已选求解器，**同时保留只读原始模型文件供原域认证或恢复求解使用**。前置缩域（含派生模型读写）与正式求解**共享用户请求的 `time_limit_seconds` 总时限预算**。

```text
[开启时] 只读原始模型 + 结构元数据（保留只读原文件于工作目录）
  ↓ 第一级漏斗：极值保留时域代理缩域（Surrogate Narrower）
同格式全周期派生模型（收紧界 / 固定冗余变量 / 联动 Big-M，保留原列映射）
  ↓ 第二级漏斗（可选且后端支持热启动时）：全周期连续松弛 + O(T) 解析互斥修复 + O(N) 财务前向递推
全周期自洽初解 x_init*（可选注入后端）
  ↓ 共享总时限内执行正式求解（必要时认证原域全局界或恢复原域求解）
标准主解（原模型列序）
```

1. **第一级漏斗 — 极值保留时域代理缩域**：按公历自然月划分时间窗，在 `CalculationConfig.seed` 确定性控制下抽取“各月强制极值窗口（最大净负荷与最低可再生出力连续段）+ 带天数权重的代表日窗口”；支持线性/MILP 代理核（求解压缩同构代理模型并结合对偶灵敏度外扩）与无梯度进化核（DE / CMA-ES，取精英种群包络外扩），结合代数界推导生成缩域界并写出同格式派生模型。
2. **第二级漏斗 — 全周期松弛与解析修复热启动**：在后端支持热启动且对照实验证实净收益时启用：使用内点法（IPM/Barrier）或一阶算子分裂法（PDLP）求解全周期连续松弛（规避单纯形法退化）；对 `x * y = 0` 互斥二元变量执行 `O(T)` 线性扫描解析取整（单侧为零时直接赋 `{0, 1}` 消除伪分数二元变量，极少数真实冲突步锁定净流方向后局部回代修复跨步状态）；最后按确定性财务与税务纯函数执行一次 `O(N)` 前向递推，填满月最大需量、VAT 留抵与所得税亏损结转列形成完整自洽的 `x_init*`。

### 可证明缩域与经验缩域的结果语义

必须严格区分两类缩域来源，不得混淆子域最优性与原问题全局最优性：

| 缩域来源 | 数学性质 | 全局最优性、恢复策略与 `math_status` 判定规则 |
|---|---|---|
| **可证明缩域**（由原模型代数关系推导的保守界） | 与原可行域严格等价，不丢失任何原问题真最优解 | 可直接写入正式模型；在总预算内求解达到容差即可直接按原问题合法签发 `solved`（`optimal`）并保留原生 `best_bound` 与 `relative_gap`。 |
| **经验缩域**（代表日、影子价格或精英种群得到的经验搜索区间） | 子域内有解**不代表**未漏掉原域真最优（例如原域最优容量为 `8` 而经验子域为 `[3, 5]` 时，子域有可行解且不会触发无解回退，但子域最优与子域界仅覆盖 `[3, 5]`）；子域无解亦**不代表**原问题无解 | （1）若需要原问题最优结论（`solved`），必须用**原完整定义域 `[L, U]` 的有效全局对偶界**认证缩域解，或在剩余预算内**恢复只读原始模型继续求解**；（2）总预算用尽时，取得原域合法可行解即发布 `feasible`（`restricted_results`），若原域全局对偶界未知则 `outputs.solver_summary` 的 `best_bound` 与 `relative_gap` **必须留空（`null`）**，严禁将子域对偶界冒充全局界；（3）经验子域返回不可行只说明本次经验区间无解，剩余预算内必须恢复原域求解，预算耗尽仍未在原域求得结果时发布 `not_solved`，严禁仅凭经验子域无解将原问题判为 `infeasible`。 |

### 两步验证与基准对照要求

1. **多后端格式与反例契约验证**：同一缩域核心须通过至少两种后端或模型格式适配，验证派生模型可由正式求解器直接读入、求得的解可按原列映射无损恢复为原模型设备/系统序列与 `financial_facts`，并显式覆盖“经验子域可行但漏掉原域最优（如原域最优 `8`、子域 `[3, 5]`）”与“经验子域不可行但原域可行”两类反例。
2. **Docker 共享总预算开关对照基准**：在 Docker 环境对各受支持后端执行前置开关对照测试，将前置缩域耗时与派生模型读写耗时全额计入同一 `time_limit_seconds` 总预算，实测比较首次可行解时间、同预算目标值与原域真实 Gap；第二级漏斗（全周期松弛、互斥修复与财务前向递推）的实际增益同样依此开关对照裁决。

## 统一结果模型与适配器契约

`ComputeResult` 通过两个正交字段表达计算结局，避免进程状态与数学状态混为一谈：

- **`execution_status`**：严格对应执行回执事实，取值为 `completed`、`rejected`（策略/输入拒收）、`failed`（非 OOM 进程错误）、`timed_out`、`cancelled`、`fenced`（租约撤销）、`resource_exhausted`（OOM/内存耗尽）与 `protocol_failed`（输出协议损坏或缺失）。
- **`math_status`**：反映求解器数学结局，取值为 `solved`（容差内已证明最优）、`feasible`（存在可行解但未证明最优，如达到时限）、`infeasible`、`unbounded`、`numeric_failure` 与 `not_solved`。

### ResultAdapter 规范与内置 HiGHS 适配器

- **注册与调用**：扩展适配器实现公开描述符与 `adapt(bundle, receipt, outputs)`，通过独立注册表按精确引用解析。
- **状态与可行解判定（`ies.result-adapter.highs@1.0.0`）**：
  - `kOptimal` 映射 `solved`；正常 `kTimeLimit` 有完整可行解时映射 `feasible`，无可行解时映射 `not_solved`；`kInfeasible` / `kUnbounded` 映射同名状态；`kUnboundedOrInfeasible` 映射 `not_solved`；监督强制超时、取消、OOM 或进程错误保持原执行状态，不读取残留输出升级为成功。
  - 启用连续需量上包络且求解未证明严格最小时，适配器在发布解前核对月峰值紧度，发现虚高时拒绝发布失真财务账单并按终止情形映射为 `not_solved` 或 `numeric_failure`。
- **同次主解恢复**：`solved` 与 `feasible` 使用同一主解按列位置映射恢复规划变量、财务分量、设备/系统步序列、财务事实（`outputs.financial_facts`）与构模来源映射（`outputs.model_sources`），执行目标偏移还原与唯一反向物理单位换算。

### 求解终止与最优性摘要

适配器正常解释求解状态后发布 `outputs.solver_summary`，包含 `termination_reason`（`optimal` / `time_limit` / `infeasible` / `unbounded` / `not_solved`）、`math_status`、`has_feasible_solution`、`objective_value`、`objective_unit`、`best_bound` 与 `relative_gap`：

- `objective_value` 与 `best_bound` 使用同一业务单位与偏移量；`relative_gap` 保留求解器原生相对间隙（MIP 在容差内最优仍保留真实间隙，已最优 LP 在无 MIP 统计时界取目标值、Gap 为 `0`；未知值保持 `null`）。
- 公开结果视图直接投影该封存摘要：`completed + feasible` 在任务层对应 `restricted_results`（可查看方案与同次财务评价，但明确未证明最优）；`completed + not_solved` 对应 `insufficient_evidence`。

### 统一求解步序列

`ResultAdapter` 依据 Bundle 声明与同次主解分别恢复设备步序列 `outputs.series`（以 `series_id` 为键，每项为 `{instance_id, variable_id, unit, values}`）与系统辅助序列 `outputs.system_series`（以 `series_id` 为键，每项为 `{block, variable, unit, values}`），`values` 为业务单位下对应完整运营时域连续步的数值列表。两者共享同一 `series_id` 命名空间，随 `solution` 请求发布；对外读取与导出协议见[公共契约](../contracts.md#求解步序列读取)。
