# CalculationConfig

> 契约标识：`ies.calculation-config`；目标 `schema_version`：`"1.0.0"`；消费者：任务提交校验、调度编排与选定计算插件容器

`CalculationConfig` 固定单次计算任务的全部插件选择与运行参数。计算按项目基线完整连续时间轴执行，本契约不包含典型日缩减、规划目标约束、设备参数、财务规则或装配连接内容，与 `ValidatedAssemblyArtifact` 独立冻结。

## 顶层字段

`1.2.0` 单向流水线目标契约包含以下 **13 个必需顶层字段**（已移除无实际业务用途的 `convergence` 字段并将单一评价器升级为并联 `evaluators` 数组；`generator`、`solver`、`algorithm` 与 `seed` 无值时须显式传 `null`，禁止省略），拒绝未知字段与非有限数值：

| 字段 | 类型与格式 | 说明 |
|---|---|---|
| `schema` | 字符串 | 固定为 `ies.calculation-config` |
| `schema_version` | 字符串 | 固定为 `"1.0.0"` |
| `mode` | 非空字符串 | 计算模式，须在所选生成器或一体化算法声明的 `modes` 中（内置为 `planning`） |
| `predictions` | 数组（可为 `[]`） | 单趟前置预测列表，每项为 `{target: {instance, interface}, algorithm, parameters}`，`target` 不可重复；在逻辑预测环节内部按所选 `algorithm`（`predictor` 插件精确引用）同层分组执行，同插件多目标一次联合预测以保留共享潜变量相关性，全部完成后由框架合并为物化输入序列 |
| `generator` | `<ID>@<版本>` 或 `null` | 所选组装/生成器精确引用（分步模式必填；一体化模式固定为 `null`） |
| `solver` | `<ID>@<版本>` 或 `null` | 所选求解器精确引用（分步模式必填；一体化模式固定为 `null`） |
| `algorithm` | `<ID>@<版本>` 或 `null` | 所选一体化算法精确引用（一体化模式必填；分步模式固定为 `null`） |
| `evaluators` | 数组（可为 `[]`） | 并联后置评价器列表，每项为 `{evaluator: "<ID>@<版本>", parameters: {...}}`，`evaluator` 引用不可重复；`[]` 表示跳过评价环节直接交付已还原的标准工程解与逐时曲线 |
| `precision` | 枚举 | 计算精度：`low`、`medium`、`high` |
| `time_limit_seconds` | 正整数 | 请求求解时限（秒） |
| `options` | 对象（可为 `{}`） | 核心优化环节分槽参数：分步模式允许 `{generator, solver}` 子对象，一体化模式允许 `{algorithm}` 子对象（缺省子槽位视为空对象 `{}`），分别按对应插件的 `parameters_schema` 独立校验（预测与评价参数已在各自数组条目 `parameters` 内携带，互不冲突） |
| `seed` | 整数或 `null` | 随机种子（禁止布尔值） |
| `outputs` | 非空字符串数组 | 请求发布的结果输出项（顺序保留，如 `["solution", "metrics"]`） |

### 两种核心求解组合规则

| 字段 | 分步模式（`generator + solver`） | 一体化模式（`algorithm`） |
|---|---|---|
| `generator` | 精确引用 | `null` |
| `solver` | 精确引用 | `null` |
| `algorithm` | `null` | 精确引用 |
| `evaluators` | `0..N` 个独立评价器（分步模式同样支持 `[]`，因原变量列号还原已由平台底座在 `solver` 后自动完成） | `0..N` 个独立评价器（支持 `[]`） |

保存与任务提交闸门严格校验上述互斥规则，禁止同时指定 `algorithm` 与 `generator`/`solver` 或全部置为 `null`。

## JSON 示例

```json
{
  "schema": "ies.calculation-config",
  "schema_version": "1.0.0",
  "mode": "planning",
  "predictions": [
    {
      "target": { "instance": "pv1", "interface": "power_out" },
      "algorithm": "ies.predictor.naive@1.0.0",
      "parameters": {}
    }
  ],
  "generator": "ies.generator.highs_milp@1.0.0",
  "solver": "ies.solver.highs@1.7.2",
  "algorithm": null,
  "evaluators": [
    {
      "evaluator": "ies.evaluator.finance@1.0.0",
      "parameters": {}
    }
  ],
  "precision": "medium",
  "time_limit_seconds": 43200,
  "options": {
    "generator": {
      "acceleration": { "mode": "on_demand_mutex" }
    },
    "solver": {}
  },
  "seed": 42,
  "outputs": ["solution", "metrics"]
}
```

## 时限预算与校验边界

- **求解时限与外层监督**：内置 HiGHS 生成器将用户请求的 `time_limit_seconds` 原样传入求解器内部 `--time_limit`，并在外层容器/进程监督预留 `+600` 秒有界宽限（`command.timeout_seconds = time_limit_seconds + 600`）用于启动、MPS 读模与解写出；整体壁钟仍须在部署策略 `max_timeout_seconds` 允许范围内（超限在启动前拒绝）。
- **静态能力与可组合检查**：配置保存与任务提交阶段校验 `mode`、`precision`、`outputs`、上下游插件公开数据契约标识（`produces` / `consumes` 匹配及可选 `companions` 限制），并分别按各插件 `parameters_schema` 独立校验 `predictions[].parameters`、`options` 子槽位与 `evaluators[].parameters`；不兼容组合在提交闸门直接拒绝，不在执行期猜测转换。
- **`1.1.5` 至 `1.2.0` 一次性迁移与历史快照只读保护**：升级 `1.2.0` 前须先等待在途 `1.1.5` 运行任务结束（或由管理员取消）；升级时由版本化数据库迁移将项目已保存的可编辑 `CalculationConfig` 修订一次性转换为 13 字段新格式（移除 `convergence`；原 `evaluator` 映射为 `evaluators: [{"evaluator": "ies.evaluator.finance@1.0.0", "parameters": {}}]`；原 `options` 归入 `options.generator` 或 `options.algorithm`），运行期代码仅维护单一当前契约。历史已完成的 `1.1.5` 任务直接从已封存 `Evidence` 展示结果，历史冻结 `CalcSnapshot` 原样只读保留，严禁原地修改历史冻结数据。
