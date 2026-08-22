# 设备模型 YAML

> 契约标识：`ies.device-model`；目标 schema：`1.0.0`；推荐文件名：`<device-id>.device.yaml`。

设备模型 YAML 是一种设备类型的公开说明书。它描述业务参数、真实端口、所需时序列、状态和可用建模命令，但不包含可执行代码的位置。设备目录、GUI、装配校验和插件测试必须消费同一份语义。

## 可直接手写的最小示例

```yaml
schema: ies.device-model
schema_version: "1.0.0"

device:
  id: acme.device.pv
  version: "1.0.0"
  names:
    zh-CN: 光伏发电
    en-US: Photovoltaic
  model_method: mechanism
  stateful: false
  fidelity: high
  energy_carriers:
    - electricity
  capabilities:
    - fixed_operation
    - capacity_planning

parameters:
  rated_capacity:
    value_type: number
    quantity: power
    unit: kW
    required: true
    minimum: 0
    optimizable: true
    stock_or_addition: addition
  conversion_efficiency:
    value_type: number
    quantity: ratio
    unit: "1"
    required: true
    minimum: 0
    maximum: 1

ports:
  electricity_out:
    carrier: electricity
    direction: out
    quantity: power
    unit: kW
    capacity_parameter: rated_capacity

data_inputs:
  irradiance:
    value_type: number
    quantity: irradiance
    unit: W/m2
    required: true
    minimum: 0

states: {}

model_commands:
  fixed_operation: acme.model-command.pv.fixed@1.0.0
  capacity_planning: acme.model-command.pv.planning@1.0.0

extensions: {}
```

## 顶层字段

| 字段 | 必需 | 作用 |
|---|---|---|
| `schema` | 是 | 固定为 `ies.device-model` |
| `schema_version` | 是 | 本文件契约版本，不是设备版本 |
| `device` | 是 | 稳定设备身份、显示名、建模方式和能力 |
| `parameters` | 是 | 实例化时可填写或优化的业务参数；无参数时写 `{}` |
| `ports` | 是 | 可连接的真实端口；键就是稳定 port ID |
| `data_inputs` | 是 | CSV 可以绑定的列；无输入时写 `{}` |
| `states` | 是 | 跨时间步状态；无状态时写 `{}` |
| `model_commands` | 是 | 按能力引用版本化命令 provider |
| `extensions` | 是 | 命名空间化可选扩展；没有扩展时写 `{}` |

## `device` 规则

- `id` 使用反向域或组织命名空间，不得以目录名或 Python 包名充当身份；
- `version` 是设备语义版本。端口删除、参数改义或单位改变必须升 MAJOR；
- `names` 至少包含产品默认语言，显示名不是引用键；
- `model_method` 使用公开枚举，例如 `mechanism`、`empirical`、`hybrid`；
- `stateful` 必须与 `states` 是否为空一致；
- `energy_carriers` 是端口载能集合的去重汇总；
- `capabilities` 声明设备可参与的业务问题，必须能在 `model_commands` 中找到对应命令。

## 参数定义

每个参数都必须声明 `value_type`。数值参数还必须声明 `quantity` 和 `unit`，并按需要声明：

- `required`：实例是否必须给值；
- `default`：只有业务确有稳定默认时才允许；缺失必填值不得由消费者猜测；
- `minimum`、`maximum`：闭区间边界；若使用开区间，必须用显式约束字段而不是文字说明；
- `enum`：字符串允许值；
- `optimizable`：规划是否可以选择该值；
- `stock_or_addition`：`stock`、`addition` 或 `not_applicable`，不能从正负号推断。

金额、比例、功率和能量必须是不同 `quantity`。不得用同一字段同时表示存量容量与新增容量，也不得把百分数 `80` 与比例 `0.8` 混用。

## 端口定义

每个端口必须声明：

- `carrier`：载能或物质类型；
- `direction`：`in`、`out` 或确有双向物理语义时的 `bidirectional`；
- `quantity` 与 `unit`：端口交换量的量纲和规范业务单位；
- 可选 `capacity_parameter`：约束该端口能力的参数 ID。

端口方向不能由装配器补齐。太阳辐照等外生资源若以 `data_inputs` 表达，就不再伪造为可连接端口。转换损失、储能延迟和非同时性应通过设备方程或明确中间设备表达，不通过“看起来能连”的双向默认端口表达。

## 数据输入与状态

`data_inputs` 的键是 [设备数据 CSV](device-data-csv.md) 的列 ID。每列至少声明类型、量纲、单位和是否必需；可增加范围、空值策略和允许的时间分辨率。CSV 的列名、单位和范围必须与这里一致。

状态定义必须包含状态量纲、单位、初值来源和跨步语义。初值只能来自显式参数、数据绑定或装配字段，不能由求解器适配器临时猜测。

## 建模命令引用

`model_commands` 的值使用 `<command-id>@<exact-version>`。它告诉装配器“哪个版本的公开命令能够把该设备实例转换成规范数学贡献”，不是动态导入地址。

禁止出现：

- `function`、`entry`、`module`、`package` 或任意代码路径；
- shell 命令、模板代码和表达式求值字符串；
- 宿主机文件路径、对象存储内部路径或凭证；
- 由消费者私下解释的 `$price:`、`${ENV}` 等隐式全局引用。

命令 provider 的代码绑定由组合根完成，设备文件只保留稳定 ID 和精确版本。

## 校验顺序

1. 校验 YAML 安全子集、顶层 schema 和未知字段；
2. 校验 ID、版本、语言、枚举和字段类型；
3. 校验参数、端口、数据列与状态的量纲、单位和范围；
4. 校验 `stateful`、载能汇总、capability 与 command 的交叉一致性；
5. 解析命令精确版本及能力，任何缺失都阻断 provider 发布；
6. 生成不可变 `DeviceDescriptor` 和规范摘要。

同一 provider 中任一设备失败时，候选集合不得部分发布。诊断必须定位文件、字段路径和稳定诊断码。

## 扩展字段

插件私有字段只能写入：

```yaml
extensions:
  acme.example:
    surface_color: blue
```

扩展键必须是插件命名空间。扩展不得覆盖核心参数、端口、单位、命令选择或安全规则；不理解该扩展的消费者可以原样保留，但不能让它改变核心计算语义。

## 完成标准

一个设备模型可交付前，应证明：

- 人工示例可以独立通过 `1.0.0` schema；
- GUI、装配和命令 provider 使用同一 descriptor，不另建设备类型表；
- 最小/最大参数、端口错误、缺数据列和命令版本缺失均有确定诊断；
- 修改文件后摘要变化，原快照仍固定旧设备版本和旧摘要；
- 文件中没有任何实现入口或运行环境秘密。
