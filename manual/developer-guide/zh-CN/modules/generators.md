# 计算生成器

> 代码边界：`backend/iesplan/computation/generators/`；交接格式：[Solver Bundle](../formats/solver-bundle.md)

生成器承担“模型编译器”的角色：接收经装配与上游校验的可信输入，完成数学关系降级、统一上下界推导与唯一一次业务单位到求解内部单位的换算，确定性地生成求解器输入文件、结构化命令、参数记录与结果映射声明，打包发布为 [Solver Bundle](../formats/solver-bundle.md)。

## 职责与边界

- **负责**：`GeneratorDescriptor` 声明与独立注册；直接消费六项冻结输入构建数学模型；合并技术贡献、规划目标约束、财务与税务关系；业务单位向求解内部单位换算；输出规范 Bundle（分块文件流、命令、输出清单及配套 `ResultAdapter` 引用）。
- **不负责**：数据库查询、对象存储读写、网络访问、启动求解器进程、任务调度与业务结果发布。
- **统一临时容器边界（`1.1.5` 目标）**：内置与第三方组装器统一遵循[算法插件包](../formats/algorithm-plugin-package.md)（`kind: generator`）契约，在由 `job_runner` 拉起的独立断网临时容器中消费 `/work/in` 冻结输入并将标准 `Solver Bundle` 流式输出至 `/work/out`，保持构模、求解与评价边界严格隔离。

## 直接输入与能力门禁

生成器每次生成直接接收以下六项来自可信上游的不可变输入（不再设外层包装，也不重复运行公开输入校验）：

| 输入 | 来源与内容 |
|---|---|
| `ValidatedAssemblyArtifact` | 规范装配文本与校验回执（设备实例、连接分区、规划配置、绑定与项目基线） |
| `models` | 按装配实例 ID 映射的设备技术数学贡献 |
| `materialized` | 全周期连续运营步对齐序列集合 `MaterializedSet` |
| `finance` | 不可变有效财务快照 `EffectiveFinanceConfig` |
| `CalculationConfig` | 计算模式、预测配置、生成器/求解器引用、精度、求解选项、种子与输出请求 |
| `evaluation` | 冻结的显式评价确认（建设/运营年限、折现率、付款比例、维护清单与比较基准） |

### `GeneratorDescriptor` 与公开能力声明

`GeneratorDescriptor` 声明支持的装配契约版本、计算模式、精度、兼容求解器与执行器、支持的输出集合、Draft 2020-12 规范的 `options_schema`（无选项须为严格空对象 Schema）以及内部单位体系。

提交计算任务时，应用层依据所选生成器的 `capabilities` 集合检查当前输入实际用到的特性；缺失任一必需能力即以 `TASK-REQ-007` 阻断并指出触发字段，未使用的能力不触发拒收：

| 能力标识 | 对应功能范围 |
|---|---|
| `planning.construction_activation` | 非空 `planning.construction_units` 建设单元与活动区间 |
| `technical.step_constraints` | 启用的跨实例逐步约束 `step_relation` |
| `network.closed_ports` / `modeling.system_equations` / `outputs.system_series` | 未连接端口关闭语义、系统技术方程块与辅助序列输出 |
| `finance.vat` / `finance.construction_input_vat` / `finance.income_tax` / `finance.credit_pools` | 增值税、建设进项、所得税与共享抵扣池规则 |
| `finance.monthly_demand` / `finance.monthly_daily_prices` / `finance.affine_maintenance` | 自然月最大需量计费、月份日曲线价格、规模/成本关联维护项 |
| `finance.named_items` / `finance.amount_references` / `finance.cash_movements` / `finance.assets` / `finance.derived_tax` / `finance.amount_constraints` | 命名成本明细、金额定义、具名收付、命名资产、派生税费与 `amount_relation` |
| `finance.frozen_baseline` / `finance.cashflow_difference` | 非 `absolute` 评价比较基准及税后差额现金流 |
| `finance.financial_facts` | 同次解财务事实与构模来源映射输出（税务、需量与财务关系能力共同依赖） |

## 构模管道与核心机制

```text
接收六项冻结输入
  ↓ 收集设备、端口网络分区、规划与财务/税务关系
  ↓ 解析引用、量纲与统一上下界分析
  ↓ 精确转换条件关系（互斥、建设激活、月峰值、分段税务）
  ↓ 按冻结时间结构实例化稀疏矩阵、来源映射与结果声明
  ↓ 分块写出 MPS / 参数记录（parameters.json & parameters.jsonl）与命令 manifest
原子发布 Solver Bundle
```

