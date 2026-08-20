# 05 架构总览:五段解耦目标架构与实施定案

> 文档编号:docs/review-0820/05-architecture-overview.md
> 定位:前四份定案文档(01 单位标准化 / 02 设备初始化 / 03 模块解耦 / 04 装配检查)的总纲。
> **效力:本总览为实施 agent 的唯一裁决依据;各文档间不一致处,一律以本文第 7 节裁决为准,未列入裁决表的内容以对应专题文档为准。**
> 阅读顺序:先读本文,再按第 5 节优先级逐份实施。

---

## 1. 目标架构一句话

后端按「设备初始化 → 建模 → 装配与检查 → 计算 → 结果分析」五段解耦为 `devices / modeling / assembly / finance / analysis` 五个新包(0-4 层),复用并改造现有 `services / worker / engines / metrics / core / models`(5/3/0 层),依赖严格单向无环(第 4 节);单位换算全库收敛到 `core/units.py` 唯一入口(01);前端本次只做最小改动(unit 字段 + units 镜像库),后端独立性不被破坏,为未来外部 API 引用做准备(03 §10.3)。

各审查意见的落点:

| 意见 | 内容 | 落点文档/模块 |
|---|---|---|
| 第 0 条 | 单位标准化 | 01(全量)+ 03 §3(并入 01,裁决见 7.4) |
| 第 2 条 | 设备初始化模块 | 02(结构按 7.1/7.6 裁决并入 03 包布局) |
| 第 3 条 | 建模模块/后台调用命令 | 03 §5 |
| 第 4 条 | 装配与检查模块(边-端) | 04(格式按 7.2 裁决) |
| 第 5 条 | 计算模块改进(算法选择/容差/seed/planning 逐时输出) | 03 §9.3-9.5 |
| 第 6 条 | 财务计算模块(逐时→财务) | 03 §7 |
| 第 7 条 | 计算分析模块(wrapper 批量/敏感性) | 03 §8 |
| 第 10 条 | 前端边界/后端独立 | 03 §10(本次仅 unit 最小集) |

---

## 2. 五段流程总览与数据流向

### 2.1 五段总览

```
┌────────────┐   ┌────────────┐   ┌────────────────┐   ┌──────────────┐   ┌──────────────┐
│ ①设备初始化  │ → │ ②建模       │ → │ ③装配与检查     │ → │ ④计算         │ → │ ⑤结果分析      │
│ devices/   │   │ modeling/  │   │ assembly/      │   │ worker+      │   │ analysis/    │
│            │   │            │   │                │   │ engines+     │   │              │
│ yaml+prices│   │ ModuleCmd  │   │ 装配文本(YAML)  │   │ services/    │   │ 四维评估/敏感性 │
│ +csv →     │   │ 注册表      │   │ + 同步闸门检查   │   │ tasks        │   │ /evidence     │
│ DeviceSpec │   │            │   │ → plan(SI)     │   │ (复用改造)    │   │ (复用改造)     │
└────────────┘   └────────────┘   └────────────────┘   └──────────────┘   └──────────────┘
```

