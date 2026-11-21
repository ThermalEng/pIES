# 设备模型 YAML

> 契约标识：`ies.device-model`；目标 `schema_version`：`"1.0.0"`；推荐文件名：`<device-id>.device.yaml`

设备模型 YAML 定义设备的纯技术规格：不随时间变化的技术参数（`properties`）、对外或数据接口（`interfaces`）以及声明式技术方程（`equations`）。设备模型不设独立语义版本（由稳定 ID、发布 `revision` 与校验回执固定），也不包含价格、成本、财务假设、计算精度、算法选择或可执行代码路径。

## 完整示例

```yaml
schema: ies.device-model
schema_version: "1.0.0"

device:
  id: acme.device.heat_pump
  names:
    zh-CN: 热泵
    en-US: Heat Pump

properties:
  cop:
    value: 3.2
    unit: "1"
    valid_range:
      minimum: 1
      maximum: 10
interfaces:
  electricity_in:
    type: in
    carrier: electricity
    unit: kW
    valid_range:
      minimum: 0
      maximum: null
  heat_out:
    type: out
    carrier: heat
    unit: kW
    valid_range:
      minimum: 0
      maximum: null
equations:
  variables: {}
  relations:
    - id: heat_conversion
      expression: "heat_out[t] = electricity_in[t] * cop"
```

## 顶层字段

正式模型顶层固定为以下六个必填字段（无内容时写 `{}` 或 `[]`）；未实例化模板额外允许顶层 `inputs`，实例化完成后删除 `inputs` 并按正式模型完整校验。

| 字段 | 作用 |
|---|---|
| `schema` | 固定为 `ies.device-model` |
| `schema_version` | 固定为 `"1.0.0"` |
| `device` | 稳定设备身份 `{id, names}`；`id` 为小写命名空间字符串（不含单设备语义版本），`names` 为本地化显示名 |
| `properties` | 不随时间变化的纯技术常量 |
| `interfaces` | 随计算步变化的序列接口定义 |
| `equations` | `properties`、接口序列与内部变量之间的受限声明式关系 |
| `inputs` | 仅未实例化模板可用；声明须由用户在实例化时填写的已有同构字段路径 |

## `properties`

`properties` 的键为稳定属性 ID，每项包含 `value`、`unit` 与 `valid_range`：

```yaml
properties:
  efficiency:
    value: 0.9
    unit: "1"
    valid_range:
      minimum: 0
      maximum: 1
```

- `value` 必须为有限 JSON 标量（由值本身确定类型，无额外类型字段）。
- `unit` 必须显式声明且来自公共单位词汇，无量纲填 `"1"`。
- `valid_range` 定义可选闭区间上下限 `{minimum, maximum}`，`null` 表示该侧无有限界。
- 禁止声明优化标志（如 `optimizable`、`stock_or_addition`）、价格/成本字段（如 `$price:`）或环境变量引用。

### 规划接管时的有效域目标语义

