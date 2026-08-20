# 单位标准化定案方案(审查意见第 0 条)

> 定案文档,供实施 agent 直接编码。配套差距调研:`docs/review-0820/00-gap-report.md`(或由本目录调研结论提供)。
> 原则:数值/单位全程标准化;初始化明确单位;前端非标准单位字符串解析转标准单位;计算默认标准单位(SI)。

---

## 1. 决策摘要

| # | 决策 | 说明 |
|---|------|------|
| D1 | **扩展 `core/units.py`,不新建独立包** | 注册表与换算保持单点权威;字符串解析新增同包 `core/unitparse.py`;避免 import 拓扑扩散与循环依赖 |
| D2 | **三层单位契约** | L1 存储/注册层=业务单位(kW/kWh/CNY/kWh);L2 计算边界=SI 基准(J/W/K/CNY/s/kg/m³);L3 展示层=前端按注册表单位渲染。换算收敛到两个边界点(worker 输入装配、引擎 KPI 输出),引擎内部零硬编码换算 |
| D3 | **前端 `parseQuantity` 镜像后端** | 前端接受"数值+单位"字符串,解析后提交"数值+注册表单位";后端保存时 `normalize_unit` 归一化落库;后端不解析字符串(数据集声明除外),数值一律 float |
| D4 | **单位由注册表驱动,参数名后缀不再承载单位语义** | `ParameterSpec.unit` 是唯一权威;`kWp` 归一化为 `kW`;`万` 作为前缀乘数(万m³=1e4 m³) |
| D5 | **复合单位量纲参与表达式约束检查** | 新增 `dims_of(unit)`,替换 config.py `_dims_for_unit` 的精确查表,修复"CNY/kWh、tCO2/MWh 视为无量纲"的量纲检查失效 |

现状差距(调研结论摘要):`core/units.py` 的 convert/energy_to_joules/power_to_watts/temperature_kelvin 全库无调用点(死代码);引擎换算散落为各调用点硬编码 `×1000`/`KWH_TO_J`(eval_run.py:435/476/500/509-510/516/562-563/570);变量 unit 字段在前端往返中丢失(types.ts ConfigVariable 无 unit、ConfigPage.tsx buildInput 不回传、config.py `_validate_variables` 不强制),导致 `_validate_expression_constraint` 量纲检查静默失效(config.py:787-791 取到 None→无量纲);`_dims_for_unit`(:393-398)对未注册单位一律返回无量纲;数据集单位仅做大小写不敏感字符串相等校验(dataset.py:866),数值不换算;结果 meta 硬编码 "W(W) / kWh(energy) / 0-1(ratio)"(executors.py:267)。

---

## 2. 标准单位体系

### 2.1 SI 基准与类别

沿用 `core/units.py` 现有 6 类(能量 J、功率 W、温度 K、金额 CNY、时长 s、角度 rad),**新增 4 类**:

| 类别 | 基准 | 说明 |
|------|------|------|
| `mass` 质量 | kg | 排放因子(tCO2/MWh、kg/kWh)与 CO2 总量需要质量维度 |
| `volume` 体积 | m³ | 燃气量、气价(元/m³)、LHV(kJ/m³)、万m³ |
| `voltage` 电压 | V | 注册表 `voltage_level_kv`(kV) |
| `dimensionless` 无量纲 | 1 | 纯数、比例、百分比;`-` 与 `%`(to_si=0.01) |
| `area` 面积 | m² | GHI(W/m²) |

`core/expression.py` 同步新增 `DIM_MASS = "mass"`、`DIM_VOLUME = "volume"`(量纲 Counter 键,供表达式引擎使用;voltage/area 不参与表达式,量纲键不存在时自然无量纲)。

### 2.2 单位注册表增补(逐条 UnitSpec,直接落地)

在 `core/units.py` `UNITS` 字典追加(沿用 `_u()` 构造器;`id` 遵循 `ies.unit.*`):

