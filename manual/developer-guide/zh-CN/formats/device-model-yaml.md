# 设备模型 YAML

> 契约标识：`ies.device-model`；目标 schema：`2.2.0`（新增状态初值表达式）；推荐文件名：`<device-id>.device.yaml`。
> 文档状态：生效目标契约；本页只定义目标文件语义，不声明实现进度。

设备模型 YAML 是所有设备共同遵守的纯技术说明。它只回答三件事：设备具有哪些不随时间变化的技术常量、通过哪些序列接口与外部交互、这些常量和序列之间满足什么方程。设备文件不保存价格、成本、财务假设、计算精度、算法选择或可执行实现入口。

设备没有独立语义版本字段。`schema_version` 只版本化统一文件格式；具体设备内容由稳定设备 ID、发布 revision 和校验回执共同固定。历史项目和任务必须固定内容，不能引用 `latest`。

## 完整示例

```yaml
schema: ies.device-model
schema_version: "2.2.0"

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

示例中的 `expression` 是受限声明式方程，不是 Python、JavaScript、模板或 shell。正式 parser 必须按固定语法生成表达式树并执行标识符、单位和时间索引校验，禁止使用通用语言 `eval`。

## 顶层字段

普通模型顶层只允许下列六个字段；全部必需，没有内容时写 `{}`，不得增加第二套核心结构。
未实例化模型仍使用同一 `schema` 和 `schema_version`，只额外允许顶层 `inputs` 声明可填写字段；
它不是新的模型类型，也没有独立模板 schema 版本。

| 字段 | 作用 |
|---|---|
| `schema` | 固定为 `ies.device-model` |
| `schema_version` | 统一设备文件契约版本；目标值为 `2.2.0` |
| `device` | 稳定设备身份和显示信息 |
| `properties` | 不随时间变化的纯技术常量 |
| `interfaces` | 与外部交互的序列数据定义 |
| `equations` | properties、接口序列与内部变量之间的声明式关系 |
| `inputs` | 仅未实例化模型可用；声明用户必须填写且在模型中已存在的同构字段路径，实例化后删除 |

禁止恢复独立顶层 `parameters`、`ports`、`data_inputs`、`states`、`model_commands`、`extensions`，也禁止通过别名兼容这些旧字段。

## `device`

`device` 只包含：

| 字段 | 必需 | 规则 |
|---|---:|---|
| `id` | 是 | 稳定、小写、带命名空间的设备模型 ID（`device.id` / 技术 `device model id`），与财务 `finance_type` 和装配实例 `instance_id` 严格分离，只标识纯技术模型 |
| `names` | 是 | 本地化显示名；显示名不参与引用和计算 |

设备不得声明独立 `version`。也不得声明 `fidelity`、`model_method`、`stateful`、`energy_carriers` 或 `capabilities`：精度和模型方法属于计算层，状态属于方程内部变量，载体集合从 interfaces 推导，能力从技术方程和计算配置判定。2.0 技术模型不新增 `finance_type`，也不恢复设备 `version` 等冗余字段；财务类别与实例身份在装配与财务配置中分别表达。

## `properties`

`properties` 的键是稳定 property ID。每个 property 表示在一次规范装配和计算快照内不随时间变化的技术常量，例如 COP、额定容量、效率、损耗率或技术寿命。

```yaml
properties:
  efficiency:
    value: 0.9
    unit: "1"
    valid_range:
      minimum: 0
      maximum: 1
