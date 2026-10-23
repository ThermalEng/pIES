# 04 装配与检查模块定案方案(审查意见第 4 条)

> 文档编号:docs/review-0820/04-assembly-checker.md
> 对应审查意见第 4 条:"装配和检查模块:使用文本文件(或专用的模型语言)来描述模型的组合及相互之间的约束。注意模型之间的装配使用边-端模型,边的作用是链接一个模型的输出和下一个模型的输入,被边相连的两个参数在同时间严格相等。如果是有状态的输入和输出,比如非常长的管道,应该增加一个管道设备,以体现上一设备的输出和下一设备的输入的非同时性。检查模块对生成的系统装配文件进行审查,从连接的合法性(是否输入对输出、参数性质是否一样等)、模型的可解性(输入是否完全等)、整体的可解性(是否存在约束不足、过度等情况进行检查),并给出错误反馈。"
> 状态:定案,供实施 agent 直接编码

---

## 1. 目标与范围

### 1.1 目标

按审查意见第 4 条,实现"装配和检查模块",补齐五段式后端流程(设备初始化 → 建模 → **装配和检查** → 计算 → 结果分析)中的第 3 段:

1. **装配**:用文本文件(YAML 1.2,专用模型语言)描述系统模型的组合与相互约束,即**边-端(edge-node)模型**:
   - 节点 = 设备实例(引用第 2/3 步建模模块产出的模型命令);
   - 端 = 端口(载体/方向/物理量/单位/瞬时或延迟);
   - 边 = 链接一个模型的输出与下一个模型的输入,**被边相连的两个参数在同一时间步严格相等**(边不引入任何映射、缩放、损耗);
   - 管道设备 = 有状态设备(输出端口声明 `delayed`),体现长管道等有状态传输的**非同时性**(t 时刻输出 = 输入端口 t−τ 时刻取值)。
2. **检查**:对装配文件做四阶段审查——语法/结构、连接合法性、模型可解性(输入完备)、整体可解性(约束不足/过度),输出结构化错误反馈(诊断码 + 定位 + 消息键 + 参数,沿用 `core/diagnostics.py` 体系)。

### 1.2 与现有代码的关系(边界划定)

| 现有实现 | 定位 | 本模块的关系 |
|---|---|---|
| `services/model.py` `connect()`/`validate_topology()` | 工作图**写入期**校验(端口存在、能源类型、方向、重复连接、孤立设备警告) | 保留,作为**编辑期即时反馈**;装配检查是**固化/计算前**的全面审查,是它的超集,不替代它 |
| `services/tasks.py` `assemble_snapshot()` | 任务快照装配(项目版本内容 + 数据集 + calc_config) | 在快照装配后**调用本模块**做计算前闸门(有 error 级诊断则拒绝下发任务) |
| `core/registry.py` | 设备类型注册表(9 类,运行期只读) | 装配文本 `model:` 字段引用 `ies.device.*@版本`;检查器据此解析端口/参数规格 |
| `core/units.py` | 单位注册表 + 换算函数 | 检查器用 `convert` 判定边两端单位量纲是否一致(不换算数值,数值换算属第 0 条单位标准化的工作) |
| `core/diagnostics.py` | 诊断码目录 + `Diagnostic`/`make_diag` | 本模块新增 `ASM` 域码,统一登记在该模块的 `assembly/diags.py`,沿用 `Diagnostic` 数据类与"后端只出 code+message_key+params,不出文案"规则 |
| `models/model.py` `Device/Port/Connection` | 图存储 | `builder` 从图序列化结构生成装配文本(确定性,content_hash 稳定);**`Connection.loss_rate > 0` 的连接在生成时自动包裹为管道设备**,保持"边=严格相等"语义 |
| `engines/eval_run.py`/`planning.py` | 计算模块 | 装配文本 + 模型命令 + 计算要求(第 5 步输入);本模块不调用引擎,只产出供引擎消费的文本 |

### 1.3 术语

- **装配文件(assembly file)**:描述一个系统组合的 YAML 文本,是计算模块的输入之一。
- **节点(device instance)**:装配文件中一个设备实例,`id` 在文件内唯一,`model` 引用建模模块注册的模型命令。
- **端(port)**:设备实例上的端口,属性:载体、方向、物理量(quantity)、标准单位、瞬时/延迟性质。
- **边(edge)**:`from_port → to_port` 的有向链接,语义为两端参数**同一时间步数值严格相等**。
- **母线(bus)**:检查器把同一载体下通过边连通的端口集合(无向连通分量,双向端口视为双向连通)聚合成母线,作为整体可解性分析的基本单元。版本 1 计算模型为每载体单母线,装配文本允许未来多母线(通过管道设备/不同边分割),检查器按连通分量处理。
- **管道设备(pipeline device)**:有状态模型(`stateful: true`),输入端口 `nature: instantaneous`,输出端口 `nature: delayed` + `delay_steps`。用于体现长管道等上一设备输出与下一设备输入的非同时性。

---

## 2. 装配文本格式(YAML 1.2)

### 2.1 章节总览

```yaml
assembly:            # 必填,顶层对象
  name: string
  format_version: "1.0"
  source_graph_id: 123          # 溯源(项目图 id),非必填

time_axis:           # 必填,时间轴(与 core/timeaxis.py 对齐)
  resolution: 1h                # 15min | 30min | 1h
  start: "2025-01-01T00:00:00Z" # UTC
  timezone_offset_min: 480

models:              # 模型命令目录(第 3 步建模模块产物,引用性快照;通常省略,检查器以注册表为准)
  - id: ies.device.heat_pump@1.2.0
    path: /ies/bin/heat_pump-1.2.0

devices:             # 必填,设备实例列表(节点)
  - id: hp1                   # 文件内唯一
    model: ies.device.heat_pump@1.2.0
    kind: new                 # existing | new(存量/新增,对齐 devices.kind)
    model_kind: mechanistic   # mechanistic | data_repeat | data_predict(建模方法标志,第 2/3 步落库字段)
    stateful: false           # 有/无状态模型标志
    params: { rated_heat_kw: 600, cop: 3.5 }
    data_refs: []             # 数据集引用(负荷 profile 等)
    meta: { layout: {x: 1, y: 2} }   # 布局等,不参与语义,缺省省略

ports:               # 可选;端口由模型注册表推导,装配文件内显式声明仅用于覆盖(capacity 等)
  - device: hp1
    name: electric_in
    carrier: electric
    direction: in
    quantity: power           # 物理量: power | energy | flow | temperature | soc | ratio | price | signal
    unit: W                   # 标准单位(SI 基准,与 core/units.py 注册单位对齐)
    nature: instantaneous     # instantaneous | delayed(有状态设备输出端口为 delayed)
    capacity: 800.0           # 可选,端口容量(同一物理量,标准单位)

edges:               # 必填,边列表(边-端模型的"边",语义=同时间严格相等)
  - id: e1
    from: grid.electric_out   # <device_id>.<port_name>
    to: hp1.electric_in
    capacity: 800.0           # 可选,边容量(标准单位),缺省不限制
  - id: e2
    from: hp1.heat_out
    to: pipe_hp.heat_in       # 有状态管道 → 经管道设备体现非同时性

pipelines:           # 可选,管道设备列表(有状态传输设备)
  - id: pipe_hp
    model: ies.device.transport_pipe@1.0.0
    params: { delay_steps: 2, loss_per_step: 0.02 }
    # 输出端口由模型注册表推导: carrier=heat, direction=out, nature=delayed, delay_steps=2

constraints:         # 可选,组合级约束(跨设备/跨边)
  - id: c1
    type: ratio               # ratio | capacity | schedule | generic
    expr: "hp1.power_in <= 0.8 * grid.electric_out"
  - id: c2
    type: capacity
    expr: "sum(pv_out) <= 1500 W"   # 支持显式单位后缀;无量纲按变量注册单位

requirements:        # 可选,计算要求(第 5 步计算模块输入;builder 从 calc_config/快照填充)
  algorithm: ies.algo.milp_hybrid@1.0.0
  tolerances: { mip_rel_gap: 0.001, time_limit_s: 600 }
  seed: 42
```

