# 后端模块解耦定案方案(审查意见第 0/1/2/3/4/5/6/7/10 条)

> 状态:定案,供实施 agent 直接编码。
> 范围:后端按「设备初始化 → 建模 → 装配与检查 → 计算 → 结果分析」五段解耦;新增 `devices/`、`modeling/`、`assembly/`、`finance/`、`analysis/` 五个独立包;计算模块复用现有 `services/tasks` + `worker/executors` + `engines`,只做改造不重建。
> 实施原则:阶段推进,每阶段保持全量测试通过(backend/tests/ 全绿);不允许一次性大改。

---

## 1. 背景与差距摘要

人工审查意见(0820)第 0-7、10 条及总述要求:后端任务解耦为独立模块,各模块路径清晰,后端实现独立功能和接口,前端引用后端函数,不得破坏后端独立性,为外部 API 引用做准备。

当前代码与要求的差距(详见调研结论 03 号文档依赖的差距调研):

| # | 差距 | 本方案落点 |
|---|---|---|
| 0 | 单位三层不一致(kW 系业务单位 / W·J 引擎单位 / 硬编码 ×1000);units.py 换算函数全库死代码;前端无 unit 字段;量纲检查静默失效 | 第 3 节 单位标准化(全局基础) |
| 1/10 | 无设备初始化/装配检查独立模块;引擎单文件巨模块(eval_run.py 40KB);API 层含直接 ORM 逻辑;services 循环依赖规避 | 第 4、6、9 节 |
| 2 | 无 yaml 设备定义、无价格初始化 yaml、无模型类型标志(机理/数据、有/无状态) | 第 4 节 |
| 3 | 建模模块无标准化函数名/输入/输出后台调用命令,引擎调用是硬编码字符串 | 第 5 节 |
| 4 | 无装配文件(边-端模型)、无装配检查任务 | 第 6 节 |
| 5 | 算法选择被忽略、tolerances 死配置、planning 无逐时输出、seed 不一致 | 第 9.3、9.4 节 |
| 6 | 财务基于年度聚合而非逐时;缺 LCOE/回收期;evidence 缺 financial 块致四维复查财务恒 unknown | 第 7 节 |
| 7 | 无计算分析 wrapper/批量/敏感性分析任务 | 第 8 节 |
| 总述 | 前端越权承载业务逻辑(命令差量、乐观锁、assumptions 键集、评估触发) | 第 10 节(边界规则) |

---

## 2. 目标架构与目录结构

```
backend/iesplan/
├── core/            # 保留(0 层,通用基础设施)
│   ├── units.py     # 扩展:复合业务单位注册 + parse_number_with_unit/to_si/from_si(第 3 节)
│   ├── registry.py  # 只保留 ParameterSpec/DeviceTypeSpec/AlgorithmSpec 数据类与校验函数,注册目录迁往 devices/(第 4.4 节)
│   ├── timeaxis.py / errors.py / idgen.py / jsonutil.py / security.py / diagnostics.py / expression.py  # 不动
├── devices/         # 【新】1 层:设备初始化模块(第 4 节)
│   ├── __init__.py          # 门面:get_device_spec / list_device_specs / get_price_default / attach_profile
│   ├── spec.py              # DeviceSpec(扩展 DeviceTypeSpec:model_method/stateful/model_function/model_file/data_file)
│   ├── loader.py            # 插件式 yaml 装载:load_device_specs / register_device_spec;catalog 目录扫描
│   ├── catalog/             # 每设备一个 yaml + 可选标准 csv(如 pv.yaml + pv_profile.csv)
│   │   ├── prices.yaml      # 统一价格初始化 yaml(第 4.3 节)
│   │   ├── pv.yaml / battery.yaml / heat_pump.yaml / boiler.yaml / chiller.yaml / wind.yaml / grid_connection.yaml / ...
│   ├── pricing.py           # PriceDefaults / load_price_defaults / get_price_default
│   └── profile.py           # attach_profile / load_profile(设备附带标准 csv 承载时间序列)
├── modeling/        # 【新】2 层:建模模块(第 5 节)
│   ├── __init__.py          # 门面:register_command / get_command / resolve_function_ref / build_command
│   ├── command.py           # ModuleCommand dataclass + 全局命令注册表(后台调用命令)
│   ├── functions.py         # 机理函数(自 engines/devices.py 迁入,SI 单位)
│   ├── datadriven.py        # 数据方法:periodic_repeat(周期重复)/ prediction_model(预测模型)
│   └── build.py             # build_command(spec, profile) → ModuleCommand
├── assembly/        # 【新】3 层:装配与检查模块(第 6 节)
│   ├── __init__.py          # 门面:build_assembly / validate_assembly / assembly_to_plan / plan_from_content
│   ├── spec.py              # AssemblyPort/AssemblyEdge/AssemblyFile(边-端模型,文本序列化)
│   ├── validate.py          # check_connection_legality / check_model_solvability / check_global_solvability
│   ├── build.py             # build_assembly(项目版本内容 → 装配文件)/ 装配文本进快照
│   └── plan.py              # plan_from_assembly / plan_from_content(→ 计算模块 SI 输入,替代 executors._build_plan)
├── finance/         # 【新】2 层:财务计算模块(第 7 节)
│   ├── __init__.py          # 门面:compute_financials / build_project_cashflows / cashflow_irr / project_npv / equity_irr
│   ├── metrics.py           # 自 metrics/financial.py 迁入(NPV/IRR/现金流/IRRStatus)
│   ├── hourly.py            # 逐时运行 → 财务数据:compute_financials / compute_lcoe / compute_payback / FinancialResult
│   └── params.py            # FinanceParams / finance_params_from_config
├── analysis/        # 【新】4 层:计算分析模块(第 8 节)
│   ├── __init__.py          # 门面:run_sweep / run_sensitivity_analysis / summarize_sweep
│   ├── wrapper.py           # SweepSpec/SweepResult / run_sweep(纯计算,无 DB)
│   ├── sensitivity.py       # run_sensitivity_analysis(任务编排)/ summarize_sweep
│   ├── indicators.py        # 自 metrics/engineering.py、environmental.py 迁入(能效/排放指标)
│   └── assessment.py        # 自 metrics/validity.py 迁入(四维评估),新增 _check_financial 读 financial 块
├── engines/         # 保留(3 层,计算引擎)
│   ├── eval_run.py          # 重构:消费 SI 单位,移除硬编码 ×1000;KPI 财务段移除(移 finance)
│   ├── planning.py          # 重构:财务计算改调 finance.metrics;seed 从 options 读取
│   ├── solver.py / balance.py / __init__.py   # 基本不动
├── metrics/         # 迁移完成后退役(保留 __init__.py 转发兼容一个版本周期)
├── services/        # 保留(5 层,编排与持久化)
│   ├── tasks.py            # assemble_snapshot 接入装配文本;新增 create assembly_check/analysis 任务
│   ├── config.py           # 单位解析接入 units;算法/容差来源统一;默认值改取 devices.pricing
│   ├── model.py            # validate_topology 委托 assembly.validate;Device 表新字段
│   ├── results.py          # 依赖改指向 finance.metrics / analysis.assessment / devices(取代 engines.planning.CAPACITY_PARAM)
│   └── ...(其余不动)
├── worker/          # 保留(5 层,执行调度)
│   ├── executors.py        # 命令分发(_run_engine 走 modeling.get_command);算法选择;新增 execute_assembly_check/execute_analysis;_build_plan 迁 assembly.plan
│   ├── runner.py           # dispatch 新增分支;load_inputs 读装配文本 + 数据集单位换算
│   └── lease.py / main.py / solver_process.py  # 基本不动
├── models/          # 保留(0 层)
│   ├── calc.py             # ck_tasks_type 加 'assembly_check','analysis';CalcSnapshot 加 assembly_text 列
│   └── model.py            # Device 表加 model_method / stateful 列
├── api/             # 保留(6 层,REST)
│   ├── __init__.py         # 文档更新(删除"业务路由在后续阶段实现"过期描述)
│   ├── devices.py / modeling.py / assembly.py / finance.py / analysis.py  # 第 10.3 节端点(后续里程碑实施)
│   └── admin.py / objects.py  # 业务逻辑下沉 services(第 9.6 节)
└── main.py          # 挂载新路由(按里程碑追加)
```

