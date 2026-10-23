# 二次审查结论

静态审查完成，未运行测试、未修改文件。

总体结论：9 项中 **3 项已修复、6 项部分修复、0 项完全未落地**。修复代码已经大量接入主链路，但装配闸门、计算引擎选择、analysis 汇总和单位表达式仍存在实质缺陷；当前“990 passed”不能证明这些路径正确，新增 `test_selector.py` 尤其存在明显覆盖空洞。

## 9 项验收表

| 项目 | 结论 | 代码证据与说明 |
|---|---|---|
| P1-1 单位扩展合并 | ⚠️ 部分修复 | 主注册表、`Quantity`、`to_si/from_si/dims_of/convert` 已合并进 `core/units.py`：[units.py:162](/home/mc/Documents/工作文档/pIES/backend/iesplan/core/units.py:162)、[units.py:408](/home/mc/Documents/工作文档/pIES/backend/iesplan/core/units.py:408)、[units.py:495](/home/mc/Documents/工作文档/pIES/backend/iesplan/core/units.py:495)。旧 `convert.py/parse.py/registry.py/fields.py` 已删除，`stdunits/__init__.py` 是 re-export shim：[stdunits/__init__.py:13](/home/mc/Documents/工作文档/pIES/backend/iesplan/core/stdunits/__init__.py:13)。`unitparse` 通过惰性 helper 避免直接循环：[units.py:394](/home/mc/Documents/工作文档/pIES/backend/iesplan/core/units.py:394)、[units.py:446](/home/mc/Documents/工作文档/pIES/backend/iesplan/core/units.py:446)。全库未发现导入旧子模块。runner 换算已调用 `to_si`：[runner.py:190](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/runner.py:190)。但复合 USD 单位可绕过禁止汇率折算，且表达式单位常量只标量纲、不换算数值，详见 High-4/Medium-7，因此不能判定“唯一换算入口正确完整”。 |
| P1-2 九类设备目录 | ✅ 已修复 | 九个 YAML 均存在；端口按 `{carrier}_in/{carrier}_out`，双向端口用单名，例如 battery `electric`：[battery.yaml:15](/home/mc/Documents/工作文档/pIES/backend/iesplan/devices/catalog/battery.yaml:15)，gas boiler `gas`：[gas_boiler.yaml:15](/home/mc/Documents/工作文档/pIES/backend/iesplan/devices/catalog/gas_boiler.yaml:15)。热/冷负荷分别为 `heat_in`、`cool_in`：[heat_load.yaml:15](/home/mc/Documents/工作文档/pIES/backend/iesplan/devices/catalog/heat_load.yaml:15)、[cooling_load.yaml:15](/home/mc/Documents/工作文档/pIES/backend/iesplan/devices/catalog/cooling_load.yaml:15)。`heat_load.csv`、`cooling_load.csv` 均存在，表头分别为 `timestamp,h_load`、`timestamp,c_load`，与 YAML [heat_load.yaml:29](/home/mc/Documents/工作文档/pIES/backend/iesplan/devices/catalog/heat_load.yaml:29)、[cooling_load.yaml:32](/home/mc/Documents/工作文档/pIES/backend/iesplan/devices/catalog/cooling_load.yaml:32) 一致。 |
| P1-3 周期 CSV 路径 | ⚠️ 部分修复 | `read_standard_csv` 对缺列/缺时间戳给出明确 `AppError`：[profile.py:71](/home/mc/Documents/工作文档/pIES/backend/iesplan/devices/profile.py:71)。内置目录最终能按 type-id 末段找到同名 CSV：[registry_loader.py:34](/home/mc/Documents/工作文档/pIES/backend/iesplan/modeling/registry_loader.py:34)。但路径规则未统一：首先错误尝试 `catalog/catalog.csv`，随后才尝试设备短名；`to_modeling_spec` 又使用另一套路由并回退到全局默认 catalog：[spec.py:532](/home/mc/Documents/工作文档/pIES/backend/iesplan/devices/spec.py:532)。如果 YAML 文件名与 type-id 末段不同，loader 会认可 `yaml_path.with_suffix(".csv")`，命令注册却找不到。读取错误还被 catch 后降为 warning，再变成笼统“缺少 CSV”：[registry_loader.py:40](/home/mc/Documents/工作文档/pIES/backend/iesplan/modeling/registry_loader.py:40)，丢失原始文件/列错误。 |
| P1-4 YAML 装配注册表 | ⚠️ 部分修复 | `to_registry_spec` 已直接导入，旧 `hasattr` 错误已消失：[checker.py:227](/home/mc/Documents/工作文档/pIES/backend/iesplan/assembly/checker.py:227)。参数/default/unit 来自 YAML 转换结果：[spec.py:488](/home/mc/Documents/工作文档/pIES/backend/iesplan/devices/spec.py:488)，端口通过运行期 YAML registry 读取：[checker.py:244](/home/mc/Documents/工作文档/pIES/backend/iesplan/assembly/checker.py:244)。`units_compatible` 已显式允许 energy↔power：[checker.py:429](/home/mc/Documents/工作文档/pIES/backend/iesplan/assembly/checker.py:429)。但 YAML registry 的任何初始化/加载错误都会被裸 `except Exception` 吞掉并回退硬编码注册表：[checker.py:221](/home/mc/Documents/工作文档/pIES/backend/iesplan/assembly/checker.py:221)，文件中也仍保留完整 `_DEVICE_PORT_DIRECTIONS`：[checker.py:51](/home/mc/Documents/工作文档/pIES/backend/iesplan/assembly/checker.py:51)。这不满足“YAML 必须是事实来源”。此外 YAML 路径下提前 return，导致显式端口 capacity 覆盖失效，详见 High-3。 |
| P1-5 快照装配闸门 | ⚠️ 部分修复 | `assemble_snapshot` 在写快照前调用 `_assembly_gate`：[tasks.py:309](/home/mc/Documents/工作文档/pIES/backend/iesplan/services/tasks.py:309)；失败抛 `AssemblyCheckError`，其 `http_status=422`：[checker.py:155](/home/mc/Documents/工作文档/pIES/backend/iesplan/assembly/checker.py:155)。API 未局部 catch，但全局 `AppError` handler 会按 `http_status` 返回 422：[main.py:207](/home/mc/Documents/工作文档/pIES/backend/iesplan/main.py:207)。`_dataset_meta_for` 构造了 vid/columns/column_units/resolution：[tasks.py:353](/home/mc/Documents/工作文档/pIES/backend/iesplan/services/tasks.py:353)。但 `check_graph_inputs` 只把 metadata 交给 builder，未设置 `CheckContext.datasets`：[checker.py:663](/home/mc/Documents/工作文档/pIES/backend/iesplan/assembly/checker.py:663)，而缺失版本、列和分辨率检查仅在 `ctx.datasets is not None` 时执行：[completeness.py:159](/home/mc/Documents/工作文档/pIES/backend/iesplan/assembly/rules/completeness.py:159)。因此关键闸门实际被绕过，详见 High-1。 |
| P1-6 命令注册表接入 | ⚠️ 部分修复 | 计算命令已登记：[command.py:136](/home/mc/Documents/工作文档/pIES/backend/iesplan/modeling/command.py:136)，catalog 注册时调用 `init_compute_commands`：[registry_loader.py:49](/home/mc/Documents/工作文档/pIES/backend/iesplan/modeling/registry_loader.py:49)，API/Worker 启动都有注册调用：[main.py:108](/home/mc/Documents/工作文档/pIES/backend/iesplan/main.py:108)、[worker/main.py:244](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/main.py:244)。calc/plan/uncertainty/analysis 均通过 `_engine_entry` 查表。但 LP 算法映射到不存在的 `evaluate_plan_lp`，seed 最终仍硬编码 42，未知算法静默回退，注册失败还允许 Worker 继续运行。详见 High-2、High-5、Medium-2。 |
| P1-7 analysis 全链路 | ⚠️ 部分修复 | ORM 约束、API Literal、service 非空 sweeps 校验、runner 分派、executor 均已加入：[calc.py:140](/home/mc/Documents/工作文档/pIES/backend/iesplan/models/calc.py:140)、[api/tasks.py:37](/home/mc/Documents/工作文档/pIES/backend/iesplan/api/tasks.py:37)、[tasks.py:614](/home/mc/Documents/工作文档/pIES/backend/iesplan/services/tasks.py:614)、[runner.py:240](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/runner.py:240)、[executors.py:717](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/executors.py:717)。但 executor 虽导入 `summarize_sweep`，实际从未调用，仅返回计数型 summary：[executors.py:731](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/executors.py:731)、[executors.py:787](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/executors.py:787)，不满足“run_batch + summarize”。payload 还缺少 `assessment`/`outcome`。DB 迁移遇到约束完全不存在时直接 return，无法补建：[db.py:68](/home/mc/Documents/工作文档/pIES/backend/iesplan/db.py:68)。 |
| P2-8 逐时财务载荷 | ✅ 已修复 | `_eval_payload` 签名与调用一致，调用点明确传入 `content`：[executors.py:188](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/executors.py:188)、[executors.py:263](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/executors.py:263)。逐时 flows/KPI 经 `compute_financials`，结果写入 payload 的 `financial`：[executors.py:227](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/executors.py:227)、[executors.py:295](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/executors.py:295)。IRR/NPV/投资/基准成本/运营成本/收入/LCOE/回收期/现金流/诊断均已映射。缺输入时捕获 `ValueError/TypeError` 返回 None，不阻断任务。不过常规项目缺 `baseline_cost` 时整块会变成 None，且 assessment 未按实际 financial 结果更新，列为 Medium 回归。 |
| P2-9 价格错误上抛 | ✅ 已修复 | `_price_finance_defaults` 已无 `except Exception: pass`，`load_price_book` 的文件缺失、语法错误和必需段缺失均转换为 `SYS-CFG-001`：[params.py:75](/home/mc/Documents/工作文档/pIES/backend/iesplan/finance/params.py:75)、[pricing.py:42](/home/mc/Documents/工作文档/pIES/backend/iesplan/devices/pricing.py:42)。加载期 `AppError` 不会被吞。但 `ImportError` 捕获范围仍过宽，模块内部依赖导入失败也会被当成“模块不存在”，见 Medium-6。 |