```
质量 mass(基准 kg):
  "kg":  ies.unit.kg , to_si=1.0, aliases=("kg", "公斤", "千克")
  "t":   ies.unit.t  , to_si=1e3, aliases=("t", "吨")
  "tCO2": ies.unit.tco2, to_si=1e3, aliases=("tco2", "吨二氧化碳", "tCO₂", "tCO2e")   # 质量语义,不做碳当量折算
体积 volume(基准 m³):
  "m³":  ies.unit.m3 , to_si=1.0, aliases=("m3", "立方米", "方")
  "千m³": ies.unit.km3, to_si=1e3, aliases=("千立方米",)        # 可被 千+m³ 前缀解析等价命中
电压 voltage(基准 V):
  "V":   ies.unit.v  , to_si=1.0, aliases=("v", "伏")
  "kV":  ies.unit.kv , to_si=1e3, aliases=("kv", "千伏")
  "MV":  ies.unit.mv , to_si=1e6, aliases=("mv",)
面积 area(基准 m²):
  "m²":  ies.unit.m2 , to_si=1.0, aliases=("m2", "平方米", "平米")
无量纲:
  "-":   ies.unit.dimless, to_si=1.0,  aliases=("-", "无量纲", "dimensionless", "1")
  "%":   ies.unit.pct , to_si=0.01, aliases=("%", "百分比", "percent", "pct")
时长 duration 增补:
  "d":   ies.unit.d   , to_si=86400.0,        aliases=("d", "天", "日", "day")
  "月":  ies.unit.month, to_si=2_592_000.0,    aliases=("月", "month", "mo")    # 1 月 = 30 d,文档注明
功率 power 增补别名(峰值容量语义,数值即功率,不另设单位):
  kW 的 aliases 追加 ("kwp", "kWp"); MW 的 aliases 追加 ("mwp", "MWp")
温度 temperature 修正:
  "C" 的 aliases 追加 ("°c",)     # 现状缺 "°C" 小写别名,补上
金额 currency 增补复合前缀:
  "CNY" 的 aliases 追加 ("万元",) 不可行(前缀乘数带系数)→ 万元由解析器 万+元 组合,不注册为别名
```

> 注意:`万元`、`万m³` 等带中文前缀乘数的写法**不注册为别名**,由解析器 `multiplier+symbol` 组合(§3.2);`ALIAS_MAP` 冲突"先注册者优先"规则不变(如 `度` 仍解析为 kWh 而非 deg)。

### 2.3 复合单位规范形表

注册表/数据集/引擎契约中出现过的全部单位串,规范形(存储与展示用)与其量纲:

| 规范形(存储/展示) | 解析分解 | 量纲 `dims_of` | 用途(现状出处) |
|---|---|---|---|
| `kW` | kW | {power:1} | 设备功率参数(registry.py:12 处) |
| `kWp` → **归一化为 `kW`** | kW | {power:1} | PV 容量(rated_capacity_kwp) |
| `kWh` | kWh | {energy:1} | 电池容量等 |
| `kV` | kV | {voltage:1} | voltage_level_kv |
| `CNY/kWh` | CNY/kWh | {currency:1, energy:-1} | import_tariff/export_tariff |
| `CNY/kW` | CNY/kW | {currency:1, power:-1} | 热泵/锅炉/冷机 unit_invest_cost |
| `CNY/kWp` → **`CNY/kW`** | CNY/kW | {currency:1, power:-1} | PV unit_invest_cost |
| `CNY/kW·月` | CNY/(kW·月) | {currency:1, power:-1, time:-1} | demand_charge(月=2,592,000 s) |
| `CNY/m³` | CNY/m³ | {currency:1, volume:-1} | gas_price |
| `kJ/m³` | kJ/m³ | {energy:1, volume:-1} | lhv_kj_per_m3(引擎需 J/m³) |
| `W/m²` | W/m² | {power:1, area:-1} | GHI 数据集列 |
| `tCO2/MWh` | tCO2/MWh | {mass:1, energy:-1} | 电网排放因子(ENVIRONMENTAL_PARAM_SPECS) |
| `tCO2/万m³` | tCO2/万m³ | {mass:1, volume:-1} | 燃气排放因子 |
| `kg/kWh` | kg/kWh | {mass:1, energy:-1} | 数据集 grid_emission_factor(与 tCO2/MWh 同量纲) |
| `元/kWh` → **`CNY/kWh`** | CNY/kWh | 同上 | 中文符号(解析器归一) |
| `°C`/`℃` → **`C`** | C(仿射,offset 273.15) | {temperature:1} | 温度 |
| `-` / `%` | 无量纲 | {} | 效率/比例/SOC/折现率/税率 |
| `a` / `s` | a / s | {time:1} | 项目期/折旧期/求解时限 |

规则:复合单位只允许一层除法(`A/B`),分子分母内部用 `·` 或 `*` 连乘;仿射单位(C/F)禁止出现在复合/分母中;`normalize_unit` 对表内任一形态输出规范形。

### 2.4 字段名与数值类型规范

- **数值类型**:配置/数据集/结果数值一律 JSON number(float64);拒绝字符串数值、NaN、±Inf(现有 `PARAM-UNIT-002` 检查保持)。字符串只出现在"解析入口"(前端输入框、数据集声明 unit、表达式文本)。
- **变量声明**(`calc_configs.variables[]` 行,注册表生成与前端提交同构):

```json
{"name": "pv_cap_1", "type": "continuous", "initial": 500.0, "min": 0.0, "max": 1000.0,
 "unit": "kW", "device_ref": "pv-1", "param": "rated_capacity_kwp"}
```

  - `unit`:continuous/integer **必填**(布尔/enum 可省略),值=注册表 `ParameterSpec.unit` 规范形;后端保存时 `normalize_unit` 归一化并回填(§9 迁移)。
  - `min`/`max`/`initial` 必须与 `unit` 同一单位(即注册表声明单位);前端输入解析后换算到该单位再提交。