### 2.2 完整示例(光储充 + 热泵 + 锅炉 + 长热网管道)

```yaml
assembly:
  name: campus_demo_v3
  format_version: "1.0"

time_axis:
  resolution: 1h
  start: "2025-01-01T00:00:00Z"
  timezone_offset_min: 480

devices:
  - id: grid
    model: ies.device.grid_connection@1.3.0
    kind: existing
    model_kind: mechanistic
    stateful: false
    params: { max_import_power_kw: 800, max_export_power_kw: 0, import_tariff: {peak: 1.1, flat: 0.7, valley: 0.3}, export_tariff: 0.35, demand_charge: 40 }
  - id: pv1
    model: ies.device.pv@1.2.0
    kind: new
    model_kind: mechanistic
    stateful: false
    params: { rated_capacity_kwp: 300, tilt: 30, azimuth: 180 }
  - id: bat1
    model: ies.device.battery@1.1.0
    kind: new
    model_kind: mechanistic
    stateful: true                    # 电池=有状态模型
    params: { capacity_kwh: 400, max_charge_power_kw: 200, max_discharge_power_kw: 200, round_trip_efficiency: 0.92 }
  - id: hp1
    model: ies.device.heat_pump@1.2.0
    kind: new
    model_kind: mechanistic
    stateful: false
    params: { rated_heat_kw: 600, cop: 3.5, max_heat_kw: 900, min_part_load: 0.2 }
  - id: boiler1
    model: ies.device.gas_boiler@1.0.0
    kind: new
    model_kind: mechanistic
    stateful: false
    params: { rated_heat_kw: 800, efficiency: 0.9, gas_price_cny_m3: 3.2 }
  - id: elec_load
    model: ies.device.electric_load@1.0.0
    kind: existing
    model_kind: data_repeat           # 数据方法-简单周期重复
    stateful: false
    params: {}
    data_refs:
      - key: load_profile
        dataset_version_id: 17
        dataset_name: campus_electric_2025
        columns: [power_kw]
        unit: kW
        resolution: 1h
  - id: heat_load
    model: ies.device.heat_load@1.0.0
    kind: existing
    model_kind: data_repeat
    stateful: false
    params: {}
    data_refs:
      - key: heat_profile
        dataset_version_id: 18
        columns: [heat_kw]
        unit: kW
        resolution: 1h

ports:                          # 端口默认由模型注册表推导,此处仅显式声明"需要覆盖容量"的端口
  - device: pv1
    name: electric_out
    carrier: electric
    direction: out
    quantity: power
    unit: W
    nature: instantaneous
    capacity: 320000.0

edges:                          # 边:一律"同时间严格相等",损耗/延迟必须经管道设备建模
  - id: e_grid_pv
    from: grid.electric_out
    to: pv1.electric_out         # 汇合到电网母线(多出对一出的写法,见 2.3 语义约定)
  - id: e_bat
    from: bat1.electric          # 双向端口:边入边出均合法
    to: grid.electric_out
  - id: e_hp_elec
    from: grid.electric_out
    to: hp1.electric_in
  - id: e_pipe_in
    from: hp1.heat_out
    to: pipe_hot.heat_in         # 热网:进管道(瞬时端)
  - id: e_pipe_out
    from: pipe_hot.heat_out      # 管道输出端 nature=delayed(非同时性)
    to: heat_load.heat_in

pipelines:
  - id: pipe_hot
    model: ies.device.transport_pipe@1.0.0
    params: { delay_steps: 2, loss_per_step: 0.02 }

constraints:
  - id: c1
    type: ratio
    expr: "hp1.power_in <= 0.8 * grid.electric_out"
  - id: c2
    type: capacity
    expr: "bat1.charge_power <= 0.5 * (grid.electric_out + pv1.electric_out)"

requirements:
  algorithm: ies.algo.milp_hybrid@1.0.0
  tolerances: { mip_rel_gap: 0.001, time_limit_s: 600 }
  seed: 42
```

### 2.3 语义约定(检查器与计算模块共同遵守)

1. **边 = 同时间严格相等**:边连接的两个端口参数(如功率)在**同一时间步**数值严格相等(等式约束,不含损耗/映射/缩放)。边本身**禁止**携带 `loss_rate`、`conversion` 等属性;需要损耗/变换时必须建模为管道设备或中间设备。
2. **管道设备 = 非同时性**:`stateful: true` 且输出端口 `nature: delayed` 的设备,其输出 t 时刻取值 = 输入 t−`delay_steps` 时刻取值(可含每步损耗),输入输出在任意时刻**不同时相等**。长管道(热网、长电缆、蓄热罐)必须显式建模为管道设备;直接连接(短母线)用瞬时边。
3. **输入对输出**:边有向,`from` 端必须是输出端口(`direction ∈ {out, bidirectional}`),`to` 端必须是输入端口(`direction ∈ {in, bidirectional}`);"输出对输出/输入对输入"为非法。
4. **母线汇合**:多个源汇入同一汇,写作多条边共用一个 `to` 端端口(如 `e_grid_pv`/`e_hp_elec` 都以 `grid.electric_out` 为起点)——因为"输出端口"是被连接侧,同一母线上各源输出端口即母线汇合点;不做星形"总线节点",保持文件最小化。
5. **双向端口**:`bidirectional` 端口(电池、燃气锅炉的燃气端)既可作边起点也可作边终点;检查器对双向-双向边放行,但要求母线两端至少各有一个非纯双向的确定方向端口参与平衡(防止"双向对双向"形成悬空母线)。
6. **载体**:端口 `carrier ∈ {electric, heat, cool, gas, solar, water, data}`,与 `models/model.py` `ports.port_type` CHECK 一致;solar 为环境侧载体(辐照、温度),不产生可连接端口(与 `services/model.py:52` 约定一致),环境输入由模型注册表 `ambient_inputs` 声明(见 4.2 C 阶段)。
7. **物理量与单位**:端口 `quantity`(物理量)与 `unit`(标准单位)必须一致或可换算(量纲一致,用 `core/units.py` 判定);数值一律为**标准单位**(功率 W、能量 J、温度 K、价格 CNY/J 等),非标准单位的解析与换算属第 0 条单位标准化模块,本模块只做量纲一致性审查。
8. **确定性与规范性**:装配文本由 `builder` 确定性生成(固定键序、固定浮点格式),同一图内容生成文本逐字节一致 → 可与 `graph_hash`/快照 `content_hash` 对齐,不破坏既有哈希语义。
9. **约束表达式**:`constraints[].expr` 使用现有表达式引擎(`core/expression.py`)语法,变量形式 `<device_id>.<port_name>`(端口)或 `<device_id>.<param_name>`(参数);显式单位后缀(如 `1500 W`)按量纲转换。
10. **requirements 章节**为第 5 步计算模块输入的一部分(builder 从 `calc_config` + 快照 `tolerances`/`random_seed` 填充),检查器只校验其引用(算法 id 注册、容差非负),不执行算法分发。