分层规则(依赖单向,禁止反向):
- **0 层**:`core`、`models`(任何层可依赖,0 层不得依赖业务层)
- **1 层**:`devices`(只依赖 core/models)
- **2 层**:`modeling`、`finance`(只依赖 core/models/devices;finance 不得依赖 engines)
- **3 层**:`assembly`、`engines`(依赖 ≤2 层;assembly 依赖 devices+modeling;engines 依赖 modeling+finance+core)
- **4 层**:`analysis`(依赖 ≤3 层)
- **5 层**:`services`、`worker`(依赖 ≤4 层)
- **6 层**:`api`(依赖 ≤5 层)

---

## 3. 单位标准化设计(审查意见第 0 条,全局基础,先行实施)

### 3.1 目标

- 三层单位约定收敛为两层:**业务层单位**(注册表/yaml/前端/存储,如 kW、kWh、kWp、CNY/kWh、CNY/kW·月、tCO2/MWh、°C)+ **引擎内部标准单位**(SI:功率 W、能量 J、温度 K、金额 CNY、比例 0-1、时间 a)。
- 唯一换算入口:`core/units.py`(统一换算层)。引擎边界(装配→计算、数据集装载、config 校验)全部经此换算,禁止任何硬编码 ×1000 或 ×3600。
- 前端非标准单位数值统一由后端解析(见 3.4),解析入口唯一。

### 3.2 core/units.py 扩展

```python
# ===== 新增类别(现有 6 类:energy/power/temperature/currency/duration/angle)=====
# 新增复合业务单位,统一进 UNITS 注册表(UnitSpec 增加可选字段):
#   dimension: str = ""        # 业务量纲串,如 "power"、"energy_cost"、"emission_factor"(缺省取 category)
#   convertible: bool = True   # 是否参与换算;False 表示仅作元数据(如 kWp→kW 换算为 True,
#                              # 但 tCO2/万m³ 与 tCO2/MWh 之间不可自动换算,标记 False)
UnitSpec 新增字段: dimension: str = "", convertible: bool = True
```

注册表新增项(单位 id / symbol / category / to_si / convertible):

| id | symbol | category | to_si(基准) | convertible | 说明 |
|---|---|---|---|---|---|
| ies.unit.kwp | kWp | power | 1000 → W | True | 容量功率,与 kW 同类别可换算 |
| ies.unit.kwh_energy | kWh(已有) | energy | 3.6e6 → J | True | 已有 |
| ies.unit.cny_per_kwh | CNY/kWh | energy_cost | — | False | 电价/度电成本量纲 |
| ies.unit.cny_per_kw_month | CNY/kW·月 | demand_charge | — | False | 需量电费 |
| ies.unit.cny_per_m3 | CNY/m³ | gas_cost | — | False | 气价 |
| ies.unit.tco2_per_mwh | tCO2/MWh | emission_factor | — | False | 电网排放因子 |
| ies.unit.tco2_per_10k_m3 | tCO2/万m³ | emission_factor | — | False | 燃气排放因子 |
| ies.unit.percent | % | ratio | 0.01 → 1 | True | 百分比(前端直接传 "5%" 或 5 + unit="%") |
| ies.unit.year | a(已有) | duration | 31536000 → s | True | 已有 |

```python
# ===== 新增函数(units.py 现有 convert/energy_to_joules/power_to_watts/temperature_kelvin 保留并改为新注册表实现)=====
def parse_number_with_unit(text: str | int | float) -> tuple[float, str]:
    """解析前端非标准数值字符串 → (数值, 标准单位)。
    "12.5 kW"→(12.5,'kW'); "3500元/kWp"→(3500,'kWp'); "5%"→(5,'%'); 纯数字→(v,'-' 按调用方约定)。"""

def to_si(value: float, unit: str) -> float:
    """业务单位 → 引擎标准单位(W/J/K/CNY/0-1/a);convertible=False 的单位原样返回数值(不报错)。"""

def from_si(value: float, si_unit: str) -> float:
    """引擎标准单位 → 业务单位(展示/落库用)。"""

def is_known_unit(unit: str) -> bool:
    """unit 是否已注册(别名归一化后查表)。"""

def canonical_unit(unit: str) -> str:
    """别名归一化:"kw"/"KW"/"千瓦" → 'kW';未注册原样返回。"""

def unit_dimension(unit: str) -> str:
    """返回 dimension;未注册返回 ''。"""
```

### 3.3 参数规格携带单位(字段标准化)

- `ParameterSpec.unit` 语义收紧:必须是 `is_known_unit` 或 `'-'`(无量纲)或 `''`(视为 `'-'`)。`devices/spec.py` 装载 yaml 时校验,违规给诊断。
- `ConfigVariable`(前端 types.ts)与后端 `_validate_variables` 强制带 `unit` 字段:

```python
# services/config.py _validate_variables 中新增(现有 :559-707 基础上):
for v in variables:
    unit = v.get("unit") or ""
    if unit and not is_known_unit(unit):
        diags.append(Diagnostic("CONF-VAR-UNIT", "error", "ies.diag.config.unknown_unit", ...))
    # 数值解析:非 number 类型但可解析(如 "12.5 kW")→ parse_number_with_unit 后写回 v["initial"]
    v["initial"] = normalize_numeric(v.get("initial"), unit)   # parse_number_with_unit 或直接 float
```

- `_dims_for_unit`(config.py:393-398)改实现:用 `unit_dimension(unit)` 映射量纲;业务复合单位(tCO2/MWh 等)计入对应量纲(排放因子),未注册单位给 warning 诊断而非静默无量纲。
- 百分比统一:后端负责 "%" → 小数(`to_si(v, '%')`)。前端 ConfigPage.tsx:184-187、652 的 ÷100 逻辑删除,改随 unit 字段原样提交,由后端解析(前端与后端解析入口唯一)。

### 3.4 前端数值解析边界

- 前端 `ConfigVariable` 增加 `unit: string`(types.ts:450-459);`buildInput`(ConfigPage.tsx:605-611)回传 `{name, type, initial, min, max, unit}`;`configToServer`(client.ts:620-635)原样透传 unit。
- 前端不做任何单位换算(百分比手工 ÷100 也删除);非标准单位字符串(如 "12.5 kW")前端允许输入,后端 `parse_number_with_unit` 解析并回写规范值。
- 兼容:老保存数据无 unit 字段 → 后端以注册表参数 unit 补全(不报错,仅诊断提示)。

### 3.5 引擎边界换算落点(消除硬编码 ×1000)

| 现有硬编码位置 | 改造后 |
|---|---|
| eval_run.py:435 `max_import_power_kw*1000.0`、:476、:500、:516、:562、:570 等全部 `×1000`/kWh→J | **删除**。装配层 `assembly/plan.py` 已把 plan 参数统一转 SI(W/J/K),evaluate_plan 直接消费 SI;`_param()` 读取即 SI 数值 |
| eval_run.py 内部温度 °C、电池 kWh | 同理:入口统一 K/J;内部不再出现业务单位 |
| runner.py:121-124、176-177 数据集 kWh/步→W | 改调 `units.to_si`(数据集声明单位从 dataset 元数据读取,见 services/dataset.py:866 校验点扩展:校验同时把单位归一化并记录) |
| executors.py:267 结果 meta 硬编码 `"W(W) / kWh(energy) / 0-1(ratio)"` | 改为由 units 注册表生成:`meta["units"] = {"power": "W", "energy": "kWh", "ratio": "0-1", "money": "CNY"}`(展示单位固定,kpi 数值由 `from_si` 转业务单位落库) |
| eval_run.py 内 `temperature` 数组 | runner 装载时 `temperature_kelvin` 转 K;PV/热泵函数按 K 计算 |

实施顺序:先建 `assembly/plan.py` 换算层与 units 扩展,再逐个引擎点替换,每步跑 `backend/tests/test_integration.py` + `contract_smoke.py` 数值回归。

---

## 4. devices/ 设备初始化模块(意见第 2 条)

### 4.1 职责

- 统一的设备接口抽象(插件式):每个设备一个 yaml + 附带统一标准格式 csv(承载时间序列)。
- 设备规格含**模型类型标志**(建模方法:机理/数据-周期重复/数据-预测;有/无状态),供建模模块处理。
- 常用设备成本、能源价格、税收默认值统一从 `catalog/prices.yaml` 取得(单一事实源)。

### 4.2 公共接口(函数签名)