1. **设备初始化(1 层,`iesplan/devices/`)**:输入为数据文件而非代码——`devices/catalog/<id>.yaml`(每设备一个,含参数/端口/状态/时间序列声明与 `model_method`/`stateful` 标志)+ `devices/catalog/<id>.csv`(标准时间序列)+ `devices/catalog/prices.yaml`(价格/成本/税收单一事实源)。经 `loader` 装载 → 运行期 `DeviceRegistry`(插件式热加载,`core/registry.py` 静态 9 类退化为兜底)。产出 `DeviceSpec`(含 `$price:` 已解析的参数默认值)。
2. **建模(2 层,`iesplan/modeling/`)**:消费 DeviceSpec(+ 标准 csv),产出**标准化后台调用命令** `ModuleCommand`(`ies.command.model.<type>.<method>.<version>`,含 `function_ref` env path、输入/输出字段规格、状态标志),注册进全局命令表;机理函数自 `engines/devices.py` 迁入 `modeling/functions.py`(SI 单位),数据方法(`data_repeat` 周期重复 / `data_predict` 预测模型)由 `datadriven.py` 封装。计算模块按命令分发,不再直接 import 设备函数。
3. **装配与检查(3 层,`iesplan/assembly/`)**:把项目图(Device/Port/Connection)+ calc_config + 数据集元信息经 `builder` 确定性生成为**装配文本**(YAML 1.2,边-端模型;`Connection.loss_rate>0` 自动包裹为管道设备)→ `checker` 四阶段审查(语法 A → 连接合法性 B → 模型可解性 C → 整体可解性 D,ASM 域诊断码)→ **同步闸门**:`check_graph_inputs` 在快照装配时执行,error 级诊断阻断任务下发 → `plan_from_assembly` 完成业务单位 → SI 的换算(经 `core/units.py`),产出计算模块输入。
4. **计算(5 层 `services/tasks` + `worker/executors` + 3 层 `engines`,复用改造)**:`execute_calc / execute_plan / execute_uncertainty`(既有)+ `execute_assembly_check / execute_analysis`(新增);引擎函数命令化(`ies.command.compute.evaluate_plan.v1` / `run_planning.v1`);算法选择走 `engines/selector.py`(03 §9.3),容差/seed 来源统一(快照 tolerances 权威);引擎全部消费 SI 量纲(零硬编码 ×1000);KPI 在引擎尾部集中 `from_si` 回业务单位;planning 的 best 候选自动补逐时运行与财务(03 §9.5)。
5. **结果分析(4 层 `analysis/` + 5 层 `services/results`)**:KPI/逐时流 → 财务计算(见第 3 节)→ 四维评估(physical/optimality/financial/reliability,自 `metrics/validity.py` 迁入,`_check_financial` 改读 evidence `financial` 块,修复"财务恒 unknown")→ 单因子敏感性汇总;payload 落 evidence,前端渲染。

### 2.2 数据流向(数值与单位)

```
[L1 业务单位]  注册表 ParameterSpec.unit / variables[].unit / 数据集声明 unit / 装配文本 params
      │        前端输入 → parseQuantity 即时回填(展示层) / 后端 parse_quantity 权威兜底
      ▼
[校验落库]     config.py:数值类型 + normalize_unit 归一 + dims_of 量纲检查(表达式约束生效)
      │        快照 content:装配文本 + calc_config(含 unit)参与 content_hash
      ▼
[装配边界]    assembly/plan_from_assembly + runner 数据集装载 → units.to_si(唯一换算入口)
      │        设备参数 → SI(W/J/K/CNY/s);逐时数据 kWh→J/步、°C→K、CNY/kWh→CNY/J 等
      ▼
[引擎]        eval_run/planning:全部 SI,零硬编码换算;flows(SI)输出
      │        KPI 构造处统一 from_si 回业务单位(唯一反向出口)
      ▼
[展示]        executors payload:meta.units 逐字段单位契约;前端 formatValue 按注册表渲染
```

换算只存在于两处:**装配边界业务→SI(输入)** 与 **引擎 KPI 尾部 SI→业务(输出)**。验收:`grep -rn "KWH_TO_J\|W_TO_KW\|1000.0" backend/iesplan/engines/ backend/iesplan/worker/` 仅允许出现在 units 相关换算处。

### 2.3 五段间的接口契约(一段产出 = 下一段输入)

| 段间接口 | 载体 | 说明 |
|---|---|---|
| ①→② | `DeviceSpec`(+ 标准 csv) | 含 `model_method/stateful/model_function/model_file/data_file/parameters/ports/states/time_series` |
| ②→③ | `ModuleCommand` 注册表 + `command_id` | 装配文本 `model: ies.device.<type>@<version>` 引用命令;端口由注册表推导 |
| ③→④ | 装配文本(YAML 1.2)+ `requirements` 章节 + SI plan | requirements = 算法 id + tolerances + seed(自 calc_config/快照填充);`plan_from_assembly` 产出 SI 输入 |
| ④→⑤ | evidence payload(含 `financial` 块)+ 逐时 flows | 财务块由 `finance.hourly.compute_financials` 产出;四维评估消费之 |

