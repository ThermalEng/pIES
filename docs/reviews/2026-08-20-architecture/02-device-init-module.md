# 设备初始化模块定案方案(审查意见第 2 条)

> 审查意见第 2 条要求:统一接口抽象、插件式 yaml 定义、每设备一个 yaml + 标准 csv 时间序列、
> 模型类型标志(机理/数据-周期重复/数据-预测;有/无状态)、常用成本/价格/税收默认值取自统一价格 yaml。
>
> 现状差距(见调研结论 topic 2):设备类型硬编码于 `core/registry.py` 静态注册(9 类,运行期只读)、
> 无 yaml 定义、Device 表无建模方法与状态字段、价格默认值分散硬编码于注册表参数默认值 /
> `financial.py` 默认参数 / `eval_run.py` 回退值 / `planning.py` 单价回退,无单一事实源;
> 设备数学模型是手写函数库 `engines/devices.py`,无统一接口与标准化调用命令。
>
> 本方案:以 **"每设备一个 yaml + 标准 csv + 统一价格 yaml"** 为输入,经 **loader → registry →
> generator** 三步生成"统一接口的设备模块函数",供计算模块按命令调用。

---

## 1. 总体架构与数据流

```
                        ┌──────────────────────────────────────────────┐
                        │          设备初始化模块 iesplan/devices/       │
输入(数据文件,非代码)   │                                              │
  devices/prices.yaml   │   prices.py ──→ PriceBook(价格单一事实源)     │
  devices/<id>.yaml     │   loader.py ──→ DeviceYamlSpec(每设备一个)    │
  devices/<id>.csv      │   csvio.py  ──→ 标准时间序列(校验/模板/读取)   │
                        │              │                                │
                        │   registry.py(运行期注册表,可热加载)           │
                        │   generator.py(按 modeling_method 生成函数)   │
                        └──────────────┼───────────────────────────────┘
                                       │ 统一调用契约 device_entry()
                                       ▼
        计算模块: worker/executors.py → engines(经 registry 取函数,不再直接 import)
```

设计原则:
1. **插件式**:新增设备 = 新增 `<type_id>.yaml`(+ 可选 csv/模型文件),不改 Python 代码、不重启可重载;
   `core/registry.py` 的静态注册退化为内置兜底副本。
2. **单文件事实源**:价格/成本/税收默认值一律从 `prices.yaml` 解析,注册表与引擎内不再硬编码数字。
3. **统一调用契约**:所有设备函数(机理/数据-周期重复/数据-预测、有/无状态)遵循同一签名
   `device_entry(params, series, state, dt_s, prices) → outputs + state_new`,计算模块按命令字符串分发。
4. **兼容过渡**:`DeviceTypeSpec`/`ParameterSpec` 数据结构复用,新增字段向后兼容;旧静态注册路径保留到迁移完成。

---

## 2. 设备 yaml 规范

### 2.1 字段总表

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type_id` | string | 是 | 注册 id,`ies.device.<name>`,校验 `^ies\.device\.[a-z][a-z0-9_]*$` |
| `version` | string | 是 | semver `x.y.z` |
| `name_zh` / `name_en` | string | 是 | 显示名 |
| `modeling_method` | enum | 是 | 模型类型标志:`mechanism` \| `data_periodic` \| `data_forecast`(见第 3 节) |
| `statefulness` | enum | 是 | `stateless` \| `stateful`(见第 3 节) |
| `fidelity` | enum | 否 | `low` \| `medium` \| `high`,默认 `medium`(兼容现有 `model_fidelity`) |
| `energy_carriers` | list[string] | 是 | 能量载体:`electric/heat/cool/gas/solar` |
| `is_load` | bool | 是 | 是否负荷类 |
| `capabilities` | list[string] | 否 | 能力字典,沿用 04 §2.3(如 `controllable/optimization_variable`) |
| `extends` | string | 否 | 基类 id,默认 `ies.device.base`(占位,本期不做继承解析) |
| `help_topic` | string | 否 | 帮助键 |
| `ports` | list[PortSpec] | 是 | 端口定义(见 2.2) |
| `parameters` | dict[str, ParameterSpec] | 是 | 参数 schema(见 2.3),字段与 `core/registry.py::ParameterSpec` 对齐 |
| `time_series` | dict | 否 | 标准 csv 列声明:`inputs`/`outputs`(见 2.4 与第 4 节) |
| `states` | list[StateSpec] | stateful 必填 | 状态定义(见 2.5) |
| `function` | dict | 见 2.6 | 函数绑定(mechanism 绑定入口;data_* 指向模型文件) |

### 2.2 端口 PortSpec

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | string | 端口名(图内唯一) |
| `port_type` | enum | `electric/thermal/cooling/fuel/water/data`(与 `Port.port_type` CHECK 对齐) |
| `direction` | enum | `in/out/bidirectional` |
| `energy_carrier` | string | 载体 |
| `capacity_ref` | string | 容量取自的参数名(可选;`capacity` 单位=参数单位) |

### 2.3 参数 ParameterSpec(yaml 键名与 `core/registry.py` 一致)

| 键 | 类型 | 说明 |
|---|---|---|
| `unit` | string | 注册单位:`kW/kWh/kWp/CNY/kWh/CNY/kW·月/°C/deg/次/a/-` 等(与注册表约定一致) |
| `min` / `max` | number|null | 取值范围(None=不限制) |
| `default` | number\|string\|dict | 默认值;字符串 `$price:<键>` 表示从 `prices.yaml` 解析(见 2.7) |
| `is_optimizable` | bool | 是否优化变量 |
| `stock_or_addition` | enum | `stock`(存量,容量固定)\| `addition`(新增,容量可优化) |
| `existing_default` | number|null | 存量默认(缺省:存量取 default,新增取 0,同 `_p()`) |
| `enum` | list|null | 枚举取值 |
| `help_key` | string | 帮助键 |

### 2.4 时间序列 time_series

```yaml
time_series:
  inputs:      # 该设备消费的标准 csv 列(必填列+可选列)
    - key: ghi
      unit: W/m²
      resolution: 1h
      required: true
  outputs:     # 该设备产出的列(机理模型通常为空;数据模型可声明学习输出)
    - key: cop
      unit: "-"
      resolution: 1h
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `key` | string | 列名(与第 4 节标准列注册表一致,或设备私有列 `device.<type_id>.<name>`) |
| `unit` | string | 列单位(与 `dataset.py::STANDARD_FIELDS` 单位对齐,数值不做换算,只做声明与校验) |
| `resolution` | enum | `15min/30min/1h`(行数 35040/17520/8760) |
| `required` | bool | 缺列是否报错 |
| `period` | enum,仅 data_periodic | 周期粒度:`day/week/year`(典型日/周/年曲线重复外推) |

