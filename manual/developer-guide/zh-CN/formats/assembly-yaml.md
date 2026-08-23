# 装配 YAML

> 契约标识：`ies.assembly`；目标 schema：`1.0.0`；推荐文件名：`<assembly-id>.assembly.yaml`。

装配 YAML 是计算意图的完整、可审查文本：它实例化设备模型，绑定数据，连接真实端口，给出约束，并精确选择生成器和求解器。它不包含任何可执行命令；只有通过全部校验并规范化后的装配产物才能进入生成器。

## 可直接手写的最小结构

```yaml
schema: ies.assembly
schema_version: "1.0.0"

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
    model: acme.device.grid_connection@1.0.0
    parameters:
      import_capacity: 1000
  load:
    model: acme.device.electric_load@1.0.0
    parameters: {}
    data:
      electric_demand:
        dataset: campus_load
        column: electric_demand

connections:
  grid_to_load:
    from: grid.electricity_out
    to: load.electricity_in

constraints: {}

calculation:
  mode: fixed_operation
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

## 顶层职责

| 字段 | 作用 |
|---|---|
| `assembly` | 本装配的稳定 ID 和人类可读名称 |
| `time_axis` | 计算区间、步长和端点语义 |
| `resources` | 数据等外部资源及其可验证来源 |
| `devices` | 设备实例、精确模型版本、参数和数据绑定 |
| `connections` | 真实 `device.port` 到 `device.port` 的有向连接 |
| `constraints` | 系统级硬约束和明确命名的业务约束 |
| `calculation` | 问题模式、生成器、求解器、选项和种子 |
| `outputs` | 希望发布的序列和指标，不是宿主机输出路径 |
| `extensions` | 命名空间化扩展 |

`constraints` 为命名映射，条目形态：

```yaml
constraints:
  c1:
    type: ratio      # ratio | capacity | schedule | generic
    expr: "hp1.electric_in <= 0.8 * grid.electric_out"
    enabled: true    # 可选
```

`outputs` 的 `series` 引用 `<device>.<output>`（通常为真实端口），`metrics` 引用 `<scope>.<metric>`（scope 为设备实例或 `system`）；两者保留声明顺序，设备或作用域不存在时校验失败。

## 引用和版本固定

设备模型、建模命令、生成器、求解器和结果适配器最终都必须固定到精确版本。人工装配文件直接固定设备、生成器和求解器；设备模型再固定所需建模命令。校验回执记录解析后的完整依赖锁。

禁止使用 `latest`、范围版本、未版本化别名和“当前默认算法”。若提供 GUI 友好别名，保存装配时必须解析为精确 ID 与版本。

## 资源

人工文件允许以下带判别字段的来源：

```yaml
source:
  kind: relative_file
  path: data/campus_load.data.csv
```

或：

```yaml
source:
  kind: object
  object_id: sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
  sha256: dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
  media_type: text/csv
