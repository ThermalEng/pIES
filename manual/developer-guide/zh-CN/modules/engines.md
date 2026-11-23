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

面向大规模长周期规划算例（如 25 年半小时级 `438,288` 步），可选前置启发式加速插件聚焦于**快速获得满足全周期物理与财务规则的高质量工程最优解**（而非耗费数倍时间去证明原宽泛定义域的理论全局对偶界）。插件基于可替换 `HeuristicProvider` 协议（首期内置唯一默认引擎），通过抽象高层决策结构引导任意后端正式求解器在缩窄后的高价值空间内快速收敛，不改变[计算生成](generators.md)与[求解运行时](solver-runtime.md)的三段式隔离边界。

### 插件协议、单次读模内存 API 与同格式适配

1. **轻量可替换协议与单引擎首发**：平台固定外层执行底座（模型读取、列映射保护、单调时钟总预算管控与不可行外扩兜底），将搜索核抽象为可替换的 `HeuristicProvider`（按 `<ID>@<版本>` 冻结引用）；首期仅内置一个结构感知默认引擎，不引入外部重型黑盒进化框架与动态加载机制。
2. **优先单次读模内存 API，跨进程保留同格式派生能力**：
   - 对于同进程绑定后端（如内置 HiGHS），优先执行**只读模型单次读入内存**，随后直接调用求解器原生内存接口（如 `changeColsBounds` / `setSolution`）完成缩界、锁定零容量变量及注入离散修复初解，避免巨型 MPS 文件二次序列化与二次解析带来的百秒级 I/O 开销；
   - 接入跨进程或多格式后端时，按“同格式模型进、同格式缩域模型出”原则产出派生模型，各格式适配器声明支持的问题类型（LP、MILP、QP/SOCP 或 NLP/MINLP）及保真变换范围。
3. **跨问题谱系加速机制与泛用结构**：所有 pIES 规划模型均具有**“上层低维规划决策（`planning.variables` 与 `planning.construction_units`）+ 下层海量高维运行与税务时步变量”**的主从结构，缩域引导在各问题谱系中的主要加速作用如下：

| 问题谱系 | 主要性能瓶颈 | 工程加速核心机制 |
|---|---|---|
| 纯 LP | 百万级行列稀疏矩阵分解或迭代慢 | 通过固定零容量冗余设备触发求解器预处理整块消元减维，并注入初解中心点 |
| MILP（主力） | 找不到可行解、海量伪分数互斥 0-1 变量与过松 Big-M | 缩窄高层容量界并经 `{property_ref: ...}` 联动压紧全周期 Big-M；结合离散固定 + LP 修复快速生成合法可行解 |
| QP / SOCP | 锥约束与长时域耦合导致内点分解开销大 | 淘汰无效候选设备减少活跃锥约束，并以候选解缩减内点收敛步数 |
| NLP / MINLP | 非凸多峰、McCormick 包络过松及对初始点高度敏感 | 直接利用收紧后的箱界压缩松弛包络，并依托工程可行初解稳定局部/全局收敛 |

### 完整时域保留与结果列映射不变量