### 2.5 状态 StateSpec(stateful 设备)

| 字段 | 类型 | 说明 |
|---|---|---|
| `key` | string | 状态名(如 `soc`) |
| `unit` | string | 单位 |
| `initial_ref` | string | 初始值取自的参数名(如 `initial_soc`) |
| `bounds` | dict | `{min_ref: 参数名, max_ref: 参数名}`,状态上下限取参数 |

### 2.6 函数绑定 function

```yaml
# mechanism(机理模型):绑定生成的模块函数入口
function:
  entry: pv_output          # 生成的模块内函数名
  package: iesplan.devices.generated.pv   # 生成的模块包路径

# data_periodic / data_forecast:指向模型文件(随设备 yaml 打包)
function:
  model_file:
    path: cop_model.onnx    # 相对设备目录
    format: onnx            # onnx | csv_lookup | python
    inputs: [t_ambient, load_ratio]
    outputs: [cop]
```

### 2.7 默认成本引用 `$price:`

`default` 为字符串 `$price:<prices.yaml 键>` 时,加载期由 `prices.py::resolve_param_default` 解析为数值;
解析失败(键缺失)抛 AppError,拒绝该设备注册(避免 `planning.py:304-315` 注释所述"默认值不落库 → CAPEX 静默 0"问题)。

### 2.8 完整示例

`devices/pv.yaml`(机理、无状态):

```yaml
type_id: ies.device.pv
version: 1.4.0
name_zh: 光伏
name_en: Photovoltaic (PV)
help_topic: help.modeling.pv
modeling_method: mechanism
statefulness: stateless
fidelity: high
energy_carriers: [solar, electric]
is_load: false
capabilities: [pv, controllable, optimization_variable]
extends: ies.device.base

ports:
  - {name: solar_in, port_type: solar, direction: in, energy_carrier: solar}
  - {name: pv_out, port_type: electric, direction: out, energy_carrier: electric,
     capacity_ref: rated_capacity_kwp}

parameters:
  rated_capacity_kwp:
    unit: kWp
    min: 0
    max: 1000000
    default: 0
    is_optimizable: true
    stock_or_addition: addition
    help_key: help.param.pv.rated_capacity_kwp
  max_capacity_kwp: {unit: kWp, min: 0, max: 1000000, default: 1000, stock_or_addition: addition,
                     help_key: help.param.pv.max_capacity_kwp}
  efficiency: {unit: "-", min: 0.05, max: 0.5, default: 0.20, help_key: help.param.pv.efficiency}
  tilt_deg: {unit: deg, min: 0, max: 90, default: 30, help_key: help.param.pv.tilt_deg}
  azimuth_deg: {unit: deg, min: 0, max: 360, default: 180, help_key: help.param.pv.azimuth_deg}
  unit_invest_cost: {unit: CNY/kWp, min: 0, default: "$price:device_costs.pv.unit_invest_cost",
                     stock_or_addition: addition, help_key: help.param.pv.unit_invest_cost}
  lifetime_years: {unit: a, min: 1, max: 50, default: "$price:device_costs.pv.lifetime_years",
                   help_key: help.param.pv.lifetime_years}

time_series:
  inputs:
    - {key: ghi, unit: W/m², resolution: 1h, required: true}
    - {key: t_ambient, unit: "°C", resolution: 1h, required: true}
  outputs: []

function:
  entry: pv_output
  package: iesplan.devices.generated.pv
```