```python
# devices/spec.py
@dataclass(frozen=True, slots=True)
class DeviceSpec:
    """设备规格(由 core.registry.DeviceTypeSpec 扩展而来;registry 数据类保留,本类在其上加模型标志)。"""
    type_id: str                 # 'ies.device.pv'
    version: str                 # '1.3.0'
    name_zh: str
    name_en: str
    energy_carriers: list[str]
    is_load: bool
    capabilities: list[str]
    extends: str = "ies.device.base"
    parameters: dict[str, ParameterSpec] = field(default_factory=dict)
    help_topic: str = ""
    # ---- 新增(意见第 2 条)----
    model_method: str = "mechanism"      # 'mechanism' | 'data_repeat' | 'data_predict'
    stateful: bool = False               # 有/无状态模型标志
    model_function: str = ""             # 机理:命令函数引用(如 'iesplan.modeling.functions.pv_output')
    model_file: str | None = None        # data_predict:模型文件路径(相对 catalog/ 或对象存储引用)
    data_file: str | None = None         # 标准 csv 数据文件(设备附带时间序列,data_repeat 必填)
    price_refs: dict[str, str] = field(default_factory=dict)   # 参数名 → prices.yaml 键(如 {'gas_price': 'energy.gas_price'})
    profile_ref: str | None = None       # attach_profile 返回的对象存储引用

# devices/loader.py
def load_device_specs(catalog_dir: str | Path | None = None) -> list[DeviceSpec]:
    """扫描 catalog/*.yaml 装载全部设备(启动时调用一次;插件式:新增设备 = 新增 yaml 文件)。"""
def register_device_spec(spec: DeviceSpec) -> None:
    """运行期注册/覆盖设备(插件扩展点;业务代码一般不用,测试与热插拔用)。"""
def get_device_spec(type_id: str) -> DeviceSpec | None:
    """取设备规格;兼容旧 id(如 'pv' → 'ies.device.pv')。"""
def list_device_specs() -> list[DeviceSpec]:
def get_parameter_spec(type_id: str, param_name: str) -> ParameterSpec | None:
def default_params(type_id: str, kind: str) -> dict[str, float]:
    """kind='new'|'existing':按 ParameterSpec.default/existing_default 生成参数默认值。"""

# devices/pricing.py
@dataclass(frozen=True, slots=True)
class PriceDefaults:
    currency: str = "CNY"
    energy: dict[str, object]        # {'grid_import_tariff': {'peak':1.1,...}, 'grid_export_tariff':0.35, 'gas_price':3.2}
    equipment_cost: dict[str, float] # {'ies.device.pv.unit_invest_cost': 3500.0, ...} 单位 CNY/容量单位
    tax: dict[str, float]            # {'income_tax_rate': 0.25, 'vat_rate': 0.13}
    finance: dict[str, object]       # {'discount_rate':0.08,'depreciation_years':10,'project_years':20,'irr_floor':0.08}

def load_price_defaults(path: str | Path | None = None) -> PriceDefaults:
    """装载 catalog/prices.yaml(单一事实源)。"""
def get_price_default(ref: str, default: float | None = None) -> float | None:
    """按 'energy.gas_price' 点路径取值;缺失返回 default。"""

# devices/profile.py
def attach_profile(spec_id: str, csv_bytes: bytes, resolution: str) -> str:
    """设备附带标准 csv(统一模板列:timestamp + 各能量载能体列)存对象存储,返回引用。"""
def load_profile(profile_ref: str) -> dict[str, np.ndarray]:
    """按引用读取 profile 逐时数据(复用 services/dataset.parse_csv,返回字段名→数组,单位按 dataset 声明换算 SI)。"""
```

### 4.3 yaml 文件格式定案

```yaml
# devices/catalog/pv.yaml —— 每设备一个 yaml
type_id: ies.device.pv
version: "1.3.0"
name_zh: 光伏
name_en: Photovoltaic
energy_carriers: [electric]
is_load: false
capabilities: [generation, intermittent]
extends: ies.device.base
model:
  method: mechanism            # mechanism | data_repeat | data_predict
  stateful: false
  function: iesplan.modeling.functions.pv_output   # mechanism: 命令函数引用
  model_file: null             # data_predict 时必填
  data_file: pv_profile.csv    # 标准 csv(与本 yaml 同目录或对象存储引用)
parameters:
  rated_capacity_kwp:
    unit: kWp
    min: 0
    max: 100000
    default: 500
    is_optimizable: true
    stock_or_addition: addition
  unit_invest_cost:
    unit: CNY/kWp
    min: 0
    default: null              # null → 取 prices.yaml: equipment_cost.ies.device.pv.unit_invest_cost
  ...
pricing_refs:
  unit_invest_cost: equipment_cost.ies.device.pv.unit_invest_cost
```

```yaml
# devices/catalog/prices.yaml —— 统一价格初始化(意见第 2 条)
currency: CNY
energy:
  grid_import_tariff: { peak: 1.1, flat: 0.7, valley: 0.3 }   # CNY/kWh
  grid_export_tariff: 0.35                                    # CNY/kWh
  gas_price: 3.2                                              # CNY/m³
equipment_cost:          # 键 = <type_id>.<参数名>, 单位 CNY/注册表容量单位
  ies.device.pv.unit_invest_cost: 3500            # CNY/kWp
  ies.device.battery.unit_invest_cost: 900        # CNY/kWh
  ies.device.heat_pump.unit_invest_cost: 1800     # CNY/kW
  ies.device.boiler.unit_invest_cost: 600         # CNY/kW
  ies.device.chiller.unit_invest_cost: 1200       # CNY/kW
  ies.device.wind.unit_invest_cost: 4500          # CNY/kWp
tax:
  income_tax_rate: 0.25
  vat_rate: 0.13
finance:
  discount_rate: 0.08
  depreciation_years: 10
  project_years: 20
  irr_floor: 0.08
algorithm:
  ies.algo.milp_hybrid: { gap_rel: 0.001, time_limit_s: 600 }
  ies.algo.lp_relax:     { gap_rel: 0.001, time_limit_s: 600 }
```

### 4.4 注册表迁移路径(core/registry.py → devices/)

| 现有代码 | 迁移动作 |
|---|---|
| `core/registry.py:1-8` docstring「不做动态导入、不做运行时增删」 | 更新为「目录扫描 yaml 装载 + 运行期可扩展」 |
| `core/registry.py:186-582` 9 类设备 `_register_device(...)` 硬编码 | 迁为 `devices/catalog/*.yaml`(9 个 yaml 文件);`loader.load_device_specs()` 启动装载(worker 进程与 API 进程各自装载一次) |
| `core/registry.py:92-104` DeviceTypeSpec | 保留数据类(services/models 广泛引用);devices.spec.DeviceSpec 继承/扩展它,装载结果为 DeviceSpec |
| `core/registry.py:224-282,337-346,480-533,570-580` 各设备默认价 | 迁入 `catalog/prices.yaml`;`pricing_refs` 声明参数默认值来源 |
| `core/registry.py:597` discount_rate=0.08 | 迁入 prices.yaml `finance.discount_rate` |
| `metrics/financial.py:324,351,445` tax_rate 默认 Decimal('0.25') | 迁入 prices.yaml `tax.income_tax_rate`;finance.params 从价格模块读取 |
| `eval_run.py:464-466` gas_price 3.2 回退 | 迁入 prices.yaml `energy.gas_price`;引擎回退值改为经 `assembly/plan.py` 注入 |
| `planning.py:301-315 _compute_capex` 注册表默认回退 | 改从 `devices.get_price_default` 取价;缺价 → 诊断 error 而非静默 0 |
| `services/model.py:71-96` `_DEVICE_PORT_DIRECTIONS`/`_DEVICE_COARSE_CATEGORY` 硬编码映射 | 迁入 devices/spec.py 的规格字段(energy_carriers/capabilities 派生);services/model.py 删表改调用 |
| `services/config.py` 参数默认值生成(`_default_parameters`/`_default_variables`) | 改用 `devices.default_params` + 注册表参数 unit |

---

## 5. modeling/ 建模模块(意见第 3 条)

### 5.1 职责

- 输入:设备初始化模块生成的模型文件(yaml)+ 标准数据文件(csv)。
- 输出:**标准化函数名、输入、输出的后台调用命令**(ModuleCommand),命令位于软件 env path(可解析 import 路径),计算模块按命令分发。
- 按设备 `model_method` 分类实现:机理函数直接映射内置函数;数据方法封装周期重复/预测模型。

### 5.2 公共接口(函数签名)