- **设备参数值**(`parameters.devices.<key>.<param>`):数值不带单位字符串,单位由注册表 `ParameterSpec.unit` 声明;前端渲染用 `unit_meta().symbol_zh/en` 展示。
- **数据集列声明**(数据集头 `# unit:` 声明):`unit` 为可解析单位串(允许 `kWp`、`元/kWh`、`度` 等非规范形态);校验规则改为**量纲一致性**(§5.5),数值换算在计算边界完成。
- **逐时结果 meta**:由硬编码字符串改为逐字段单位契约表(§5.6)。

---

## 3. 非标准单位字符串解析规则

### 3.1 词法与文法

```
quantity    := number [ws] unit-string?
number      := sign? int (',' int)* ['.' frac]? [('e'|'E') sign? digits]?    # 十进制/科学计数/千分位
             | sign? digits ['.' frac]? MULT?                                # 中文乘数后缀: 1.5万 = 15000
unit-string := compound ('/' compound)?      # 分母至多一层,拒绝嵌套除法与括号
compound    := token (('·'|'*') token)*      # 分子/分母内部连乘
token       := MULT? symbol                  # 如 万m³、kW、元、月
symbol      := UNITS 键 或 别名(大小写不敏感,含新注册 kWp/MWp/°C 别名)
MULT        := 百(1e2) | 千(1e3) | 万(1e4) | 亿(1e8)
```

约束:
1. 数值与单位之间允许零个或多个空格(`"1000kW"`、`"1000 kW"`、`"3 元/kWh"` 均合法)。
2. 单位串缺失时:若调用方提供 `context`(期望单位,来自注册表),取 context;否则抛 `UnitParseError`(初始化必须明确单位)。
3. 仿射单位(C/F)只允许独立出现,禁止进入复合/分母。
4. 纯乘数无单位符号(`"0.5 万"`)不合法,必须带 symbol 或 context。

### 3.2 解析算法

`core/unitparse.py` 内部流程(供实现,伪代码):

```
def parse_quantity(text, context=None):
    num_match = NUMBER_RE.match(text.strip())
    if not num_match: 抛 UnitParseError(定位 0 字符, 候选: 数字格式示例)
    value = parse_number(num_match.group())           # 处理千分位/万亿/科学计数
    rest = text[num_match.end():].strip()
    unit_s = rest
    if not rest:
        if context is None: 抛 UnitParseError(要求明确单位)
        unit_s = context
    norm, factor = parse_unit_string(unit_s)          # 规范形 + 组合系数
    if norm in AFFINE_UNITS:                          # C/F 独立温度
        si_value = value * to_si + offset             # 经 units.convert(value, norm, "K")
    else:
        si_value = value * factor                     # 线性: ×∏num.to_si / ∏den.to_si
    return Quantity(value=value, unit=norm, si_value=si_value, si_unit=si_unit_of(norm))

def parse_unit_string(s):
    # 1) normalize_unit 全串查表(含复合词条 kWp/CNY/kWh 等别名)命中则直返
    # 2) 按 '/' 拆分子分母(数量>2 → 抛错)
    # 3) 每侧按 '·'/'*' 拆 token; 每 token 匹配 (MULT?)(symbol) 最长前缀
    #    符号查找顺序: UNITS 键 → ALIAS_MAP(含 kWp→kW 别名), 大小写不敏感
    # 4) 出现仿射单位 → 抛 UnitParseError(复合中禁用仿射)
    # 5) 返回 (规范化串: 各 token 规范形以 '/'、'·' 连接, factor = ∏num.to_si*MULT / ∏den.to_si*MULT)
```

### 3.3 解析示例(测试用例表,直接进 `backend/tests/test_unit_parse.py`)