---

## 3. 检查器:规则清单

检查分 4 阶段顺序执行:阶段 A(语法/结构)失败即终止,不进入后续阶段;阶段 B/C/D 各自独立,全部收集(一次检查产出完整诊断列表)。

### 3.1 阶段 A:语法与结构(parser 内完成)

| 码 | 严重度 | blocking | 检查项 | location |
|---|---|---|---|---|
| ASM-SYN-001 | error | true | YAML 解析失败/类型错误(键非字符串、值类型不符) | `{object_type:"assembly", field:"<yaml 路径>"}` |
| ASM-SYN-002 | error | true | 未知章节(assembly/time_axis/devices/ports/edges/pipelines/constraints/requirements 之外) | `{object_type:"assembly", field:"<section>"}` |
| ASM-SYN-003 | error | true | `format_version` 不受支持(≠ "1.0") | `{object_type:"assembly", field:"format_version"}` |
| ASM-SYN-004 | error | true | 必填字段缺失(assembly 缺 `name`/`format_version`、time_axis 缺 `resolution`、device 缺 `id`/`model`、edge 缺 `from`/`to`) | `{object_type:"assembly", field:"devices[3].id"}` |
| ASM-SYN-005 | error | true | 键类型错误(如 `params` 非 map、`delay_steps` 非整数) | `{object_type:"device", object_id:"hp1", field:"params.delay_steps"}` |

### 3.2 阶段 B:连接合法性(输入对输出、参数性质一致)

| 码 | 严重度 | blocking | 检查项 | location |
|---|---|---|---|---|
| ASM-EDGE-001 | error | true | 边起点不是输出端口(`direction=in`),即"输出对输出/输入对输出" | `{object_type:"edge", object_id:"e1", field:"from"}` |
| ASM-EDGE-002 | error | true | 边终点不是输入端口(`direction=out`),即"输入对输入/输出对输入" | `{object_type:"edge", object_id:"e1", field:"to"}` |
| ASM-EDGE-003 | error | true | 两端载体不一致(electric ↔ thermal) | 同上,`field:"carrier"` |
| ASM-EDGE-004 | error | true | 两端物理量不一致(power ↔ energy) | 同上,`field:"quantity"` |
| ASM-EDGE-005 | error | true | 两端单位量纲不可换算(经 `units.convert` 判定,如 W ↔ K) | 同上,`field:"unit"` |
| ASM-EDGE-006 | error | true | 自环(同一设备同一端口连到自身;DB 层已禁,装配文本层防手写) | `{object_type:"edge", object_id:"e1", field:"ends"}` |
| ASM-EDGE-007 | error | true | 同两端同载体重复边(多边并行) | `{object_type:"edge", object_id:"e2"}` |
| ASM-EDGE-008 | warning | false | 双向-双向直连且该母线上无其他确定方向端口(母线悬空风险) | `{object_type:"bus", field:"carrier:heat"}` |
| ASM-EDGE-009 | warning | false | 边容量为 0 或负值 | `{object_type:"edge", object_id:"e1", field:"capacity"}` |

复用现有码:`CONN-TYPE-002`(设备类型未注册)、`CONN-PORT-001/002/003`(能源类型/方向/跨项目,与写入期一致)。参数校验复用 `PARAM-RNG-003`/`PARAM-UNIT-002`/`PARAM-CONF-001`。

### 3.3 阶段 C:模型可解性(输入完备)

| 码 | 严重度 | blocking | 检查项 | location |
|---|---|---|---|---|
| ASM-REF-001 | error | true | 设备实例 `id` 重复 | `{object_type:"device", object_id:"hp1"}` |
| ASM-REF-002 | error | true | `model` 引用未注册(注册表快照无 `ies.device.*@version`);version 缺省时取注册表最新 | `{object_type:"device", object_id:"hp1", field:"model"}` |
| ASM-REF-003 | error | true | 端口引用 `<dev>.<port>` 不存在(设备 id 未定义或端口未声明/未推导) | `{object_type:"edge", object_id:"e1", field:"from"}` |
| ASM-REF-004 | error | true | 数据集引用缺失(dataset_version_id 不存在、列不存在、分辨率与时间轴不符) | `{object_type:"device", object_id:"elec_load", field:"data_refs.load_profile"}` |
| ASM-REF-005 | warning | false | 端口在显式 `ports:` 中声明的载体/方向与模型注册表推导不一致(以注册表为准,告警提示手写冲突) | `{object_type:"port", object_id:"pv1.electric_out"}` |
| ASM-INPUT-001 | error | true | 设备输入端口无来边(输入不完备);**环境侧输入**(solar 辐照/环境温度等,注册表 `ambient_inputs` 声明)除外 | `{object_type:"device", object_id:"hp1", field:"electric_in"}` |
| ASM-INPUT-002 | error | true | 必填参数缺失(注册表 required 清单;缺省可填默认值的不算) | `{object_type:"device", object_id:"pv1", field:"params.rated_capacity_kwp"}` |
| ASM-INPUT-003 | error | false | 参数值越界/枚举不符(复用 PARAM-RNG-003 语义,装配层再查一次,防手写文本绕过写入期校验) | `{object_type:"device", object_id:"boiler1", field:"params.efficiency"}` |
| ASM-INPUT-004 | error | true | 负荷类设备(`is_load`)缺 `data_refs`(无 profile 数据不可解) | `{object_type:"device", object_id:"elec_load", field:"data_refs"}` |
| ASM-INPUT-005 | error | true | `data_refs` 声明单位与端口单位量纲不可换算(如 kW ↔ ℃) | `{object_type:"device", object_id:"elec_load", field:"data_refs.load_profile.unit"}` |
| ASM-PIPE-001 | warning | false | 有状态设备(`stateful: true` 或输出端口 `nature: delayed`)未声明 `delay_steps`(按 1 处理) | `{object_type:"pipeline", object_id:"pipe_hot", field:"params.delay_steps"}` |
| ASM-PIPE-002 | error | true | `delay_steps` 超出时间轴范围(≥ 年步数 n,或使任意步输出无输入来源) | `{object_type:"pipeline", object_id:"pipe_hot", field:"params.delay_steps"}` |
| ASM-PIPE-003 | warning | false | 管道设备无入边或无出边(未形成通路) | `{object_type:"pipeline", object_id:"pipe_hot"}` |

