# 装配 YAML

> 契约标识：`ies.assembly`；目标 schema：`2.0.0`；推荐文件名：`<assembly-id>.assembly.yaml`。
> 实现状态：生效目标契约；当前代码仍实现旧设备版本、parameters/ports/model commands 绑定，迁移顺序见 [Roadmap](../../../changelog/roadmap.md)。

装配 YAML 是计算意图的完整、可审查文本：它实例化一个精确设备内容，绑定预定义序列，连接可连接的真实 interfaces，区分存量与新增设备，给出规划经济输入、系统约束，并精确选择生成器和求解器。它不包含可执行命令；只有通过全部校验并规范化后的装配产物才能进入生成器。

## 最小结构示例

```yaml
schema: ies.assembly
schema_version: "2.0.0"

assembly:
  id: campus_demo
  name: 园区最小算例

time_axis:
  start: "2025-01-01T00:00:00+08:00"
  end: "2025-01-02T00:00:00+08:00"
  resolution: 1h
  endpoint: left_closed_right_open

resources:
  datasets:
    campus_load:
      source:
        kind: relative_file
        path: data/campus_load.data.csv

devices:
  grid:
    definition:
      id: acme.device.grid_connection
      content_sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    asset_origin: existing
    properties:
      import_capacity:
        value: 1000
        unit: kW
  load:
    definition:
      id: acme.device.electric_load
      content_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    asset_origin: existing
    predefined_interfaces:
      electric_demand:
        dataset: campus_load
        column: electric_demand

connections:
  grid_to_load:
    from: grid.electricity_out
    to: load.electricity_in

constraints: {}

planning_economics:
  currency: CNY
  base_year: 2025
  devices:
    grid:
      fixed_om_per_year: {value: 0, unit: CNY/year}
  energy_prices: {}

calculation:
  mode: fixed_operation
  precision_profile: standard
  generator: acme.generator.highs_milp@1.0.0
  solver: ies.solver.highs@1.7.2
  options:
    relative_gap: 0.0001
    time_limit_seconds: 600
  random_seed: 42

outputs:
  series:
    - grid.import_power
  metrics:
    - system.total_energy_cost

extensions: {}
```

示例中的摘要仅为占位值。正式文件必须引用设备规范内容的真实 SHA-256。

## 顶层职责

| 字段 | 作用 |
|---|---|
| `assembly` | 本装配的稳定 ID 和人类可读名称 |
| `time_axis` | 计算区间、步长和端点语义 |
| `resources` | 数据等外部资源及其可验证来源 |
| `devices` | 设备实例、精确设备内容、存量/新增身份、允许的 property 覆盖和预定义接口绑定 |
| `connections` | 可连接的真实 `<device>.<interface>` 之间的有向连接 |
| `constraints` | 系统级硬约束和明确命名的业务约束 |
| `planning_economics` | 求解前影响容量或运行决策的投资、O&M、购售价格等经济输入 |
| `calculation` | 问题模式、计算精度、生成器、求解器、选项和种子 |
| `outputs` | 希望发布的序列和指标，不是宿主机输出路径 |
| `extensions` | 命名空间化扩展 |

`constraints` 为命名映射；表达式只能使用受限声明式语法：

```yaml
constraints:
  c1:
    type: ratio
    expression: "hp1.electricity_in[t] <= 0.8 * grid.electricity_out[t]"
    enabled: true
```

## 设备内容固定与实例

每个设备实例用 `definition.id + definition.content_sha256` 固定具体设备内容。`schema_version` 版本化统一设备格式，不是某台设备的语义版本；装配不得出现 `device_version`、`model@version`、`latest` 或设备私有命令版本。

实例规则：

- `asset_origin` 必须是 `existing` 或 `new`；不得从设备类型、创建时间或是否填写成本推断；
- `properties` 只能覆盖设备定义已声明且允许实例化的非时变技术常量，并保留明确单位；不能新增字段，也不能放价格、成本或计算精度；
- `predefined_interfaces` 只能绑定设备中 `type: predefined` 且 `source.mode` 为 `data_repeat` 或 `data_predict` 的 interface；`constant` 直接来自设备内容并按时间轴展开；
- `in/out/bidirectional` 通过 connections 取得外部交互；`blind` 既不能连接，也不能绑定预定义数据；
- 每项覆盖、绑定和身份均进入规范装配摘要与校验回执。

`existing` 的历史投资是沉没成本，不能作为新增投资再次进入目标；但未来 O&M、剩余寿命、残值和退役成本可在规划经济配置中显式提供。`new` 的投资必须与候选容量或建设决策绑定。

## 连接

连接端点使用 `<device-instance>.<interface-id>`。装配校验必须确认：

