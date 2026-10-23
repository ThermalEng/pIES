# C0 税后求解接线方案（只读提案，非规范）

日期 2026-10-20。基线：分支 `slice/0.14.5-opencode` 合并 `8427d8c` 后（`dec336f`），B1a/B1b/B1c 已审未合入。本轮只定接线，不实现 C、不改指南、不重开税种税率口径。

## 1. 评价确认事实进入生成器

实际调用路径（本分支源码）：`worker/runner.py:434` → `compute_cases.generate_bundle(plane, snapshot, artifact, models, materialized, finance, config)` → `provider.generate(artifact, models, materialized, finance, config)`（`highs_milp.py:126`）。五项签名固定，且 `highs_milp.py:96-103` 以同一五项输入恒得逐字节相同 Bundle 为身份语义。`load_compute_inputs`（`compute_cases.py:216`）已从 `snapshot.evaluation` 读 `operation_years` 并经 `complete_materialized`（`compute_cases.py:282-318`）传给 `MaterializedSet`；其余五键（`evaluation_years/construction_period/discount_rate/construction_payments/maintenance_expenses`，见 `submissions.py:272-277` 冻结口径）停在 `snapshot.evaluation`，生成器不可见。`build_financial_evaluation`（`compute_cases.py:665`）求解后才读它们。

现有来源：`snapshot.evaluation` 冻结块（提交边界唯一校验，内部信任）。真实缺口：K（月份索引与折旧/付款映射起点）、`discount_rate`（NPV 系数）、`construction_payments`（payment_period 确认与建设现金时点）、`maintenance_expenses`（年金额与归属）、E（12E 上界；解析已保证 E=K+N，但生成器只有 N）。

单一推荐：沿 `MaterializedSet` 已有先例（`operation_years` 必填、无默认值、由快照评价确认块读取，见 `sequences.py:131` 文档）扩展同源冻结事实字段：`evaluation_years`、`construction_period`、`discount_rate`、`construction_payments`、`maintenance_expenses`，全部必填、无默认值；`load_compute_inputs` 直接索引快照块（缺件即 KeyError 失败关闭），`complete_materialized` 透传。五项输入不变、无新包装、单一事实源（仍是 `snapshot.evaluation`）。否决项：塞 `FinanceProfile`（财务指南已明令禁止）、塞封闭 `CalculationConfig`（其文档串与 `tasks/contracts.py` 均排除评价确认）、塞装配规范文本（改变装配语义）、第六参数（破坏 Bundle 身份语义）。以上禁区若确需突破，属指南修订，由审阅方裁决。

必要版本变化：`MaterializedSet` 无 schema 版本（纯代码契约），不 bump；`complete_materialized` 与 4 处测试构造点随改（`test_highs_milp_provider.py:236`、`test_price_series_csv.py:484`、`test_project_model.py:317`、`test_computation_boundary.py:312`）。Provider 描述符输入不变。

## 2. 税后 NPV 目标入口

实际调用路径：`planning.objective = {sense, expression}`（`planning_config.py:80`）随装配文本进入产物（`project.py:312` 取 `doc["planning"]`），`_build_objective`（`project.py:954`）经 `compile_planning_expression` 在{成本实例分量、价格绑定、规划变量}符号上编译仿射目标。税额状态变量不在符号表内，用户表达式写不出税后 NPV；静默替换用户目标被禁止。

单一推荐：保留形状，表达式取保留字 `system.after_tax_npv`（`system.` 命名空间新建保留），`sense` 仍由用户给（`maximize`）。Provider 在编译前截获该保留字，由生成器按财务规则与已确认折现率构建税后 NPV 仿射目标；其他表达式走现有路径。非财务物理目标不受影响。否决项：通用公式 DSL、目标塞财务 Profile、按税前表达式近似。