| 输入 | context | 结果 `Quantity` | 说明 |
|---|---|---|---|
| `"1000 kW"` | — | value=1000, unit=`kW`, si=1e6, si_unit=`W` | 空格分隔 |
| `"1000kW"` | — | 同上 | 无空格 |
| `"1.5MWh"` | — | value=1.5, unit=`MWh`, si=5.4e9, si_unit=`J` | 无空格+复合前缀 |
| `"3 元/kWh"` | — | value=3, unit=`CNY/kWh`, si=3/3.6e6, si_unit=`CNY/J` | 中文符号归一 |
| `"40 元/kW·月"` | — | value=40, unit=`CNY/kW·月`, si=40/(1e3×2.592e6), si_unit=`CNY/(W·s)` | 分母连乘+月 |
| `"2 tCO2/万m³"` | — | value=2, unit=`tCO2/万m³`, si=0.2, si_unit=`kg/m³` | 万 前缀乘数 |
| `"0.581 tCO2/MWh"` | — | si=1.6139e-7, si_unit=`kg/J` | 排放因子 |
| `"25℃"` / `"25 °C"` | — | value=25, unit=`C`, si=298.15, si_unit=`K` | 仿射 |
| `"10%"` | — | value=10, unit=`%`, si=0.1 | 百分比 |
| `"1.5万"` | `"kW"` | value=15000, unit=`kW`, si=1.5e7, si_unit=`W` | 中文乘数+context |
| `"1000"` | `"kW"` | value=1000, unit=`kW`, si=1e6 | 纯数字+context 兜底 |
| `"0.35 元/kWh"` | — | si=9.7222e-8, si_unit=`CNY/J` | 电价 |
| `"500 kWp"` | — | unit=`kW`(归一), si=5e5 | 峰值容量 |
| `"abc kW"` | — | 抛错(数字) | |
| `"3 元/(kWh·h)"` | — | 抛错(括号/嵌套除法拒绝) | |
| `"30 C"` | — | 抛错(复合上下文拒绝独立温度,应带 context) | 规则 4 宽松化:独立 C 允许,进入复合拒绝 |

### 3.4 错误与诊断

- 新增 `UnitParseError(AppError)`,code **`PARAM-UNIT-001`**(现空闲;`PARAM-UNIT-002`=单位不匹配、`PARAM-UNIT-003`=平衡量纲不一致,见 core/diagnostics.py:44-45),message_key `ies.diag.param.unit_parse`(i18n 中英文案新增)。
- params 字段:`{"text": 原文, "position": 失败偏移, "expected": 期望单位/类别, "suggestions": [ALIAS_MAP 近匹配]}`。
- 数据集列声明单位无法识别:沿用 `DATA-COL-002`(core/diagnostics.py:33)。

---

## 4. 转换层 API 设计

### 4.1 与现有 units.py 的关系(定案)

- **扩展 `core/units.py`**:注册表(§2.2 增补)+ 换算函数扩展(Quantity/normalize_unit/to_si/from_si/dims_of/unit_meta/assert_same_dims)。`convert` 扩展支持复合单位。现有 `energy_to_joules/power_to_watts/temperature_kelvin` 保留(内部改走 `to_si`),使其从死代码变为唯一换算入口的薄封装。
- **新增 `core/unitparse.py`**:纯字符串词法/语法解析(§3),只依赖 `core.units` 与 `core.errors`,无业务依赖;`parse_quantity` 在此定义。
- **不新建包**、不复制注册表到其它模块;禁止任何模块自行维护单位换算表(现状 eval_run 的 `KWH_TO_J`/`W_TO_KW`、runner 的 `*1000.0/step_hours` 全部删除)。
- **前端镜像** `frontend/src/lib/units.ts` + 数据文件 `frontend/src/lib/units.json`:TS 实现与后端逐函数对齐,数据由后端导出脚本生成(§4.4),前端不得手改换算系数。

### 4.2 backend `core/units.py` 扩展(签名)

```python
@dataclass(frozen=True, slots=True)
class Quantity:
    """"数值+单位"解析产物(标准单位形态,非 SI;si_* 供计算边界)。"""
    value: float          # 数值(所在单位为 unit)
    unit: str             # 规范化单位串(如 "kW" / "CNY/kW·月"; 必可被 normalize_unit 接受)
    si_value: float       # 换算到 SI 基准后的数值(复合单位=value×组合系数; 温度=仿射)
    si_unit: str          # SI 基准单位描述(如 "W"; 复合如 "CNY/J"、"kg/m³")

    def to(self, target: str) -> float:
        """换算到目标注册单位(同量纲断言, 跨量纲抛 UnitError)。"""
    def __float__(self) -> float:
        """返回 si_value(SI 优先, 防误用原值)。"""

def normalize_unit(unit: str) -> str:
    """单位串 → 规范形: 别名/大小写/kWp→kW/元→CNY/℃→C/复合统一。
    normalize_unit("kWp") == "kW"; normalize_unit("元/kWh") == "CNY/kWh";
    normalize_unit("℃") == "C"; normalize_unit("0.35元/kWh") == "CNY/kWh"(忽略数值);
    未注册抛 UnitError(PARAM-UNIT-002)。"""

def to_si(value: float, unit: str) -> float:
    """任意注册单位(含复合) → SI 数值(线性×系数, 温度仿射)。计算边界唯一入口。
    to_si(1000, "kW") == 1e6; to_si(40, "CNY/kW·月") == 40/(1e3*2.592e6);
    to_si(25, "C") == 298.15。"""

def from_si(si_value: float, unit: str) -> float:
    """SI → 注册单位数值(逆变换; 温度仿射取反)。结果装配/展示层唯一出口。"""

def dims_of(unit: str) -> Dimensions:
    """注册单位(含复合) → 量纲多重集(复用 core.expression.Dimensions=Counter[str]);
    无量纲返回空 Counter。dims_of("kW")=={power:1}; dims_of("CNY/kWh")=={currency:1,energy:-1};
    dims_of("tCO2/万m³")=={mass:1,volume:-1}; dims_of("%")=={}。"""

def unit_meta(unit: str) -> dict:
    """单位元数据(前端渲染/校验用): {"unit","category","si_unit","to_si","dims":dict,
    "precision_digits","symbol_zh","symbol_en"}。"""

def assert_same_dims(unit_a: str, unit_b: str) -> None:
    """量纲一致性断言(跨类换算防护的字符串层), 抛 UnitError。"""

def convert(value: float, from_unit: str, to_unit: str) -> float:
    """现有函数扩展: 支持复合单位(经 dims_of 一致性检查后 to_si/from_si 组合)。"""
```