---

## 3. 财务计算模块与计算分析模块的定位与依赖

### 3.1 finance/ 财务计算模块(意见第 6 条)

- **定位**:2 层独立包,在计算模块逐时运行结果**之上**做财务数据(现金流/NPV/IRR/LCOE/回收期),修复「财务基于年度聚合、缺 LCOE/回收期、evidence 缺 financial 块」三个缺口。**不得依赖 engines**(依赖方向是 engines→finance)。
- **组成**:`finance/metrics.py`(自 `metrics/financial.py` 迁入,Decimal 语义不变)、`finance/hourly.py`(新增:逐时费用列 → 财务口径的 `compute_financials / compute_lcoe / compute_payback / FinancialResult`)、`finance/params.py`(`FinanceParams / finance_params_from_config`,默认值自 `prices.yaml` finance 节)。
- **输入**:逐时 flows(费用列 cost_buy/cost_gas/revenue_sell,CNY/步)+ 年度 KPI + capex + baseline_cost + FinanceParams;**输出**:`FinancialResult`(irr/irr_status/npv/payback_years/lcoe/capex/cashflows/detail),即 evidence `financial` 块。
- **依赖关系**(第 4 节图中可见):`engines→finance`(planning.py 的现金流/IRR 改调 finance.metrics)、`worker→finance`(executors 装配 financial 块)、`analysis→finance`(run_sweep 每个扫描点调用 compute_financials)、`services→finance`(results.py 的 IRRStatus/财务评估)。财务默认参数来源统一 `devices.pricing`(prices.yaml),删除 `financial.py::tax_rate=0.25`、`eval_run.py::gas_price=3.2` 等硬编码。

### 3.2 analysis/ 计算分析模块(意见第 7 条)

- **定位**:4 层包,**计算模块 + 财务计算模块的 wrapper**,用于批量分析(单因子敏感性扫描等)。纯计算逻辑在 `analysis/wrapper.py`(无 DB,纯函数可单测);任务编排放 `worker/executors.execute_analysis` + `services/tasks`(新增 `'analysis'` 任务类型)。
- **组成**:`analysis/wrapper.py`(`SweepSpec{param_path, values, unit}` / `run_sweep`(对每个值:apply_param → 引擎 → compute_financials → SweepResult)/ `summarize_sweep`)、`analysis/sensitivity.py`(DB 层任务编排 `run_sensitivity_analysis` + `build_analysis_payload`)、`analysis/indicators.py`(自 metrics/engineering、environmental 迁入)、`analysis/assessment.py`(自 metrics/validity 迁入,四维评估,`check_financial` 读 evidence financial 块)。
- **与 uncertainty 的边界**:Monte Carlo 采样保留在 executors(随机);analysis 为确定性单因子扫描(确定性),两者并存不重叠(03 §8.4)。
- **依赖关系**:`analysis→engines`(默认 engine=evaluate_plan,命令化后经 get_command)、`analysis→finance`(compute_financials)、`analysis→assembly`(apply_param 改写 content 后经 plan_from_content 装配)。

---

## 4. 模块依赖图(单向无环)与分层规则

### 4.1 分层规则

| 层 | 包 | 允许依赖 | 禁止 |
|---|---|---|---|
| 0 | `core`(含扩展后的 units)、`models` | — | 依赖业务层 |
| 1 | `devices` | 0 | — |
| 2 | `modeling`、`finance` | 0、1 | finance 不得依赖 engines |
| 3 | `assembly`、`engines` | ≤2(assembly 依赖 devices+modeling;engines 依赖 modeling+finance+core) | — |
| 4 | `analysis` | ≤3 | — |
| 5 | `services`、`worker` | ≤4 | services 不得依赖 worker |
| 6 | `api` | ≤5 | 不得直接 import 其他层(ORM 逻辑下沉 services) |

