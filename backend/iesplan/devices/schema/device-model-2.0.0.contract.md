# ies.device-model 2.0.0 — 纯技术设备模型契约

> 契约标识：`ies.device-model`；schema：`2.0.0`。
> 设备模型只表达纯技术语义：稳定身份、非时变 properties、五类序列 interfaces 与受限声明式 equations。
> 不允许独立设备语义版本、价格/成本/财务假设、计算精度、算法选择或可执行入口。

## 完整合法示例（标准设备）

```yaml
schema: ies.device-model
schema_version: "2.0.0"

device:
  id: acme.device.heat_pump
  names:
    zh-CN: 热泵
    en-US: Heat Pump

properties:
  cop:
    value: 3.2
    unit: "1"
    valid_range: {minimum: 1, maximum: 10}
  rated_heat_kw:
    value: 500
    unit: kW
    valid_range: {minimum: 0, maximum: 1000000}

interfaces:
  electricity_in:
    type: in
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
  heat_out:
    type: out
    carrier: heat
    unit: kW
    valid_range: {minimum: 0, maximum: null}
  ambient_temperature:
    type: predefined
    carrier: environment
    unit: "°C"
    valid_range: {minimum: -50, maximum: 60}

equations:
  variables: {}
  relations:
    - id: heat_conversion
      expression: "heat_out[t] = electricity_in[t] * cop"
```

## 模板（未实例化阶段）

模板与普通模型结构相同，只额外包含顶层 `inputs`。`inputs.properties` 与模型使用同构树形结构；
`inputs.predefined_interfaces` 只声明实例化时需要读取的项目 CSV 路径，不写入最终技术模型。
叶子节点对应模型中的具体字段（如 `properties.<id>.value`）或 predefined interface，叶子声明携带表单元数据
（`type`、相邻 `unit`/`valid_range`、`default`）。

```yaml
schema: ies.device-model
schema_version: "2.0.0"

device:
  id: acme.device.electric_load
  names: {zh-CN: 电负荷, en-US: Electric Load}

inputs:
  properties:
    peak_power_kw:
      value:
        type: number
        unit: kW
        valid_range: {minimum: 0, maximum: 10000000}
        default: 100
    is_switchable:
      value:
        type: boolean
        default: false
  predefined_interfaces:
    electric_demand:
      path:
        type: data_repeat

properties:
  cop:
    value: 3.0
    unit: "1"
    valid_range: {minimum: 1, maximum: 10}

interfaces:
  electricity_in:
    type: in
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: null}

equations:
  variables: {}
  relations: []
```

实例化规则见「inputs 实例化」。

## 顶层字段

| 字段 | 必需 | 作用 |
|---|---:|---|
| `schema` | 是 | 固定 `ies.device-model` |
| `schema_version` | 是 | 固定 `2.0.0` |
| `device` | 是 | 稳定设备身份与显示信息 |
| `properties` | 是 | 非时变纯技术常量（无内容时 `{}`） |
| `interfaces` | 是 | 序列接口定义（无内容时 `{}`） |
| `equations` | 是 | 声明式关系（无内容时 `variables: {}, relations: []`） |
| `inputs` | 模板 | 模板专用输入声明；实例化后删除 |

禁止独立顶层 `parameters`、`ports`、`data_inputs`、`states`、`model_commands`、`extensions` 及别名兼容。

## `device`

| 字段 | 必需 | 规则 |
|---|---:|---|
| `id` | 是 | 稳定、小写、带命名空间的设备类型 ID，如 `acme.device.heat_pump` |
| `names` | 是 | 本地化显示名（至少一个语言键） |

禁止 `version`、`fidelity`、`model_method`、`stateful`、`energy_carriers`、`capabilities`。

## `properties`

- 键是稳定 property ID；`value` 是有限 JSON 标量（number/boolean/string），类型从值本身确定；
- 数值必须声明 `unit`；无量纲使用 `"1"`；
- `valid_range` 为 `{minimum, maximum}`，`null` 表示无边界；
- 禁止价格、成本、税、折旧、融资、残值、币种与 `optimizable`/`stock_or_addition`。

## `interfaces`

每个 interface：稳定 ID、`carrier`、`unit`、`valid_range`、`type`。五类：

| 类型 | 语义 |
|---|---|
| `in` | 接收其他设备输出序列 |
| `out` | 向其他设备输出序列 |
| `bidirectional` | 双向交换序列 |
| `predefined` | 不连接；由 `constant`/`data_repeat`/`data_predict` 提供 |
| `blind` | 不连接、不接收预定义数据 |