`valid_range` 表达设备自身技术有效域，而非表单默认建议。当某属性被规划变量接管时，模型有效域依然生效并与规划边界取交集（详见[统一上下界](../modules/generators.md#统一上下界)）；模板中的默认 `value` 不作为规划上界，设备方程中通过 `property` 引用的关系保持符号关联，不提前代入常量默认值。

## 模板 `inputs`

未实例化模板通过 `inputs` 声明实例化时必填且指向既有结构路径的字段叶子，不用于创建新属性或新接口：

- 每个输入叶子须声明 `type`（`number` / `boolean` / `string` / `array` / `object` / `data_repeat` / `data_full` / `data_predict`）；数值类型必须明确声明 `unit`。
- 数据类输入（`data_repeat` / `data_full` / `data_predict`）的值为项目目录内的 `.csv` 相对路径（禁止绝对路径、`..` 与 URL），仅在实例化阶段用于生成装配数据源绑定，不写入最终纯技术模型。
- 不允许在模板字段内设 `default`、`source`、`mode` 等隐式回退值；提交时必须完整覆盖所有声明叶子并拒绝多余字段。

## `interfaces`

所有随计算步变化的序列定义为 `interfaces`，每项需声明 `carrier`、`unit`、`valid_range` 与 `type`（省略时规范化为 `blind`）：

| `type` | 语义与连接规则 |
|---|---|
| `in` | 从外部设备兼容输出端口接收序列 |
| `out` | 向外部设备兼容输入端口输出序列 |
| `bidirectional` | 在同一端口双向交换序列；以有符号净供给（供给为正、吸收为负）参与母线平衡 |
| `predefined` | 不连线，在项目装配的 `predefined_interfaces` 中显式绑定外部数据源（`constant` / `data_repeat` / `data_full` / `data_predict`） |
| `blind` | 既不对外连线也不接收预定义数据，只能由同一设备的 `equations` 确定 |

- 接口与内部变量的 `valid_range` 边界支持具体数值、`null` 或 `{property_ref: <property_id>}`（按实例覆盖后的 `property` 实际值动态解析，规范化保存与读取必须保留该引用，不得展开或置为 `null`）。
- 设备模型本身不携带项目级来源字段（`source`、`mode`、`value`、`data_ref`、`target_type`）。

## `equations`

```yaml
equations:
  variables:
    stored_energy:
      unit: kWh
      valid_range:
        minimum: 0
        maximum: null
      initial:
        expression: "initial_soc * capacity_kwh"
  relations:
    - id: energy_transition
      expression: "stored_energy[t] = stored_energy[t-1] + (charge_in[t] * charge_efficiency - discharge_out[t] / discharge_efficiency) * step_duration"
```

- **作用域与单位**：表达式仅可引用本设备声明的 `properties`、`interfaces`、`equations.variables`、项目基线公开步长 `step_duration` 及允许的数学运算；所有量按 SI 基准换算求值，裸数字视为 SI 常量（含偏置的仿射温度单位如 `°C` 的参考点应通过带单位 `property` 声明）。
- **时间索引**：仅支持 `[t]` 与 `[t-1]`（`[t-k]` 当 `k > 1` 时拒绝）。`t=0` 处的 `[t-1]` 由对应状态变量的 `initial` 替代。
- **互斥表达**：支持 `a[t] * b[t] = 0` 形式的非负变量互斥等式。
- **禁止行为**：禁止任何代码/模块路径、动态函数调用、反射、环境变量及 `eval`。

### 状态初值

状态变量通过 `initial` 声明 `t=0` 的前驱值，支持两种互斥形态：

- `{property_ref: <property-id>}`：直接引用标量技术属性；
- `{expression: "<表达式>"}`：使用四则运算与括号组合本设备未带时间索引的数值 `properties`（不得引用接口、内部变量或 `step_duration`），结果量纲须与状态变量一致。

当所引用属性被规划变量接管时，初值保留对该规划变量的符号关联（是否可求解取决于所选生成器对线性或双线性表达式的支持能力）。

## 校验与项目保存门禁

无论来自模板实例化、结构化表单还是直接 YAML 编辑，候选设备文档均经过同一后端正式解析器执行四步校验：

1. YAML 1.2 安全子集解析、重复键、`schema` 与未知字段检查；
2. 稳定 ID 规则、`properties` 标量与单位/值域检查；
3. 五类 `interfaces`、载体与禁用来源字段检查；
4. 方程变量、符号引用、量纲与一阶时间索引检查。

项目内新建或保存模型时，校验在事务内先行阻断非法候选（聚合同一阶段所有独立诊断并附 YAML 行列与字段路径），校验通过后原子分配项目递增 `_N` 后缀、写入规范 YAML 与校验回执。该保存过程不直接处理或捆绑数据文件，数据绑定统一在装配阶段完成。