### 4.3 backend `core/unitparse.py`(新增,签名)

```python
"""非标准单位字符串解析(审查意见第 0 条): "1000 kW" / "1.5MWh" / "3 元/kWh" → Quantity。
词法文法见方案 §3;本模块只依赖 core.units, 无业务依赖。"""

NUMBER_RE: Final[re.Pattern]     # 见 §3.1 number 文法(含千分位/科学计数/万后缀)
MULTIPLIERS: Final[dict[str, float]] = {"百": 1e2, "千": 1e3, "万": 1e4, "亿": 1e8}

class UnitParseError(AppError):
    """单位字符串解析失败。code=PARAM-UNIT-001, message_key=ies.diag.param.unit_parse。
    params={"text","position","expected","suggestions"}。"""

def parse_unit_string(s: str) -> tuple[str, float]:
    """单位串 → (规范形, 组合系数)。内部实现 §3.2 算法步骤 2-5。
    parse_unit_string("CNY/kW·月") == ("CNY/kW·月", 1/(1e3*2.592e6))。"""

def parse_quantity(text: str, *, context: str | None = None) -> Quantity:
    """唯一解析入口。context=期望单位(注册表 ParameterSpec.unit / 数据集声明单位),
    纯数字时兜底; 数值换算到 SI 见 Quantity.si_value。"""
```

### 4.4 frontend 镜像 `frontend/src/lib/units.ts` + `units.json`

```typescript
// frontend/src/lib/units.ts(新增; 与 backend core/units.py 逐函数对齐)
export interface UnitMeta {
  unit: string; category: string; siUnit: string; toSi: number;
  dims: Record<string, number>; precision: number; symbolZh: string; symbolEn: string;
}
export interface Quantity { value: number; unit: string; siValue: number; siUnit: string }

export const UNITS: Record<string, UnitMeta>        // 从 units.json 加载, 启动时自检
export function normalizeUnit(unit: string): string // 与后端同规则
export function parseQuantity(text: string, contextUnit?: string): Quantity
export function toSi(value: number, unit: string): number
export function fromSi(siValue: number, unit: string): number
export function dimsOf(unit: string): Record<string, number>
export function formatValue(value: number, unit: string, lang: 'zh' | 'en'): string
```

- 数据同步机制:`backend/tools/gen_units_json.py`(新增)从 `core.units.UNITS` 导出 `frontend/src/lib/units.json`(含 `schema_version` 字段);CI/前端测试比对 `schema_version` 与换算系数,不一致即失败。后端为唯一权威。
- 前端使用点(改造):`ConfigPage.tsx` 输入框失焦 `parseQuantity(v, contextUnit)` → 标准值回填 + unit 展示;`pctToDecimal/percentText`(ConfigPage.tsx:184-187,652)由 `parseQuantity("%")`/`formatValue` 取代;`buildInput`(:605-611)回传 `unit`(来自默认变量/注册表元数据);`types.ts` ConfigVariable 增加 `unit: string | null`。

---

## 5. 计算边界默认标准单位的落点

### 5.1 三层契约与数据流

```
[L1 业务单位] 注册表 ParameterSpec.unit / 变量 unit 字段 / 数据集声明 unit
      │  (数值: 注册表单位)
[校验] config.py: 数值类型 + normalize_unit 归一 + dims_of 量纲(表达式约束)
      │  快照落库(calc_configs, 含 unit)
[L2 SI 基准]  ← 唯一边界: worker/boundary.py
      │   plan_to_si(设备参数) + data_to_si(逐时数据)
      ▼
[引擎] eval_run/planning: 全部量纲 SI(W/J/K/CNY/s/kg/m³), 零硬编码换算
      │  输出: flows(SI)+ KPI(引擎尾部 from_si 集中换算回业务单位)
[L3 展示] executors payload: hourly meta 逐字段单位契约; 前端 formatValue 渲染
```

