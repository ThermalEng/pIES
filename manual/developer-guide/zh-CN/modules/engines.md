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