```python
# modeling/command.py
@dataclass(frozen=True, slots=True)
class ModuleCommand:
    """标准化后台调用命令(建模模块成果,计算模块唯一调用途径)。"""
    command_id: str                    # 'ies.command.model.pv_output.v1'
    function_ref: str                  # env path 可解析:'iesplan.modeling.functions.pv_output'
    version: str
    stateful: bool = False
    inputs: tuple[ParameterSpec, ...] = ()     # 输入字段规格(字段名+单位+min/max)
    outputs: tuple[ParameterSpec, ...] = ()    # 输出字段规格(字段名+单位)
    data_file: str | None = None               # 数据方法:绑定的标准 csv 引用

_COMMANDS: dict[str, ModuleCommand] = {}

def register_command(cmd: ModuleCommand) -> None:
    """注册命令(建模成果入库;启动时装载 catalog 后自动注册)。"""
def get_command(command_id: str) -> ModuleCommand | None:
def list_commands() -> list[ModuleCommand]:
def resolve_function_ref(function_ref: str) -> Callable:
    """沿 env path 解析函数对象(importlib,等价现有 executors._run_engine 内联解析)。"""
def make_command_id(device_type: str, model_method: str, version: str) -> str:
    """'ies.command.model.{device_type}.{method}.{version}' 约定。"""

# modeling/functions.py —— 自 engines/devices.py 迁入,全部 SI 单位(W/J/K),签名不变只改单位约定
def pv_output(ghi_w_m2: np.ndarray, temperature_k: np.ndarray,
              rated_capacity_w: float, efficiency: float = 0.18) -> np.ndarray: ...
def heat_pump_cop(temperature_k: np.ndarray, mode: str) -> np.ndarray: ...
def boiler_output(heat_demand_w: np.ndarray, efficiency: float = 0.9) -> np.ndarray: ...
def chiller_output(elec_power_w: np.ndarray, cop: float = 4.0) -> np.ndarray: ...
def gas_volume_m3(energy_j: np.ndarray, efficiency: float = 0.9) -> np.ndarray: ...
def simulate_battery(charge_w: np.ndarray, discharge_w: np.ndarray,
                     capacity_j: float, soc_initial: float,
                     charge_efficiency: float = 0.95, discharge_efficiency: float = 0.95,
                     state: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """有状态模型示例:返回 (soc, 末态),输入含 t-1 状态(意见第 2 条状态模型标志的体现)。"""

# modeling/datadriven.py —— 数据方法
def periodic_repeat(profile: dict[str, np.ndarray], n_steps: int) -> dict[str, np.ndarray]:
    """data_repeat:历史曲线周期重复扩展到 n_steps(输入 = 标准 csv 数据)。"""
def prediction_model(model_file: str, features: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """data_predict:加载模型文件(如 joblib/pkl)预测输出(阶段 B 实现,接口先定)。"""

# modeling/build.py
def build_command(spec: DeviceSpec, profile: dict[str, np.ndarray] | None = None) -> ModuleCommand:
    """按 spec.model_method 生成命令:
    - mechanism    → function_ref = spec.model_function, 直接注册
    - data_repeat  → 包装 periodic_repeat(profile)
    - data_predict → 包装 prediction_model(model_file)
    返回 ModuleCommand 并 register_command。"""
```

### 5.3 现有代码迁移路径(engines/devices.py → modeling/functions.py)

| 现有代码 | 迁移动作 |
|---|---|
| `engines/devices.py:35-279` pv_output/heat_pump_cop/boiler_output/chiller_output/gas_volume_m3/simulate_battery | 整体迁入 `modeling/functions.py`,单位改 SI(W/J/K),函数签名显式带单位后缀参数名 |
| `engines/eval_run.py` 的 `from iesplan.engines.devices import ...` | 改 `from iesplan.modeling.functions import ...`;eval_run 内调用改为 SI 输入 |
| `worker/executors.py:126-161 _run_engine` 字符串函数解析 | 改为 `fn = modeling.resolve_function_ref(get_command(command_id).function_ref)`(命令化,意见第 5 条「模块调用命令」) |
| `services/config.py:110-114 ALGO_DB_CLASS` | 计算引擎命令(`ies.command.compute.evaluate_plan.v1` / `ies.command.compute.run_planning.v1`)也在 modeling/command.py 注册;ALGO_DB_CLASS 映射迁到 `engines/selector.py`(见 9.3) |
| `eval_run.py` 中直接内联的 PV/热泵公式(若与 devices.py 重复) | 统一收口到 functions.py,引擎只调命令 |

---

## 6. assembly/ 装配与检查模块(意见第 4 条)

### 6.1 职责

- 用文本装配文件(边-端模型)描述模型组合与约束:边连接一设备输出与下一设备输入,被边相连的两个参数在同时间严格相等;有状态/长时滞输入输出之间增加「管道设备」体现非同时性。
- 检查模块对装配文件审查:(a) 连接合法性(输入对输出、参数性质/单位一致);(b) 模型可解性(各设备输入是否完全);(c) 整体可解性(约束不足/过度),给出错误反馈。
- 装配文件是计算模块的输入之一(进入 CalcSnapshot)。

### 6.2 公共接口(函数签名)

```python
# assembly/spec.py
@dataclass(frozen=True, slots=True)
class AssemblyPort:
    device_id: str
    carrier: str        # 'electric'|'heat'|'cool'|'gas'|'solar'
    direction: str      # 'in' | 'out'
    stateful: bool = False
    unit: str = ""      # 端口量纲(如 'W'/'J'),连接双方须同量纲(参数性质一致检查)

@dataclass(frozen=True, slots=True)
class AssemblyEdge:
    edge_id: str
    source: AssemblyPort
    target: AssemblyPort
    time_delay_steps: int = 0     # >0: 有状态/管道设备滞后(意见第 4 条管道设备)

@dataclass(frozen=True, slots=True)
class DeviceInstance:
    instance_id: str          # 图内设备名(对应 models.Device.name)
    type_id: str
    params: dict[str, float]  # 数值(业务单位,装配序列化原样;计算前经 plan.py 转 SI)
    is_new: bool
    stateful: bool = False
    command_id: str = ""      # 建模模块命令(由 type_id+method 生成)

@dataclass(frozen=True, slots=True)
class AssemblyFile:
    assembly_id: str
    version: str
    devices: tuple[DeviceInstance, ...]
    edges: tuple[AssemblyEdge, ...]
    options: dict      # reverse_feed_allowed/lambda_h/lambda_c/c_ph/c_pc/shedding 等

    def to_text(self) -> str:
        """装配文件文本(JSON 定案;行内注释允许):确定性序列化(canonical_json)。"""
    @classmethod
    def from_text(cls, text: str) -> AssemblyFile:
        """反序列化,失败抛 AssemblyParseError(含行号定位)。"""

# assembly/validate.py
def check_connection_legality(af: AssemblyFile, specs: Mapping[str, DeviceSpec]) -> list[Diagnostic]:
    """(a) 边端点存在/方向为 out→in/载能体一致/量纲(unit)一致/无重复边/无自环。"""
def check_model_solvability(af: AssemblyFile) -> list[Diagnostic]:
    """(b) 每个设备每个 in 端口有且至少一条边覆盖(输入完全);无悬空 out 端口。"""
def check_global_solvability(af: AssemblyFile, n_steps: int) -> list[Diagnostic]:
    """(c) 图结构检查:强连通环内流可解性、自由流(无源/无汇连通分量)、
    时间滞后边与周期边界相容(time_delay_steps < n_steps)。"""
def validate_assembly(af: AssemblyFile, specs: Mapping[str, DeviceSpec],
                      n_steps: int = 8760) -> list[Diagnostic]:
    """三查合一,按序执行,error 级诊断含可读文案(错误反馈)。"""

# assembly/build.py
def build_assembly(content: dict, data: dict, axis: TimeAxis) -> AssemblyFile:
    """项目版本内容(model.devices/ports/connections 边-端图 + calc_config) → AssemblyFile。
    当前阶段适配器:ports/connections 表语义 → AssemblyPort/AssemblyEdge;计算选项 → options。"""
def assembly_text(content: dict, data: dict, axis: TimeAxis) -> str:
    """build_assembly(...).to_text():供快照哈希与落库(确定性,同输入同文本)。"""
def build_assembly_from_project(db: Session, project_id: int, *, freeze: bool = True) -> tuple[str, AssemblyFile]:
    """服务层便捷入口(供 tasks.assemble_snapshot 与 assembly_check 任务调用,复用 _resolve_project_inputs)。"""

# assembly/plan.py —— 装配 → 计算模块输入(SI 边界换算唯一入口)
def plan_from_assembly(af: AssemblyFile, data: dict, axis: TimeAxis) -> dict:
    """AssemblyFile → evaluate_plan/run_planning 输入 plan dict;设备参数经 units.to_si 转 SI;
    逐时 data 由 runner 已转 SI(数据集声明单位换算),此处只做量纲校验。"""
def plan_from_content(content: dict, data: dict, axis: TimeAxis) -> dict:
    """兼容入口:content → build_assembly → plan_from_assembly(替代 executors._build_plan)。"""
```