`devices/battery.yaml`(机理、有状态):

```yaml
type_id: ies.device.battery
version: 1.5.0
name_zh: 电池储能
name_en: Battery Storage
help_topic: help.modeling.battery
modeling_method: mechanism
statefulness: stateful
fidelity: high
energy_carriers: [electric]
is_load: false
capabilities: [storage, controllable, optimization_variable]

ports:
  - {name: bat_in, port_type: electric, direction: in, energy_carrier: electric,
     capacity_ref: rated_power_kw}
  - {name: bat_out, port_type: electric, direction: out, energy_carrier: electric,
     capacity_ref: rated_power_kw}

parameters:
  capacity_kwh: {unit: kWh, min: 0, max: 10000000, default: 0, is_optimizable: true,
                 stock_or_addition: addition, help_key: help.param.battery.capacity_kwh}
  rated_power_kw: {unit: kW, min: 0, max: 1000000, default: 0, is_optimizable: true,
                   stock_or_addition: addition, help_key: help.param.battery.rated_power_kw}
  charge_efficiency: {unit: "-", min: 0.5, max: 1.0, default: 0.95, help_key: help.param.battery.charge_efficiency}
  discharge_efficiency: {unit: "-", min: 0.5, max: 1.0, default: 0.95, help_key: help.param.battery.discharge_efficiency}
  max_soc: {unit: "-", min: 0.5, max: 1.0, default: 0.90, help_key: help.param.battery.max_soc}
  min_soc: {unit: "-", min: 0, max: 0.5, default: 0.1, help_key: help.param.battery.min_soc}
  initial_soc: {unit: "-", min: 0, max: 1.0, default: 0.5, help_key: help.param.battery.initial_soc}
  unit_invest_cost: {unit: CNY/kWh, min: 0, default: "$price:device_costs.battery.unit_invest_cost",
                     stock_or_addition: addition, help_key: help.param.battery.unit_invest_cost}
  lifetime_years: {unit: a, min: 1, max: 50, default: "$price:device_costs.battery.lifetime_years",
                   help_key: help.param.battery.lifetime_years}

states:
  - {key: soc, unit: "-", initial_ref: initial_soc, bounds: {min_ref: min_soc, max_ref: max_soc}}

time_series:
  inputs: []
  outputs: []

function:
  entry: battery_output
  package: iesplan.devices.generated.battery
```

`devices/electric_load.yaml`(数据-周期重复、无状态):

```yaml
type_id: ies.device.electric_load
version: 1.2.0
name_zh: 电负荷
name_en: Electric Load
help_topic: help.modeling.electric_load
modeling_method: data_periodic
statefulness: stateless
fidelity: medium
energy_carriers: [electric]
is_load: true
capabilities: [load, switchable]

ports:
  - {name: load_in, port_type: electric, direction: in, energy_carrier: electric,
     capacity_ref: peak_power_kw}

parameters:
  peak_power_kw: {unit: kW, min: 0, max: 10000000, default: 0, help_key: help.param.load.peak_power_kw}
  load_profile: {unit: reference, default: null, help_key: help.param.load.load_profile}
  annual_energy_kwh: {unit: kWh, min: 0, default: 0, help_key: help.param.load.annual_energy_kwh}
  is_switchable: {unit: "-", default: false, help_key: help.param.load.is_switchable}

time_series:
  inputs:
    - {key: e_load, unit: kWh, resolution: 1h, required: true, period: day}
  outputs:
    - {key: e_load_kw, unit: kW, resolution: 1h}

function:
  entry: periodic_load_output
  package: iesplan.devices.generated.electric_load
```

`devices/heat_pump.yaml`(数据-预测、有状态,示例):

```yaml
type_id: ies.device.heat_pump_dr
version: 1.0.0
name_zh: 热泵(数据预测模型)
name_en: Heat Pump (forecast model)
help_topic: help.modeling.heat_pump
modeling_method: data_forecast
statefulness: stateful
fidelity: high
energy_carriers: [electric, heat, cool]
is_load: false
capabilities: [heat_pump, controllable]

parameters:
  rated_heat_kw: {unit: kW, min: 0, max: 1000000, default: 0, is_optimizable: true,
                  stock_or_addition: addition, help_key: help.param.heatpump.rated_heat_kw}
  unit_invest_cost: {unit: CNY/kW, min: 0, default: "$price:device_costs.heat_pump.unit_invest_cost",
                     stock_or_addition: addition, help_key: help.param.heatpump.unit_invest_cost}

states:
  - {key: cop_est, unit: "-", initial_ref: cop_init}

time_series:
  inputs:
    - {key: t_ambient, unit: "°C", resolution: 1h, required: true}
    - {key: h_load, unit: kWh, resolution: 1h, required: true}
  outputs:
    - {key: cop, unit: "-", resolution: 1h}

function:
  model_file:
    path: cop_model.onnx
    format: onnx
    inputs: [t_ambient, h_load]
    outputs: [cop]
```