## 红队新发现

### Critical

未发现可直接导致任意代码执行、权限绕过或持久数据不可恢复的 Critical 缺陷。

### High

1. **装配闸门没有真正执行数据集版本、列、分辨率检查**

   - 位置：[tasks.py:347](/home/mc/Documents/工作文档/pIES/backend/iesplan/services/tasks.py:347)、[checker.py:687](/home/mc/Documents/工作文档/pIES/backend/iesplan/assembly/checker.py:687)、[completeness.py:159](/home/mc/Documents/工作文档/pIES/backend/iesplan/assembly/rules/completeness.py:159)
   - `_assembly_gate` 获取了 metadata，但 `check_graph_inputs` 只传给 `build_assembly`；随后创建的默认 `CheckContext` 中 `datasets=None`。
   - 后果：显式 `dataset_version_id` 不存在、列不存在、resolution 不匹配时，对应检查整体不运行；HTTP 422 闸门可能放行错误快照。
   - 建议：构造或更新 `CheckContext(datasets=datasets)` 后传给 `check_assembly`；builder 合并和 checker 校验都使用同一 metadata 快照。补测不存在 vid、缺列、resolution 不一致三个 API 422 用例。

2. **LP 算法映射到不存在的函数，选择后必然执行失败**

   - 位置：[selector.py:22](/home/mc/Documents/工作文档/pIES/backend/iesplan/engines/selector.py:22)、[command.py:138](/home/mc/Documents/工作文档/pIES/backend/iesplan/modeling/command.py:138)
   - `ies.algo.lp_relax` 映射至 `ies.command.compute.evaluate_plan_lp.v1`，但 `engines/eval_run.py` 没有 `evaluate_plan_lp`。
   - 测试还明确跳过了该命令的解析验证：[test_selector.py:87](/home/mc/Documents/工作文档/pIES/backend/tests/test_selector.py:87)。
   - 建议：实现并注册真实 LP entry，或在实现前不要将算法暴露为可选；所有 `_COMPUTE_COMMANDS` 必须逐一执行 `get_compute_entry()` 的契约测试。