### 3.4 阶段 D:整体可解性(母线级约束不足/过度)

阶段 D 先构造母线:按载体 + 无向连通分量(边连通;双向端口视为双向连通,管道设备的两条边同属一个分量)分组,得 `BusSummary{carrier, ports, sources, sinks, controllable, fixed_supply_max, demand_max, has_storage}`。

| 码 | 严重度 | blocking | 检查项 | location |
|---|---|---|---|---|
| ASM-SOLV-001 | error | true | **约束不足**:某载体母线无源(只有汇)——能量无法产生 | `{object_type:"bus", field:"carrier:heat"}` |
| ASM-SOLV-002 | error | true | **约束不足/能量无归处**:某载体母线无汇(只有源,且无储能/无 export 通道;含 grid 禁止反送电时 PV 过剩) | `{object_type:"bus", field:"carrier:electric"}` |
| ASM-SOLV-003 | error | true | **必然不可行**:母线无任何可调手段(无 storage、无可控源、grid 容量 0)且 `Σ固定供给上限 < Σ需求最大值` | `{object_type:"bus", field:"carrier:heat", params:{fixed_supply_max, demand_max}}` |
| ASM-SOLV-004 | error | true | **约束过度**:两个互斥的固定约束同时成立(如:两个固定量源被边强制相等但数值不同;grid 禁止反送电且 export_tariff ≥ 0 与"过剩必须有处可去"矛盾) | `{object_type:"edge", object_id:"e3"}` / `{object_type:"bus"}` |
| ASM-SOLV-005 | error | true | **因果环**:有状态设备(管道/延迟)构成闭环,任意时刻输入依赖未来输出(时间不一致) | `{object_type:"pipeline", object_id:"pipe_hot"}`(环上任一成员) |
| ASM-SOLV-006 | warning | false | 孤立设备/孤立母线(无任何边,或母线只有孤立端口;与现有 CONN-NODE-001 语义一致) | `{object_type:"device", object_id:"pv2"}` |
| ASM-SOLV-007 | info | false | 自由度提示:母线可控变量数 vs 平衡方程数(可控变量 = 有界可调的源/储能输出;方程 = 步数 × 平衡),相差过大提示"约束过度/不足"倾向 | `{object_type:"bus", field:"carrier:electric", params:{n_vars, n_eq, ratio}}` |

### 3.5 约束表达式检查

| 码 | 严重度 | blocking | 检查项 | location |
|---|---|---|---|---|
| ASM-CONST-001 | error | true | `expr` 语法错误(复用 EXPR-SYN-001 语义,装配层报 ASM-CONST-001) | `{object_type:"constraint", object_id:"c1", field:"expr"}` |
| ASM-CONST-002 | error | true | 表达式量纲不一致(经 `core/expression.py` 维度检查 + 端口单位解析,替代现有"缺 unit 即无量纲"的失效路径) | `{object_type:"constraint", object_id:"c1", field:"expr"}` |
| ASM-CONST-003 | error | true | 表达式引用未定义设备/端口/参数符号 | `{object_type:"constraint", object_id:"c2", field:"expr"}` |

### 3.6 错误反馈结构

检查器输出与 `core/diagnostics.py` 完全一致(`Diagnostic` 对象列表,JSON 可序列化),示例:

```json
{
  "code": "ASM-SOLV-001",
  "message_key": "ies.diag.asm.solv_no_source",
  "params": {"carrier": "heat", "bus_ports": ["hp1.heat_out", "boiler1.heat_out"], "sink_devices": ["heat_load"]},
  "severity": "error",
  "blocking": true,
  "location": {"object_type": "bus", "field": "carrier:heat"},
  "fix_hint_key": "ies.fix.asm.solv_no_source",
  "ref_ids": ["help.modeling.bus_balance", "ASM-SOLV-002"],
  "occurred_at": "2026-08-20T08:00:00Z",
  "source": "assembly.checker.solvability",
  "project_id": "prj-001",
  "task_id": "tsk-042",
  "suppressed": false
}
```

定位约定:`object_type ∈ {assembly, device, port, edge, pipeline, constraint, bus}`;`object_id` 为点路径(`hp1.electric_in`、`devices[3].id`、`carrier:electric`);`params` 只含可序列化数据(设备名、数值、路径),供前端按 `message_key` + locale 渲染文案。前端不解析结构语义,只渲染(与 04 文档 §5.4 一致)。

---

## 4. 诊断码登记(ASM 域)

新增码全部在 `backend/iesplan/assembly/diags.py` 集中声明并注册(参照 `core/diagnostics.py` 的 `NEW_DIAG_CODES` 模式,同时向 `core/diagnostics.py` 文档登记),消息键层级 `ies.diag.asm.<类别>.<名称>`、修复键 `ies.fix.asm.<类别>.<名称>`:

```
ies.diag.asm.
├── syntax.*        ASM-SYN-001..005     (parse / unknown_section / version / missing_field / bad_type)
├── edge.*          ASM-EDGE-001..009     (bad_source / bad_sink / carrier / quantity / unit_dim / self_loop / duplicate / loose_bidi / zero_capacity)
├── ref.*           ASM-REF-001..005      (dup_device / model_unregistered / port_undefined / dataset_missing / port_decl_mismatch)
├── input.*         ASM-INPUT-001..005    (port_unfed / param_missing / param_range / load_no_data / data_unit_dim)
├── pipe.*          ASM-PIPE-001..003     (delay_missing / delay_out_of_range / not_in_path)
├── solv.*          ASM-SOLV-001..007     (no_source / no_sink / infeasible / over_constrained / causal_cycle / orphan / dof_info)
└── const.*         ASM-CONST-001..003    (syntax / dim / undefined_symbol)
```

登记规则与 04 文档 §5.1 一致:码一经发布永久稳定;只增不改;码 ↔ 消息键一一对应;后端不输出文案。前端 `messages_zh.ts`/`messages_en.ts` 与 `pageMessages.ts` 补充对应文案键(实施时列出完整键表)。

---

## 5. 模块目录与公共函数签名

### 5.1 目录结构