换算只存在于两个点:**(a) worker 边界业务→SI(输入)**;**(b) 引擎 KPI 构造处 SI→业务(输出)**。`grep` 验收:全库不再出现 `* 1000.0`、`KWH_TO_J`、`W_TO_KW` 等引擎内换算。

### 5.2 新增 `worker/boundary.py`(唯一换算层)

```python
"""业务单位 ↔ SI 换算唯一边界层(审查意见第 0 条)。

职责:
- plan_to_si:  设备参数(注册表业务单位数值)→ SI 数值(W/J/K/CNY/s)
- data_to_si:  逐时数据(数据集声明单位)→ SI 数组
- hourly_meta: 引擎逐时输出字段 → 逐字段单位契约(替代 executors.py:267 硬编码串)
禁止在引擎/执行器其它位置出现 ×1000 / KWH_TO_J 类换算。
"""

# 逐时数据引擎字段 → 声明单位(与 services/dataset.py STANDARD_FIELDS 一致, 此处为引擎侧契约)
DATA_FIELD_UNITS: Final[dict[str, str]] = {
    "e_load": "kWh", "h_load": "kWh", "c_load": "kWh",   # 每步能量 → W(=J/步长秒)
    "temperature": "C", "ghi": "W/m²",
    "tariff_buy": "CNY/kWh", "tariff_sell": "CNY/kWh",
    "emission_factor_grid": "kg/kWh", "gas_price": "CNY/m³",
}

def plan_to_si(plan: dict) -> dict:
    """_build_plan 输出(设备 params 业务单位数值)→ SI 计划。
    返回 {"devices": [{"type", "params_si": {param: SI值}, "is_new"}], "meta": {"converted": n, "warnings": []}}
    单位来源: core.registry.resolve_device_type(dev["type"]).parameters[name].unit(禁止自建映射表);
    每参数 units.to_si(value, unit); 未注册参数名走 §9 名称后缀推断表, 无法推断抛 UnitError。"""

def data_to_si(data: dict[str, np.ndarray], resolution: str) -> dict:
    """逐时数据 → SI 数组。能量型字段: to_si(arr, "kWh")/step_seconds(step_seconds=
    步长分钟×60); 电价: to_si(arr, "CNY/kWh")→CNY/J; 温度: to_si(arr, "C")→K;
    排放因子: to_si(arr, "kg/kWh")→kg/J; ghi(W/m²)/体积类直通。返回全部 SI 字段。"""

def hourly_meta(fields: list[str]) -> dict:
    """逐时流字段 → {"unit": 业务单位, "si": SI 单位} 契约(由引擎输出字段表驱动, 见 5.6)。"""
```

### 5.3 runner.py 数据集换算改造

- `_merge_rows`(runner.py:158-186):删除 `arr * 1000.0 / step_hours`(:176-177)与逐列手写换算;改为产出"声明单位数组"并记录 `field_units`;`load_inputs`(runner.py:68-112)末尾调用 `boundary.data_to_si(data, resolution)` 得 SI 数据。温度列 `t_ambient` 保持 `C` 声明(换算在 data_to_si)。
- 数据集声明单位校验(dataset.py:866)改造:由"字符串相等"改为"`normalize_unit` 可解析 + `dims_of` 与 `STANDARD_FIELDS` 声明一致"(如声明 `KWH` 等价 `kWh` 通过;声明 `kW` 与期望 `kWh` 量纲不同 → `PARAM-UNIT-002` 阻断);数值换算不再在解析层做,统一在计算边界。

### 5.4 eval_run.py / devices.py 去硬编码改造

- 删除模块常量 `KWH_TO_J`、`W_TO_KW`(eval_run.py:59-61)及全部调用点换算:
  - `:435-436` `max_import_power_kw*1000.0` → 直接消费 `plan_to_si` 后的 `c_import_w`
  - `:476` `rated_capacity_kwp*1000.0`、`:486` `inverter_capacity_kw*1000.0` → `params_si`
  - `:500` `capacity_kwh*KWH_TO_J`、`:509-510` `rated_power_kw*1000.0` → `params_si`
  - `:516` `rated_heat_kw*1000.0`、`:562-563` `lhv_kj_per_m3*1000.0`、`:570` `rated_cooling_kw*1000.0` → `params_si`
  - `:464-466` `gas_price`(CNY/m³)保留体积计价,由 plan_to_si 换算为 CNY/m³(SI 基准)后直通
- 引擎接口语义:`evaluate_plan(plan, data, axis, options)` 签名不变,但 `plan["devices"][i]["params"]` 与 `data` 全部为 SI 数值(文档注释更新);新增 `_param_si(dev, name, default)` 读取 SI 值。
- 温度:逐时 `temperature` 为 K;`devices.py` 接口改为 K:
  - `pv_output`:入口 `ta_c = temperature - 273.15` 一处换算,公式其余不变(NOCT 定义 °C);
  - `heat_pump_cop`:删除内部 `ta_k = ta_c + 273.15`(:160),输入即 K。
