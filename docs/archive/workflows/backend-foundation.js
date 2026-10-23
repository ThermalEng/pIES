export const meta = {
  name: 'iesplan-backend-foundation',
  description: '后端基础层: 配置/DB模型/核心工具/计算引擎/指标 + 集成验证',
  phases: [
    { title: 'Implement', detail: '5 个实现 agent 并行(互不重叠的目录)' },
    { title: 'Integrate', detail: '构建镜像并跑通全部测试' },
  ],
}

const WORK = [
  {
    key: 'config-db',
    prompt: `你是 pIES 后端的数据层实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md (实现契约, 必须遵守, 尤其是第2节接口签名与第3节数据约定)
- /home/mc/Documents/工作文档/pIES/docs/spec/01-db-schema.md (数据库权威事实关系模式, 全部 41 张表的列/约束/索引定义)

## 任务: 只写以下文件(不得写其他路径)
1. iesplan/config.py: pydantic-settings Settings 类, env_prefix=IESPLAN_, 字段:
   - db_url: str (默认 postgresql+psycopg://iesplan:iesplan_dev_password@localhost:5432/iesplan)
   - redis_url: str (默认 redis://localhost:6379/0)
   - data_dir: Path (默认 /data)
   - secret_key: str (默认 dev-only-secret-change-me)
   - app_url: str (默认 http://localhost:8080)
   - worker_type: str = compute (compute|io)
   - compute_slots: int = 2
   - task_timeout_hours: int = 8
   - session_ttl_minutes: int = 480
   - default_admin_password: str (仅首启种子用, 默认 iesplan-admin-initial)
   - storage_min_free_bytes: int = 2000000000 (2GB 安全阈值)
   - debug: bool = False
   - 提供 @property sqlalchemy_url 或直接兼容
2. iesplan/db.py:
   - engine = create_engine(settings.db_url, pool_pre_ping=True), SessionLocal
   - Base = DeclarativeBase
   - get_db() FastAPI 依赖 (yield session, close)
   - init_db(): Base.metadata.create_all(engine) + 幂等 seed_admin()
   - seed_admin(password=None): 若 users 表无管理员则创建 (username=admin, role=admin, force_password_change=True, 初始密码用 settings.default_admin_password 或参数)
3. iesplan/models/__init__.py 与 iesplan/models/*.py (可按域拆多个文件: identity.py, project.py, model.py, dataset.py, calc.py, result.py, uncertainty.py, audit.py):
   - 按 01-db-schema.md 全部 41 张表实现 SQLAlchemy 2.0 ORM (Mapped/mapped_column)
   - 表清单(01 的章节号): 第1节身份(users, roles, user_roles, credentials, window_sessions, auth_events) 第2节权限(project_members, ownership_transfers, admin_maintenance_actions) 第3节项目(projects, drafts, project_versions, version_refs) 第4节模型(system_graphs, devices, ports, connections) 第5节数据集(datasets, dataset_versions, dataset_files) 第6节计算配置(calc_configs) 第7节快照任务(calc_snapshots, tasks, task_attempts, task_leases, task_progress, task_diagnostics, compute_slots) 第8节结果(evidence_packages, result_assessments, result_index, result_selections, reports) 第9节不确定性(uncertainty_snapshots, sample_tasks, sample_records) 第10节审计对象(objects, object_refs, audit_log, import_proposals, retention_rules)
   - 严格按文档: 列名/类型(如 Numeric(18,4), TIMESTAMPTZ 用 DateTime(timezone=True))/默认/约束
   - CHECK 约束用 __table_args__ CheckConstraint
   - 部分唯一索引(每账号最多一个 active window_session, 每项目一个当前草稿, 每槽一个当前任务等)用 Index(..., unique=True, postgresql_where=...)
   - 不可变表(project_versions, evidence_packages, result_assessments, audit_log, auth_events, calc_snapshots 等): 提供常量集合 IMMUTABLE_TABLES 供工具使用; 触发器 SQL 以字符串常量写入 iesplan/models/immutable_triggers.py (按 01 第0节的三道防线, 生成所有不可变表的触发器 DDL)
   - 时间列全部 DateTime(timezone=True) (TIMESTAMPTZ)
   - 枚举用 String + CheckConstraint (与文档一致)
   - 最后在 iesplan/models/__init__.py 导出全部模型类

## 测试
写 tests/test_models.py (SQLite :memory: 或 postgres 不可用时 skip), 主要验证: 模型可导入、约束字符串正确、关键模型字段存在。tests/test_config.py 验证 Settings 读取环境变量。

## 注意
- 不要创建 alembic 目录, 本阶段用 create_all
- 不要写 iesplan/api/, iesplan/core/, iesplan/engines/, iesplan/metrics/, iesplan/worker/
- 用中文注释; 类型标注完整; 完成后报告创建的文件清单`,
  },
  {
    key: 'core',
    prompt: `你是 pIES 后端的核心工具层实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md (实现契约 第2节接口签名必须精确实现, 第3节数据约定)
- /home/mc/Documents/工作文档/pIES/docs/spec/02-calc-model.md (时间轴 第1节, 单位 第2节)
- /home/mc/Documents/工作文档/pIES/docs/spec/04-registry-diagnostics.md (注册表 第2-3节, 表达式 第4节, 诊断 第5节, 单位 第8节)

## 任务: 只写 iesplan/core/ 下的模块(不得写其他路径)
1. iesplan/core/units.py: 按 CONTRACT 第2节接口: convert/energy_to_joules/power_to_watts/temperature_kelvin/format_value + 单位注册表(能量 J/kWh/MWh/GJ, 功率 W/kW/MW, 温度 K/C, 金额, 时长, 角度) 含中英展示格式
2. iesplan/core/timeaxis.py: TimeAxis dataclass + build_axis + validate_timestamps (按 02 第1节: 365 天非闰年, 15/30/60 分钟步长, n=35040/17520/8760, 固定 UTC 偏移, hour_of_year/day_of_year/season 数组; validate_timestamps 返回 Diagnostic 列表: 行数不匹配/乱序/重复/越界)
3. iesplan/core/diagnostics.py: Diagnostic dataclass + make_diag (按 04 第5节: code 域-类别-编号, severity blocking/error/warning/info, message_key, params, location, fix_hint_key, ref_ids) + 诊断码目录常量(04 第5.3节中的 DATA-, CONN-, PARAM-, TASK-, SYS-STORE- 等已登记码)
4. iesplan/core/errors.py: AppError/ForbiddenError/NotFoundError/ConflictError (携带 code/severity/blocking/message_key/params/location)
5. iesplan/core/idgen.py: new_id/new_idempotency_key/sha256_hex
6. iesplan/core/security.py: hash_password(bcrypt via passlib)/verify_password/check_password_strength(至少8位含大小写数字)/new_session_token/token_hash
7. iesplan/core/registry.py: 设备类型注册表 (按 04 第3节的 9 类设备: grid_connection, pv, battery, electric_load, heat_load, cooling_load, heat_pump, gas_boiler, electric_chiller) 每类含参数 schema(参数名/单位/范围/默认值/存量默认/是否可优化/帮助键); get_device_type/list_device_types/get_algorithm(算法注册: 默认 ies.algo.milp_hybrid, 记录 id/version/能力/参数); 受控加载流程(模块内静态注册 + 版本校验, 不做动态导入)
8. iesplan/core/expression.py: 受限表达式引擎 (按 04 第4节): 安全 AST 解析(只允许白名单节点: 数字/变量/算术/比较/逻辑), 白名单函数(abs/min/max/sin/cos/tan/exp/log/sqrt/pow), 量纲与范围校验, 禁止 eval/exec/属性访问/调用非白名单/导入; 实现 parse_expr(text, allowed_vars) 返回 CompiledExpr, CompiledExpr.eval(values: dict) 返回 float; 安全评估说明在 docstring

## 测试
tests/test_units.py, tests/test_timeaxis.py, tests/test_diagnostics.py, tests/test_security.py, tests/test_expression.py, tests/test_registry.py: 每个模块的单元测试, 关键行为覆盖(换算正确性/时间轴 n 与数组长度/诊断字段/密码哈希往返/表达式拒绝恶意输入/设备类型齐全)

## 注意
- numpy/scipy 可用; 不依赖 DB
- 诊断码只引用 04 第5.3节已登记码; 如需新码在模块常量 NEW_DIAG_CODES 中集中声明
- 中文注释, 类型标注完整; 完成后报告文件清单`,
  },
  {
    key: 'engines',
    prompt: `你是 pIES 后端的计算引擎实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md (第2节接口签名: TimeAxis, SolveResult, solve_milp, solve_lp, evaluate_plan, EvalResult; 第3节数据约定)
- /home/mc/Documents/工作文档/pIES/docs/spec/02-calc-model.md (第1节时间轴, 第3节能量平衡, 第4节设备模型, 第5节 MILP 构建, 第7节任意方案评价, 第8节逐时结果字段, 第9节收敛停止)
- /home/mc/Documents/工作文档/pIES/docs/spec/04-registry-diagnostics.md (第3节设备参数 schema)

## 任务: 只写 iesplan/engines/ 下的模块(不得写其他路径)
1. iesplan/engines/solver.py: scipy.optimize.milp/linprog (HiGHS) 封装:
   - solve_milp(c, integrality, bounds, constraints) 返回 SolveResult; 输入用 scipy.optimize.LinearConstraint / Bounds
   - solve_lp 类似
   - SolveResult: status(ok/infeasible/unbounded/time_limit/numerical_failure), objective, x(np.ndarray), gap(可行+有界时计算), stop_reason, raw
   - 时间上限: 支持 timeout 参数(scipy 的 time_limit 秒)
   - 残差与容差约定按 02 第9节
2. iesplan/engines/balance.py: 三母线平衡矩阵构建器(按 02 第3节):
   - build_electric_balance(...) 返回 LinearConstraint (购电+光伏+电池放电 = 电负荷+热泵耗电+制冷机耗电+泵耗电+电池充电+售电)
   - 热平衡/冷平衡同
   - 禁止反送电(p_grid_sell = 0), 并网容量约束
   - 负荷满足(默认不削减; 允许削减时 penalty 参数 + 显著报告标记)
   - 输配损耗 lambda 系数
3. iesplan/engines/devices.py: 设备出力参数化(按 02 第4节):
   - pv_output(ghi_series, capacity, temperature, eff=0.20, temp_coeff, tilt, azimuth) 返回 np.ndarray 逐时出力(按 02 第4.3节公式: P = GHI*eff*C*(1-tc*(Tc-25)); Tc 用 NOCT 近似)
   - heat_pump_cop(temperature_series, mode, cop_min=2.0, cop_max=6.5, ref_temp) 返回 np.ndarray (卡诺近似/线性插值, 按 02 第4.5节)
   - boiler_output / chiller_output 基本线性
   - 电池 SOC 递推 simulate_battery(p_bat_ch, p_bat_dis, capacity_kwh, soc0, eta=0.95, soc_min=0.10, soc_max=0.90) 返回 soc 数组(确定性模拟, 供验证用; 优化器内用线性约束)
4. iesplan/engines/eval_run.py: 任意方案评价引擎(按 02 第5/7节):
   - evaluate_plan(plan: dict, data: dict, axis: TimeAxis, options) 返回 EvalResult
   - plan: 设备实例列表(类型/容量/参数/存量或新增); data: 逐时数据 dict(电/热/冷负荷 W, 温度 C, GHI W/m2, 分时电价(元/kWh 数组或时段), 天然气价, 排放因子)
   - 构建运行 MILP: 变量为逐时功率流(电网购/售、电池充/放、热泵热/冷、锅炉、制冷机、泵耗、损耗), 二进制为电池充放互斥(每步)
   - 目标: 最小化运行成本(购电费 - 售电收入 + 燃气费)
   - 输出 EvalResult: 逐时流字段(字段名按 02 第8节: p_grid_buy, p_grid_sell, p_pv, p_bat_ch, p_bat_dis, soc, p_hp_heat, p_hp_cool, p_hp_elec, p_boiler, p_boiler_gas, p_chiller, p_chiller_elec, p_pump, e_load, h_load, c_load 等) + kpi dict(年购电量 kWh, 年售电量, 自用率, 总运行费用, 购电费用, 燃气费用, 碳排放 kg, 峰值购电 kW, 需量, 最大 SOC 范围等) + diagnostics
   - 15/30/60 分钟步长均可用 (时间步长因子 step_minutes/60 用于能量换算)
   - 注意: 精确金额在计算后用 Decimal 转, 矩阵用 float
5. iesplan/engines/planning.py: 规划引擎(简版, 按 02 第5节):
   - 策略: 新增设备容量离散网格枚举(如 0..max 步长) + 对每个容量组合调用 evaluate_plan 求运行成本 → 现金流 → IRR; 返回候选列表(容量, IRR, NPV, 年运行成本)排序
   - 用于当前单目标 IRR 最大化; 硬约束: 最低税后项目 IRR (irr_floor); 过滤不满足者
   - 网格步长可配, 防组合爆炸(上限组合数, 超出时按项目优先级抽样)
   - 输出 PlanningResult: candidates(list), best, status(ok/no_feasible), diagnostics

## 测试
tests/test_solver.py, tests/test_balance.py, tests/test_devices.py, tests/test_eval_run.py, tests/test_planning.py:
- solver: 简单 LP/MILP 解析解验证
- balance: 守恒系数行正确
- devices: PV 在给定 GHI 下的输出量级正确; COP 单调性
- eval_run: 构造 24 步(或 4 步)迷你算例, 手算校验电平衡与费用; 电池充放互斥生效
- planning: 迷你算例跑通, 候选排序正确
- 全部用 numpy 手算断言, 不依赖 DB

## 注意
- 只用 numpy/scipy, 不引入其他求解器库
- 中文注释; 类型标注完整; 完成后报告文件清单`,
  },
  {
    key: 'metrics',
    prompt: `你是 pIES 后端的财务/环境/工程指标实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md (第2节接口签名: IRRStatus, cashflow_irr, operational_emissions; 第3节数据约定: 金额 Decimal)
- /home/mc/Documents/工作文档/pIES/docs/spec/02-calc-model.md (第5.2-5.4节目标函数与现金流, 第8节逐时结果与 KPI)
- /home/mc/Documents/工作文档/pIES/docs/spec/01-db-schema.md (第8.2节 result_assessments 四维状态枚举)

## 任务: 只写 iesplan/metrics/ 下的模块(不得写其他路径)
1. iesplan/metrics/financial.py:
   - IRRStatus 枚举: unique/none/multiple/degenerate/out_of_domain/numerical_failure (按 02 第5.2节与 RPD REQ-FIN-005)
   - cashflow_irr(cashflows: list[Decimal] | list[float]) 返回 (rate|None, IRRStatus, message): 用数值方法(牛顿+二分/多项式), 完整分类: 无解(none, 符号无变化), 多解(multiple, 符号变化>1), 退化(degenerate, 现金流全零/常数), 超出定义域(out_of_domain, 无正实根), 数值失败(numerical_failure)
   - npv(rate, cashflows) 返回 Decimal
   - 项目投资现金流构建: build_project_cashflows(investment, annual_om, annual_energy_saving, revenue, tax_rate, depreciation_years, discount_rate, project_years, salvage) 返回 list[Decimal] (按 02 第5.4节: 初始投资, 年运行节省-运行成本-税+折旧税盾, 期末残值)
   - 税后项目投资 IRR 与税后资本金 IRR 分开的函数签名
   - 增量现金流语义: 只计算新增部分, 基准方案收益参照 (文档说明 + 参数 baseline_annual_cost)
2. iesplan/metrics/environmental.py:
   - operational_emissions(energy_flows: dict, factors: dict, boundary: str, factor_version: str) 返回 dict: 输入逐时/年度能量流(电网购电 kWh, 燃气 m3 等)与排放因子(kgCO2/kWh, kgCO2/m3), 输出 {total_kg, by_fuel, boundary, factor_version, data_refs}
   - 排放边界与因子版本必须绑定输出(按 02 第8节与 RPD REQ-ENV-001)
3. iesplan/metrics/engineering.py:
   - 工程指标: energy_balance_summary(flows dict) 返回 电/热/冷年度平衡表(生产-消费-损耗-残差); peak_demand(series, resolution); capacity_utilization(capacity, annual_energy, resolution); load_met_ratio(delivered, required) 返回 ratio + 未满足量
   - 输出每个指标带 definition_version, unit, refs (按 RPD REQ-RESULT-002)
4. iesplan/metrics/validity.py:
   - 四维结果状态模型(按 RPD 第10.4节与 01 第8.2节): PhysicalValidity(passed/restricted/failed/na/insufficient), OptimalityValidity(同), FinancialValidity(passed/... + irr_status 细分), ReliabilityStatus(not_executed/partial/insufficient/ok)
   - summarize_four_dimensions(...) 返回 dict: 只派生 可用/受限使用/不可用 摘要, 不隐藏任一维度 (按 RPD 核心不变量 4)

## 测试
tests/test_financial.py, tests/test_environmental.py, tests/test_engineering.py, tests/test_validity.py:
- cashflow_irr 已知解验证(如 [-1000, +1100] 应约 10%); 无解/多解/退化各构造用例
- NPV 手算验证
- build_project_cashflows 现金流形状正确(期数, 符号)
- emissions 手算
- 平衡表: 构造守恒数据验证残差约等于 0
- 全部纯计算, 不依赖 DB

## 注意
- Decimal 用于金额; numpy 用于数组
- 中文注释; 类型标注完整; 完成后报告文件清单`,
  },
  {
    key: 'api-stub',
    prompt: `你是 pIES 后端的应用入口实现者。工作目录 /home/mc/Documents/工作文档/pIES/backend。

## 先读(必读)
- /home/mc/Documents/工作文档/pIES/docs/CONTRACT.md (整体约定)

## 任务: 只写以下文件(不得写其他路径)
1. iesplan/main.py: FastAPI 应用入口:
   - create_app() 返回 FastAPI: 挂载 CORS(允许同域, 凭据)、健康端点 GET /api/healthz (存活) 与 GET /api/readyz (就绪: 检查 DB 连接可用的最小实现, DB 不可用返回 503)
   - lifespan: 启动时 init_db() + seed_admin() (幂等)
   - 根路径 GET /api 返回 {name, version, docs}
   - 404 处理返回标准 JSON 错误 {error: {code, message_key, ...}}
   - 挂载后续阶段会实现的 API 路由(用 include_router, 本阶段只有 health)
   - 全局异常处理: AppError 对应 HTTP 状态 + 诊断 JSON; 未捕获异常返回 500 且不泄露堆栈(日志可含, 响应不含)
   - app = create_app() 模块级实例 (uvicorn 入口)
2. iesplan/api/__init__.py: 空包标记 + 版本导出

## 测试
tests/test_main.py: 用 fastapi TestClient (httpx), 验证 /api/healthz 200, /api 返回版本, 未知路由 404 JSON 结构, AppError 映射(直接构造一个小路由测异常处理器, 或单元测试异常处理函数)

## 注意
- 本阶段不实现业务 API, 只保证应用可启动、健康检查可用
- 中文注释; 类型标注完整; 完成后报告文件清单`,
  },
]

