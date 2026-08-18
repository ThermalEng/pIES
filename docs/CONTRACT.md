# IES Plan 后端实现契约 (Backend Contract)

本契约定义后端实现的目录边界与公共接口,供并行实现的多个 agent 遵守,避免相互冲突。
规格文档: docs/spec/01-db-schema.md, 02-calc-model.md, 03-task-scheduling.md, 04-registry-diagnostics.md

## 1. 目录边界(每个 agent 只写自己负责的路径,不得写入他人路径)

| 包 | 内容 | 依据 |
| --- | --- | --- |
| `iesplan/config.py` | pydantic-settings Settings, 环境变量前缀 IESPLAN_ | - |
| `iesplan/db.py` | engine, SessionLocal, Base, get_db(), init_db(), seed_admin() | 01 |
| `iesplan/models/*` | SQLAlchemy 2.0 ORM 模型(全部 41 表) | 01 |
| `iesplan/core/*` | units, timeaxis, diagnostics, errors, idgen, security, registry | 02/04 |
| `iesplan/engines/*` | balance, devices, solver, eval_run, planning | 02 |
| `iesplan/metrics/*` | financial, environmental, engineering, validity | 02 |
| `iesplan/api/*` | FastAPI 路由(后续阶段写) | - |
| `iesplan/worker/*` | Worker(后续阶段写) | - |
| `tests/*` | 对应各包的 pytest | - |

## 2. 公共接口签名(跨 agent 依赖,必须先满足)

```python
# iesplan/core/units.py
def convert(value: float, from_unit: str, to_unit: str) -> float
def energy_to_joules(value: float, unit: str) -> float        # kWh/MWh/GJ/J -> J
def power_to_watts(value: float, unit: str) -> float          # kW/MW/W -> W
def temperature_kelvin(value: float, unit: str) -> float      # K/C -> K
def format_value(value, unit, lang='zh') -> str               # 展示格式(不用于计算)

# iesplan/core/timeaxis.py
class TimeAxis:  # dataclass
    resolution: str            # '15min' | '30min' | '1h'
    n: int                     # 35040 | 17520 | 8760
    step_minutes: int          # 15 | 30 | 60
    utc_offset_minutes: int    # 项目固定偏移(如 480)
    t0_utc: datetime           # 非闰年 1 月 1 日 00:00 UTC
    hour_of_year: np.ndarray   # (n,) 0..8759
    day_of_year: np.ndarray    # (n,) 0..364
    season: np.ndarray         # (n,) 0=冬 1=春 2=夏 3=秋(按月份)
    def timestamp(self, i) -> datetime
def build_axis(resolution: str, utc_offset_minutes: int, t0_utc: datetime | None = None) -> TimeAxis
def validate_timestamps(timestamps: list[datetime], resolution: str) -> list[Diagnostic]

# iesplan/core/diagnostics.py
class Diagnostic:  # dataclass, 字段齐全
    code: str                  # 域-类别-编号, 如 DATA-TS-001
    severity: str              # blocking | error | warning | info
    blocking: bool             # 是否阻断
    message_key: str           # 如 ies.diag.data.ts_dup
    params: dict
    location: dict | None      # {object_type, object_id, field, row}
    fix_hint_key: str
    ref_ids: list[str]
def make_diag(code, severity, message_key, fix_hint_key, **kw) -> Diagnostic

# iesplan/core/errors.py
class AppError(Exception):  # 携带 code/severity/blocking/message_key/params/location
class ForbiddenError(AppError)
class NotFoundError(AppError)
class ConflictError(AppError)

# iesplan/core/idgen.py
def new_id(prefix: str = '') -> str        # 不可猜测随机 id (secrets.token_urlsafe)
def new_idempotency_key() -> str
def sha256_hex(data: bytes) -> str

# iesplan/core/security.py
def hash_password(password: str) -> str                      # bcrypt
def verify_password(password: str, password_hash: str) -> bool
def check_password_strength(password: str) -> tuple[bool, str]  # (ok, reason)
def new_session_token() -> str
def token_hash(token: str) -> str                            # sha256(token) 存库

# iesplan/core/registry.py
class DeviceTypeSpec:  # dataclass
    type_id: str             # 'ies.device.heat_pump' 等
    version: str             # '1.0.0'
    name_zh: str; name_en: str
    energy_carriers: list[str]     # ['electric','heat','cool','gas','solar']
    is_load: bool
    parameters: dict[str, ParameterSpec]  # 参数名 -> 规格(unit, min, max, default, is_optimizable, existing_default, help_key)
def get_device_type(type_id: str) -> DeviceTypeSpec
def list_device_types() -> list[DeviceTypeSpec]
def get_algorithm(name: str) -> AlgorithmSpec

# iesplan/db.py
def get_db() -> Generator[Session, None, None]   # FastAPI 依赖
def init_db() -> None                            # create_all + 幂等
def seed_admin(password: str | None = None) -> None  # 内置管理员(force_password_change=True)

# iesplan/metrics/financial.py
class IRRStatus(str, Enum):  # unique, none, multiple, degenerate, out_of_domain, numerical_failure
def cashflow_irr(cashflows: list[Decimal]) -> tuple[float | None, IRRStatus, str]
def project_irr(...)  # 按 02 §5.4 口径
# iesplan/metrics/environmental.py
def operational_emissions(energy_flows, factors) -> dict  # 排放边界+因子版本绑定
# iesplan/engines/solver.py
class SolveResult:  # dataclass: status(str), objective(float|None), x(dict), gap(float|None), stop_reason(str), raw(dict)
def solve_milp(model) -> SolveResult       # 封装 scipy.optimize.milp(HiGHS)
def solve_lp(model) -> SolveResult
# iesplan/engines/eval_run.py
def evaluate_plan(plan: dict, data: dict, axis: TimeAxis) -> EvalResult
class EvalResult:  # 逐时流 np.ndarray 各字段 + kpi dict + diagnostics list
```