3. **YAML 端口路径提前返回，显式 capacity 覆盖在生产注册表下成为死代码**

   - 位置：[checker.py:286](/home/mc/Documents/工作文档/pIES/backend/iesplan/assembly/checker.py:286)
   - `yaml_ports` 非空时在 296–298 行直接 return；320–338 行的显式 capacity 合并只对静态 fallback 生效。
   - 后果：初始化 YAML registry 的生产环境与未初始化 registry 的测试环境行为不同；图中端口容量可能被忽略，检查结果和求解上限不一致。
   - 建议：无论端口来自 YAML 还是 fallback，都进入统一的显式覆盖合并阶段；增加“registry 已初始化 + 显式 capacity”测试。

4. **表达式单位常量只赋量纲，没有转换为统一数值**

   - 位置：[expression.py:409](/home/mc/Documents/工作文档/pIES/backend/iesplan/core/expression.py:409)
   - `"50 kW"` 被编译为数值 `50`，仅附加 power 量纲：[expression.py:423](/home/mc/Documents/工作文档/pIES/backend/iesplan/core/expression.py:423)。若变量值是 SI W，则实际应为 `50000`。
   - 后果：带显式单位的约束运行时阈值偏差 1000 倍；`800 W` 与 `0.8 kW` 会产生不同物理结果。
   - 建议：在 rewrite 阶段使用 `parse_quantity`/`to_si` 保存 SI value，同时使用 `dims_of`；新增 W/kW/MW 等价求值测试，不能只测量纲通过。