```

规则：

- `value` 必须是有限 JSON 标量；类型从值本身确定，不另写重复 `value_type`；
- 数值必须声明 `unit`；无量纲使用 `"1"`；
- `valid_range.minimum`、`valid_range.maximum` 是可选闭区间边界，`null` 表示该侧无有限边界；
- 设备定义只表达技术常量，不使用 `optimizable` 或 `stock_or_addition`；项目中的存量/新增身份及规划上下界由项目实例和规划配置表达；
- 禁止价格、单位投资成本、固定/可变运维费、税、折旧、融资、残值和币种；财务成本模型与能源价格定义在财务文件契约（见[财务 YAML 契约](finance-yaml.md)），不写入设备文件；
- 禁止 `$price:`、环境变量、路径引用和消费者私下解释的默认值。

## 模板 `inputs`

`inputs` 只用于同一 `ies.device-model` 范式的未实例化编辑态，不建立独立模型类型或 schema。模板必须预先完整声明目标模型结构，输入只能覆盖模板已声明的路径，不能创建新的 property、interface、变量、单位或值域。

- 每个输入叶子必须声明 `type: number|boolean|string|array|object|data_repeat|data_full|data_predict`；number 还必须显式声明单位，不补 `-` 或其他默认单位；
- `data_repeat`/`data_full`/`data_predict` 的用户值是项目目录内 `.csv` 相对路径；后端在实例化入口直接读取并绑定该 CSV。禁止绝对路径、`..`、URL、临时上传路径和项目外文件；
- 不允许 `default`、`source`、`mode`、`data_ref` 或任何隐式回退；数据路径只是实例化输入，不进入最终纯技术设备模型；
- 用户必须提交所有声明叶子，并且只能提交已声明路径；缺少或多余字段均阻断；
- array 作为整体提交，`items` 只逐元素校验，不形成额外的 `path[]` 必填输入；
- object 的 `fields` 全部必填并拒绝多余键；空 object、空 array 声明均非法；
- 实例化完成后必须删除 `inputs`，再按只有六个顶层字段的正式模型执行一次完整 `2.2.0` 校验。

## `interfaces`

只有随计算 `step` 变化的序列数据才能定义为 interface。设备定义中的 `predefined` 声明可绑定输入槽，不写入某个项目的来源；每个项目模型实例都必须为该槽声明来源。装配阶段只校验并固定来源声明，不展开、重复或预测序列；计算阶段才按项目基线统一物化。普通设备属性不能伪装成 interface。

每个 interface 至少定义稳定 ID、`carrier`、`unit` 和 `valid_range`。可选 `type` 只有五种，省略时规范化为 `blind`：

| 类型 | 连接与数据语义 |
|---|---|
| `in` | 从其他设备的兼容输出接口接收序列 |
| `out` | 向其他设备的兼容输入接口输出序列 |
| `bidirectional` | 可在同一接口双向交换序列；必须具有确切物理语义；以有符号净流量参与母线守恒，正值表示向母线供给、负值表示从母线吸收，设备方程必须按此约定书写 |
| `predefined` | 不连接其他设备，由 `constant`、`data_repeat`、`data_full` 或 `data_predict` 提供输入序列 |
| `blind` | 不连接其他设备，也不接收预定义数据 |

未写 `type` 时规范化为 `blind`。除此之外不得猜测方向或连接能力。

共同规则：

- `carrier` 表达接口承载的稳定技术对象；连接双方 carrier 必须相同；
- `unit` 必须来自公共单位规范，连接双方必须量纲兼容；
- `valid_range` 约束每个时间点的数据有效区间，越界必须阻断，不能自动截断；接口与方程变量的值域界可写成 `{property_ref: <property>}` 引用实例额定能力（实例级按实例 property 值解析，量纲必须一致），数值/`null`/引用三选一；
- 所有 interface 都禁止携带 `source`、`mode`、`value`、`data_ref` 或 `target_type`；
- `in`、`out`、`bidirectional` 只能通过装配连接取得或提供序列；
- `predefined` 不连接其他设备；每个项目实例都必须在装配的 `predefined_interfaces` 中显式声明来源且不得缺省；
- `blind` 不连接设备也不接受预定义数据；其数值如被方程引用，只能由同一设备的声明式方程确定，无法确定时装配失败；
- 设备数据 CSV 只绑定 `predefined` interface，不再绑定独立 `data_inputs`。

项目实例可使用 `constant`、`data_repeat`、`data_full` 或 `data_predict` 声明来源；字段结构、完整示例和校验规则以[装配 YAML](assembly-yaml.md)「设备实例」为唯一正文。本页只规定设备定义不携带项目来源，并规定每个 `predefined` 槽在进入计算前必须已有明确来源。装配不生成或替换未来全周期序列；展开、重复、原样使用和预测统一发生在计算阶段，预测算法、revision 和参数属于 `CalculationConfig`。

## `equations`

`equations` 是设备技术行为的唯一声明来源，包含内部序列变量和关系式。下例是状态关系的局部示意；完整设备可以按技术需要声明更多 properties、interfaces 和内部变量，示例中引用的名称必须在完整文件中逐一声明：

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

规则：

- relation ID 在文件内唯一；
- 表达式只能引用本文件 properties、interfaces、`equations.variables`、时间索引、项目基线公开变量 `step_duration` 和公开数学运算；
- 方程以降级后 SI 单位求值（序列与标量在代入前一律换算为 SI）：裸数字常量即 SI 值；仿射单位（°C 等含偏置单位）的参考常量不得写接口单位数值，必须以 SI 声明属性表达（如 K），方程文法不支持小数字面量；
- 每个关系必须通过标识符、单位、有效区间和时间索引校验；
- 内部变量只存在于 equations，不成为可连接接口；
- 初值、边界和跨步关系必须显式，不能由 solver 猜测；
- 状态关系的 `t-1` 在项目基线首步 `t=0` 由变量声明的 `initial` 替代，后续步引用前一步状态；更深过去引用（`t-2` 及以前）在设备发布输入边界拒绝；`step_duration` 是项目计算基线提供的唯一公开步长变量，不按设备 ID 或 generator 猜测；
- 受限语法必须支持数值零及 `a[t] * b[t] = 0` 形式的非负变量互斥等式；
- 禁止函数路径、动态导入、任意函数调用、脚本、模板、环境变量和凭证；
- 计算层按项目基线完整连续序列构造问题，可选择不同精度或求解方法，但不得改变设备文件声明的技术关系。

### 状态初值

`initial` 恰选一种形态：`{property_ref: <property-id>}` 直接引用初值属性，或 `{expression: "<表达式>"}` 组合属性。表达式复用方程的数字、括号、一元正负和算术运算，仅引用本设备未带时间索引的数值 properties；不引用接口、内部变量或 `step_duration`。结果量纲必须与状态变量一致，加减两侧量纲一致。

生成器将属性换算为 SI 后求值。属性被规划变量接管时，初值保留同一个规划变量，不提前代入设备默认值；表达式是否可降级由生成器公开数学能力决定，线性生成器只接受降级后为仿射的形式。例如 `initial_soc * capacity_kwh` 在 SOC 固定、容量规划时仍为仿射，两个均为规划变量时为双线性，不能冒充线性能力。

储能以能量状态承接规划容量；充放电递推、SOC 上下限和初值共同使用该容量。上述片段只示范初值与递推，完整设备还须声明接口、属性、容量相关边界与互斥关系。

`2.2.0` 在 `2.1.0` 基础上增加可选初值表达式；现行文件统一使用新字头，不设旧版本解析分支。设备数据 CSV 的版本独立，仍为 `2.1.0`。

## 财务边界（术语澄清）

`device.id` 只标识纯技术模型，与财务类别 `finance_type`（独立于技术模型）和装配实例 `instance_id` 严格分离。设备文件承载零财务输入：不出现成本模型、成本/价格金额、能源价格、税、币种或折旧等字段。成本分量、能源价格与装配绑定（`finance_binding` / `tariff_bindings`）分别定义在[财务 YAML 契约](finance-yaml.md)与[装配 YAML](assembly-yaml.md)；改变市场价格、项目币种或财务假设不得改变设备模型内容与校验结果。

## 校验与规范化

1. 校验 YAML 安全子集、顶层字段和统一 schema 版本；
2. 校验稳定 ID、property 标量、单位和有效区间；
3. 校验五类 interface、carrier、单位，并拒绝设备文件中的来源字段；
4. 校验方程标识符、内部变量、单位和时间关系；
5. 生成唯一规范字节和校验回执。

任一步失败都不得产生可选设备描述。设备文件修改后形成新的 revision，旧项目、任务和证据继续固定旧 revision；不得用同 ID 的当前内容解释历史。

## 进入项目前的候选模型门禁

无论候选模型来自模板实例化、结构化表单还是直接编辑 YAML，前端都必须生成一份完整配置文件字节，并交给后端同一正式解析器直接读取。不得用表单专用领域 DTO、测试专用解析器或预先构造的领域对象绕过文件解析。正式顺序固定为：

```text
编辑/实例化候选模型
  → 事务内确定项目 `_N` ID
  → 对带最终 ID 的候选模型解析并校验一次
  → 失败：返回诊断，不写项目模型目录
  → 成功：规范化并原子保存