---

## 3. 模型类型标志枚举(定案)

```python
# iesplan/devices/enums.py(或 core/registry.py 常量,二选一,建议 devices/enums.py)
MODELING_METHODS = ("mechanism", "data_periodic", "data_forecast")
STATEFULNESS_VALUES = ("stateless", "stateful")
FIDELITY_VALUES = ("low", "medium", "high")
```

| 枚举值 | 中文 | 含义 | 函数来源 | 典型设备 |
|---|---|---|---|---|
| `mechanism` | 机理模型 | 解析式/物理公式,由 yaml `function.entry` 绑定内置公式库 | `iesplan.devices.generated.<id>` 内 import | pv、battery、gas_boiler、chiller |
| `data_periodic` | 数据-周期重复 | 从标准 csv 提取典型日/周/年曲线,按 `period` 周期重复外推生成全年序列 | `generator.py::build_periodic_function` 运行时生成 | 电/热/冷负荷(load_profile) |
| `data_forecast` | 数据-预测 | 加载预测模型文件(onnx/csv 查表/python),输入时间序列输出预测序列 | `generator.py::load_forecast_model` | 数据驱动的 COP/出力预测 |

| 枚举值 | 中文 | 含义 |
|---|---|---|
| `stateless` | 无状态 | 输出只依赖当前步输入(如 pv 出力、锅炉) |
| `stateful` | 有状态 | 输出依赖跨步状态(如电池 SOC);由 `states` 声明,运行期在 `state` 字典中传递与回写 |

约束:
- `stateful` 设备必须声明 `states`;`states` 仅允许出现在 `stateful` 设备(校验期强制)。
- `data_periodic` 设备 `time_series.inputs` 中至少一个 `required: true` 列带 `period`。
- `data_forecast` 设备必须声明 `function.model_file`。
- **与现有字段关系**:`modeling_method`/`statefulness` 是新维度,与 `model_fidelity`(low/medium/high 精度档)正交共存;
  `fidelity` 只影响收敛/采样策略,不影响函数入口选择。

**DB 迁移**(`models/model.py::Device`,同步 `backend/tests` 中的 fixture):

```sql
ALTER TABLE devices ADD COLUMN modeling_method TEXT NOT NULL DEFAULT 'mechanism';
ALTER TABLE devices ADD COLUMN statefulness TEXT NOT NULL DEFAULT 'stateless';
-- CHECK: modeling_method IN ('mechanism','data_periodic','data_forecast')
--       statefulness IN ('stateless','stateful')
-- 保留原有 model_fidelity/kind/status CHECK 不动
```

前端 `types.ts::Device` 增补 `modeling_method: string; statefulness: string`,保存/展示透传;设备类型目录接口返回 `modelingMethod/statefulness` 供建模页筛选。

---

## 4. 标准 csv 时间序列格式

### 4.1 文件组织(每设备一个 yaml + 一个标准 csv)

```
devices/                         # 设备数据目录(仓库内置;可由项目数据目录覆盖)
├── prices.yaml                  # 统一价格 yaml
├── pv.yaml
├── pv.csv                       # 与 pv.yaml 同名打包的标准时间序列(可选列模板/样例)
├── battery.yaml
├── electric_load.yaml
├── electric_load.csv
└── ...
```

- 每个设备目录内 `<type_id>.yaml` 必选;`<type_id>.csv` 为可选的**标准时间序列数据文件**:
  - mechanism 设备:csv 可省略(数值序列由计算模块运行时装配);
  - data_periodic 设备:csv 必选(周期曲线的数据来源);
  - data_forecast 设备:csv 必选(预测模型的历史输入/校验数据)+ 模型文件(`function.model_file.path`,如 `cop_model.onnx`)同目录打包。
- 模型文件+标准数据文件 → `generator.py` 生成模块函数(`build_module_function`),满足审查意见"以模型文件+标准数据文件为输入生成模块函数"。

### 4.2 列规范

| 列 | 必选 | 单位 | 说明 |
|---|---|---|---|
| `timestamp` | 是(第一列) | — | ISO8601 含固定 UTC 偏移;严格递增无重复(复用 `core/timeaxis.py::validate_timestamps`) |
| `e_load` | 是 | kWh | 时段电负荷(现有 `dataset.py::STANDARD_FIELDS` 全套保留) |
| `h_load` / `c_load` | 否 | kWh | 热/冷负荷 |
| `t_ambient` | 否 | °C | 环境温度 |
| `ghi` | 否 | W/m² | 水平总辐照 |
| `electricity_price` | 否 | 元/kWh | 分时购电价(仅当设备/计算需要分时价格序列) |
| `grid_emission_factor` | 否 | kgCO₂/kWh | 电网排放因子 |
| `device.<type_id>.<name>` | 设备私有 | 由 yaml 声明 | 设备专属列(如 `device.heat_pump.cop`),命名空间避免冲突 |