### 6.3 装配文件格式(JSON 定案)

```json
{
  "assembly_id": "p12-asmb-3f9a",
  "version": "1",
  "devices": [
    {"instance_id": "pv1", "type_id": "ies.device.pv", "is_new": true,
     "params": {"rated_capacity_kwp": 500, "unit_invest_cost": 3500}, "command_id": "ies.command.model.ies.device.pv.mechanism.1.3.0"},
    {"instance_id": "load_e", "type_id": "ies.device.load", "is_new": false, "params": {}}
  ],
  "edges": [
    {"edge_id": "e1", "source": {"device_id": "pv1", "carrier": "electric", "direction": "out"},
     "target": {"device_id": "load_e", "carrier": "electric", "direction": "in"}},
    {"edge_id": "e2", "source": {"device_id": "hp_pipe", "carrier": "heat", "direction": "out"},
     "target": {"device_id": "h_load", "carrier": "heat", "direction": "in"}, "time_delay_steps": 4}
  ],
  "options": {"reverse_feed_allowed": false, "lambda_h": 0.05, "lambda_c": 0.08,
              "c_ph": 0.02, "c_pc": 0.02, "shedding": false}
}
```

### 6.4 装配检查任务(task_type = 'assembly_check',新增)

- `models/calc.py:140` `ck_tasks_type` CHECK 增加 `'assembly_check'`(数据库迁移见 9.7)。
- 任务输入:快照(复用 `assemble_snapshot`,装配文本随快照)。
- 执行器:`worker/executors.execute_assembly_check(ctx)`(见 9.2),产物 payload:`{"assembly_file": {...}, "checks": {"connection": [...], "solvability": [...], "global": [...]}, "passed": bool}`。
- 快照哈希:装配文本参与 `content_hash`(见 9.1),保证「同输入同哈希」。

### 6.5 现有代码迁移路径

| 现有代码 | 迁移动作 |
|---|---|
| `services/model.py:1073-1171 validate_topology`(连接合法性/端口匹配) | 迁入 `assembly/validate.py`(check_connection_legality 主干);services/model.py 保留薄包装供 API 同步校验 |
| `services/tasks.py:207-241 _resolve_project_inputs` + :267-327 assemble_snapshot | 快照装配新增:生成装配文本并入哈希(9.1) |
| `worker/executors.py:794-816 _build_plan` | 迁入 `assembly/plan.py`(plan_from_content);executors 改调用 |
| `services/results.py` / `validation.py` 中对图结构的检查 | 统一委托 assembly.validate(校验来源单一) |

---

## 7. finance/ 财务计算模块(意见第 6 条)

### 7.1 职责

- 在计算模块逐时运行结果基础上计算财务数据(现金流/NPV/IRR/LCOE/回收期)。
- 输入 = 逐时流 + KPI + 投资额 + 财务参数;输出 = FinancialResult(evidence `financial` 块)。

### 7.2 公共接口(函数签名)

```python
# finance/metrics.py —— 自 metrics/financial.py 整体迁入(函数签名、Decimal 语义不变)
class IRRStatus(StrEnum): ...                      # 迁入(validity.py 的引用改指向本处)
def npv(rate: Decimal | float, cashflows: Sequence[Decimal | float]) -> Decimal: ...
def cashflow_irr(cashflows, *, scale: float = 1.0, eps: float = 1e-9) -> tuple[IRRStatus, float | None, str]: ...
def build_project_cashflows(capex, annual_om, annual_energy_saving, revenue,
                            tax_rate, depreciation_years, project_years=20,
                            discount_rate=Decimal("0.08")) -> list[Decimal]: ...
def project_npv(discount_rate, *, investment, annual_om, annual_energy_saving,
                tax_rate, depreciation_years, project_years) -> Decimal: ...
def build_equity_cashflows(...) -> list[Decimal]: ...
def equity_irr(...) -> tuple[IRRStatus, float | None, str]: ...

# finance/hourly.py —— 新增:逐时 → 财务
@dataclass(frozen=True, slots=True)
class FinancialResult:
    irr: float | None
    irr_status: IRRStatus
    npv: Decimal
    payback_years: float | None          # 回收期(新增实现)
    lcoe: Decimal | None                 # 平准化度电成本(新增实现)
    capex: Decimal
    baseline_cost: Decimal
    annual_op_cost: Decimal
    annual_revenue: Decimal
    cashflows: list[Decimal]             # 税后项目现金流(证据 financial.cashflows)
    detail: dict = field(default_factory=dict)   # 折旧/税/年费分解

def compute_financials(
    kpi: dict,                          # evaluate_plan 的 kpi(年度聚合:total_op_cost/buy_cost/sell_revenue/gas_cost)
    flows: dict[str, np.ndarray],       # 逐时流(用于逐时费用列 → 财务口径,见 7.3)
    capex: Decimal,
    baseline_cost: Decimal,
    params: FinanceParams,
) -> FinancialResult:
    """逐时运行 → 财务数据。现金流口径: 年运营费 = 逐时费用列求和(而非仅 kpi),
    节能收益 = baseline_cost - annual_op_cost;20 年现金流 + LCOE + 回收期。"""

def compute_lcoe(total_lifecycle_cost: Decimal, total_energy_kwh: Decimal) -> Decimal | None:
    """LCOE = Σ(年成本贴现) / Σ(年发电量贴现);total_energy_kwh ≤ 0 → None。"""
def compute_payback(cashflows: list[Decimal]) -> float | None:
    """静态回收期:累计现金流首次转正的年数(小数);未转正 → None。"""

# finance/params.py
@dataclass(frozen=True, slots=True)
class FinanceParams:
    discount_rate: Decimal = Decimal("0.08")
    tax_rate: Decimal = Decimal("0.25")
    depreciation_years: int = 10
    project_years: int = 20
    currency: str = "CNY"
    irr_floor: Decimal = Decimal("0.08")

def finance_params_from_config(calc_config: dict) -> FinanceParams:
    """calc_config.params(economic_*)/calc_config.irr_floor + prices.yaml finance 节 → FinanceParams。"""
```

### 7.3 逐时费用列 → 财务口径(修复「财务基于年度聚合」)

- `eval_run.py:826-841` 逐时费用列(cost_buy/cost_gas/revenue_sell,元/步)目前在 kpi 里只汇成年度值。**不改引擎**:`finance/hourly.py` 直接接收 flows 中的逐时费用列(单位 CNY/步,SI 已归一),`annual_op_cost = Σ(cost_buy+cost_gas) - Σ(revenue_sell)`(或按配置口径),与 kpi 交叉校验(偏差 >1% 给诊断)。

### 7.4 evidence `financial` 块(修复四维复查财务恒 unknown)

- `worker/executors._eval_payload` / `_planning_payload` / uncertainty 样本载荷新增键:

```python
payload["financial"] = {
    "irr": ..., "irr_status": ..., "npv": ..., "investment": ...,
    "baseline_cost": ..., "cashflows": [...], "lcoe": ..., "payback_years": ...,
}
```

- 来源:`finance.hourly.compute_financials`(calc: 逐时;planning: 对 best 候选调用 evaluate_plan 补逐时后再算财务 — 同时解决意见第 5 条「最优候选逐时运行不落盘」,见 9.5)。
- `services/results.py:522-548 _check_financial` 迁移到 `analysis/assessment.py`,读取 `content["financial"]`,缺失 → insufficient 但给诊断说明;与 worker 提交评估口径一致(见 8.2)。

### 7.5 现有代码迁移路径

| 现有代码 | 迁移动作 |
|---|---|
| `metrics/financial.py` 全部 | 迁入 `finance/metrics.py`;`metrics/financial.py` 保留 `from iesplan.finance.metrics import *` 转发(兼容一个版本周期后删除) |
| `metrics/validity.py:89 financial_validity_from_irr` 的 IRRStatus 引用 | 改 `from iesplan.finance.metrics import IRRStatus`(validity.py 将迁入 analysis/assessment.py) |
| `engines/planning.py:239-261` 内联财务计算 | 改调 `finance.metrics.build_project_cashflows/cashflow_irr/project_npv`(方向 engines→finance,允许) |
| `services/config.py:127-151 ECONOMIC_PARAM_SPECS` | 保留为元数据表;默认值来源改 `devices.pricing`(finance 节) |
| `planning.py:154-162` planning_options 财务参数 | 收敛为 `FinanceParams`(finance.params),planning options 只留计算相关 |