5. **快照 seed 已进入 selector，但求解器仍固定使用 42**

   - 位置：[selector.py:75](/home/mc/Documents/工作文档/pIES/backend/iesplan/engines/selector.py:75)、[eval_run.py:755](/home/mc/Documents/工作文档/pIES/backend/iesplan/engines/eval_run.py:755)
   - selector 正确生成 `opts["seed"]`，但 `evaluate_plan` 调用 `solve_milp(..., seed=42)`。
   - 后果：快照记录的随机种子与实际计算不一致，破坏可复现性和证据包可信度。
   - 建议：改成 `seed=int(opts.get("seed", 42))`，并通过 mock `solve_milp` 验证非默认 seed 实际传入。

6. **YAML/命令注册失败后 API 和 Worker 仍继续启动**

   - 位置：[main.py:115](/home/mc/Documents/工作文档/pIES/backend/iesplan/main.py:115)、[worker/main.py:244](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/main.py:244)
   - 两处都捕获全部异常，仅记录日志继续运行；checker 又会静默回退静态设备表。
   - 后果：系统可在价格表损坏、设备目录不完整或计算命令为空的状态下对外提供服务，错误延迟到任务执行阶段。
   - 建议：API 至少将 registry 状态纳入 readyz 且拒绝计算提交；计算 Worker 应启动失败退出，不能进入消费循环。

### Medium

1. **analysis executor 未执行汇总，且证据评估全部退化为 unknown**

   - 位置：[executors.py:731](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/executors.py:731)、[executors.py:787](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/executors.py:787)
   - 导入 `summarize_sweep` 后未使用；payload 没有 `assessment` 和 `outcome`。
   - `submit_result` 对缺失 assessment 会写四个 unknown：[lease.py:220](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/lease.py:220)。
   - 建议：为 BatchResult 实现 `summarize_batch`，输出变化率、单调性、极值点和敏感度；同时生成四维 assessment 和 partial/no-feasible outcome。

2. **未知算法被无诊断地回退默认算法**

   - 位置：[selector.py:61](/home/mc/Documents/工作文档/pIES/backend/iesplan/engines/selector.py:61)
   - 注释声称“诊断 error 并回退”，函数实际只返回默认 command，没有返回或记录任何诊断。
   - 建议：在快照创建时拒绝未知算法，或令 selector 返回 diagnostics 并写入任务证据；不要把静默回退本身写成通过测试的期望。

3. **DB 约束迁移无法修复“约束不存在”状态**

   - 位置：[db.py:62](/home/mc/Documents/工作文档/pIES/backend/iesplan/db.py:62)
   - 查询不到 `ck_tasks_type` 时直接返回，而不是补建。
   - 建议：`row is None` 时执行 `ADD CONSTRAINT`；存在旧定义时执行 drop/add。并添加约束不存在、旧约束、新约束三种幂等迁移测试。

4. **周期 CSV 路径没有以 YAML 文件路径为权威**

   - 位置：[registry_loader.py:29](/home/mc/Documents/工作文档/pIES/backend/iesplan/modeling/registry_loader.py:29)、[spec.py:532](/home/mc/Documents/工作文档/pIES/backend/iesplan/devices/spec.py:532)
   - `DeviceYamlSpec` 只保存目录，不保存源 YAML 路径，因此不能严格实现 `yaml_path.with_suffix(".csv")`。
   - 建议：在 spec 中保存 `source_path`，提供唯一 `standard_csv_path(spec)` helper，loader、registry_loader、to_modeling_spec 全部复用。