### 4.2 依赖图

```
┌───────────────────────── 6 层: api ──────────────────────────┐
│  auth objects admin projects model datasets config validation │
│  tasks results exports (+devices modeling assembly finance    │
│   analysis 未来里程碑)                                        │
└──────────────────────┬────────────────────┬───────────────────┘
                       │                    │
┌──────────────────────▼──── 5 层 ──────────▼───────────────────┐
│  services: config model tasks results project dataset ...      │
│  worker: runner executors lease main solver_process            │
└──┬─────────┬─────────┬─────────┬─────────┬─────────┬──────────┘
   │         │         │         │         │         │
   ▼         ▼         ▼         ▼         ▼         ▼
┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
│ 4 层    │ │ 3 层    │ │ 3 层    │ │ 2 层    │ │ 2 层    │ │ 1 层    │
│ analysis│→│ assembly│ │ engines│→│ modeling│ │ finance │ │ devices │
│         │ │        │ │        │ │        │ │         │ │        │
└────┬────┘ └───┬────┘ └───┬────┘ └───┬────┘ └────┬────┘ └───┬────┘
     │ 依赖方向 上→下,无环    │         │          │           │
     └──────────┴──────────┴──────────┴──────────┴───────────▼
                                             ┌────────────────────┐
                                             │ 0 层: core / models │
                                             └────────────────────┘
关键跨层边:
  services → devices(config 默认值/model 序列化/validation)
  services → assembly(tasks 快照装配与闸门、model 拓扑校验委托)
  services → finance(results 评估)、services → analysis(results 四维评估)
  worker → assembly(runner 装配预检、executors plan_from_content)
  worker → modeling(命令分发)、worker → finance(financial 块)、worker → analysis(execute_analysis)
  engines → modeling(函数库)、engines → finance(planning 现金流/IRR)
  analysis → engines / finance / assembly(wrapper)
```

### 4.3 必须消除的现状违规边(迁移中消除)

- `services/results.py → engines.planning.CAPACITY_PARAM`:改走 `devices` 门面;
- `api/admin.py / api/objects.py` 路由内直接 ORM:下沉 `services/admin.py`(后续里程碑 M7);
- `engines → services` 反向依赖:现状不存在,保持;
- `worker/boundary.py`(01 草案):**不新建**——01 的 plan_to_si/data_to_si/hourly_meta 职责按分层落入 assembly/plan.py 与 core/units.py(见 7.5 裁决),避免 3 层依赖 5 层。

---

## 5. 实施优先级(本次实施 / 后续迁移)

### 5.1 总体顺序

里程碑依赖:M0 → M1 → (M2/M3 可并行)→ M4 → M5 → M6 → M7。每阶段保持 `backend/tests/` 全绿,M1 数值回归风险最高(一次替换一个换算点并对照旧数值)。

### 5.2 本次实施(01-04 四份定案文档覆盖的全部内容)

| 里程碑 | 内容 | 对应文档 | 交付/验收 |
|---|---|---|---|
| M0 骨架 | 新建五包 + `__init__.py` 门面(转发现有实现);依赖规则 README | 03 §13 | test_module_structure.py |
| M1 单位标准化 | units 扩展 + unitparse + config 校验 + 前端 unit 最小集 + units.ts/units.json + 引擎去硬编码 | 01 P1-P6 | 01 §8 各步验收;grep 无 ×1000 |
| M2 devices | 9 个 yaml + loader + prices.yaml + pricing + 注册表迁移 + Device 表新列 | 02(结构按 7.1/7.6) | 02 §7 验收(插件式/价格键缺失拒载/device_entry 契约) |
| M3 modeling | command 注册表 + functions 迁入 + build;executors 命令化 + selector(算法/容差/seed) | 03 §5、§9.3-9.4 | test_modeling_module.py、test_selector.py |
| M4 assembly | spec/parser/checker/rules/builder + 快照装配文本入哈希 + 同步闸门 + assembly_check 任务 + DB 迁移 | 04(格式按 7.2) | 04 §7.2 验收(合法样例 ok、每码反例、确定性、loss_rate 包裹、五类阻断) |
| M5 finance | finance 包 + metrics 迁入 + hourly(LCOE/回收期)+ evidence financial 块 + planning best 补逐时 | 03 §7、§9.5 | test_finance_module.py;四维复查财务不再 unknown |
| M6 analysis | wrapper/sensitivity + analysis 任务 + indicators/assessment 迁入 + metrics 转发退役 | 03 §8 | test_analysis_module.py(单因子扫描/汇总) |