- 缺省 `type` 规范化为 `blind`；
- 所有正式 interface 都禁止 `source`；
- `predefined` 的来源只由项目实例化配置声明；
- `blind` 禁止来源绑定与连接。

`predefined_interfaces` 三种实例化输入：

```yaml
ambient_temperature: {mode: constant, value: 25}
electric_demand: {mode: data_repeat, path: data/typical_day_load.csv}
weather: {mode: data_predict, path: data/weather_prediction.csv, target_type: ambient_temperature}
```

`constant` 按时间轴展开；`data_repeat`/ `data_predict` 的 `path` 只接受项目目录内 CSV 相对路径，并在实例化入口读取和绑定。

## `equations`

```yaml
equations:
  variables:
    soc:
      unit: "1"
      valid_range: {minimum: 0.1, maximum: 0.9}
      initial: {property_ref: initial_soc}
  relations:
    - id: soc_transition
      expression: "soc[t] = soc[t-1] + charge_in[t] * charge_efficiency - discharge_out[t] / discharge_efficiency"
```

- relation ID 文件内唯一；
- 表达式只引用本文件 properties、interfaces、`equations.variables`、时间索引与公开数学运算；
- 禁止函数路径、动态导入、任意函数调用、脚本、模板、环境变量与凭证。

## inputs 实例化

`inputs.properties` 与模型 property 使用同构树形结构，`inputs.predefined_interfaces` 按 interface ID 声明数据输入；叶子节点是 `type` 声明（`number`/`boolean`/`string`/`data_repeat`/`data_predict`）。前端表单必须生成一份完整实例化配置文件，后端实例化入口直接使用正式解析器读取该文件，不接受另一套表单专用领域 DTO 或简化解析逻辑。叶子位置决定实例化目标：

- `properties.<id>.value`：标量叶子，替换或添加 property 的 `value`；添加新 property 时以
  叶子声明的 `unit`/`valid_range`/`default` 构造完整字段；
- `predefined_interfaces.<id>` 的数据输入：`constant` 提交标量；`data_repeat`/`data_predict`
  提交项目目录内 `.csv` 相对路径。后端在实例化边界解析路径、直接读取 CSV，并把解析结果绑定到对应 `predefined` interface；路径不存在、文件不可读或 CSV 关键字/业务内容错误时返回定位诊断；
- `equations.variables.<id>.initial.value`：替换内部变量初值。

实例化规则：

1. 按 `mapping` 递归合并；
2. 标量和数组整体替换；
3. property 输入覆盖模板声明值；predefined interface 输入形成项目实例来源绑定，不写入技术模型；
4. 用户只能提交 `inputs` 已声明字段，未声明字段拒绝；
5. 合并后删除 `inputs`，输出普通 2.0.0 模型；
6. 输出必须重新通过完整 2.0.0 校验，schema 不允许的新增字段拒绝。

数据路径只是实例化配置的输入关键字，不写入实例化后的纯技术设备模型。CSV 必须事先位于项目目录中；实例化不上传、复制、摘要化或迁移该文件。

模板修改不改变已经生成的模型。追溯使用模板 ID/revision、用户输入记录、实例化器算法标识与最终模型 ID/revision；
该算法标识不是新的模型类型或 schema 版本。

## 校验与规范化

1. YAML 安全子集、顶层字段、schema 与未知字段；
2. 稳定 ID、property 标量、单位与有效区间；
3. 五类 interface、carrier、单位与连接资格；
4. 方程标识符、内部变量、单位、时间关系与循环引用；
5. 实例化配置中的项目相对 CSV 路径及其 predefined interface 绑定；
6. 生成唯一规范 YAML 与校验回执。

规范化算法标识 `ies.device-model.canonical@2.0.0`。相同语义必须得到相同规范文本。

## 进入项目前的候选模型门禁

候选模型（模板实例化 / 直接 YAML）在落盘前，在同一事务中确定项目内 `_N` ID，然后对带最终 ID 的候选内容完成唯一一次后端校验；失败返回聚合诊断（消息键、字段路径、YAML 行列、expected/actual）并回滚，不保存、不占号；成功后规范化并原子保存。

## 完成标准

- 合法/非法样例通过 `2.0.0` schema 与纯协议测试；
- 五类 interface、三种 predefined 来源、方程与非法引用有契约测试；
- 模板实例化与等价直接 YAML 产生相同规范文本；
- 旧 `1.0.0` schema、模板、迁移器、迁移回执和兼容路径全部删除。
