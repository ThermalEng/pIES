# 计算生成与求解

> 代码边界：`backend/iesplan/computation/`；相关子模块：[计算生成器](generators.md)、[求解运行时](solver-runtime.md)、[Solver Bundle](../formats/solver-bundle.md)

`computation` 将已校验的业务系统转换为具体求解器计算包并执行，再将原始求解结果还原为统一的业务输出与后期财务评价事实，使求解器扩展与容器隔离执行彻底解耦于项目与界面逻辑。

## 计算全链路与子模块分工

在 `1.2.0` 单向流水线架构下，预测（`predictor`）、组装（`generator`）、求解（`solver`）、一体化算法（`algorithm`）与并联后置评价（`evaluator`）按统一 `/work/in → /work/out` 契约由 `backend` 经 `job_runner` 分步拉起断网临时计算容器执行；跨环节所需的只读不可变上下文由调度框架通过硬链接零拷贝组装进各环节只读 `/work/in`：

```text
ValidatedAssemblyArtifact + CalculationConfig
  ↓ 序列物化（constant / data_repeat / data_full + predictions[] 同层按插件分组联合预测）
  ├─ 分步模式：GeneratorProvider ──→ SolverRuntime ──→ backend 独立受限子进程按 variable_map 还原标准工程解
  └─ 一体化模式：Algorithm 容器 ─────────────────────→ 直接产出标准工程解
                                                              ↓
        最终封存结果 ←── evaluators[]（仅在有可行解时运行；局部评价失败保留工程解并支持单点重试）
```

| 子模块 | 职责 | 不负责 |
|---|---|---|
| 序列物化与前置预测 | 将静态来源与按插件同层分组执行的单趟预测结果合并为全周期对齐 `MaterializedSet` | 修改装配或跨容器往返循环 |
| [生成器](generators.md) | 数学降级、正向单位换算并产出求解模型文件与 `variable_map` | 访问数据库、直接联网 |
| [求解运行时](solver-runtime.md) | 经容器代理在隔离临时容器内执行求解并产出 0-based 原列序标准原变量解与 `ExecutionReceipt` | 解释设备语义、单位换算或搬运业务文件 |
| 受限子进程还原与并联评价 | `backend` 在独立受限子进程内流式结合 `variable_map` 忠实还原标准工程解；`0..N` 个 `evaluator` 并联运行并支持单评价器失败重试 | 重跑主求解、读取当前草稿或替第三方求解器重验算 |

## 同层分组预测与单容器内部循环接口

- **同层分组与多目标同源联合预测**：任务提交阶段按各预测插件自身的 `parameters_schema` 独立校验 `predictions[].parameters`。执行阶段在逻辑预测环节内部按所选 `predictor` 插件分组运行（路由至同一插件的多个关联目标在单次容器调用内联合预测，保留共享气温/日照潜变量相关性），全部完成后由框架合并为物化输入序列，不再设依赖求解结果的跨容器外循环。
- **单容器内部循环接口（`iteration`）**：凡含内部迭代寻优（如元启发式搜索、仿真优化或单求解器内部多轮恢复）的插件，统一在**单个临时计算容器内部**自闭环执行，并通过 `/work/out/iterations.jsonl` 专用通道逐轮追加收敛度量记录（`iteration`、`metric`、`value`、`unit: "1"`），严格受单容器总超时约束。

## 求解器内建结构加速

面向大规模长周期规划算例，结构加速作为求解执行器的内建能力提供，通过冻结的 [CalculationConfig](../formats/calculation-config.md) `options` 控制开关与参数，不改变[计算生成](generators.md) → Solver Bundle → [求解运行时](solver-runtime.md) → `ResultAdapter` 的三段式隔离边界；非 HiGHS 求解路线继续沿既有 `GeneratorProvider`、`ExecutorProvider` 与 `ResultAdapterProvider` 公开契约扩展。