- **列 → 单位**映射只认 yaml `time_series` 声明与 `STANDARD_FIELDS` 注册表;表头第二行沿用现有双语注释行模板
  (字段说明/单位/示例,`dataset.py::make_template` 逻辑复用)。
- **分辨率与行数**:`15min → 35040`、`30min → 17520`、`1h → 8760`(`core/timeaxis.py::RESOLUTIONS`);
  设备 csv 与计算快照时间轴分辨率必须一致,否则报诊断(复用 `DATA_TS_*` 码)。
- **校验规则**(`csvio.py::validate_series_csv`,错误定位文件/列/行,复用 `Diagnostic`):
  1. 列名齐全(yaml required 列缺失 → 错误);2. 时间戳递增无重复、步长对齐;3. 数值非空、在
  `FieldSpec.min/max` 或参数 min/max 范围内;4. 单位声明一致(大小写不敏感,沿用
  `dataset.py:866` 语义,但**新增数值换算校验**:非标准单位列在 yaml 中必须给出换算声明,否则报错而非静默透传)。

### 4.3 模板生成

`make_template_csv(spec, resolution="1h", rows=8760)` 输出含双语文案注释行的可下载模板(与现有 U05 数据集模板同风格)。

---

## 5. 价格初始化 yaml(统一价格事实源)

`devices/prices.yaml` 是"常用成本/价格/税收默认值"的**唯一事实源**;现有硬编码
(registry.py 参数默认值、`financial.py::tax_rate=0.25`、`eval_run.py::gas_price=3.2`、
`planning.py` 单价回退)全部迁移于此。

```yaml
# devices/prices.yaml — 统一价格/成本/税收默认值(版本化,加载期校验键完整性)
version: 1.0.0
currency: CNY

# 能源价格(默认值;项目级配置可覆盖)
energy_prices:
  electricity:
    import_tariff: {peak: 1.1, flat: 0.7, valley: 0.3}   # CNY/kWh
    export_tariff: 0.35                                  # CNY/kWh
    demand_charge: 40.0                                  # CNY/kW·月
  gas:
    price: 3.2                                           # CNY/m³
    lhv_kj_per_m3: 35900                                 # kJ/m³
  grid_emission_factor: 0.581                            # kgCO2/kWh

# 设备单位成本与寿命默认(键名 = 设备 type_id 末段)
device_costs:
  pv:            {unit_invest_cost: 3500,  unit: CNY/kWp, lifetime_years: 25, annual_om_rate: 0.01}
  battery:       {unit_invest_cost: 900,   unit: CNY/kWh, lifetime_years: 10, annual_om_rate: 0.02}
  heat_pump:     {unit_invest_cost: 1800,  unit: CNY/kW,  lifetime_years: 20, annual_om_rate: 0.01}
  gas_boiler:    {unit_invest_cost: 600,   unit: CNY/kW,  lifetime_years: 15, annual_om_rate: 0.02}
  electric_chiller: {unit_invest_cost: 1200, unit: CNY/kW, lifetime_years: 18, annual_om_rate: 0.01}
  electric_load: {unit_invest_cost: 0,     unit: CNY/kW,  lifetime_years: 20}

# 财务默认(替换 financial.py 默认参数与注册表 discount_rate)
finance:
  tax_rate: 0.25
  discount_rate: 0.08
  project_years: 20
  depreciation_years: 10
  irr_floor: 0.08
```

- **引用解析**:设备 yaml 中 `default: "$price:device_costs.pv.unit_invest_cost"` → `PriceBook`
  按点分路径查值;加载设备时解析并写入该参数的运行时默认值。
- **覆盖优先级**:项目级 `calc_config` 显式参数 > 设备 yaml `default` > `prices.yaml` 默认值(即
  yaml 缺省时最后落回 `PriceBook` 对应键)。
- **消费方**:
  1. `services/config.py::_default_variables` 变量默认值(带 `unit` 字段);
  2. `metrics/financial.py` 的 `tax_rate`/`discount_rate` 等函数默认参数改为注入 `PriceBook.finance`;
  3. `engines/eval_run.py`/`planning.py` 的 gas_price/单价回退改为 `price_book.get(...)`,缺失即抛错(不再静默 0)。

---

## 6. 模块目录 `backend/iesplan/devices/` 文件结构与公共函数签名