---

## 8. analysis/ 计算分析模块(意见第 7 条)

### 8.1 职责

- 调用计算模块 + 财务计算模块的 wrapper,用于批量分析(单因子敏感性分析等)。
- 纯计算逻辑放 analysis(无 DB);任务编排放 worker/executors + services/tasks(新增 'analysis' 任务类型)。

### 8.2 公共接口(函数签名)

```python
# analysis/wrapper.py —— 纯计算,无 DB 依赖
@dataclass(frozen=True, slots=True)
class SweepSpec:
    param_path: str        # 'calc_config.params.discount_rate' | 'device.pv1.params.rated_capacity_kwp' | 'calc_config.irr_floor'
    values: tuple[float, ...]
    unit: str | None = None   # 覆盖单位;None 取注册表单位

@dataclass(frozen=True, slots=True)
class SweepResult:
    param_path: str
    param_value: float
    unit: str
    status: str            # 'ok' | 'infeasible' | 'error'
    kpi: dict | None = None
    financial: FinancialResult | None = None
    solver_status: str = ""

def run_sweep(
    content: dict, data: dict, axis: TimeAxis,
    spec: SweepSpec, base_options: dict | None = None,
    *,
    finance_params: FinanceParams | None = None,
    engine: Callable = evaluate_plan,          # 默认计算模块引擎(命令化后改 get_command)
) -> list[SweepResult]:
    """单因子扫描:对 spec.values 每个值,应用 param_path(含单位换算 to_si/from_si),
    调用引擎 → compute_financials → SweepResult。纯函数,便于单测。"""

def apply_param(content: dict, param_path: str, value: float, unit: str | None) -> dict:
    """按点路径改写 content(深拷贝),校验参数存在且单位合法。"""

def summarize_sweep(results: list[SweepResult]) -> dict:
    """汇总表:基准值/变化率/单调性/极值点(前端图表数据)。"""
```

```python
# analysis/sensitivity.py —— 任务编排(DB 层)
def run_sensitivity_analysis(
    db: Session, project_id: int, base_config: dict,
    sweeps: list[SweepSpec],
) -> int:
    """创建 'analysis' 类型任务(任务参数含 sweeps),返回 task_id。"""

def build_analysis_payload(sweep_results: list[SweepResult]) -> dict:
    """SweepResult[] → evidence payload(含 sweeps 表 + financial 列)。"""
```

```python
# analysis/assessment.py —— 自 metrics/validity.py 迁入(四维评估)
class ValidityLevel(StrEnum): ...
class PhysicalValidity(StrEnum): ...
class OptimalityValidity(StrEnum): ...
class FinancialValidity(StrEnum): ...
class ReliabilityStatus(StrEnum): ...
def summarize_four_dimensions(...) -> dict: ...
def check_financial(content: dict) -> tuple[FinancialValidity, dict]:
    """读 content['financial'](7.4 块)计算财务维度;缺失 → insufficient+诊断。"""
```

### 8.3 执行路径(worker 侧,新增 'analysis' 任务)

- `worker/executors.execute_analysis(ctx)`:
  1. `run_sweep` 对每个 sweep 跑引擎(`_run_engine` 隔离子进程,命令化函数);planning 类 sweep 的逐时财务走 9.5 的自动串联;
  2. 逐时大结果不落盘,只落 SweepResult 表 + financial 块;
  3. payload:`{"result_kind": "analysis_result", "sweeps": [...], "summary": {...}, "financial": {...}}`。
- `models/calc.py` `ck_tasks_type` 增加 `'analysis'`。
- 前端入口(未来):ConfigPage/ResultsPage「敏感性分析」按钮 → 提交任务 → 结果表 + 图。

### 8.4 现有代码迁移路径

| 现有代码 | 迁移动作 |
|---|---|
| `worker/executors.py:443-505,522-527` uncertainty 的 Monte Carlo 采样 | 保留在 executors(与 analysis 并存,定位不同:随机 vs 确定性单因子);样本指标扩展 NPV/LCOE(调 finance) |
| `metrics/engineering.py`、`metrics/environmental.py` | 迁入 `analysis/indicators.py`(energy_balance_summary/peak_demand/capacity_utilization/load_met_ratio/operational_emissions);executors/results 引用改指向 |
| `metrics/validity.py` | 迁入 `analysis/assessment.py`;`services/results.py:450-605` 的 `_check_*` 四维函数迁入,results 调用 |
| `metrics/` 目录 | 全部迁出后退役(删除;保留一个版本周期转发) |

---

## 9. 计算模块复用与迁移路径(services/tasks + worker/executors + engines)

计算模块不新建包:由 `services/tasks`(快照与任务编排)+ `worker/executors`(执行与证据)+ `engines`(引擎)组成,按下列改造点演进。

### 9.1 快照装配接入装配文件(services/tasks.py)

```python
# assemble_snapshot(db, project_id, task_type, config, user) 改造(现 :267-327)
# 1. _resolve_project_inputs 不变(取版本内容)
# 2. 新增: version, content, axis 就绪后:
#    af_text, af = assembly.build_assembly_from_project(db, project_id, freeze=True)   # 或从 content 生成
#    content["assembly_text"] = af_text        # 装配文本进入版本内容(计算输入之一,意见第 5 条)
# 3. hash_input 增加 "assembly_text": af_text → content_hash 覆盖装配(同输入同哈希保持)
# 4. CalcSnapshot 新增列 assembly_text(Text, 可空;旧快照为 NULL 时按旧路径装载,兼容期一个版本)
```

### 9.2 执行器改造(worker/executors.py)

```python
# _run_engine(ctx, fn, args, ...) 改造:fn 参数允许 command_id
#   command_id 以 'ies.command.' 前缀识别 → modeling.get_command → resolve_function_ref
#   execute_calc 的 "iesplan.engines.eval_run.evaluate_plan" → "ies.command.compute.evaluate_plan.v1"
#   execute_plan 的 "iesplan.engines.planning.run_planning"  → "ies.command.compute.run_planning.v1"
#   (命令在 modeling/command.py 启动时注册,function_ref 指向 engines.eval_run/planning)

def execute_calc(ctx, content, data, axis, options=None) -> dict:
    """改造点:
    - plan 不再调 _build_plan,改 assembly.plan_from_content(content, data, axis)(SI 换算,见 3.5)
    - solver_opts 来源:快照 tolerances(权威) + task_params.solver_options(覆盖)
      (修复 tolerances 死配置,意见第 5 条)
    - random_seed: eval_run 的 seed 不再硬编码 42,从 options["seed"] 读取,快照 random_seed 注入
      (修复 seed 不一致)
    - payload 增加 financial 块(7.4)"""

def execute_plan(ctx, content, data, axis, options=None) -> dict:
    """改造点:同上 + 对 best 候选执行 evaluate_plan 补逐时运行与财务(9.5)"""

def execute_assembly_check(ctx) -> dict:
    """新增:从快照取装配文本 → assembly.validate_assembly → payload(6.4)"""

def execute_analysis(ctx) -> dict:
    """新增:任务参数 sweeps → analysis.run_sweep → payload(8.3)"""

def execute_uncertainty(ctx, content, data, axis, options=None) -> dict:
    """改造点:payload 增加 financial 块(样本指标扩展 NPV/LCOE)"""

def _build_plan(content, config=None) -> dict:
    """删除;调用点改 assembly.plan_from_content(迁移期可保留薄转发一行)。"""
```

`worker/runner.py`:
- `dispatch`(:220)新增分支:`task_type == "assembly_check" → executors.execute_assembly_check(ctx)`;`task_type == "analysis" → load_inputs(...) → executors.execute_analysis(ctx)`;`COMPUTE_TASK_TYPES` 增加 `'analysis'`,`IO_TASK_TYPES` 增加 `'assembly_check'`(装配检查不入快照数据集装载,独立分支)。
- `load_inputs`(:68-112)改造:装载内容后,`content["assembly_text"]` 存在则 `assembly.validate_assembly` 预检(失败 → SnapshotInputError 带装配错误反馈);数据集换算(3.5)统一经 units。

### 9.3 算法选择落地(意见第 5 条「算法选择被忽略」)