phase('Implement')
const results = await parallel(WORK.map((w) => () =>
  agent(w.prompt, { label: 'impl:' + w.key, phase: 'Implement', effort: 'high' })
))

phase('Integrate')
const done = results.filter(Boolean)
const integration = await agent(
  `你是 pIES 后端的集成验证者。工作目录 /home/mc/Documents/工作文档/pIES。

五个实现 agent 已并行完成基础层代码(目录互不重叠): config/db/models, core, engines, metrics, main/api-stub。现在需要你:

## 步骤
1. 先浏览代码结构(用 ls/find 查看 backend/iesplan 与 backend/tests), 读关键文件了解现状
2. 检查五个部分的接口是否按 docs/CONTRACT.md 对齐(签名/字段名/导入路径)。发现问题直接修改代码修复(小修); 若需要跨模块的大改, 记录问题列表
3. 用 Docker 构建测试环境并运行测试:
   - 在 /home/mc/Documents/工作文档/pIES 下执行: docker compose build backend  (只构建后端镜像, 不启动全部)
   - 然后运行: docker compose run --rm backend pytest -x -q 2>&1 | tail -30
   - 如果有失败, 修复代码后重跑, 直到全部通过(或记录无法修复的问题)
4. 补一个 smoke 测试: tests/test_smoke.py —— 用 TestClient 验证应用可启动, healthz 200(不依赖 DB 的场景下: 如果 readyz 依赖 DB, 在 smoke 里只测 healthz)
5. 运行 ruff 检查: docker compose run --rm backend ruff check iesplan tests (有错误则修复)
6. 输出最终报告: 各模块文件清单, 测试通过数/失败数, 遗留问题列表

## 注意
- 你修改代码时只改 backend/ 下文件
- 不要在主机跑 python, 全部在 docker 内
- 中文输出最终报告`,
  { label: 'integrate:backend', phase: 'Integrate', effort: 'high' }
)

log('基础层完成')
return { agentsDone: done.length, integrated: !!integration }
