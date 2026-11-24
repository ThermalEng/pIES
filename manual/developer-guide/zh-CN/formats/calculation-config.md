# CalculationConfig

> 契约标识：`ies.calculation-config`；目标 `schema_version`：`"1.0.0"`；消费者：任务提交校验与 `GeneratorProvider`

`CalculationConfig` 固定单次计算任务的全部算法与求解器选项。计算按项目基线完整连续时间轴执行，本契约不包含典型日缩减、规划目标约束、设备参数、财务规则或装配连接内容，与 `ValidatedAssemblyArtifact` 独立冻结。

## 顶层字段

以下 12 个顶层字段全部必需（`convergence` 与 `seed` 无值时显式传 `null`，禁止省略），拒绝未知字段与非有限数值：

| 字段 | 类型与格式 | 说明 |
|---|---|---|
| `schema` | 字符串 | 固定为 `ies.calculation-config` |
| `schema_version` | 字符串 | 固定为 `"1.0.0"` |
| `mode` | 非空字符串 | 计算模式，须在所选 `GeneratorDescriptor.modes` 中（内置为 `planning`） |
| `predictions` | 数组（可为 `[]`） | 预测配置列表，每项为 `{target: {instance, interface}, algorithm, parameters}`，`target` 不可重复 |
| `generator` | `<ID>@<版本>` | 所选生成器精确引用（分步模式必填；一体化算法模式互斥为空） |
| `solver` | `<ID>@<版本>` | 所选求解器精确引用（分步模式必填；一体化算法模式互斥为空） |
| `precision` | 枚举 | 计算精度：`low`、`medium`、`high` |
| `convergence` | `null` 或 `{tolerance, max_iterations}` | 当包含耦合预测目标时必填（`tolerance > 0` 无量纲阈值，`max_iterations >= 2`）；仅有独立预测或无预测时固定为 `null` |
| `time_limit_seconds` | 正整数 | 请求求解时限（秒） |
| `options` | 对象（可为 `{}`） | 生成/求解扩展选项，按所选 `GeneratorDescriptor` 的 `options_schema` 严格校验 |
| `seed` | 整数或 `null` | 随机种子（禁止布尔值） |
| `outputs` | 非空字符串数组 | 请求发布的结果输出项（顺序保留，如 `["solution", "metrics"]`） |

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
  "precision": "medium",
  "convergence": null,
  "time_limit_seconds": 43200,
  "options": {},
  "seed": 42,
  "outputs": ["solution", "metrics"]
}
```

## 时限预算与校验边界

- **求解时限与外层监督**：内置 HiGHS 生成器将用户请求的 `time_limit_seconds` 原样传入求解器内部 `--time_limit`，并在外层进程监督预留 `+600` 秒有界宽限（`command.timeout_seconds = time_limit_seconds + 600`）用于启动、MPS 读模与解写出；整体壁钟仍须在部署策略 `max_timeout_seconds` 允许范围内（超限在启动前拒绝）。
- **静态能力检查**：配置保存与任务提交阶段依据所选 `GeneratorDescriptor` 校验 `mode`、`precision`、`solver`、`outputs`、`options` 与预测算法参数规则（参见[预测参数与收敛能力](../modules/engines.md#预测参数与收敛能力)）；具体数学模型是否可成功降级由生成阶段判定。
- **一体化算法与可选评价器（`1.1.5` 目标）**：在统一插件契约下，分步模式继续指定精确的 `generator` 与 `solver`（并可按需指定配套 `evaluator`，缺省使用默认评价器）；当选用 `kind: algorithm` 的极简一体化插件时，通过一体化 `algorithm` 引用字段选择，与 `generator` / `solver` 严格互斥，保存与提交边界拒绝混用或未知字段，避免强迫一体化插件伪造拆分引用。