- **不改变正式求解的全周期口径**：代表日与极值感知时间窗仅用于前置引擎快速评估与缩域；正式求解器消费的模型**必须保留完整全周期时步约束、公历自然月最大需量、跨期储能状态及全部税务关系**，从而保证最终工程解在全时域上 100% 物理守恒且同次解财务严丝合缝。
- **优先原位保留原列身份与映射**：改界、更新 Big-M 或将冗余设备变量固定为 `0` 时，应优先在原模型列空间内原位修改边界或固定界，交由正式求解器内部预处理自动消元，确保 `ResultAdapter` 按 `parameters.jsonl` 原列索引无误恢复设备/系统步序列与 `financial_facts`；若特定适配器主动删列，必须在交接给 `ResultAdapter` 前将解向量无损还原回原模型完整列向量。
- **领域元数据补足结构缺口**：通用模型格式（如原始 MPS）未必原生携带 `{property_ref: ...}`、公历月界和建设激活关联，由[生成器](generators.md#高层规划结构导出与前置缩域配合)随 Bundle 导出这类结构元数据供缩域与 Big-M 联动使用。

### 配置冻结、统一截止时钟与两级工程加速路线

- **复用 `CalculationConfig.options` 与统一单调时钟截止点**：前置加速开关与参数先复用[计算配置](../formats/calculation-config.md)的 `options` 字段（按 `GeneratorDescriptor.options_schema` 严格校验并随 `CalcSnapshot` 冻结），关闭时完全走原直通路径；开启时在受控执行器内基于单一 `time.monotonic()` 截止点统一扣除前置读取、代理缩域与修复耗时，剩余时间交由正式求解器执行，绝不让各阶段重复叠加总预算。

```text
[开启时] 冻结配置 options + 单调时钟截止点 deadline = now + time_limit_seconds
  ↓ 单次读模（同进程内存句柄；跨进程后端按需产出同格式派生文件）
  ↓ 第一阶段：极值感知（Extreme-Aware）代理缩域
直接收紧正式模型容量界 [L_tight, U_tight]、固定零容量设备、联动压紧 Big-M（保留原列映射）
  ↓ 第二阶段：结构感知 Fix-and-Repair / 可选松弛修复
固定或构造离散/容量候选决策 → 借由原模型 LP 修复或局部扫描修复补全全周期连续变量 → 注入初解
  ↓ 窄域精求（在剩余预算内直接在缩窄后的工程高价值区间求解）
  ├─ 有合法可行解：经原模型残差核查通过后直接交付工程最优解（不退回全空间重跑！）
  └─ 若窄域意外不可行（infeasible）：在剩余预算内自动外扩边界或恢复原域兜底重求
```

1. **第一阶段 — 极值感知（`Extreme-Aware`）代理缩域**：按公历自然月划分时间窗，在 `CalculationConfig.seed` 确定性控制下抽取“峰值负荷日、连续多日能量缺口/阴雨无风关键段（覆盖长时储能容量决定因素）、高价差日与代表日窗口”，在微型同构代理模型上快速锁定工程高价值容量区间 `[L_tight, U_tight]`，并把显著不经济设备锁定为 `0`，**直接写入正式模型生效**。
2. **第二阶段 — 结构感知 `Fix-and-Repair / Fix-and-Optimize` 与局部扫描增强**：在缩窄域内，优先通过结构感知规则或轻量子模型给出建设激活与运行离散开关决策，再利用求解器原生能力（如 `setSolution` 触发原模型内置 LP 修复或固定部分整数变量求解子 LP/MILP）自动补齐跨步储能 `SOC`、启停爬坡与全周期税务列；逐时 `O(T)` 互斥取整仅作为局部候选构造手段，任何候选解均须经过原模型约束、变量界与整数性残差检查后方可作为正式初解。

### 工程直接交付与结果状态诚实性

1. **有解直接交卷，不强制二次重跑全空间**：在经验缩域 `[L_tight, U_tight]` 内求得满足工程容差的合法解并经原模型约束核查后，**直接收敛发布完整工程方案、步序列与同次解财务评价**，不再为了证明原宽泛定义域 `[L, U]` 的理论对偶界而回退原模型重算。
2. **仅在窄域无解时外扩或恢复原域兜底**：经验子域不可行只说明缩域区间过紧，受控执行器应在剩余预算内自动外扩搜索区间或恢复原始只读模型边界兜底求解；预算耗尽仍未取得原域合法解时发布 `not_solved`，严禁仅凭缩域无解将原问题误判为 `infeasible`。
3. **诚实区分“工程子域收敛”与“原域理论最优证明”**：若未花费额外算力计算原完整定义域 `[L, U]` 的有效全局对偶界，不得拿窄域子问题的 `best_bound` 冒充原问题全局界——此时在 `outputs.solver_summary` 中如实标明采用启发式缩域求解及对应状态（按 `feasible` 对应 `restricted_results` 发布或将未认证的原域全局界与 `relative_gap` 置 `null`），仅当在原域边界或经严格代数保守缩界证明达到容差时才签发 `solved`；工程界面的全部方案查看、财务评价与导出能力在两者下均完整可用。

### 渐进实施与 Docker 对照验收（`S0 -> S3`）

- **`S0` 基线建档 → `S1` 最小契约打通 → `S2` 首个 Fix-and-Repair 引擎落地 → `S3` 真实 Docker 开关对照**：先建立当前版本原生参数（`presolve` / 内点法 / 启发式力度）基线定位“无解 vs 慢收敛”瓶颈，再打通配置冻结与内存改界/初解注入契约，随后实现首个结构感知修复与极值感知缩域引擎，并在 Docker 同预算环境下实测开启/关闭前后的端到端耗时、首次可行解时间、目标值提升及峰值 RSS（全周期松弛与后续代表日代理扩展均以实测净收益为准）。

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