- 两个实例和 interface 存在；
- `out → in`、与语义相容的 `bidirectional` 连接成立；
- carrier 相同、单位量纲兼容、取值区间不冲突；
- `predefined` 和 `blind` 从不出现在 connections；
- 无禁止的自环、重复边和未满足的必需连接；
- 损耗、延迟和转换由明确设备方程或系统约束表达；
- 网络在所选计算模式下具备完整供需和平衡语义。

校验失败不能删边后继续，也不能创建默认 interface、把缺失 type 猜成双向或改变 `blind` 的含义。

## 预定义序列与资源

人工文件可以绑定包内相对文件：

```yaml
source:
  kind: relative_file
  path: data/campus_load.data.csv
```

也可以绑定内容寻址对象：

```yaml
source:
  kind: object
  object_id: sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
  sha256: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
  media_type: text/csv
```

`relative_file` 只用于作者包内，不能逃逸包目录。校验器读取后计算摘要，规范装配统一改写为内容寻址对象。网络 URL、宿主机绝对路径、临时上传路径和存储 provider 私有路径不得进入可执行快照。

CSV 必须固定相同的设备 ID 与内容摘要，并且列 ID、单位、`source_mode`、时间轴和有效区间与目标 predefined interface 一致。

## 规划经济与财务边界

设备 YAML 始终保持纯技术。为了让规划结果正确反映“建什么、建多大、怎样运行”，以下输入在求解前进入 `planning_economics`：

- 新增设备投资和建设决策关系；
- 存量或新增设备未来固定/可变 O&M；
- 能源购入价、售出价及其时序数据；
- 必要的剩余寿命、退役成本和规划期残值假设。

币种、基准年、单位、适用实例和时间范围必须明确。改变这些内容会改变装配摘要，但不会改变设备内容摘要。

税、折旧、融资结构、折现现金流、NPV 和 IRR 属于方案形成后的 finance 输入；不得用事后财务计算替代上述求解前经济输入。

## `calculation`

`mode` 决定问题能力，例如 `fixed_operation`、`capacity_planning` 或 `scenario_evaluation`。`precision_profile` 以及其他离散化/数值精度选择属于计算层。`generator` 声明如何把规范装配转换成 Solver Bundle，`solver` 声明实际求解器能力与版本。

`options` 只允许生成器公开 schema 中的选项；未知字段拒绝。`random_seed` 对任何可能使用随机性的生成器或求解器都是快照的一部分。

装配 YAML 禁止 shell、executable、参数字符串、脚本、动态导入路径、环境变量、凭证、宿主机工作目录和输出文件路径。这些执行细节只由受信任生成器写入 [Solver Bundle](solver-bundle.md)。

## 四阶段校验

1. **结构校验**：安全 YAML、schema、字段类型、ID、内容摘要和引用形状；
2. **模型与数据校验**：设备内容、properties、equations、predefined interfaces、数据时间覆盖、单位与状态；
3. **图与系统校验**：interface 类型、连接、carrier、拓扑、平衡和系统约束；
4. **计算兼容校验**：存量/新增、规划经济输入、精度、mode、生成器、求解器、选项和输出能力。

同阶段尽量聚合可修复诊断；任何 error 都不产生可执行产物。

## `ValidatedAssemblyArtifact`

成功结果是不可变三件套：

1. 规范装配文本：时间统一为 UTC，资源变为内容 ID，字段和集合按规定排序；
2. `assembly_sha256`：对规范字节计算 SHA-256；
3. 校验回执：校验器 ID/版本、schema、设备内容锁、方程 contract、经济配置摘要、provider 依赖锁、资源摘要和零阻断诊断。

生成器必须同时验证三者。人工修改规范文本、替换资源或变更任一依赖后，摘要和回执失效，必须重新装配。任务创建后只消费该产物，不得重新读取“当前设备”“当前价格”或“最新项目”。

规范化算法标识为 `ies.assembly.canonical@2.0.0`。相同语义必须得到相同规范文本、摘要和回执；算法语义变化必须升级版本并保留历史解释能力。

## 完成标准

- 示例补齐真实设备摘要和数据后可通过 `2.0.0` schema；
- 装配没有设备独立版本、parameters/ports/model commands 或设备经济字段；
- 五类 interface、三类 predefined 来源、存量/新增和规划经济边界均有契约测试；
- 非法文件没有旁路进入生成器，合法文件只产生一个规范形态；
- 生成器只读 `ValidatedAssemblyArtifact`，不重新解释原始项目、CSV 路径或 GUI 状态；
- 旧装配通过显式离线迁移和迁移回执进入新格式，不保留运行期兼容分支。