## 3. 关键数据约定

- 时间列一律 TIMESTAMPTZ (UTC 存储); 项目固定偏移存 `fixed_utc_offset_minutes` (int, 分钟)。
- 金额: Python `Decimal`, DB `NUMERIC(18,4)`, 币种 `CHECK IN ('CNY','USD')`。
- 内部计算: numpy float64; 逐时数组形状 (n,); 设备流字段名见 02 §8 (p_grid_buy, p_grid_sell, p_pv, p_bat_ch, p_bat_dis, soc, p_hp_heat, p_hp_cool, p_boiler, p_chiller, e_load, h_load, c_load, ...)。
- 任务状态枚举: queued/running/completed/cancelling/cancelled/timed_out/failed。
- 业务结局枚举: normal_completion/no_recommendation/no_feasible_multi_objective/partial_batch/restricted_results/insufficient_evidence。
- 诊断码 域-类别-编号 (DATA-TS-001); 严重度 blocking/error/warning/info。
- 单位内部 SI: J, W, K; 展示 kWh/MW/°C。
- 算法: scipy>=1.13 (linprog/milp, HiGHS)。禁止引入 OR-Tools/PuLP。
- 时间轴: 标准非闰年 365 天, 无闰年, 无夏令时。
- 所有随机采样用 `numpy.random.default_rng(seed)`, 种子进入快照。

## 4. 代码风格

- Python 3.12, SQLAlchemy 2.0 (Mapped/mapped_column), Pydantic v2, FastAPI。
- 类型标注完整; ruff 默认规则 (E,F,I,B,UP); 行宽 110。
- 注释用中文, 简洁; 每个公开函数有 docstring。
- 测试: pytest, `tests/` 下, 不用真实 DB (SQLite :memory: 或 mock), 纯计算测试不依赖 DB。
- 不允许写其他文件夹; 不允许污染主机环境; 测试在 Docker 内运行。

## 5. 本阶段(基础层)交付清单

1. `iesplan/config.py`, `iesplan/db.py`, `iesplan/models/*`(全部 41 表, 含约束/索引/不可变触发器 SQL 文本常量)
2. `iesplan/core/*` 全部模块 + 内置设备注册表(9 类设备, 依据 04 §3)
3. `iesplan/engines/*` 求解器封装 + 平衡矩阵 + 评价引擎 (依据 02 §3-§8)
4. `iesplan/metrics/*` 财务(IRR 分类)/环境/工程指标 (依据 02 §5/§8)
5. `tests/*` 各模块单元测试, 可在 Docker 中 `pytest` 全部通过