- KPI 输出集中换算:引擎 KPI 构造处(eval_run.py:852-876)统一经 `units.from_si` 转业务展示单位(`annual_buy_kwh` 等 = SI J → kWh;金额 CNY 直通;`co2` kg → 展示 tCO2 由装配层决定),换算只此一处。

### 5.5 config.py 校验与量纲改造

- 删除 `_dims_for_unit`(config.py:393-398),`_validate_expression_constraint`(:787-791)改用 `units.dims_of(v["unit"])`;表达式量纲检查对 `kW`/`kWh`/`CNY/kWh` 等真实变量生效(修复"前端保存→后端校验"路径静默失效)。
- `_validate_variables`(:559-704):continuous/integer 变量 `unit` 必填且 `normalize_unit` 通过,否则 `PARAM-UNIT-002`;保存入库前归一化(`unit: normalize_unit(v["unit"])`)。
- `_validate_parameters`(:451-556):单位由注册表声明、不随请求覆盖(现行为保持);数值类型检查不变。
- `_default_variables`(:333-343)已带 unit,继续保留;`configToServer` 前端透传 variables(含新 unit 字段)后端按上述校验。

### 5.6 输出侧单位

- `executors.py:267` 硬编码 `"W(W) / kWh(energy) / 0-1(ratio)"` 删除,改 `boundary.hourly_meta(fields)` 逐字段契约,示例:

```json
{"meta": {"resolution": "1h", "n": 8760,
  "units": {
    "e_import":      {"unit": "W",  "si": "W"},
    "e_battery":     {"unit": "J",  "si": "J"},
    "soc":           {"unit": "-",  "si": "1"},
    "pv_gen":        {"unit": "W",  "si": "W"}}}}
```

  字段清单取自 eval_run flows 键集,按类别映射(功率类 W、能量类 J、比例类 `-`);`flows` 数值保持 SI 不变(结果视图 read_hourly 由前端 `fromSi`/`formatValue` 按 meta 渲染)。
- KPI 装配(`_eval_payload`/`_planning_payload`/uncertainty payload):键值已是业务单位(引擎尾部换算),`meta.units` 补充 KPI 单位表,供结果页渲染。

### 5.7 不确定性分布单位

- `task_params.distributions` 的 `t_ambient` 扰动:`sigma_abs`/`amplitude_abs` 文档声明单位 °C(1°C=1K 差值,数值不变),分布 spec 增加显式 `"unit": "C"` 字段(executors.py:605,624-629 注释同步);其余乘性键(SI 数组上相乘,量纲不变)。

---

## 6. 数据结构与 JSON 形态汇总

```python
# core/units.py
@dataclass(frozen=True, slots=True) class UnitSpec:   # 现有,字段不变
@dataclass(frozen=True, slots=True) class Quantity:   # 新增,见 §4.2

# core/registry.py
@dataclass(frozen=True, slots=True) class ParameterSpec:  # unit 字段不变;
    # 新增约束(运行时校验): unit 必可被 normalize_unit 解析(load_registry 时全量自检, 失败拒绝加载)

# worker/boundary.py(新增)
# plan_to_si(plan: dict) -> dict          # {"devices":[{type, params_si, is_new}], "meta": {...}}
# data_to_si(data: dict, resolution) -> dict
# hourly_meta(fields: list[str]) -> dict

# frontend/src/lib/units.ts + units.json(新增, 见 §4.4)

# 数据形态(JSON):
#   variables[] 行: {name, type, initial, min, max, unit(必填, continuous/integer), device_ref?, param?}
#   parameters.devices.<key>.<param>: 数值(单位由注册表声明)
#   数据集声明: {"columns": {"e_load": {"unit": "kWh", ...}}}
#   逐时结果 meta.units: {field: {"unit": 业务单位, "si": SI 单位}}
```

---

## 7. 改造映射表(现有调用点 → 新调用)