```
backend/iesplan/assembly/
├── __init__.py        # 公共 API 导出(parse_assembly / build_assembly / dumps_assembly /
│                      #   check_assembly / check_assembly_text / check_graph_inputs / ASM 域码常量)
├── diags.py           # ASM 域诊断码目录 + 消息键/修复键映射(仿 core/diagnostics.py 风格)
├── schema.py          # 装配数据模型(dataclass,见 5.2)
├── parser.py          # 文本(YAML 1.2)→ AssemblySpec(阶段 A,含语法诊断)
├── builder.py         # 项目图(DB 序列化结构)→ AssemblySpec → 规范文本(确定性)
├── checker.py         # 检查器编排:阶段 B/C/D,返回 CheckResult
└── rules/
    ├── __init__.py
    ├── connection.py  # 阶段 B:连接合法性规则
    ├── completeness.py# 阶段 C:模型可解性(输入完备)规则
    └── solvability.py # 阶段 D:母线构造 + 整体可解性规则
```

依赖方向:`assembly/ → core/ (diagnostics, units, registry, expression, timeaxis), models/ (仅类型常量), services/model.py 仅 builder 内部以 get_graph 序列化结构为输入(不依赖 services 内部函数)`,不依赖 `engines/`、`worker/`。API 层新增 `backend/iesplan/api/assembly.py`(见 5.7)。

### 5.2 `schema.py` — 装配数据模型

```python
"""装配数据模型(与装配文本一一对应,文本为唯一事实源)。

所有 dataclass 均为 slots + frozen 语义;列表字段为普通 list,解析后不再可变。
id 引用一律用点路径字符串 "<device>.<port>",在文件内解析为对象时用
AssemblySpec.resolve_port(ref) 返回 (AssemblyDevice, AssemblyPort) 或 None。
"""
from __future__ import annotations
from dataclasses import dataclass, field

#: 装配文本格式版本(与文本 format_version 一致)
FORMAT_VERSION = "1.0"

#: 建模方法标志(与第 2/3 步设备初始化/建模模块的模型文件标志一致)
MODEL_KIND_MECHANISTIC = "mechanistic"      # 机理方法
MODEL_KIND_DATA_REPEAT = "data_repeat"      # 数据方法-简单周期重复
MODEL_KIND_DATA_PREDICT = "data_predict"    # 数据方法-历史数据预测模型

#: 端口物理量枚举
QUANTITY_POWER = "power"          # W
QUANTITY_ENERGY = "energy"        # J
QUANTITY_FLOW = "flow"            # m3/s(燃气/水)
QUANTITY_TEMPERATURE = "temperature"  # K
QUANTITY_SOC = "soc"              # 0..1
QUANTITY_RATIO = "ratio"          # 无量纲
QUANTITY_PRICE = "price"          # CNY/J
QUANTITY_SIGNAL = "signal"        # 控制信号(无量纲)

#: 端口时间性质
NATURE_INSTANT = "instantaneous"  # 同时间严格相等
NATURE_DELAYED = "delayed"        # 输出滞后 delay_steps(管道设备输出端)


@dataclass(slots=True)
class TimeAxisRef:
    """时间轴引用(与 core/timeaxis.py 对齐)。"""
    resolution: str                 # 15min | 30min | 1h
    start: str                      # ISO8601 UTC
    timezone_offset_min: int = 0

    @property
    def steps_per_year(self) -> int: ...   # 35040 / 17520 / 8760


@dataclass(slots=True)
class AssemblyPort:
    """端口(端)。"""
    device: str                     # 所属设备 id
    name: str                       # 端口名(设备内唯一)
    carrier: str                    # electric|heat|cool|gas|solar|water|data
    direction: str                  # in|out|bidirectional
    quantity: str                   # QUANTITY_*
    unit: str                       # 标准单位(W|J|K|m3/s|...)
    nature: str = NATURE_INSTANT    # instantaneous|delayed
    delay_steps: int = 0            # nature=delayed 时有效(由管道设备 params.delay_steps 推导)
    capacity: float | None = None   # 端口容量(标准单位)

    @property
    def ref(self) -> str: ...        # "<device>.<name>"


@dataclass(slots=True)
class AssemblyDevice:
    """设备实例(节点)。"""
    id: str
    model: str                      # "ies.device.heat_pump@1.2.0"
    kind: str = "existing"          # existing | new
    model_kind: str = MODEL_KIND_MECHANISTIC   # 建模方法标志
    stateful: bool = False          # 有/无状态模型标志
    params: dict[str, object] = field(default_factory=dict)
    data_refs: list[DataRef] = field(default_factory=list)
    ports: list[AssemblyPort] = field(default_factory=list)   # 从模型注册表推导,可覆盖 capacity
    meta: dict[str, object] = field(default_factory=dict)     # 布局等,不参与语义/哈希


@dataclass(slots=True)
class DataRef:
    """数据集引用(设备承载时间序列数据的标准文件引用)。"""
    key: str                        # 参数名(load_profile/heat_profile/...)
    dataset_version_id: int
    dataset_name: str = ""
    columns: list[str] = field(default_factory=list)
    unit: str = ""                  # 文件头声明单位(非标准单位,检查器只做量纲一致性)
    resolution: str = ""            # 15min|30min|1h


@dataclass(slots=True)
class AssemblyEdge:
    """边:from 端输出 → to 端输入,两端参数同一时间步数值严格相等。"""
    id: str
    from_port: str                  # "<device>.<port>"
    to_port: str                    # "<device>.<port>"
    capacity: float | None = None   # 边容量(标准单位),None=不限制
    meta: dict[str, object] = field(default_factory=dict)

    @property
    def ends(self) -> tuple[str, str]: ...
    def resolve(self, spec: "AssemblySpec") -> tuple[AssemblyPort | None, AssemblyPort | None]: ...


@dataclass(slots=True)
class AssemblyPipeline:
    """管道设备(有状态传输,体现非同时性;在 devices 之外单独列出,便于检查器特判)。"""
    id: str
    model: str = "ies.device.transport_pipe@1.0.0"
    params: dict[str, object] = field(default_factory=dict)   # delay_steps/loss_per_step


@dataclass(slots=True)
class AssemblyConstraint:
    """组合级约束(表达式引擎语法)。"""
    id: str
    type: str                       # ratio|capacity|schedule|generic
    expr: str                       # "hp1.power_in <= 0.8 * grid.electric_out"
    enabled: bool = True


@dataclass(slots=True)
class CalcRequirements:
    """计算要求(第 5 步计算模块输入;builder 从 calc_config/快照填充)。"""
    algorithm: str = "ies.algo.milp_hybrid@1.0.0"
    tolerances: dict[str, float] = field(default_factory=lambda: {"mip_rel_gap": 0.001, "time_limit_s": 600.0})
    seed: int | None = None


@dataclass(slots=True)
class AssemblySpec:
    """装配文件解析结果(内存模型)。"""
    name: str = ""
    format_version: str = FORMAT_VERSION
    source_graph_id: int | None = None
    time_axis: TimeAxisRef | None = None
    devices: list[AssemblyDevice] = field(default_factory=list)
    edges: list[AssemblyEdge] = field(default_factory=list)
    pipelines: list[AssemblyPipeline] = field(default_factory=list)
    constraints: list[AssemblyConstraint] = field(default_factory=list)
    requirements: CalcRequirements | None = None

    def device_by_id(self, device_id: str) -> AssemblyDevice | None: ...
    def port_by_ref(self, ref: str) -> AssemblyPort | None: ...          # "<dev>.<port>"
    def pipeline_by_id(self, pipeline_id: str) -> AssemblyPipeline | None: ...
    def device_ids(self) -> set[str]: ...
    def all_ports(self) -> list[AssemblyPort]: ...                        # 含管道设备推导端口
```