- **配置与隔离契约**：加速开关与策略随 `CalcSnapshot` 冻结并受公开 `options_schema` 约束；内置 `ies.generator.highs_milp@1.0.0` 默认开启无损结构加速（`acceleration.mode: "on_demand_mutex"`），显式设 `"off"` 时严格直通原始模型求解，开启时保持全部执行均处于同一隔离运行时约束内。
- **合法结构松弛与原模型权威性**：[生成器](generators.md#求解加速结构元数据导出)仅随 Bundle 导出经编译期证明可安全松弛的独立辅助结构元数据，不越界启动求解；求解执行器可在迭代过程中对上述已证明结构暂时省略约束，但原始模型不可变且始终是最终可行性、列映射与状态证明的唯一权威依据（无法证明结构安全时必须退回原始求解路径），最终交付方案必须在完整全周期原模型上满足物理守恒、自然月需量、储能连续性与税务约束。
- **时限、保解与状态诚实性**：全阶段严格遵守总时间预算不超限续发，并在预算耗尽前保全已核查通过的最佳合法可行解；`ResultAdapter` 仅在无损等价或合法松弛达容差最优且通过原模型约束核查时签发 `solved`，凡时限提前停止或任何搜索域受限的求解统一发布 `feasible` 且未经认证的原域全局 `best_bound` 与 `relative_gap` 置 `null`。

## 统一结果模型与适配器契约

`ComputeResult` 通过两个正交字段表达计算结局，避免进程状态与数学状态混为一谈：

- **`execution_status`**：严格对应执行回执事实，取值为 `completed`、`rejected`（策略/输入拒收）、`failed`（非 OOM 进程错误）、`timed_out`、`cancelled`、`fenced`（租约撤销）、`resource_exhausted`（OOM/内存耗尽）与 `protocol_failed`（输出协议损坏或缺失）。
- **`math_status`**：反映求解器数学结局，取值为 `solved`（容差内已证明最优）、`feasible`（存在可行解但未证明最优，如达到时限）、`infeasible`、`unbounded`、`numeric_failure` 与 `not_solved`。

### ResultAdapter 规范与内置 HiGHS 适配器

- **注册与调用**：扩展适配器实现公开描述符与 `adapt(bundle, receipt, outputs)`，通过独立注册表按精确引用解析。
- **状态与可行解判定（`ies.result-adapter.highs@1.0.0`）**：
  - 适配器须综合求解器状态、实际求解域及原模型验证证明共同判定 `math_status`：仅在无损原域或合法松弛达 `kOptimal` 且原模型核查通过时映射 `solved`；搜索域受限时即便 `kOptimal` 亦映射 `feasible` 且未经认证的原域全局界与 Gap 置 `null`；正常 `kTimeLimit` 有完整可行解时映射 `feasible`，无可行解时映射 `not_solved`；`kInfeasible` / `kUnbounded` 映射同名状态；`kUnboundedOrInfeasible` 映射 `not_solved`；监督强制超时、取消、OOM 或进程错误保持原执行状态，不读取残留输出升级为成功。
  - 启用连续需量上包络且求解未证明严格最小时，适配器在发布解前核对月峰值紧度，发现虚高时拒绝发布失真财务账单并按终止情形映射为 `not_solved` 或 `numeric_failure`。
- **同次主解恢复**：`solved` 与 `feasible` 使用同一主解按列位置映射恢复规划变量、财务分量、设备/系统步序列、财务事实（`outputs.financial_facts`）与构模来源映射（`outputs.model_sources`），执行目标偏移还原与唯一反向物理单位换算。

### 求解终止与最优性摘要

适配器正常解释求解状态后发布 `outputs.solver_summary`，包含 `termination_reason`（`optimal` / `time_limit` / `infeasible` / `unbounded` / `not_solved`）、`math_status`、`has_feasible_solution`、`objective_value`、`objective_unit`、`best_bound` 与 `relative_gap`：

- `objective_value` 与 `best_bound` 使用同一业务单位与偏移量；`relative_gap` 保留求解器原生相对间隙（MIP 在容差内最优仍保留真实间隙，已最优 LP 在无 MIP 统计时界取目标值、Gap 为 `0`；搜索域受限或未知时，未经认证的原域全局 `best_bound` 与 `relative_gap` 保持 `null`）。
- 公开结果视图直接投影该封存摘要：`completed + feasible` 在任务层对应 `restricted_results`（可查看方案与同次财务评价，但明确未证明最优）；`completed + not_solved` 对应 `insufficient_evidence`。

### 统一求解步序列

`ResultAdapter` 依据 Bundle 声明与同次主解分别恢复设备步序列 `outputs.series`（以 `series_id` 为键，每项为 `{instance_id, variable_id, unit, values}`）与系统辅助序列 `outputs.system_series`（以 `series_id` 为键，每项为 `{block, variable, unit, values}`），`values` 为业务单位下对应完整运营时域连续步的数值列表。两者共享同一 `series_id` 命名空间，随 `solution` 请求发布；对外读取与导出协议见[公共契约](../contracts.md#求解步序列读取)。