```python
# engines/selector.py(新,~60 行)
ALGO_TO_ENGINE: Final[dict[str, str]] = {
    "ies.algo.milp_hybrid": "ies.command.compute.evaluate_plan.v1",
    "ies.algo.lp_relax":    "ies.command.compute.evaluate_plan_lp.v1",   # evaluate_plan 的 LP 松弛变体(阶段 B 注册)
    "ies.algo.mc_sampling": "ies.command.compute.uncertainty.v1",        # 采样/不确定性(不参与 calc)
}

def select_engine(calc_config: dict, task_type: str) -> tuple[str, dict]:
    """calc_config.algorithm{mode, name} → (command_id, solver_opts)。
    mode='auto' 或缺省 → 默认 milp_hybrid;name 未注册 → 诊断 error 并回退默认。
    solver_opts = 快照 tolerances ∪ task_params.solver_options(tolerances 权威, 后者覆盖)。"""
```

- `services/config.py:110-114 ALGO_DB_CLASS` 保留 DB 落库映射;执行层读取改走 `engines/selector.py`。
- 阶段 B(后续里程碑):`lp_relax` 实现为 evaluate_plan 的 LP 变体(整数变量松弛),命令注册后自动生效,计算模块无需改分发代码。

### 9.4 收敛精度来源统一(tolerances 死配置修复)

- 现状:`solver_opts` 只来自 `task_params.solver_options`(executors.py:169-183),快照 tolerances(:366-370 默认 gap_rel/time_limit_s)零引用。
- 改造:`selector.select_engine` 合并来源;**优先级:task_params.solver_options > 快照 tolerances > solver.py 默认值(DEFAULT_MIP_REL_GAP=0.001/DEFAULT_TIME_LIMIT=600)**。随机 seed 统一:快照 random_seed 注入 options["seed"],eval_run/planning 的 `seed=42` 硬编码移除。

### 9.5 最优策略组合的逐时运行自动串联(意见第 5 条输出侧缺口)

- `execute_plan` 改造:对 `PlanningResult.best` 候选,用其容量构造 plan → `evaluate_plan`(命令化)→ 得逐时 flows;flows 经 `_store_hourly_refs` 落 hourly_refs(calc 同款);财务用逐时费用列经 `finance.hourly.compute_financials` 计算。
- 输出 payload 因此同时含:`candidates/best`(容量+IRR/NPV)+ `flows` + `hourly_refs` + `financial` —— 用户无需手工复制容量再跑 calc。

### 9.6 API 层下沉业务逻辑(意见第 10 条)

| 现状 | 动作 |
|---|---|
| `api/admin.py:104-121,175-242,265-325` 路由内直接 ORM | 下沉到 `services/admin.py`(新,薄服务);路由只做参数校验与响应组装 |
| `api/objects.py:58-68` 路由内 ORM 统计 | 下沉 `services/objects.py`(已有统计函数,路由改调用) |
| `services/config.py:1172` / `worker/executors.py:116` 延迟导入 | 依赖图(第 11 节)保证单向无环后,删除延迟导入注释,改顶层导入 |
| `api/__init__.py`、`services/__init__.py` 过期文档 | 更新为实际模块清单与依赖说明 |

### 9.7 数据库变更(models/)

| 变更 | 说明 |
|---|---|
| `models/calc.py:140` `ck_tasks_type` 增加 `'assembly_check'`, `'analysis'` | 迁移脚本:`ALTER TABLE tasks DROP CONSTRAINT ck_tasks_type; ADD CONSTRAINT ck_tasks_type CHECK (type IN ('calc','optimization','uncertainty','import','export','report','dataset_build','assembly_check','analysis'))`(Postgres/SQLite 均需执行;部署脚本 `scripts/` 提供) |
| `models/calc.py:85-109` CalcSnapshot 增加 `assembly_text: Mapped[str | None]`(Text, 可空) | 兼容旧快照(NULL 按旧装载路径) |
| `models/model.py:53-84` Device 增加 `model_method: Mapped[str]`(Text, default 'mechanism', CHECK IN ('mechanism','data_repeat','data_predict'))、`stateful: Mapped[bool]`(Boolean, default false) | 模型类型标志落库(意见第 2 条);`model_fidelity` 保留 |

---

## 10. 前端边界(意见总述)

### 10.1 原则

- `frontend/src/api/client.ts` 保持**薄封装**:HTTP 序列化/错误映射/分页解析,不承载业务逻辑(命令差量生成、乐观锁编排、assumptions 键集、评估触发、下载 token 会话)。
- 前端引用后端函数的方式不变:`api` 对象按资源分组(client.ts:920-1059 现为 auth/projects/model/datasets/config/tasks/results/exports/admin/health),新模块按 `devices/modeling/assembly/finance/analysis` 分组扩展(未来端点就绪后各加一组方法)。
- 本次实施范围:**仅 ConfigVariable.unit 字段**(配合第 3 节单位标准化,后端解析入口统一)。其余前端薄化项列为后续里程碑。

### 10.2 本次前端改动(最小集)

| 文件 | 改动 |
|---|---|
| `frontend/src/types.ts:450-459` | `ConfigVariable` 增加 `unit: string` |
| `frontend/src/pages/ConfigPage.tsx:605-611` | `buildInput` 回传 `unit`(取变量规格 unit) |
| `frontend/src/pages/ConfigPage.tsx:184-187,652` | 删除百分比 ÷100 手工换算,原样提交,后端解析 |
| `frontend/src/api/client.ts:620-635` | `configToServer` 透传 `unit` |

### 10.3 未来 REST 端点设计(本次不实施,仅设计;供外部 API 引用对齐)

| 模块 | 方法/路径 | 请求 | 响应 |
|---|---|---|---|
| devices | `GET /api/devices` | — | 设备规格列表(含 model_method/stateful/unit 元数据) |
| devices | `GET /api/devices/{type_id}` | — | 单设备规格(含参数 unit/min/max/default) |
| devices | `GET /api/prices` | — | 统一价格初始化(PriceDefaults 序列化) |
| devices | `POST /api/devices/upload` | multipart: yaml + csv | 设备规格 + profile 引用(插件式上传) |
| modeling | `GET /api/modeling/commands?device_type=` | — | 已注册 ModuleCommand(命令 id/function_ref/inputs/outputs) |
| modeling | `POST /api/modeling/commands/{command_id}/invoke` | 输入字段值 | 输出字段值(同步试算,调试/教学用) |
| assembly | `POST /api/projects/{pid}/assembly/check` | — | 装配检查诊断(同步,复用 validate_project 的 diags 结构) |
| assembly | `GET /api/projects/{pid}/assembly/file` | — | 装配文件文本(下载/审阅) |
| assembly | `POST /api/projects/{pid}/tasks` | type=assembly_check | 任务(异步装配检查) |
| finance | `GET /api/projects/{pid}/tasks/{tid}/financial` | — | FinancialResult(evidence financial 块) |
| analysis | `POST /api/projects/{pid}/analysis/sensitivity` | base_config + sweeps | task_id(异步批量分析) |
| analysis | `GET /api/projects/{pid}/tasks/{tid}/result` | — | 现结果端点自动含 sweeps/summary(analysis_result) |

前端 client.ts 对应新增组:`api.devices.* / api.modeling.* / api.assembly.* / api.finance.* / api.analysis.*`(形状与现有 `api.config.*` 一致)。

---

## 11. 模块间依赖图(单向无环)

```
┌─────────────── 6 层:api ───────────────┐
│ auth objects admin projects model       │
│ datasets config validation tasks        │
│ results exports (+devices modeling      │
│  assembly finance analysis 未来)        │
└──────┬────────────────────────┬─────────┘
       │                        │
┌──────▼─────────── 5 层:services / worker ──────────────┐
│ services: config model tasks results project dataset    │
│ validation package queue audit identity objects         │
│ worker: runner executors lease main solver_process      │
└─┬───────────────┬─────────────────┬──────────────────┬──┘
  │               │                 │                  │
  ▼               ▼                 ▼                  ▼
┌──────────┐ ┌──────────┐   ┌──────────────┐   ┌──────────────┐
│ 4 层      │ │ 3 层     │   │ 3 层         │   │ 3 层         │
│ analysis │ │ assembly │   │ engines      │   │              │
└────┬─────┘ └───┬──────┘   │ eval_run      │   │              │
     │           │          │ planning      │   │              │
     ▼           ▼          │ solver balance│   │              │
┌──────────┐ ┌──────────┐   └──┬───────┬────┘   │              │
│ 2 层      │ │ 2 层     │      │       │        │              │
│ finance  │ │ modeling │      │       ▼        │              │
└────┬─────┘ └───┬──────┘      │  ┌─────────┐   │              │
     │           │             │  │ 2 层     │   │              │
     ▼           ▼             │  │ finance │   │              │
┌────────────┐ ┌───────────────┐ └────┬────┘   │              │
│ 1 层 devices│◄┤ 依赖方向:      │      │        │              │
└─────┬──────┘ │ 上→下,无环     │      ▼        │              │
      │        └───────────────┘  ┌─────────┐   │              │
      ▼                            │ 1 层     │   │              │
┌────────────┐                     │ devices │   │              │
│ 0 层 core/  │                     └────┬────┘   │              │
│    models  │◄──────────────────────────┘        │              │
└────────────┘                                    └──────────────┘
```