```
backend/iesplan/devices/
├── __init__.py          # 公共出口:load_all_devices / DeviceRegistry / load_price_book
├── enums.py             # MODELING_METHODS / STATEFULNESS_VALUES / FIDELITY_VALUES
├── schema.py            # yaml → DeviceYamlSpec / PortSpec / SeriesSpec / StateSpec 解析与结构校验
├── prices.py            # PriceBook 加载与 $price: 解析
├── loader.py            # 目录发现、加载、联合校验(yaml+csv+prices)
├── registry.py          # 运行期设备注册表(替代 core/registry.py 静态注册)
├── generator.py         # 模块函数生成(按 modeling_method 分发,统一调用契约)
├── csvio.py             # 标准 csv 读取/模板/校验
└── validate.py          # yaml+csv 联合校验 → list[Diagnostic]
```

### 6.1 `schema.py` — yaml 数据模型(复用 `core/registry.py::ParameterSpec`)

```python
from iesplan.core.registry import ParameterSpec

@dataclass(frozen=True, slots=True)
class SeriesSpec:
    key: str
    unit: str
    resolution: str                 # "15min" | "30min" | "1h"
    required: bool = True
    period: str | None = None       # "day" | "week" | "year"(仅 data_periodic)

@dataclass(frozen=True, slots=True)
class PortSpec:
    name: str
    port_type: str                  # electric/thermal/cooling/fuel/water/data
    direction: str                  # in/out/bidirectional
    energy_carrier: str
    capacity_ref: str | None = None

@dataclass(frozen=True, slots=True)
class StateSpec:
    key: str
    unit: str
    initial_ref: str | None = None
    bounds: dict[str, str] | None = None   # {"min_ref": ..., "max_ref": ...}

@dataclass(frozen=True, slots=True)
class DeviceYamlSpec:
    type_id: str
    version: str
    name_zh: str
    name_en: str
    modeling_method: str            # mechanism | data_periodic | data_forecast
    statefulness: str               # stateless | stateful
    fidelity: str = "medium"
    energy_carriers: list[str] = field(default_factory=list)
    is_load: bool = False
    capabilities: list[str] = field(default_factory=list)
    extends: str = "ies.device.base"
    help_topic: str = ""
    ports: list[PortSpec] = field(default_factory=list)
    parameters: dict[str, ParameterSpec] = field(default_factory=dict)
    time_series: dict[str, list[SeriesSpec]] = field(default_factory=dict)  # {"inputs": [...], "outputs": [...]}
    states: list[StateSpec] = field(default_factory=list)
    function: dict[str, object] = field(default_factory=dict)  # {"entry","package"} 或 {"model_file": {...}}
    base_dir: str = ""              # yaml 所在目录(相对引用 model_file/csv 用)

def load_yaml(path: Path) -> DeviceYamlSpec:
    """解析单个设备 yaml;结构/枚举/必填字段非法抛 AppError(码沿用 SYS-CFG-001)。"""

def to_registry_spec(spec: DeviceYamlSpec) -> "DeviceTypeSpec":
    """转 core/registry.py::DeviceTypeSpec(兼容层,供现有 API 复用);
    DeviceTypeSpec 增补 modeling_method/statefulness 两字段后直接透传。"""
```

### 6.2 `prices.py` — 价格事实源

```python
@dataclass(frozen=True, slots=True)
class PriceBook:
    version: str
    currency: str
    energy_prices: dict[str, object]
    device_costs: dict[str, dict[str, object]]
    finance: dict[str, float]
    emissions: dict[str, float]

def load_price_book(path: Path | None = None) -> PriceBook:
    """加载 prices.yaml;缺省路径 devices/prices.yaml。键缺失/类型非法抛 AppError。"""

def get(book: PriceBook, dotted_key: str) -> object:
    """按点分路径取价格值('device_costs.pv.unit_invest_cost'),缺失抛 NotFoundError。"""

def resolve_param_default(spec: DeviceYamlSpec, book: PriceBook) -> dict[str, object]:
    """把 parameters 中所有 '$price:...' 字符串默认值解析为数值;
    解析失败抛 AppError 并拒绝该设备注册(杜绝静默 0 投资额)。"""

def finance_defaults(book: PriceBook) -> dict[str, float]:
    """返回 finance 段(tax_rate/discount_rate/project_years/depreciation_years/irr_floor),
    供 financial.py / eval_run.py 注入。"""
```

### 6.3 `loader.py` — 目录发现与加载

```python
def discover_device_dirs(base_dir: Path) -> list[Path]:
    """扫描 base_dir 下含 <type_id>.yaml 的子目录;返回按 type_id 排序的目录列表(确定性)。"""

def validate_device_dir(dir_path: Path, book: PriceBook) -> list["Diagnostic"]:
    """联合校验一个设备目录:yaml 结构 + $price 引用 + (如有)csv 列/单位/分辨率/行数 + 模型文件存在;
    返回 Diagnostic 列表(错误级别拒载,警告级别放行)。"""

def load_device_type(dir_path: Path, book: PriceBook) -> DeviceYamlSpec:
    """加载单个设备(校验通过 + 价格解析完成后返回);失败抛 AppError 并携带全部诊断。"""

def load_all_devices(base_dir: Path, book: PriceBook) -> list[DeviceYamlSpec]:
    """加载目录下全部设备;任一设备校验失败 → 整体拒绝加载(保持受控加载语义),
    与 core/registry.py 现状一致(失败即拒绝,不部分生效)。"""
```