```

保存前校验必须覆盖：

- 文件级：编码、安全 YAML、重复键、schema 和未知字段；
- 类型级：标量、mapping、sequence、枚举以及五种 interface type；
- 技术语义：property 单位和值域、carrier、interface 规则与方程，并确认设备模型没有来源绑定；
- 身份：基础设备 ID 合法，项目内最终 `_N` 编号可唯一分配。

失败诊断必须包含已登记诊断码、消息键、字段路径；能定位 YAML 时还应包含行列，并按问题提供 expected/actual。一次请求应尽可能聚合互不依赖的错误和非法类型，不能只返回首个问题，也不能把失败解释为空模型。

候选模型可以留在前端编辑状态或受控临时隔离区，但不得进入项目正式模型目录、设备目录、装配目录或可选择目录。校验失败不得分配或消耗正式编号。全部校验通过后，后端在一个原子提交中：

1. 在项目范围内确定只递增、不复用的 `_1`、`_2`……后缀，并将最终 `device.id` 与文件名统一为带后缀 ID；
2. 对带最终 ID 的完整候选模型执行唯一一次入口校验，生成规范 YAML 和校验回执；
3. 提交模型文件及项目模型清单引用；此用例不接受、校验、落盘或关联 `data_files`。

数据文件由独立数据集流程保存，并只在装配 `predefined_interfaces` 中绑定。任一模型文件写入或清单登记失败时整次保存失败，不得留下半个模型或已占用但不可见的正式编号。

## 完成标准

- 合法示例可独立通过 `2.2.0` schema；
- 所有设备只使用上述统一结构，不为具体设备增加核心关键词；
- GUI、装配、技术模型和计算生成消费同一规范 descriptor；
- properties、五类 interface、无项目来源的 `predefined` 槽、方程与非法引用均有契约测试；四种来源绑定（`constant`/`data_repeat`/`data_full`/`data_predict`）由装配格式负责；
- 设备文件不含独立设备版本、计算精度、价格、成本或实现入口；
- 非法候选不会进入项目模型目录，成功保存必有校验回执；
- 设备模型 `1.0.0` 是未发布开发草案；schema、迁移器、回执、模板和兼容路径全部删除，不提供 1.0/2.0 双轨或迁移入口。现行目标为 `2.2.0`，见页首。
