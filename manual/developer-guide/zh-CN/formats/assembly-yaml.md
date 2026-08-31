# 装配 YAML

> 契约标识：`ies.assembly`；目标 schema：`2.0.0`；推荐文件名：`<assembly-id>.assembly.yaml`。
> 文档状态：生效目标契约；本页只定义目标文件语义，不声明实现进度。

装配 YAML 是系统模型与规划意图的完整、可审查文本：它固定项目计算基线，实例化精确设备内容，绑定已完成预备的全周期 `step` 序列，连接可连接的真实 interfaces，区分存量与新增设备，并给出规划配置与公共财务配置。它不包含 generator、solver、计算精度或求解选项；这些计算配置在规范装配产物转换为计算包时固定。

## 最小结构示例

```yaml
schema: ies.assembly
schema_version: "2.0.0"

assembly:
  id: campus_demo
  name: 园区最小算例

project_baseline:
  resolution: 1h
  leap_year: false
  scenario_mode: single

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

finance:
  currency: CNY
  base_year: 2025
  devices:
    grid:
      unit_price: {value: 0, unit: CNY/kW}
      fixed_om_rate: 0
  energy_prices: {}
  tax_rate: 0.25
  capital_time_cost: 0.08

planning:
  objective:
    sense: minimize
    expression: system.total_financial_cost
  variables: {}
  constraints: {}

extensions: {}
```

示例中的摘要仅为占位值。正式文件必须引用设备规范内容的真实 SHA-256。

## 顶层职责

| 字段 | 作用 |
|---|---|
| `assembly` | 本装配的稳定 ID 和人类可读名称 |
| `project_baseline` | 项目创建时固定的时间分辨率、闰年口径和场景模式 |
| `resources` | 数据等外部资源及其可验证来源 |
| `devices` | 设备实例、精确设备内容、存量/新增身份、允许的 property 覆盖和预定义接口绑定 |
| `connections` | 可连接的真实 `<device>.<interface>` 之间的有向连接 |
| `finance` | 规划与财务计算共同消费的设备单价、O&M、能源价格、税率和资金时间成本等公共财务参数 |
| `planning` | 目标函数、规划变量、上下界和规划/系统约束 |
| `extensions` | 命名空间化扩展 |

`planning.constraints` 为命名映射；表达式只能使用受限声明式语法：

```yaml
planning:
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
- `predefined_interfaces` 只能绑定设备中 `type: predefined` 的 interface；`constant/data_repeat/data_predict` 均必须先完成序列预备，并绑定项目模型实例中已经替换好的计算用序列文件；
- `in/out/bidirectional` 通过 connections 取得外部交互；`blind` 既不能连接，也不能绑定预定义数据；
- 每项覆盖、绑定和身份均进入规范装配摘要与校验回执。

`existing` 的历史投资是沉没成本，不能作为新增投资再次进入目标；但规划和财务计算共同使用的未来 O&M、剩余寿命、残值和退役成本可在公共财务配置中显式提供。`new` 的投资必须与候选容量或建设决策绑定。

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

## 项目计算基线

`project_baseline` 在创建项目时一次性固定，当前只包含：

- `resolution`：全项目统一计算分辨率，必须能确定性切分一天；
- `leap_year`：是否按 366 天生成全周期序列；
- `scenario_mode`：当前固定为 `single`。

基线不保存时区、开始/结束时间、典型日/周/年或计算截取区间。计算序列统一使用从 `0` 开始的连续 `step`，点数由 `resolution` 和 `leap_year` 唯一推导。已有项目基线变更或多场景必须通过新的公开契约定义，不能作为本格式的隐式例外。

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

CSV 必须固定相同的设备 ID 与内容摘要，并且列 ID、单位、`source_mode`、项目基线摘要、分辨率、点数和有效区间与目标 predefined interface 一致。装配只绑定已经序列预备、覆盖全周期且 `step` 连续的计算用数据版本；原始周期模板、训练输入和预测输入不直接进入装配。

## 规划配置与公共财务配置

设备 YAML 始终保持纯技术。`finance` 是整体模型的公共财务参数输入，由规划生成和结果财务计算共同消费，包括：

- 设备单价与建设投资参数；
- 存量或新增设备固定/可变 O&M；
- 能源购入价和售出价；
- 税率、资金时间成本以及其他规划和财务计算共同使用的参数。

财务配置不保存目标函数、规划变量/约束、计算选项或仅在某一阶段使用的数据。币种、基准年、单位和适用实例必须明确。改变这些内容会改变装配摘要，但不会改变设备内容摘要。

`planning` 只保存目标函数、目标权重、规划变量、上下界和规划/系统约束。目标函数可引用模型技术量、规划变量和 `finance` 公共参数，具体计算方法由后续计算包生成阶段选择。

## 计算包生成边界

装配 YAML 不包含 `calculation`。规范 `ValidatedAssemblyArtifact` 与独立计算配置一起进入计算包生成用例。计算配置固定 mode、计算精度、离散化、generator、solver、容差、时间限制、选项、随机种子和输出选择，并在生成 Solver Bundle 前完成能力兼容校验。

更换 generator、solver、精度或求解选项只会形成新的计算配置和 Solver Bundle，不改变装配文本及其摘要。

装配 YAML 禁止 shell、executable、参数字符串、脚本、动态导入路径、环境变量、凭证、宿主机工作目录和输出文件路径。这些执行细节只由受信任生成器写入 [Solver Bundle](solver-bundle.md)。

## 四阶段校验

1. **结构校验**：安全 YAML、schema、字段类型、ID、内容摘要和引用形状；
2. **模型与数据校验**：设备内容、properties、equations、predefined interfaces、数据时间覆盖、单位与状态；
3. **图与系统校验**：interface 类型、连接、carrier、拓扑、平衡和系统约束；
4. **规划与财务完整性校验**：存量/新增、目标函数、规划变量/约束以及规划和财务计算共同必需的公共财务参数。

同阶段尽量聚合可修复诊断；任何 error 都不产生可执行产物。

## `ValidatedAssemblyArtifact`

成功结果是不可变三件套：

1. 规范装配文本：序列统一为项目基线下连续 `step`，资源变为内容 ID，字段和集合按规定排序；
2. `assembly_sha256`：对规范字节计算 SHA-256；
3. 校验回执：校验器 ID/版本、schema、项目基线摘要、设备内容锁、方程 contract、规划配置 revision/摘要、财务配置 revision/摘要、资源摘要和零阻断诊断。

生成器必须同时验证三者。人工修改规范文本、替换资源或变更任一依赖后，摘要和回执失效，必须重新装配。任务创建后只消费该产物，不得重新读取“当前设备”“当前价格”或“最新项目”。

规范化算法标识为 `ies.assembly.canonical@2.0.0`。相同语义必须得到相同规范文本、摘要和回执；算法语义变化必须升级版本并保留历史解释能力。

## 完成标准

- 示例补齐真实设备摘要和数据后可通过 `2.0.0` schema；
- 装配没有设备独立版本、parameters/ports/model commands 或设备经济字段；
- 五类 interface、三类 predefined 来源、存量/新增、规划配置和公共财务配置边界均有契约测试；
- 非法文件没有旁路进入生成器，合法文件只产生一个规范形态；
- 生成器只读 `ValidatedAssemblyArtifact`，不重新解释原始项目、CSV 路径或 GUI 状态；
- 旧装配通过显式离线迁移和迁移回执进入新格式，不保留运行期兼容分支。