### 6.4 `registry.py` — 运行期注册表(替换静态注册)

```python
class DeviceRegistry:
    def __init__(self, base_dir: Path, price_book: PriceBook): ...

    def load(self) -> None:
        """幂等加载;支持目录 mtime 变化后 reload()(插件式热加载,运行期只读仍成立:
        计算快照引用 id@version,reload 后旧版本仍可解析)。"""

    def reload(self) -> None: ...

    def get(self, type_id: str) -> DeviceYamlSpec: ...          # 未注册抛 NotFoundError(CONN-TYPE-002)
    def list(self) -> list[DeviceYamlSpec]: ...                  # 按注册顺序,确定性
    def snapshot(self) -> list[str]: ...                         # ["ies.device.pv@1.4.0", ...],沿用现有格式
    def get_entry_function(self, type_id: str) -> "Callable":    # generator 生成的 device_entry
    def port_directions(self, type_id: str) -> dict[str, str]:   # 替代 services/model.py::_DEVICE_PORT_DIRECTIONS
    def coarse_category(self, type_id: str) -> str:              # 替代 services/model.py::_DEVICE_COARSE_CATEGORY

# 进程内单例(与 core/registry.py 现语义一致,启动时由 main.py 初始化)
_registry: DeviceRegistry | None = None
def get_registry() -> DeviceRegistry: ...
def init_registry(base_dir: Path, book: PriceBook) -> DeviceRegistry: ...
```

`core/registry.py` 改造(兼容层):
- `DeviceTypeSpec` 增加 `modeling_method: str = "mechanism"`、`statefulness: str = "stateless"`;
- `load_registry()` 改为:优先调用 `devices.loader.load_all_devices`(yaml 目录),yaml 缺失时回退内置 9 类静态注册(迁移期兜底);
- `get_device_type/list_device_types/snapshot` 签名不变,内部委托 `DeviceRegistry`。

### 6.5 `generator.py` — 模块函数生成(统一调用契约)

**统一设备运行接口(所有设备函数遵循,标准化函数名/输入/输出)**:

```python
def device_entry(
    params: dict[str, float],          # 注册表单位(业务单位:kW/kWh/kWp 等),已含价格解析后的默认值
    series: dict[str, np.ndarray],     # 标准列 → 内部单位序列(W/J/K,由 runner 在装配期换算)
    state: dict[str, float] | None,    # 有状态设备的当前状态快照;stateless 传 None
    dt_s: float,                       # 时间步长(秒)
    prices: dict[str, float],          # 运行期价格(来自 PriceBook + 项目覆盖)
) -> "DeviceRunResult"

@dataclass(frozen=True, slots=True)
class DeviceRunResult:
    outputs: dict[str, np.ndarray]     # 端口输出(内部单位:W 功率 / J 能量 / 无量纲),键=端口名
    state_new: dict[str, float] | None # 有状态设备的下一状态;stateless 为 None
    cost: dict[str, np.ndarray]        # 运行成本序列(CNY,分项:buy/gas/om 等,可选)
    emissions: dict[str, np.ndarray]   # 排放序列(kgCO2,可选)

def build_module_function(spec: DeviceYamlSpec, data_dir: Path | None = None) -> "Callable":
    """按 modeling_method 分发,返回符合 device_entry 签名的函数:
      - mechanism:     import 内置公式库入口(engines/devices.py 现有函数适配包装),
                       包装层完成 W/J 换算与参数映射(yaml parameters → 函数参数);
      - data_periodic: build_periodic_function(spec, csv_df) 生成的周期外推闭包;
      - data_forecast: load_forecast_model(spec) 加载 onnx/csv 查表模型并包装。
    生成结果缓存于 registry(按 id@version 缓存,不落盘)。"""

def build_periodic_function(spec: DeviceYamlSpec, csv_df: pd.DataFrame) -> "Callable":
    """从标准 csv 提取典型曲线(按 period 分组平均:day→24 点、week→168 点、year→8760 点),
    生成"按时间轴周期重复 + 容量缩放(×params[capacity_ref]/曲线峰值)"的 device_entry 闭包。"""

def load_forecast_model(spec: DeviceYamlSpec) -> "Callable":
    """按 function.model_file.format 加载(onnxruntime / pandas 查表 / python 模块),
    输入 yaml 声明的 inputs 列,输出 outputs 列;缺失模型文件抛 AppError。"""

def call_command(registry: DeviceRegistry, command: str, ctx: dict) -> DeviceRunResult:
    """计算模块的标准化调用命令分发入口:command = 'ies.device.<id>@<version>:<entry>',
    拆解后经 registry.get_entry_function 调用;未知命令抛 NotFoundError。
    (对接审查意见第 1 条'模块调用命令':设备函数具备标准命令串,供装配/计算模块引用)"""
```