```

`relative_file` 只用于作者包内，路径必须相对装配文件所在包且不能逃逸。校验器读取后计算摘要，规范装配产物统一改写为内容寻址对象。网络 URL、宿主机绝对路径、临时上传路径和存储 provider 私有路径不得成为可执行快照的一部分。

## 设备实例和数据绑定

`devices` 的键是项目内稳定实例 ID。每个实例：

- 必须引用精确设备模型版本；
- 参数只能使用模型已声明字段，并按模型的业务单位解释；
- 必填参数不得由生成器补默认；
- 数据绑定必须指向已校验数据版本及模型声明列；
- 可以填写模型明确开放的实例值，但不能改写端口载能、方向、量纲和 command ID。

模型命令产生的变量和约束以实例 ID 命名空间隔离。生成器不得用显示名或数组位置作为稳定身份。

## 连接

连接端点使用 `<device-instance>.<port-id>`。装配校验必须确认：

- 两个实例和端口存在；
- 方向、载能、量纲和连接基数兼容；
- 无禁止的自环、重复边和悬空必需端口；
- 损耗、延迟和转换由明确设备或约束表达；
- 网络在所选计算模式下具备完整供需和平衡语义。

校验失败不能删边后继续，也不能创建默认端口或默认设备。

## `calculation`

`mode` 决定问题能力，例如 `fixed_operation`、`capacity_planning` 或 `scenario_evaluation`。`generator` 声明如何把规范装配转换成某类 Solver Bundle，`solver` 声明实际求解器能力与版本。

`options` 只允许生成器公开 schema 中的业务选项。字段必须有类型、范围和默认语义；未知选项拒绝。`random_seed` 对任何可能使用随机性的生成器或求解器都是快照的一部分。

装配 YAML 中禁止：

- `command`、`shell`、`executable`、参数字符串或脚本；
- Python/JavaScript 模块、类、函数或动态导入路径；
- 环境变量、凭证、宿主机工作目录和输出文件路径；
- 让求解器在运行时下载数据或访问业务数据库的配置。

这些执行细节只由已信任生成器写入 [Solver Bundle](solver-bundle.md)，并受运行时策略约束。

## 四阶段校验

1. **结构校验**：安全 YAML、schema、字段类型、ID、版本和引用形状；
2. **模型与数据校验**：设备、命令、参数、数据列、时间覆盖、单位与状态；
3. **图与系统校验**：端口、连接、载能、拓扑、平衡和系统约束；
4. **计算兼容校验**：mode、生成器、求解器、选项和输出能力。

同阶段尽量聚合可修复诊断；若结构已不足以可靠解释后续字段，则停止后续阶段。任何 error 都不产生可执行产物。

## `ValidatedAssemblyArtifact`

成功结果不是内存中的任意 `dict`，而是不可变三件套：

1. 规范装配文本：时间统一为 UTC，资源变为内容 ID，字段和集合按规定排序；
2. `assembly_sha256`：对规范字节计算 SHA-256；
3. 校验回执：校验器 ID/版本、schema、依赖锁、资源摘要和零阻断诊断。

生成器必须同时验证三者一致。人工修改规范文本、替换资源或变更依赖后，摘要和回执失效，必须重新装配。

业务单位在规范装配中继续保持可读且明确；单位兼容在装配阶段证明，向求解器内部单位的实际换算由生成器完成，并记录在 Bundle 证据中。

## 规范化和摘要

- 映射键按格式规则稳定排序；有业务顺序的列表保留声明顺序；
- 时间换算为带 `Z` 的 UTC；
- `relative_file` 解析成内容寻址对象；
- 数值使用唯一有限十进制表示，不依赖本地 locale；
- 注释、显示空白和 YAML 表示差异不参与语义摘要；
- 规范化算法 ID 和版本写入回执。

规范化算法标识为 `ies.assembly.canonical@1.0.0`（回执 `canonical_algorithm` 记录
ID 与版本）。规范文本为 UTF-8/LF 的紧凑 JSON：顶层键序固定
（schema → schema_version → assembly → time_axis → resources → devices →
connections → constraints → calculation → outputs → extensions），嵌套映射按
键名排序，`series`/`metrics` 保留声明顺序；整值浮点与整数同规范文本
（`800` 与 `800.0` 语义相同），非有限数值确定性拒绝。任何语义变化必须升级
规范化算法版本并保留历史解释能力。

相同语义必须得到相同摘要。若规范化算法发生语义变化，必须升级其版本并保留历史解释能力。

## 完成标准

- 本页示例补齐对应设备和数据后可直接通过 schema；
- 单点和多点错误均返回稳定、定位明确的诊断；
- 非法文件没有旁路进入生成器，合法文件只产生一个规范形态；
- 生成器只读 `ValidatedAssemblyArtifact`，不重新解释原始项目、CSV 路径或 GUI 状态；
- 装配文件不具备任意代码或命令执行能力。