### 5.3 `parser.py` — 文本解析(阶段 A)

```python
"""装配文本解析器:YAML 1.2 文本 → AssemblySpec。

阶段 A(语法/结构)检查在此完成:解析失败/未知章节/版本不支持/必填缺失/
类型错误产出 ASM-SYN-* 诊断;存在 error 级诊断时 spec 为 None,不进入阶段 B/C/D。
解析采用严格模式:未知键/未知枚举值一律报错(不静默忽略),保证文本确定性。
"""
from __future__ import annotations

from iesplan.assembly.diags import *          # noqa: F401  (ASM-SYN-* 码)
from iesplan.assembly.schema import AssemblySpec
from iesplan.core.diagnostics import Diagnostic


@dataclass(slots=True)
class ParseResult:
    """解析结果:spec 与诊断列表。"""
    spec: AssemblySpec | None
    diagnostics: list[Diagnostic]

    @property
    def ok(self) -> bool:                       # 无 error/blocking 级诊断
        ...


def parse_assembly(text: str, *, source_name: str = "assembly.yaml") -> ParseResult:
    """YAML 文本 → AssemblySpec(严格模式)。

    产出 ASM-SYN-001..005 诊断;端口推导不在此阶段(检查器阶段 B 以注册表为准)。
    """


def load_assembly_file(path: str) -> ParseResult:
    """从文件读取并解析(供离线 CLI 与测试使用)。"""
```

### 5.4 `builder.py` — 从项目图生成装配文本

```python
"""项目图(DB 序列化结构)→ AssemblySpec → 规范装配文本。

确定性要求:同一图内容 + 同一 calc_config/数据集元信息,输出文本逐字节一致
(键序固定、浮点格式固定、list 按 id 排序),从而装配文本可纳入内容哈希。
迁移约定:Connection.loss_rate > 0 或显式管道语义(conn_type=thermal_pipe/
cooling_pipe 且 loss_rate > 0)的连接,生成时自动包裹为管道设备
(生成 <id>_pipe 管道 + 两条瞬时边:原 from_port→管道 in、管道 out→原 to_port),
保证"边=严格相等"语义成立;loss_rate == 0 的连接直接映射为瞬时边。
"""
from __future__ import annotations

from iesplan.assembly.schema import AssemblySpec, CalcRequirements


def build_assembly(
    graph: dict,                                    # get_graph 序列化结构:{devices,ports,connections}
    *,
    datasets: dict[int, dict] | None = None,        # dataset_version_id → {name,unit,columns,resolution}
    calc_config: dict | None = None,                # {algorithm, params, tolerances}(calc_config 行)
    solver_options: dict | None = None,             # 任务级 task_params.solver_options
    random_seed: int | None = None,                 # 快照 random_seed
    source_graph_id: int | None = None,
) -> AssemblySpec:
    """项目内容 → 装配对象(不做检查,检查由 check_assembly 完成)。

    requirements 从 calc_config.algorithm/solver_options/random_seed 填充
    (algorithm 缺省 "ies.algo.milp_hybrid@1.0.0";tolerances 缺省
    {mip_rel_gap:0.001, time_limit_s:600})。
    """


def dumps_assembly(spec: AssemblySpec) -> str:
    """AssemblySpec → 规范 YAML 1.2 文本(确定性;与 parse_assembly 互逆)。"""


def build_assembly_text(
    graph: dict,
    *,
    datasets: dict[int, dict] | None = None,
    calc_config: dict | None = None,
    solver_options: dict | None = None,
    random_seed: int | None = None,
    source_graph_id: int | None = None,
) -> str:
    """便捷入口:项目图 → 规范装配文本(供 API 与任务装配直接调用)。"""
```

### 5.5 `checker.py` + `rules/` — 检查器编排

```python
"""装配检查器编排:阶段 B(连接合法性)→ C(模型可解性)→ D(整体可解性)。

入口统一返回 CheckResult(diagnostics 全量收集,不做短路,一次检查给出完整清单);
check_graph_inputs 是任务装配集成点(tasks.assemble_snapshot 调用):以项目版本
content(含 model 图)为输入,先 build_assembly 再 check_assembly。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from iesplan.assembly.schema import AssemblySpec
from iesplan.core.diagnostics import Diagnostic
from iesplan.core.registry import DeviceTypeSpec
from iesplan.core.timeaxis import TimeAxis


@dataclass(slots=True)
class CheckContext:
    """检查上下文:注册表快照/时间轴/数据集元信息(缺省时内部按需惰性加载)。"""
    registry: dict[str, DeviceTypeSpec] | None = None   # 设备模型命令目录(建模模块产物)
    time_axis: TimeAxis | None = None                   # 用于 PIPE-002 延迟范围判定
    datasets: dict[int, dict] | None = None             # dataset_version_id → 元信息
    seed: int | None = None
    max_diags: int = 200                                # 单次检查诊断上限(防风暴)


@dataclass(slots=True)
class BusSummary:
    """母线汇总(阶段 D 产物,随 CheckResult 返回供 UI/审计)。"""
    carrier: str
    port_refs: list[str] = field(default_factory=list)
    device_ids: list[str] = field(default_factory=list)
    source_port_refs: list[str] = field(default_factory=list)
    sink_port_refs: list[str] = field(default_factory=list)
    has_storage: bool = False
    has_grid: bool = False
    fixed_supply_max_w: float | None = None             # Σ固定源上限(W)
    demand_max_w: float | None = None                   # Σ需求上限(W)
    n_controllable: int = 0                             # 可控变量数(自由度提示)
    n_balance_eq: int = 0                               # 平衡方程数(步数×载体数)


@dataclass(slots=True)
class CheckResult:
    """检查结果:诊断全量 + 母线汇总。"""
    diagnostics: list[Diagnostic]
    buses: list[BusSummary] = field(default_factory=list)

    @property
    def ok(self) -> bool:               # 无 error/blocking 级诊断
        ...
    @property
    def blocking_diags(self) -> list[Diagnostic]: ...
    def by_code(self, code: str) -> list[Diagnostic]: ...


def check_assembly(spec: AssemblySpec, *, ctx: CheckContext | None = None) -> CheckResult:
    """装配对象检查:阶段 B/C/D 全量执行;阶段 A 已由 parse 完成(文本入口再次校验)。"""


def check_assembly_text(text: str, *, ctx: CheckContext | None = None) -> CheckResult:
    """文本 → parse(A) → check(B/C/D),一次调用返回完整结果。"""


def check_graph_inputs(
    project_version_content: dict,                      # 快照 content(model.devices/ports/connections + calc_config)
    *,
    datasets: dict[int, dict] | None = None,            # 快照数据集版本元信息
    ctx: CheckContext | None = None,
) -> CheckResult:
    """任务装配集成点:内容 → build_assembly → check_assembly。

    供 tasks.assemble_snapshot 在 content_hash 去重之后调用;
    返回结果中 error 级诊断即阻断任务下发(写入任务 diagnostics)。
    """
```