### 6.6 `csvio.py` — 标准 csv 读写

```python
def read_standard_csv(path: Path, spec: DeviceYamlSpec) -> pd.DataFrame:
    """读取设备 csv:表头(含可选注释行)解析 → 列存在性(required)校验 → timestamp 归一化。"""

def validate_series_csv(df: pd.DataFrame, spec: DeviceYamlSpec) -> list["Diagnostic"]:
    """列/单位声明一致、时间戳递增无重复步长对齐、行数(35040/17520/8760)、数值范围;
    错误定位到文件/列/行(复用 Diagnostic 与 DATA-* 码)。"""

def make_template_csv(spec: DeviceYamlSpec, resolution: str = "1h", rows: int = 8760) -> str:
    """生成带双语注释行(字段说明/单位/示例)的模板 csv 文本(复用 dataset.py 模板风格)。"""

def extract_period_curve(df: pd.DataFrame, period: str) -> np.ndarray:
    """data_periodic:按 period 聚合出典型曲线(day/week/year),供 build_periodic_function 使用。"""
```

### 6.7 `__init__.py` 公共出口

```python
from iesplan.devices.registry import DeviceRegistry, get_registry, init_registry
from iesplan.devices.loader import load_all_devices, validate_device_dir
from iesplan.devices.prices import PriceBook, load_price_book, finance_defaults
from iesplan.devices.schema import DeviceYamlSpec, PortSpec, SeriesSpec, StateSpec
__all__ = ["DeviceRegistry", "get_registry", "init_registry", "load_all_devices",
           "validate_device_dir", "PriceBook", "load_price_book", "finance_defaults",
           "DeviceYamlSpec", "PortSpec", "SeriesSpec", "StateSpec"]
```

---

## 7. 与现有代码的接入点(实施清单)

| # | 位置 | 改动 |
|---|---|---|
| 1 | `devices/enums.py` | 新增(枚举常量) |
| 2 | `devices/schema.py` / `prices.py` / `loader.py` / `registry.py` / `generator.py` / `csvio.py` / `validate.py` | 新增(本方案主体) |
| 3 | `devices/prices.yaml` + 9 个 `devices/<id>.yaml`(+ electric_load.csv 等样例) | 新增数据文件;yaml 内容 = 现有 `load_registry()` 9 类参数原样迁移,`default` 数值换成 `$price:` 引用 |
| 4 | `core/registry.py` | `DeviceTypeSpec` 增 `modeling_method/statefulness`;`load_registry()` 改为 yaml 优先、静态兜底;`ParameterSpec` 不变 |
| 5 | `models/model.py::Device` | 新增 `modeling_method`/`statefulness` 两列 + CHECK(见第 3 节);同步 `backend/tests` fixture 与 `services/model.py` 序列化 |
| 6 | `services/model.py` | `_DEVICE_PORT_DIRECTIONS`/`_DEVICE_COARSE_CATEGORY` 硬编码表删除,改调 `registry.port_directions/coarse_category`(yaml 派生) |
| 7 | `worker/executors.py` / `runner.py` | 设备函数改经 `registry.get_entry_function(type_id)` 调用;快照携带注册表 `snapshot()`;装配期按 yaml 做单位换算(替代各调用点硬编码 ×1000 的边界:内部统一 W/J/K) |
| 8 | `metrics/financial.py` / `engines/eval_run.py` / `engines/planning.py` / `services/config.py` | `tax_rate`/`discount_rate`/`gas_price`/`unit_invest_cost` 回退值全部改由 `PriceBook` 注入(删除硬编码) |
| 9 | `services/config.py::_dims_for_unit` | 单位查表补充 yaml 声明单位(注册表单位与 `units.py` 映射对齐,见意见第 0 条联动) |
| 10 | `frontend/src/types.ts` + 建模/设备类型相关页面 | `Device`/设备类型目录类型增 `modelingMethod`/`statefulness`;设备类型列表接口返回新字段 |
| 11 | `main.py` | 启动时 `init_registry(devices_dir, load_price_book())`;提供 `GET /api/devices/types`(来自注册表,非硬编码) |
| 12 | 测试 | `backend/tests/test_device_init.py`:yaml 解析/价格解析/联合校验/周期外推函数/统一调用契约;与现有 `test_model_api.py` 兼容(Device 表新列默认值) |

**验收标准**:
1. 新增设备类型 = 放入 `<type_id>.yaml`(+csv),无需改代码,`GET /api/devices/types` 即出现;
2. `prices.yaml` 任一价格键删除 → 相关设备加载失败(错误含键名),不再出现静默 0 投资额;
3. 电池(有状态)、光伏(无状态)、负荷(data_periodic)三类调用均走同一 `device_entry` 契约,`engines/devices.py` 不再被 executors 直接 import;
4. `modeling_method`/`statefulness` 落库并随设备快照/前端往返完整(不丢失);
5. 存量 14/15 条核心闭环用例回归通过。