### 统一上下界

求解前界分析用于证明精确离散/分段转换所需的有限界（如 Big-M 系数），而非代替完整线性预处理或替代输入范围校验：

1. **界来源交集**：模型声明的 `valid_range` 与规划变量项目边界取交集（下界取最大、上界取最小）；未声明某侧边界时为未知（`null`），不使用模型 `properties.value` 冒充规划上界。
2. **关系推导**：从等式、不等式与跨步状态关系推导出保守有限界；仅作用于当前槽位的约束推导，不猜测大常数。
3. **精细区分**：额定容量、瞬时流量、状态量与金额表达式各自独立求界（如某侧因子已证明恒为零，则乘积互斥直接满足，无需引入二元开关与有限上界）。
4. **明确失败归因**：若精确转换所需有限界未能证明，发布 `TASK-SOLVE-001` 并附带 `context.model_source` 精确定位原关系与缺失边界侧，不将其误报为数学问题无界或输入非法。

### 时间结构与等价优化

- **多时间轴隔离**：运行步（`step`）、税务结算月（`tax_month`）与财务年度（`financial_year`）各自保持明确索引与公历映射；输入序列按 `data_repeat` 重复并不意味优化决策变量或各期税额可以未经证明地重复。
- **结构复用与等价前提**：优先复用表达式模板结构与紧凑数值表示降低内存与构模开销；若采用决策压缩或简化编码，必须数学等价于完整全周期模型（覆盖状态连续性、月界、税务结转与目标函数）。例如最大化税后 NPV 且单调性及依赖认证成立时，月最大需量可采用连续上包络 `peak >= flow[t]` 替代离散峰值时刻选择变量，未满足认证条件时自动回退精确最大值编码。

### 求解加速结构元数据导出

生成器保持纯内存确定性编译边界（禁止在 `generate` 内启动求解或搜索进程）。当 `CalculationConfig.options` 启用求解器内建结构加速时（内置 `ies.generator.highs_milp@1.0.0` 默认开启 `"on_demand_mutex"`，可显式设 `"off"` 关闭），生成器随 Bundle 导出经编译期核验的结构元数据，供求解执行器在不破坏原模型列序映射与约束完整性的前提下执行结构加速与原模型容差核查（详见[计算生成与求解](engines.md#求解器内建结构加速)）。

### 生成阶段协作停止

常规生成接口保持纯输入签名；支持长时构模中断的 Provider 可实现 `generate_with_stop(..., stop)` 扩展。当外部任务取消、超时或管理员撤销租约时，停止探针返回停止原因，生成器及时中止构模与文件流出并抛出带对应原因的 `ComputationUnavailableError`，避免继续消耗资源或产出半成品 Bundle。

## 内置生成器：`ies.generator.highs_milp@1.0.0`

| 协议项 | 约束与语义 |
|---|---|
| 配套组件 | 求解器 `ies.solver.highs@1.7.2`、执行器 `ies.executor.local_process@1.0.0`、适配器 `ies.result-adapter.highs@1.0.0` |
| 计算支持 | `mode: planning`、`precision: medium`、`options.acceleration.mode` 默认 `"on_demand_mutex"`（可设 `"off"` 关闭）、`outputs` 支持 `solution` 与 `metrics`；`seed` 为 `null` 时固定取 `0` |
| 数学表达 | 仿射关系、有限非负界下的 `x * y = 0` 精确 Big-M 互斥、一阶状态 `[t]`/`[t-1]`、建设激活与财务税务分段关系（不支持变量乘变量的双线性关系） |
| 单位与坐标 | 物理量换算至 SI 内部计算，金额保持冻结财务币种；目标常数以 `objective_offset` 单独传递 |
| 产物与超时期 | 按列分块流式生成 `input/problem.mps`，配合 `input/parameters.json` 与 `input/parameters.jsonl`；用户请求 `time_limit_seconds` 原样传给 HiGHS `--time_limit`，外层进程监督宽限固定为 `+600` 秒用于启停、读模与解写出（整体仍受部署 `max_timeout_seconds` 约束） |
| 纯度与确定性 | 不读写临时目录或环境变量，相同冻结输入保证产出逐字节一致的 Bundle 内容 |