本次实施边界内的前端改动(最小集,03 §10.2 + 01 §4.4):`ConfigVariable.unit`、`buildInput` 回传 unit、`configToServer` 透传、`units.ts`/`units.json` 镜像库与输入即时解析回填、删除百分比 ÷100 手工换算。

### 5.3 后续迁移(不在本次范围,列为后续里程碑)

| 项目 | 说明 |
|---|---|
| M7 清理 | admin/objects ORM 下沉 services;延迟导入消除;`metrics/` 目录删除(转发兼容期满) |
| 前端薄化 | 命令差量生成、乐观锁编排、assumptions 键集、评估触发、下载 token 会话回退后端(03 §10.1) |
| 未来 REST 端点 | `api/devices.py / modeling.py / assembly.py / finance.py / analysis.py`(03 §10.3 设计表,供外部 API 引用对齐,本次不实施) |
| lp_relax 算法 | evaluate_plan 的 LP 松弛变体,命令注册后自动生效(03 §9.3 阶段 B) |
| 多母线计算 | 装配文本/检查器已按"载体×连通分量"构造母线,天然兼容;引擎层多母线求解与 `nature: delayed` 端口逐时语义(04 §8.3)属后续 |
| 设备上传/热插拔 UI | `POST /api/devices/upload`、前端建模页按 model_method/stateful 筛选 |

---

## 6. 与现有系统的兼容边界(现有 API/前端不受影响)

1. **API 兼容**:既有路由、请求/响应结构不变;`variables[].unit` 为**可选入参**(后端回填);新增字段(unit_meta、装配/财务/分析端点字段)只增不改必填项;诊断码只增不改(`ASM-*` 新域,向 `core/diagnostics.py` 文档登记)。
2. **存量配置兼容**:老保存数据 variables 无 unit → 后端保存/加载时自动回填(有 device_ref+param 查注册表;自定义变量按名称后缀推断表,无法推断仅 warning 且该变量降级不参与量纲检查),**不拒绝旧配置**(01 §9.1)。
3. **前端兼容**:本次仅加 unit 字段透传与 units 库,既有页面行为不变;非规范单位串(`"1000 kW"`、`"1.5MWh"`、`"5%"`)输入由后端解析兜底,规范值回填(01 P3/P6)。
4. **数据集兼容**:旧文件声明单位缺失按 `STANDARD_FIELDS` 默认;等价声明(`KWH`/`元/kWh`/`℃`)经 normalize 通过;数值换算在计算边界完成,历史文件行为不变(01 §9.3)。
5. **快照与哈希兼容**:旧快照无 `assembly_text` → 按旧路径装载、不参与哈希,兼容一个版本;新快照自动含装配文本(确定性生成,同输入同文本,content_hash 语义保持)(03 §9.1、04 §8)。
6. **DB 迁移顺序**:`ck_tasks_type` 增加 `'assembly_check'`/`'analysis'` 必须先迁移数据库再发布新代码(否则 CHECK 拒绝新类型);Device 新列(`model_method` TEXT default 'mechanism'、`stateful` Boolean default false)有默认值,旧行兼容(03 §9.7)。
7. **引擎数值回归**:以 `backend/tests/test_integration.py` + `contract_smoke.py`(14/15 核心闭环)为基线;任何换算变化必须同步更新断言,禁止代码与断言脱节(03 §14.2)。
8. **注册表兜底**:yaml 目录缺失时 `core/registry.py` 静态 9 类注册保留为迁移期兜底;`DeviceTypeSpec` 数据类保留(services/models 广泛引用),新增字段向后兼容(02 §1、03 §4.4)。
9. **metrics 转发兼容**:迁移后 `metrics/` 各模块保留 `from iesplan.finance.metrics import *` / `from iesplan.analysis.indicators import *` 转发一个版本周期(03 §14.4)。
10. **编辑期校验保留**:`services/model.py::validate_topology` 保留为写入期即时反馈(装配检查是其超集,不替代)(04 §1.2)。