关键边(谁导入谁,只列跨层边):

| 方向 | 导入点 | 说明 |
|---|---|---|
| services → devices | `services/config.py`、`services/model.py`、`services/validation.py` | 注册表访问改走 devices 门面(get_device_spec/get_parameter_spec/default_params) |
| services → assembly | `services/tasks.py`(快照装配)、`services/model.py`(拓扑校验委托)、`services/validation.py` | 装配文本与检查 |
| services → finance | `services/results.py`(IRRStatus/financial 评估)、`services/config.py`(默认值) | finance 低于 services |
| services → analysis | `services/results.py`(四维评估 assessment/indicators) | 结果分析 |
| worker → assembly | `worker/runner.py`(load_inputs 装配预检)、`worker/executors.py`(plan_from_content) | 计算输入装配 |
| worker → modeling | `worker/executors.py`(_run_engine 命令解析) | 模块调用命令 |
| worker → finance | `worker/executors.py`(financial 块) | 财务计算 |
| worker → analysis | `worker/executors.py`(execute_analysis/四维评估) | 结果分析 |
| engines → modeling | `engines/eval_run.py`(函数库) | 机理函数命令化后经 modeling |
| engines → finance | `engines/planning.py`(现金流/IRR) | finance 低于 engines,允许 |
| analysis → engines/finance/assembly | `analysis/wrapper.py`(默认 engine=evaluate_plan;compute_financials) | wrapper 调用计算与财务 |
| api → services | 现有全部路由 | API 不直接 import 其他层(admin/objects 改造后) |

**禁止边**(现状存在的违规,须在迁移中消除):`services/results.py → engines.planning.CAPACITY_PARAM`(改走 devices);`services → worker`(config.py:1172 的环是 services 内部 project,与 worker 无关,保留 services 内扁平依赖即可);`engines → services`(现状 engines 不依赖 services,保持)。

---

## 12. 现有代码迁移路径总表(按模块)

| 现有文件 | 目标 | 动作 |
|---|---|---|
| `core/registry.py`(注册目录段 186-582) | `devices/catalog/*.yaml` + `devices/loader.py` | 内容迁 yaml;registry 只留数据类与校验;门面访问改 devices |
| `core/units.py` | `core/units.py`(原地扩展) | 新增 9 个复合单位 + parse/to_si/from_si/is_known_unit/canonical_unit/unit_dimension;消除全库硬编码换算 |
| `engines/devices.py` | `modeling/functions.py` | 整体迁入,SI 化 |
| `engines/eval_run.py`(40KB) | 保留引擎主体 | 拆三块:设备函数→modeling;财务段→finance;换算→assembly/plan.py;seed/容差→options |
| `engines/planning.py` | 保留引擎主体 | 财务调用改 finance;best 候选补逐时+财务(9.5);seed 去硬编码 |
| `metrics/financial.py` | `finance/metrics.py` | 迁入,转发兼容一版 |
| `metrics/engineering.py`、`environmental.py` | `analysis/indicators.py` | 迁入,转发兼容一版 |
| `metrics/validity.py` | `analysis/assessment.py` | 迁入(四维评估+_check_financial 读 financial 块) |
| `metrics/__init__.py` | 退役 | 全部迁出后删除(转发保留一版) |
| `services/config.py` | 保留(编排) | 单位解析(3.4)、默认值来源 devices.pricing、_dims_for_unit 重写、ALGO_DB_CLASS 映射移 engines/selector.py、tolerances 语义修复 |
| `services/model.py` | 保留 | validate_topology 委托 assembly;Device 新字段;删 _DEVICE_PORT_DIRECTIONS 硬编码表 |
| `services/tasks.py` | 保留(计算模块编排) | assemble_snapshot 接入装配文本;assembly_check/analysis 任务创建 |
| `services/results.py` | 保留 | 导入改 devices/finance/analysis;四维检查迁 analysis/assessment |
| `worker/executors.py` | 保留(计算模块执行) | 命令化/算法选择/容差与 seed 修复/financial 块/新执行器;_build_plan 迁出 |
| `worker/runner.py` | 保留 | dispatch 新分支;load_inputs 装配预检+单位换算 |
| `models/calc.py`、`model.py` | 保留 | 新任务类型 CHECK;CalcSnapshot.assembly_text;Device.model_method/stateful |
| `api/admin.py`、`objects.py` | 保留 | ORM 逻辑下沉 services/admin.py(新) |
| `frontend/src/types.ts`、`ConfigPage.tsx`、`client.ts` | 保留 | unit 字段最小集(10.2) |

---

## 13. 实施里程碑(每阶段可独立合入,测试全绿)

| 里程碑 | 内容 | 交付物/测试 |
|---|---|---|
| **M0 骨架** | 新建五个包 + `__init__.py` 门面(转发现有实现);依赖规则 README;api/services `__init__` 文档更新 | `backend/tests/test_module_structure.py`(包可导入、门面转发等价) |
| **M1 单位标准化** | units.py 扩展;config 校验接入(unit 字段、数值解析、_dims_for_unit 重写);前端 unit 最小集;runner/executors 换算点接 units;eval_run 移除 ×1000 第一批 | `test_units_api.py`、`test_unit_regression.py`(现有集成数值对照) |
| **M2 devices** | 9 个 yaml + loader + prices.yaml + pricing;registry 注册目录迁移;services 引用改门面;Device 表新列 | `test_devices_module.py`(yaml 装载/价格来源/插件注册) |
| **M3 modeling** | command.py 注册表 + functions.py 迁入 + build.py;executors 命令化(compute 命令注册);ALGO/容差/seed 修复(selector.py) | `test_modeling_module.py`、`test_selector.py` |
| **M4 assembly** | spec/validate/build/plan;validate_topology 委托;快照装配文本+哈希;assembly_check 任务;DB 迁移(ck_tasks_type+assembly_text) | `test_assembly_module.py`(边-端构造/三类检查/装配文本往返) |
| **M5 finance** | finance 包;metrics 迁入;hourly(LCOE/回收期);evidence financial 块(calc/planning/uncertainty);planning best 补逐时 | `test_finance_module.py`、`test_financial_evidence.py`(四维复查财务不再 unknown) |
| **M6 analysis** | wrapper/sensitivity;analysis 任务;indicators/assessment 迁入;metrics 退役转发 | `test_analysis_module.py`(单因子扫描/汇总) |
| **M7 清理与前端薄化** | admin/objects 下沉;延迟导入消除;前端薄化项(命令差量/乐观锁/assumptions 回退后端,独立小步);未来 REST 端点按 10.3 逐项开放 | 全量回归 + `contract_smoke.py` + `frontend_smoke.mjs` |

里程碑依赖:M0 → M1 → (M2/M3 可并行)→ M4 → M5 → M6 → M7。M1 的数值回归风险最高,要求逐步替换(一次替换一个换算点并对照旧数值)。

---

## 14. 兼容性与风险控制

1. **快照哈希变更**(M4 装配文本入哈希):装配文本为空/旧快照 → 不参与哈希,按旧路径装载,兼容一个版本;新快照自动含文本。
2. **单位 SI 化数值回归**:eval_run 边界一次改一个换算点,`backend/tests/test_integration.py` 与 `contract_smoke.py` 的数值断言是回归基线;任何换算变化必须同步更新断言(禁止"先改代码后修断言"之外的不一致)。
3. **ck_tasks_type CHECK 迁移**:必须先迁移数据库再发布新代码(否则写入新类型被约束拒绝);迁移脚本放入 `scripts/`,部署顺序文档化。
4. **metrics 转发兼容**:迁移后保留 `from iesplan.metrics.financial import ...` 转发一个版本周期,避免一次性改动面过大。
5. **前端 unit 字段兼容**:老保存数据无 unit → 后端以注册表补全,仅诊断提示,不阻断保存。
6. **devices yaml 与注册表一致性**:装载时校验 type_id 与 DB `ck_devices_type` 粗类映射(`services/model.py:_resolve_type_id`)一致,不一致给诊断并拒载。
7. **插件式设备安全**:yaml 仅声明式(参数/函数引用白名单:function 只能是 `iesplan.modeling.functions.*` 或已注册命令),不做任意代码执行。