| 现状 | 位置 | 改造 |
|---|---|---|
| 死代码 convert/energy_to_joules/... | core/units.py:181-221 | 扩展为全库唯一换算入口(to_si/from_si 薄封装) |
| 注册单位串 kWp/CNY/kW·月/... 未注册 | core/registry.py:199,236,254,300,339 等 | §2.2/§2.3 增补;`load_registry` 自检 normalize 通过 |
| `_dims_for_unit` 精确查表 | services/config.py:393-398 | 删除,改 `units.dims_of` |
| 量纲检查取 unit=None 失效 | services/config.py:787-791 | unit 必填 + dims_of(§5.5) |
| 变量 unit 字段丢失 | frontend/src/types.ts:450-459;ConfigPage.tsx:605-611;client.ts:620-635 | 类型加 unit;buildInput 回传;透传 |
| 百分比手工 ÷100 | ConfigPage.tsx:184-187,652 | `parseQuantity("%")`/`formatValue` 取代 |
| eval_run 硬编码 ×1000/KWH_TO_J | eval_run.py:435,476,486,500,509-510,516,562-563,570 | 全部删除,消费 `plan_to_si` 输出 |
| runner 数据集 kWh/步→W | worker/runner.py:176-177 | `data_to_si` |
| 数据集声明单位字符串相等校验 | services/dataset.py:866 | normalize+dims 一致性校验 |
| 结果 meta 硬编码单位串 | worker/executors.py:267 | `hourly_meta` 逐字段契约 |
| 设备参数原样透传不换算 | worker/executors.py:794-816 `_build_plan` | 输出后接 `plan_to_si`(execute_calc/execute_plan 共用) |
| 引擎温度 °C 隐式 | engines/devices.py:160 等 | 接口改 K,内部一处换算(§5.4) |
| t_ambient 扰动无单位声明 | worker/executors.py:605,624-629 | distributions 增 `"unit":"C"` |

---

## 8. 实施步骤(每步含验收)

**P1 后端单位内核**:`core/units.py` 扩展(§2.2 注册表、Quantity/normalize_unit/to_si/from_si/dims_of/unit_meta、convert 复合支持)+ `core/unitparse.py`(§3)+ 测试 `backend/tests/test_units_ext.py`、`test_unit_parse.py`(§3.3 表全量)。
验收:示例表全绿;`to_si(40,"CNY/kW·月")`、`dims_of("tCO2/万m³")` 精确;`normalize_unit("kWp")=="kW"`。

**P2 注册表与校验**:registry 单位串归一化(kWp→kW 等)+ `load_registry` 自检;config.py 校验改造(unit 必填/归一化/dims_of);服务端保存时回填缺失 unit(§9)。
验收:`test_registry_units.py`(全注册表 unit 可 normalize);表达式约束 `pv_cap_kw * price_cny_per_kwh` 类量纲检查生效(错误表达式报量纲不一致)。

**P3 前端**:`frontend/src/lib/units.ts` + `units.json`(gen_units_json.py)+ ConfigPage 输入解析/buildInput 带 unit/client 透传。
验收:输入 `"1.5MWh"` 失焦回填 `1500`+`kWh`(按 context);保存后 `calc_configs.variables[].unit` 存在且为规范形。

**P4 计算边界**:`worker/boundary.py` + runner `data_to_si` + eval_run/devices 去硬编码 + executors 接入。
验收:`grep -rn "KWH_TO_J\|1000.0\|W_TO_KW" backend/iesplan/engines/ backend/iesplan/worker/` 仅 boundary.py 出现;黄金算例(固定输入)改造前后 KPI 数值一致(浮点误差 <1e-9 相对);温度 K 改造后 PV 出力/COP 与基线一致。

**P5 输出侧**:hourly_meta + KPI 换算集中 + 不确定性单位声明。
验收:meta.units 逐字段;结果页单位渲染正确。

**P6 端到端回归**:E2E 覆盖"前端输入 `"1000 kW"` 保存 → 快照(unit 落库)→ calc 任务 → 结果 KPI 单位正确";数据集声明 `KWH` 与 `kWh` 等价通过、声明 `kW` 阻断。

---

## 9. 兼容与迁移

1. **存量配置 variables 无 unit**:后端保存/加载时自动回填——有 `device_ref+param` 的查注册表;无 device_ref 的自定义变量按名称后缀推断表(`kw`→kW、`kwp`→kW、`kwh`→kWh、`mw`→MW、`kwh`→kWh、`deg`→deg、`s`→s、`a`→a、`kv`→kV),无法推断的发出 warning 且该变量不参与量纲检查(降级为现行为),不拒绝旧配置;新保存的 continuous/integer 变量无 unit 则 `PARAM-UNIT-002` 报错。
2. **API 兼容**:`variables[].unit` 为可选入参(后端回填),`unit_meta`/`format_value` 为新增端点字段,不改既有响应必填项。
3. **数据集旧文件**:声明单位缺失时按 `STANDARD_FIELDS` 默认;非规范但等价声明(`KWH`、`元/kWh`、`℃`)经 normalize 通过,数值换算在边界完成,历史文件行为不变。
4. **USD**:汇率非固定换算,`convert`/`to_si` 对 currency 跨币种(CNY↔USD)抛 `UnitError`,汇率走经济参数配置(现状行为保持)。
5. **展示层**:金额仍用 Decimal 重算(CONTRACT §3 现状),`Quantity` 为 float 仅计算链路使用。