5. **逐时财务常规情况下容易整块返回 None，assessment 却仍判 pass**

   - 位置：[executors.py:227](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/executors.py:227)、[wrapper.py:328](/home/mc/Documents/工作文档/pIES/backend/iesplan/analysis/wrapper.py:328)、[executors.py:370](/home/mc/Documents/工作文档/pIES/backend/iesplan/worker/executors.py:370)
   - `baseline_cost` 仅从显式配置提取；缺失时 `compute_financials` 抛错并降为 None。与此同时 `_assess_eval` 只看 `total_op_cost`，即使 `financial=None` 仍可判 financial pass。
   - 建议：明确基准成本推导口径；assessment 在财务计算之后生成，`financial is None` 时必须为 unknown，并附降级诊断。

6. **“仅模块不存在才回退”仍可能被模块内部 ImportError 绕过**

   - 位置：[params.py:82](/home/mc/Documents/工作文档/pIES/backend/iesplan/finance/params.py:82)
   - 捕获的是所有 `ImportError`。若 `iesplan.devices.pricing` 存在但其内部依赖导入失败，也会被当成兼容性缺失。
   - 建议：捕获 `ModuleNotFoundError`，并仅在 `exc.name == module_name` 时继续；其他 ImportError 原样上抛。

7. **复合 USD 单位绕过非固定汇率禁令**

   - 位置：[units.py:486](/home/mc/Documents/工作文档/pIES/backend/iesplan/core/units.py:486)
   - `_check_convertible` 仅判断整个 canonical 是否等于 `"USD"`；`USD/kWh`、`USD/kW` 不会被拒绝。
   - 建议：解析分子/分母 token，只要包含 `NON_CONVERTIBLE_CURRENCIES` 就拒绝自动换算。增加 `USD/kWh ↔ CNY/kWh` 回归测试。

8. **装配 metadata 合并信任引用自报 unit，未核对数据集真实 unit**

   - 位置：[builder.py:182](/home/mc/Documents/工作文档/pIES/backend/iesplan/assembly/builder.py:182)
   - `ref.unit or dataset column unit` 使用户自报单位优先；completeness 也没有引用单位与数据集列单位的直接比较。
   - 建议：数据集 metadata 为权威；显式 unit 只能作为期望声明，若与 metadata 不同应报阻断诊断。

### Low

1. **`UNIT_META_TABLE` shim 导出可能永久保持 None**

   - 位置：[units.py:630](/home/mc/Documents/工作文档/pIES/backend/iesplan/core/units.py:630)、[stdunits/__init__.py:37](/home/mc/Documents/工作文档/pIES/backend/iesplan/core/stdunits/__init__.py:37)
   - shim 使用 `from ... import UNIT_META_TABLE` 复制初始 None；之后 `unit_meta_table()` 在源模块重新绑定变量，shim 中的绑定不会同步。
   - 建议：兼容层通过 `__getattr__` 动态转发，或模块加载时直接构造不可变表。

2. **单位后缀正则覆盖不完整**

   - 位置：[expression.py:393](/home/mc/Documents/工作文档/pIES/backend/iesplan/core/expression.py:393)
   - 不支持科学计数、带符号数、中文单位、`%`、`m²/m³` 等注册单位形态。
   - 建议：不要维护独立正则子文法；复用 `unitparse.NUMBER_RE` 和正式单位解析器。

## 对新增 `test_selector.py` 的覆盖评价

覆盖不足，不能支撑“引擎命令化与 seed 收敛已完成”的结论。缺失的关键测试包括：

- `evaluate_plan_lp` 命令可解析且可调用；
- selector 输出的 seed 确实到达 `solve_milp`；
- canonical tolerance 输入和 task override 的完整矩阵；
- 未知算法必须产生诊断或拒绝，而非仅断言回退；
- calc、optimization、uncertainty、analysis 四条 executor 实际查表；
- API/Worker 注册失败的 fail-fast 行为；
- registry 重载后计算命令仍完整；
- 非默认算法的端到端执行。

其中 [test_selector.py:87](/home/mc/Documents/工作文档/pIES/backend/tests/test_selector.py:87) 主动排除了正好会暴露 LP 死命令的问题，是当前基线仍可全绿的直接原因。