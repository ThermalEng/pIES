# CalculationConfig

> 契约标识：`ies.calculation-config`；目标 schema：`1.0.0`；选择方：计算提交；消费者：GeneratorProvider（生成前能力核对）。

CalculationConfig 固定一次计算的全部选择：mode、预测目标所用算法与参数、generator、solver、计算精度、阶段二适用时的收敛容差与最大迭代数、时间限制、选项、随机种子和输出选择。计算按项目基线完整连续序列进行，不采用典型日、分段近似、容量步长或场景缩减；本契约不含离散化字段，也不含规划目标/变量/约束、设备参数、财务配置、资源或装配内容。

CalculationConfig 不进入 assembly，在计算包生成时与 `ValidatedAssemblyArtifact` 一起固定。

## 字段

| 字段 | 形态 | 说明 |
|---|---|---|
| `schema` | `ies.calculation-config` | 字头 |
| `schema_version` | `1.0.0` | 字头 |
| `mode` | 非空字符串 | 计算模式，由 GeneratorDescriptor.modes 声明；首个内置模式为 `planning` |
| `predictions` | 列表（可空列表，不接受 null） | 预测配置；每项 `{target: {instance, interface}, algorithm, parameters}`，`algorithm` 为精确 provider 引用，`parameters` 为对象；`target` 不得重复 |
| `generator` | `<稳定ID>@<语义化版本>` | 所选生成器精确引用 |
| `solver` | `<稳定ID>@<语义化版本>` | 所选求解器精确引用 |
| `precision` | `low`、`medium`、`high` | 计算精度（与界面精度语义一致） |
| `convergence` | null 或 `{tolerance, max_iterations}` | 阶段二收敛：正有限容差与正整数迭代数；多个 `data_predict` 时是否必填由后续 application 结合 artifact 判断 |
| `time_limit_seconds` | 正整数 | 时间限制（秒） |
| `options` | 对象（可空对象，不接受 null） | 生成/求解选项；生成前由所选 descriptor 的 options schema 核对，未知字段与类型/范围错误直接拒绝 |
| `seed` | 整数（不接受布尔值）或 null | 随机种子 |
| `outputs` | 非空字符串列表 | 输出请求，顺序保留 |

12 个顶层字段全部必需（`convergence` 与 `seed` 允许显式 null，但不得省略）；未知字段一律拒绝，不静默丢弃，不补默认。`NaN`、`Infinity`、`-Infinity` 不得进入（含嵌套值）。

## 示例

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
  "generator": "acme.generator@1.0.0",
  "solver": "ies.solver.highs@1.7.2",
  "precision": "medium",
  "convergence": { "tolerance": 0.001, "max_iterations": 50 },
  "time_limit_seconds": 600,
  "options": { "gap_rel": 0.001 },
  "seed": 42,
  "outputs": ["solution", "metrics"]
}
```

## 校验边界

公开 CalculationConfig 输入边界校验全部字段形态与领域规则；生成前由 GeneratorDescriptor 核对 mode、精度与 solver 兼容性。多个 `data_predict` 时收敛必填性由后续 application 结合 artifact 判断，本契约载体不重读装配。