---

## 7. 接口不一致裁决表(定案,实施 agent 以本表为准)

实施时若发现某文档与下表冲突,以本表为准;本表未列出的差异,以专题文档为准。

| # | 冲突点 | 涉及文档 | 裁决 | 理由 |
|---|---|---|---|---|
| 7.1 | **建模方法/状态枚举与字段命名**:02 用 `modeling_method: mechanism\|data_periodic\|data_forecast` + `statefulness: stateless\|stateful`(DB 两 TEXT 列);03 用 `model_method: mechanism\|data_repeat\|data_predict` + `stateful: bool`(DB TEXT+Boolean);04 装配文本用 `model_kind: mechanistic\|data_repeat\|data_predict` | 02 §3 / 03 §4.2,§9.7 / 04 §5.2 | **统一为 03**:yaml、DB、装配文本三处键名一律 `model_method`,枚举 `mechanism\|data_repeat\|data_predict`,状态一律 `stateful: bool`;02 的 `data_periodic/data_forecast`、04 的 `model_kind/mechanistic` 命名全部按本表映射废止(04 schema 常量 `MODEL_KIND_*` 改名 `MODEL_METHOD_*`,取值 mechanism/data_repeat/data_predict) | 03 为总纲文档,覆盖全部意见且与 04 的 `stateful: bool` 一致;短名避免三套命名并存;02 的 statefulness 枚举列不建 |
| 7.2 | **装配文件格式**:03 定案 JSON(`AssemblyFile.to_text` canonical_json,边携带 `time_delay_steps`,含 `options` 段);04 定案 YAML 1.2(章节 assembly/time_axis/models/devices/ports/edges/pipelines/constraints/requirements,边=同时间严格相等,延迟走管道设备) | 03 §6.2-6.3 / 04 §2,§5 | **以 04 为准**:装配文本 = YAML 1.2,格式版本 "1.0";`AssemblyEdge` 无 `time_delay_steps` 字段(删除),延迟/损耗一律经 `pipelines:` 管道设备(`ies.device.transport_pipe`);03 的 `options`(reverse_feed_allowed/lambda_h/lambda_c/c_ph/c_pc/shedding)并入 `requirements:` 章节的扩展键;03 的 `AssemblyFile` dataclass 整体被 04 `AssemblySpec` 取代 | 04 是装配专题定案,边-端语义更完整(管道设备/母线/约束章节),且与审查意见第 4 条原文(文本文件/管道设备)一致;03 §6.1 文字也认可管道设备,仅数据模型字段落后 |
| 7.3 | **检查闸门形态**:04 为同步闸门(check_graph_inputs 在 assemble_snapshot,error 阻断任务下发 HTTP 422);03 新增 `'assembly_check'` 异步任务类型 + execute_assembly_check | 03 §6.4,§9.2 / 04 §5.7 | **并存,主从明确**:同步闸门(04)为必选主路径——快照装配时 `check_graph_inputs` 执行,error 级诊断阻断任务创建/下发并写入任务 diagnostics;`'assembly_check'` 异步任务(03)作为可选复审/留档入口,复用同一 `validate_assembly` 实现,在 M4 一并落地 | 两者复用同一检查器,不冲突;同步闸门保证"不可解不下发",异步任务提供人工复查路径 |
| 7.4 | **单位体系**:01 不注册 kWp(归一化为 kW)、复合单位全量换算(to_si 支持 CNY/kWh→CNY/J)、新增 mass/volume/voltage/area/dimensionless 类别;03 注册 `ies.unit.kwp`、UnitSpec 增 `dimension/convertible` 字段且 `convertible=False` 单位 to_si 原样返回、用 dimension 串而非类别制 | 01 §2,§4 / 03 §3.2 | **以 01 为准**:不注册 kWp 单位(ALIAS_MAP 归一 → kW);不引入 `dimension/convertible` 字段(类别制 + `dims_of` 量纲);复合单位必须参与全量 SI 换算(否则 01 §5.2 `data_to_si` 的 CNY/J、kg/J 无法实现,引擎无法消费);03 的 `canonical_unit` 并入 01 `normalize_unit`,`is_known_unit` 保留为辅助函数 | 01 为单位专题定案,换算契约完整;03 的 convertible=False 语义与引擎 SI 需求直接矛盾,弃用 |
| 7.5 | **SI 换算边界位置**:01 新增 `worker/boundary.py`(plan_to_si/data_to_si/hourly_meta);03 由 `assembly/plan.py` 的 plan_from_assembly 做参数 SI 换算、runner 数据集换算直接调 units | 01 §5.2 / 03 §3.5,§6.2 | **按 03 分层落位,不新建 worker/boundary.py**:换算函数唯一入口 `core/units.py`(0 层);`plan_to_si` 职责并入 `assembly/plan.py::plan_from_assembly`(3 层);`data_to_si` 在 runner/executors 直接调 `units.to_si` 实现;01 的 `DATA_FIELD_UNITS` 契约表与 `hourly_meta` 归 `core/units.py`(0 层,worker 与 assembly 共用);01 §5.2 三个函数的验收语义(grep 无 ×1000)保持不变 | worker 是 5 层,若 boundary 建在 worker 则 3 层 assembly 无法使用,违反单向依赖;换算工具必须放 0 层 |
| 7.6 | **devices 包文件结构**:02 为 `enums/schema/prices/loader/registry/generator/csvio/validate`;03 为 `spec/loader/catalog/pricing/profile` | 02 §6 / 03 §2 | **以 03 为基础 + 02 补充**:文件 = `spec.py`(02 schema 的 DeviceYamlSpec/PortSpec/SeriesSpec/StateSpec 并入)+ `loader.py` + `registry.py`(运行期 DeviceRegistry,含 get_entry_function,02 保留)+ `pricing.py`(02 prices.py)→ 统一命名 `pricing.py` + `profile.py`(02 csvio 的标准 csv 读写/校验/模板并入)+ `catalog/`;02 的 `generator.py`(device_entry 统一契约/build_module_function)职责整体归 **modeling**(build.py+functions.py+datadriven.py),函数包路径统一 `iesplan.modeling.functions.*`(02 的 `iesplan.devices.generated.*` 废止) | 03 门面命名更简;generator 产出命令本就是建模段职责,避免 devices 越层 |
| 7.7 | **设备 yaml 结构**:02 顶层字段 `modeling_method/statefulness/function{entry,package}` + 显式 `ports/states/time_series` 章节;03 用 `model:{method,stateful,function,model_file,data_file}` 块 + `pricing_refs` | 02 §2 / 03 §4.3 | **以 02 的章节结构为主,字段名按 7.1 裁决**:yaml 顶层 = type_id/version/name_zh/name_en/**model_method/stateful**/fidelity/energy_carriers/is_load/capabilities/extends/help_topic/ports/parameters/time_series/states/function;`function` 块:mechanism 用 `entry+package`(package 限 `iesplan.modeling.functions.*` 白名单),data_* 用 `model_file{path,format,inputs,outputs}`;`$price:` 字符串引用为主,03 的 `pricing_refs` 不落 yaml(loader 自 `$price:` 推导) | 02 字段表更完整(ports/states/series 是装配与检查必需);3 的 model 块是缩写 |
| 7.8 | **prices.yaml 键结构**:02 用 energy_prices/device_costs/finance/emissions 四段;03 用 energy/equipment_cost/tax/finance/algorithm | 02 §5 / 03 §4.3 | **以 02 结构为准 + 03 的 algorithm 段**:prices.yaml = `version/currency/energy_prices/device_costs/finance/emissions` + `algorithm`(03 §4.3,算法容差默认值,供 engines/selector 读取);03 的 tax 并入 `finance.income_tax_rate/vat_rate`;equipment_cost 键格式取 02 的 `device_costs.<type末段>.<param>`(03 的 `<type_id>.<param>` 全键形式不采用) | 02 为价格专题定案,键组织与 `$price:` 引用一致;algorithm 段是 selector 的配置来源,保留 |
| 7.9 | **前端解析边界**:01 前端 `parseQuantity` 镜像后端解析;03 前端不做任何解析,字符串原样提交由后端解析回写 | 01 §4.4 / 03 §3.4 | **并存,权威在后端**:前端保留 01 的 `units.ts`/`units.json`(数据由 `gen_units_json.py` 后端导出,禁止手改系数)与输入框失焦 `parseQuantity` **即时回填显示**;提交与落库以后端解析为准(后端 `parse_quantity` 兜底非规范字符串、normalize 归一、回填 unit);03 的"前端不做解析"仅指"不做换算权威" | 01 提供即时反馈 UX,03 保证解析单一权威;两者结合不冲突 |
| 7.10 | **02 设备 csv 与 04 data_refs 的关系**:02 每设备附带标准 csv(模板/样例/周期数据源);04 装配文本 DataRef 引用项目数据集 dataset_version_id | 02 §4 / 04 §5.2 | **并存,用途不同**:02 的设备目录 csv 是设备模型的"标准数据文件"(data_repeat 的周期曲线来源、data_predict 的训练/校验输入、机制模型的样例);04 的 `data_refs` 是**计算装配时**项目数据集的引用(load 类设备必填,ASM-INPUT-004);两者不互替;03 `profile.py::attach_profile` 的存储引用语义与 02 csvio 文件读写兼容(上传 → 引用 → 读取) | 一为模型参数,一为运行输入,阶段不同 |
| 7.11 | **财务/分析模块层次细节**:02 §3 提到 fidelity 只影响收敛;03 财务依赖线 engines→finance 与"finance 不得依赖 engines"并存 | 03 §7.5 / 分层规则 | **确认 03 分层**:finance(2 层)不得依赖 engines;engines(3 层)→ finance(2 层)单向允许;analysis(4 层)→ engines/finance/assembly 单向允许 | 无实质冲突,仅需明确箭头方向 |
| 7.12 | **assembly 依赖 services 的问题**:04 允许 builder 以 services/model.py 的 get_graph 序列化结构为输入 | 04 §5.1 | **确认**:`builder.build_assembly(graph: dict)` 只接收**序列化字典**作为参数,不 import services 内部函数;取图逻辑在 services/tasks 调用侧完成,assembly 包保持 ≤3 层依赖(仅 core/models) | 数据入参不产生 import 依赖,分层保持 |

---

## 8. 风险提示(实施时注意)

1. M1 数值回归是本批次最高风险点:一次替换一个换算点,以 test_integration.py/contract_smoke.py 为基线对照(03 §14.2)。
2. `ck_tasks_type` 的 DB CHECK 迁移必须先于代码发布,否则新任务类型写入被拒(03 §14.3)。
3. `data_repeat` 设备的周期曲线提取与 `data_predict` 的 onnx 加载依赖第三方库,验收以 02 §7 标准为准,模型文件缺失时设备加载失败要有明确诊断(禁止静默降级)。
4. 装配检查阶段 D 是结构性前置筛查,不替代求解器运行期检查(TASK-SOLVE-001/002 保留);母线假设(每载体单母线)是 v1 约束(04 §8)。
5. 引擎层需同步实现 `nature: delayed` 端口语义(04 §8.3),与 M3/M4 联调,单独列为后续项不得阻塞 M4 验收中已声明的范围。