`rules/` 各阶段模块统一签名:

```python
# rules/connection.py / completeness.py / solvability.py 共用的规则函数签名
from collections.abc import Callable
from iesplan.assembly.schema import AssemblySpec
from iesplan.assembly.checker import CheckContext
from iesplan.core.diagnostics import Diagnostic

RuleFn = Callable[[AssemblySpec, CheckContext], list[Diagnostic]]

# connection.py
def run_phase_b(spec: AssemblySpec, ctx: CheckContext) -> list[Diagnostic]: ...

# completeness.py
def run_phase_c(spec: AssemblySpec, ctx: CheckContext) -> list[Diagnostic]: ...

# solvability.py
def build_buses(spec: AssemblySpec) -> list[dict]:      # 载体×连通分量 → 母线字典(内部)
def run_phase_d(spec: AssemblySpec, ctx: CheckContext) -> tuple[list[Diagnostic], list[BusSummary]]: ...
```

`checker.check_assembly` 编排逻辑:

```python
def check_assembly(spec, *, ctx=None) -> CheckResult:
    ctx = ctx or _default_context()
    # 阶段 B
    diags = run_phase_b(spec, ctx)
    # 阶段 C(依赖 B 的端口方向结论,但收集方式独立)
    diags += run_phase_c(spec, ctx)
    # 阶段 D
    d_diags, buses = run_phase_d(spec, ctx)
    diags += d_diags
    # 约束表达式(复用 core/expression.py,量纲来自端口单位)
    diags += run_constraint_checks(spec, ctx)
    diags = diags[: ctx.max_diags]
    return CheckResult(diagnostics=diags, buses=buses)
```

### 5.6 `diags.py` — ASM 域诊断码目录

```python
"""ASM 域诊断码目录(审查意见第 4 条装配/检查)。

码格式遵循 04 文档 §5.1:<域>-<类别>-<三位序号>;码一经发布永久稳定;
码 ↔ 消息键一一对应(ies.diag.asm.*);修复键独立维护(ies.fix.asm.*)。
所有码同时登记入 core/diagnostics.py 的文档目录(只登记不改既有码)。
"""
from __future__ import annotations

# 阶段 A:语法与结构
ASM_SYN_PARSE = "ASM-SYN-001"             # YAML 解析失败/类型错误
ASM_SYN_SECTION = "ASM-SYN-002"           # 未知章节
ASM_SYN_VERSION = "ASM-SYN-003"           # format_version 不受支持
ASM_SYN_FIELD = "ASM-SYN-004"             # 必填字段缺失
ASM_SYN_TYPE = "ASM-SYN-005"              # 键类型错误

# 阶段 B:连接合法性
ASM_EDGE_BAD_SOURCE = "ASM-EDGE-001"      # 起点非输出端口(输入对输出违反)
ASM_EDGE_BAD_SINK = "ASM-EDGE-002"        # 终点非输入端口
ASM_EDGE_CARRIER = "ASM-EDGE-003"         # 载体不一致
ASM_EDGE_QUANTITY = "ASM-EDGE-004"        # 物理量不一致
ASM_EDGE_UNIT_DIM = "ASM-EDGE-005"        # 单位量纲不可换算
ASM_EDGE_SELF_LOOP = "ASM-EDGE-006"       # 自环
ASM_EDGE_DUPLICATE = "ASM-EDGE-007"       # 同两端重复边
ASM_EDGE_LOOSE_BIDI = "ASM-EDGE-008"      # 双向-双向直连且母线悬空(警告)
ASM_EDGE_ZERO_CAP = "ASM-EDGE-009"        # 边容量 0/负(警告)

# 引用与输入完备(阶段 C 骨架)
ASM_REF_DUP_DEVICE = "ASM-REF-001"        # 设备 id 重复
ASM_REF_MODEL_UNREG = "ASM-REF-002"       # 模型命令未注册
ASM_REF_PORT_UNDEF = "ASM-REF-003"        # 端口引用未定义
ASM_REF_DATASET = "ASM-REF-004"           # 数据集引用缺失
ASM_REF_PORT_DECL = "ASM-REF-005"         # 端口声明与注册表不一致(警告)

ASM_INPUT_UNFED = "ASM-INPUT-001"         # 输入端口无来边(输入不完备)
ASM_INPUT_PARAM = "ASM-INPUT-002"         # 必填参数缺失
ASM_INPUT_RANGE = "ASM-INPUT-003"         # 参数越界
ASM_INPUT_LOAD_DATA = "ASM-INPUT-004"     # 负荷缺数据引用
ASM_INPUT_DATA_UNIT = "ASM-INPUT-005"     # 数据单位量纲不一致

ASM_PIPE_DELAY_MISSING = "ASM-PIPE-001"   # 有状态设备缺 delay_steps(警告)
ASM_PIPE_DELAY_RANGE = "ASM-PIPE-002"     # 延迟超出时间轴范围
ASM_PIPE_NOT_PATH = "ASM-PIPE-003"        # 管道未形成通路(警告)

# 阶段 D:整体可解性
ASM_SOLV_NO_SOURCE = "ASM-SOLV-001"       # 母线无源(约束不足)
ASM_SOLV_NO_SINK = "ASM-SOLV-002"         # 母线无汇(能量无归处)
ASM_SOLV_INFEASIBLE = "ASM-SOLV-003"      # 固定供给 < 需求(必然不可行)
ASM_SOLV_OVER_CONSTRAINED = "ASM-SOLV-004"  # 约束过度(互斥固定约束)
ASM_SOLV_CAUSAL_CYCLE = "ASM-SOLV-005"    # 有状态设备构成因果环
ASM_SOLV_ORPHAN = "ASM-SOLV-006"          # 孤立设备/母线(警告)
ASM_SOLV_DOF = "ASM-SOLV-007"             # 自由度提示(info)

# 约束表达式
ASM_CONST_SYNTAX = "ASM-CONST-001"        # 表达式语法错误
ASM_CONST_DIM = "ASM-CONST-002"           # 表达式量纲不一致
ASM_CONST_UNDEF = "ASM-CONST-003"         # 表达式引用未定义符号

# 码 → 消息键 / 修复键映射(与 core/diagnostics.py DIAG_MESSAGE_KEYS 同构)
ASM_MESSAGE_KEYS: dict[str, str] = { ... }   # "ies.diag.asm.syntax.parse" 等
ASM_FIX_HINT_KEYS: dict[str, str] = { ... }  # "ies.fix.asm.syntax.parse" 等

# 集中登记(供 core/diagnostics.py NEW_DIAG_CODES 模式引用与测试断言)
ASM_ALL_CODES: tuple[str, ...] = ( ... )
```

