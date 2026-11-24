# CalculationConfig

> 契约标识：`ies.calculation-config`；目标 `schema_version`：`"1.0.0"`；消费者：任务提交校验、调度编排与选定算法/生成器容器

`CalculationConfig` 固定单次计算任务的全部算法与求解器选项。计算按项目基线完整连续时间轴执行，本契约不包含典型日缩减、规划目标约束、设备参数、财务规则或装配连接内容，与 `ValidatedAssemblyArtifact` 独立冻结。

## 顶层字段

以下 14 个顶层字段全部必需（无值字段如 `generator`、`solver`、`algorithm`、`evaluator`、`convergence` 与 `seed` 须显式传 `null`，禁止省略），拒绝未知字段与非有限数值：

| 字段 | 类型与格式 | 说明 |
|---|---|---|
| `schema` | 字符串 | 固定为 `ies.calculation-config` |
| `schema_version` | 字符串 | 固定为 `"1.0.0"` |
| `mode` | 非空字符串 | 计算模式，须在所选生成器或一体化算法描述符的 `modes` 中（内置为 `planning`） |
| `predictions` | 数组（可为 `[]`） | 预测配置列表，每项为 `{target: {instance, interface}, algorithm, parameters}`，`target` 不可重复 |
| `generator` | `<ID>@<版本>` 或 `null` | 所选组装/生成器精确引用（分步算法必填；一体化算法固定为 `null`） |
| `solver` | `<ID>@<版本>` 或 `null` | 所选求解器精确引用（分步算法必填；一体化算法固定为 `null`） |
| `algorithm` | `<ID>@<版本>` 或 `null` | 所选一体化算法精确引用（一体化算法必填；分步算法固定为 `null`） |
| `evaluator` | `<ID>@<版本>` 或 `null` | 所选结果适配与后期财务评价器精确引用（按需选择；为 `null` 时使用默认配套评价器） |
| `precision` | 枚举 | 计算精度：`low`、`medium`、`high` |
| `convergence` | `null` 或 `{tolerance, max_iterations}` | 当包含耦合预测目标时必填（`tolerance > 0` 无量纲阈值，`max_iterations >= 2`）；仅有独立预测或无预测时固定为 `null` |
| `time_limit_seconds` | 正整数 | 请求求解时限（秒） |
| `options` | 对象（可为 `{}`） | 算法/生成/求解扩展选项，按所选生成器或一体化算法描述符的选项 Schema（`options_schema` / `parameters_schema`）严格校验 |
| `seed` | 整数或 `null` | 随机种子（禁止布尔值） |
| `outputs` | 非空字符串数组 | 请求发布的结果输出项（顺序保留，如 `["solution", "metrics"]`） |

### 两种计算组合规则

| 字段 | 分步算法 | 一体化算法 |
|---|---|---|
| `generator` | 精确引用 | `null` |
| `solver` | 精确引用 | `null` |
| `algorithm` | `null` | 精确引用 |
| `evaluator` | 按需选择 | 按需选择 |

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
  "evaluator": null,
  "precision": "medium",
  "convergence": null,
  "time_limit_seconds": 43200,
  "options": {},
  "seed": 42,
  "outputs": ["solution", "metrics"]
}
```

## 时限预算与校验边界

- **求解时限与外层监督**：内置 HiGHS 生成器将用户请求的 `time_limit_seconds` 原样传入求解器内部 `--time_limit`，并在外层容器/进程监督预留 `+600` 秒有界宽限（`command.timeout_seconds = time_limit_seconds + 600`）用于启动、MPS 读模与解写出；整体壁钟仍须在部署策略 `max_timeout_seconds` 允许范围内（超限在启动前拒绝）。
- **静态能力检查**：配置保存与任务提交阶段依据所选生成器或一体化算法描述符校验 `mode`、`precision`、`solver`、`evaluator`、`outputs`、`options` 与预测算法参数规则（参见[预测参数与收敛能力](../modules/engines.md#预测参数与收敛能力)）；具体数学模型是否可成功降级或求解由对应计算环节判定。