必要版本变化：`PlanningConfig` 无版本号，形状不变，不 bump；Provider 描述符能力文本更新。税后支持随 `highs_milp@2.0.0`（`8427d8c` 意图如此；空规则行为不变，既有引用不迁移）。若要求 bump provider，引用点（`test_bootstrap`、`test_campus_project_chain`、`test_full_horizon_solve` 等）需同步，由审阅方定。提交边界能力门：C 接通时把 `_reject_unsupported_tax_rules` 的一刀切 007 换成按关系支持度检查（财务指南“公开提交边界核对生成器是否支持所选关系”），不支持仍回能力诊断——属 C3 实现事项，此处只定方向。

## 3. 同次解税额沿 Bundle→Adapter→封存消费

实际调用路径：`generate()` 写 `parameters.json`（`highs_milp.py:217-257`：`unit_map`、`objective_offset/unit`、`outputs`、`step_series`、`finance_component_declarations`）→ `result_adapters/highs.py` 按名恢复 LP 原始解并发布 `outputs.solution{objective_value, objective_unit, components}` / `series` / `metrics`（`highs.py:440-475`）→ `runner.py:434` 以同次解调 `build_financial_evaluation` → 封存 `payload.financial_evaluation`。税后 NPV 作为目标值已自动进 `solution.objective_value`，无需增量。

单一推荐：生成器在 `parameters.json` 增 `tax_states` 声明（税额 LP 变量名→口径/单位，沿用 components/series 同一具名恢复机制）；Adapter 恢复进 `outputs.solution.tax_states`（仅请求且求得 solution 时存在；metrics-only 时无，`build_financial_evaluation` 沿现有 `None` 路径返回）。`build_financial_evaluation` 直接消费该税额（月缴/留抵、年应税/亏损使用与核销、退税到账），组装月表/年表与现金流，不重算税决策（同次解为唯一权威；B0 纯函数只做独立对照与测试 oracle）。`evaluate_project_investment` 现有 NPV/IRR 复算逻辑复用税后现金流。

必要版本变化：`parameters.json` 加一键（生成器/适配器配套升级，原子提交）；`ComputeResult.outputs` 加一分支；封存 `financial_evaluation` 载荷加税额行（证据载荷加键，后向兼容，由审阅方确认证据版本策略）。快照 schema、DB 列、包格式不动。

## C1/C2/C3 依赖与可手算验收

- C1（模型）：依赖第 1、2 项。VAT 月状态约束（`net/paid/credit` max 分段）、亏损 vintage 先到期先用精确约束、折旧时点、建设付款映射；界全由业务参数与变量界推导（财务指南求解边界节＋`8427d8c` 离散变量），不猜 big-M/epsilon，不靠目标压紧。验收：微型可枚举合成案，税后排序与手算一致；至少一例税前税后排序反转。
- C2（输出与评价）：依赖第 3 项。验收：`objective_value` 与独立税后现金流折现一致；连续留抵跨 12→13 月后实缴；建设付款、进项抵扣、维护支出各计一次；`r=0` 与静态口径一致。
- C3（提交门）：依赖 C1。验收：不支持的关系仍回能力诊断；支持的真实求解建 Task/快照（首个含税成功冻结）；空税则路径不变。

## 待裁决事项

1. 年度维护进项在月结算下的确认月份归属（财务指南只给“复用抵扣池与确认方法”，未定年内哪一月）。建议：维护发生年 12 月（与年末费用归集同口径）；由审阅方定。
2. 税后支持随 `2.0.0` 还是 bump provider 版本（建议随 `2.0.0`，见第 2 项）。
3. 目标保留字拼写（建议 `system.after_tax_npv`）。
4. `tax_states` 在 outputs 下的准确键位与封存证据版本策略（建议 `solution.tax_states`，证据加键兼容）。
5. 税月时轴（12E，含建设期）与运营步长时轴（N 年步进）的聚合口径属 C1 建模细节，实现时按本方案取数，不另增输入。