### 5.7 服务层与 API 集成

```python
# backend/iesplan/api/assembly.py(FastAPI 路由,main.py 挂载,前缀 /api/v1/projects/{project_id}/assembly)

@router.get("")
def get_assembly(project_id: int, db: Session, current_user: User) -> dict:
    """生成并返回规范装配文本(工作图;version 参数可选取版本图)。

    响应:{"assembly_text": str, "spec_summary": {device_count, edge_count,
    pipeline_count, constraint_count, time_axis}, "graph_hash": str}
    """

@router.post("/check")
def check_project_assembly(project_id: int, db: Session, current_user: User) -> dict:
    """运行装配检查(工作图 → build_assembly → check_assembly)。

    响应:{"ok": bool, "diagnostics": [Diagnostic...], "buses": [BusSummary...]}
    ok = 无 error/blocking 级诊断;error 级诊断阻止任务创建/下发。
    """

@router.post("/check-text")
def check_assembly_text(body: AssemblyTextIn, current_user: User) -> dict:
    """手写装配文本离线检查(不含项目上下文,registry/time_axis 取默认)。
    AssemblyTextIn = {"text": str};响应同 /check。
    """
```

任务装配集成(`services/tasks.py` 修改点,实施时补丁):

```python
# 在 assemble_snapshot 的 content_hash 去重之后、创建任务行之前:
from iesplan.assembly.checker import check_graph_inputs
result = check_graph_inputs(version_content, datasets=dataset_metas)
if not result.ok:                       # 存在 error/blocking 级诊断
    raise AssemblyCheckError(           # → HTTP 422,携带完整诊断
        diagnostics=result.diagnostics
    )
# 通过后:装配文本 = build_assembly_text(version_content, ...) 存入快照
# (content_hash 保持基于图内容,装配文本为派生产物,不破坏既有哈希语义)
```

---

## 6. 与其他模块的接口(第 1/2/3/5 步对接)

| 模块 | 对接点 | 内容 |
|---|---|---|
| 第 2 步 设备初始化 | `AssemblyDevice.model_kind/stateful` 字段来源 | 设备初始化 yaml 落库 `model_kind`(机理/数据-周期/数据-预测)与 `stateful` 标志;装配文件携带,检查器 C 阶段据此要求管道设备/延迟声明 |
| 第 3 步 建模 | `model: ies.device.*@version` 解析 | 建模模块注册的模型命令目录 = `core/registry.py` 注册表快照;`AssemblyDevice.ports` 从 `DeviceTypeSpec` 推导(载体/方向/物理量/单位),检查器 B/C 阶段以此为准 |
| 第 5 步 计算 | 装配文本 + `requirements` | 计算模块输入 = 装配文件(builder 产出)+ 模型命令(注册表引用)+ 计算要求(`requirements` 章节:算法选择/收敛精度/随机种子,从 calc_config/solver_options/random_seed 填充) |
| 快照/任务 | `check_graph_inputs` 闸门 | 装配检查 error 级诊断阻断任务下发;诊断写入任务 `diagnostics`(不可变表),与 `core/diagnostics.py` 一致 |
| 前端 | `GET /assembly`、`POST /assembly/check` | 前端只消费后端装配文本与诊断(不自行构造装配文本,不破坏后端独立性);诊断渲染按 message_key + locale |
| 第 0 条 单位标准化 | `unit` 字段贯通 | 装配文件端口单位 = 标准单位(W/J/K);非标准数值解析与换算由单位标准化模块在边界完成,检查器仅做量纲一致性(`units.convert` 可换算即一致) |

---

## 7. 实施顺序与验收标准

### 7.1 实施顺序

1. `assembly/diags.py`:ASM 域码目录 + 映射表(纯声明,先行落地便于测试断言);
2. `assembly/schema.py`:数据模型;
3. `assembly/parser.py` + 阶段 A 诊断(单测:合法/非法文本样例);
4. `assembly/checker.py` + `rules/connection.py`(阶段 B);
5. `rules/completeness.py`(阶段 C)+ `rules/solvability.py`(阶段 D,含母线构造);
6. `assembly/builder.py`(含 loss_rate 连接→管道设备包裹迁移逻辑);
7. API 路由 + `services/tasks.py` 闸门集成;
8. 前端诊断键文案(消息键/修复键)补齐。

### 7.2 验收标准

- 合法装配文本(§2.2 示例)全量检查输出 `ok=true`,0 条 error;
- 每条 ASM 码至少一个触发用例(构造性反例),诊断的 code/location/params/message_key 断言通过;
- 阶段 A 失败时 spec 为 None 且不进入 B/C/D(阶段隔离);
- 同一图内容两次 `build_assembly_text` 输出逐字节一致(确定性);
- `loss_rate > 0` 的既有连接生成后包含管道设备,且所有边均为"瞬时严格相等"语义;
- `check_graph_inputs` 对"母线无源/无汇/必然不可行/因果环/输入不完备"五类错误全部阻断任务下发,诊断进入任务 diagnostics;
- 新增 `backend/tests/test_assembly_api.py`(API 层)与 `backend/tests/test_assembly_checker.py`(纯函数层),测试与源码分离。

---

## 8. 边界与风险

1. **整体可解性的启发式局限**:阶段 D 的"约束过度"检测(ASM-SOLV-004)只能覆盖结构可判定的互斥固定约束(固定源数值冲突、禁止反送电与过剩并存);通用约束系统可解性依赖求解器运行期结果(`TASK-SOLVE-001/002` 已存在),检查器定位为**前置结构性筛查**,不替代求解。
2. **母线假设**:版本 1 计算模型为每载体单母线(02 §3);装配文本与检查器按"载体 × 连通分量"构造母线,天然兼容未来多母线扩展,但检查器当前不判定"同一载体应为一个母线"的物理合理性(跨母线热网需管道设备,管道延迟可造成母线间能量滞后,检查器不禁止)。
3. **管道设备模型**:`ies.device.transport_pipe` 为新增注册项(第 2 步设备初始化模块落地);检查器对其只检查结构(delay_steps 范围/通路/因果环),数值传递语义(逐时延时复制)由第 5 步计算模块实现,需同步在引擎层新增对 `nature: delayed` 端口的处理。
4. **诊断码登记**:ASM 域码需在 `core/diagnostics.py` 文档目录登记(只增不改);前端 locale 键同步补充,缺失键按"ies.diag.generic"兜底渲染,不阻断。
5. **手写文本与图来源的一致性**:`POST /assembly/check-text` 支持离线手写文本检查,但不参与任务快照(快照永远用 builder 产物),防止手写文本与图内容分叉。
