# 财务计算

> 代码边界：`backend/iesplan/finance/`；文件格式正文：[财务 YAML 契约](../formats/finance-yaml.md)、[命名金额、支付与资产](../formats/finance-relations.md)

`finance` 管理区域财务基准与项目覆盖三件套的领域校验和合并，定义参与全周期规划优化的成本、收入、资产折旧与税务现金流关系，并基于同次解产出项目投资口径的财务评价结果。

## 职责与边界

- **财务三件套管理**：拥有 `FinanceProfile`、`FinanceOverrides` 与 `EffectiveFinanceConfig` 的领域校验与确定性合并语义，向装配、规划与计算暴露不可变的 `EffectiveFinanceConfig` 快照（字段结构以[财务 YAML 契约](../formats/finance-yaml.md)为唯一正文）。
- **成本分量与计价语义**：提供 `upfront_capex`（建设期一次性）、`annual_fixed_om`（币种/年）、`period_variable_om`（运行量费用）及 `tariff_bindings` 能源购售贡献（`purchase` 为 `+raw_charge`，`sale` 为 `-raw_charge`，负电价自然反转符号）。各分量时间口径独立，按年分量与窗口合计相加前须乘以显式时间跨度（如 `1 year`）。
- **严格与技术解耦**：设备模型 `ies.device-model` 不包含任何价格或成本；装配通过显式 `finance_binding`（`costed` / `none`）、`tariff_bindings` 与 `demand_bindings` 绑定成本和计费关系，严禁按设备名称或技术接口方向猜测经济属性。

### 评价边界与基准

`finance` 支持绝对评价（`absolute`）、分别计税差额评价（`difference`）与外部常量参考（`reference`）三种评价口径（完整输入格式见[评价范围与冻结基准](../contracts.md#评价范围与冻结基准)）：

1. **独立维度**：清晰区分经济承担范围、纳税范围与冻结基准来源；基准事实只能来自用户确认的 `declared` 数据或已封存的 `task_result` 证据，禁止在求解过程中自动构建“零容量基准”或回读草稿。
2. **分别计税原则**：`difference` 模式在双方按各自冻结税务规则计算税后现金流后作差（`CF_with - CF_without`），不对现金差额重新套税率，避免 `Tax(c) - Tax(b) != Tax(c - b)` 带来的口径失真。
3. **同次寻优一致性**：影响应税所得、抵扣余额或约束的基准常量必须在优化求解阶段即纳入关系，不得按税前求解后再扣减基准或税额。

## 税务参与规划与现金流

影响规划决策的增值税、建设进项抵扣、所得税、计税折旧、亏损结转、具名收付及派生税费均由生成器纳入同一次全周期优化求解，最终评价直接消费同次解结果。

### 规划目标与同源评价

全周期税后规划以最大化项目税后净现值（`system.after_tax_npv`）为目标，现金流采用含税实际收付款口径：

```text
CF[t](x) = R_gross[t](x) - O_gross[t](x) - K_gross[t](x)
           - VAT_paid[t](x) - other_tax[t](x) - income_tax[t](x)
           + tax_refund[t](x) + explicit_net_cash[t](x)

maximize NPV_after_tax(x) = Σ(t=0..H) CF[t](x) / (1 + r)^t
```

其中 `t=0..H` 覆盖建设期与运营期，`R_gross` 为含税经营收入，`O_gross` 为经营运维及指定年度维护支出，`K_gross` 按建设付款计划归期；其余为实缴增值税、派生税费、实缴所得税、实际到账退税与显式净现金项。

### 税务核算核心规则

- **增值税与共享池**：含税单价 `p` 按税率 `v` 拆分为不含税单价 `p/(1+v)` 与税额 `p*v/(1+v)`。同一抵扣池在每个结算月 `k` 执行 `net[k] = output_VAT[k] - eligible_input_VAT[k] - credit[k-1]`，正值缴税、负值留抵；确认退税按声明额度扣减可用留抵并在 `arrival_period` 计入现金，评价期末未退留抵与未到账应收作为期末余额封存，不计入期内现金收益。
- **所得税与折旧**：应税收入与可扣费用按剔除已声明销项税与可抵进项后的净口径归集；直线折旧仅在资产形成且有效净基础 `B > 0` 时按 `(B - R_effective)/life_years` 计提（资本化支出不重复当期费用扣除，折旧不产生额外现金流出）；期初与新生亏损按先到期先用原则在结转年限内抵扣正应税所得，过期自动核销。
- **纯计算对照接口**：模块公开无状态纯函数（金额含税拆分、单期抵扣结转、直线折旧与亏损结转），供测试及结果验证对照使用。

### 求解能力边界

已知系数（税率、单价、固定折旧系数）与决策变量相乘采用线性或预合并表达；`max/min` 分段税务与跨期余额通过生成器精确线性化约束表达，所需有限界覆盖对应税基与累计余额，不支持的非线性或未满足能力边界在提交检查阶段按能力诊断拒收。

## 项目评价、建设时轴与封存结果

### 项目投资评价口径

基于完整现金流序列 `CF[0..evaluation_years]` 与显式评价参数计算 NPV、IRR、静态回收期与动态回收期：

- **近零判零阈值**：IRR 求根与回收期判断采用 `max(1e-6, 1e-12 * max(|CF|))` 过滤求解浮点残差（原始现金流与 NPV 保留封存精度）；全序列位于阈值内时 IRR 记为 `degenerate`。
- **回收期状态**：仅在累计净流（或贴现累计净流）先出现实质负值后再行判断——回正时按原始年轴线性插值并标 `recovered`；未回正标 `not_recovered`；未出现实质资金缺口标 `not_applicable`（`null`，不误报为 0 年）。
- **IRR 状态集合**：严格区分 `normal`、`no_solution`、`multiple`、`degenerate`、`out_of_domain` 与 `numeric_failure`。

### 建设安排与手算时轴示例

令 `K = construction_period`、`N = operation_years`、`E = K + N`：

- `t=0` 开工并按 `construction_payments` 在 `t=0..K` 计入建设付款（投产瞬间 `t=K` 无前序运营收益）；
- `t=K+j`（`j=1..N`）归集第 `j` 个完整运营年的运行净收益；仿真运行步仅覆盖投产后连续 `N` 年（依据项目基线 `start_year` 按公历推导，与价格 `base_year` 无关）。

**示例**：`K=1`、`N=2`、`E=3`，付款计划 `[{t: 0, ratio: 0.6}, {t: 1, ratio: 0.4}]`，建设总额 `C`，两年运行净额 `R1`、`R2`，则 `CF = [-0.6C, -0.4C, R1, R2]`，长度为 `4`。

### 运行期换新与指标口径

维持运行能力的维护与部件换新支出通过任务提交的 `maintenance_expenses` 清单显式申报发生评价年与金额来源（支持确定金额、`scale_unit_cost` 与 `cost_component_share` 组合，详见[公共契约](../contracts.md#年度维护金额来源)）。每笔条目在冻结清单中的 `(year, position)` 作为进项抵扣与所得税折旧/扣除的唯一身份；已纳入 O&M 的费用不再重复扣减。

### 月最大需量计费

启用 `demand_bindings` 后，按各运营自然月（由项目基线与公历推导，含闰年）绑定受电 `out` 功率接口的月最大瞬时功率乘以 `demand_charges` 费率逐月计费，纳入同一次优化并按运营年汇入年度净额。零费率绑定仍记录真实月峰值与零费用；无绑定则不产生月账单。

### 封存评价块 `financial_evaluation`

Worker 在求解获得可用解后执行一次同次评价，以 `schema: ies.financial-evaluation`（`schema_version: "1.0.0"`）随结果证据封存：

| 核心字段 | 内容说明 |
|---|---|
| 冻结输入回显 | `evaluation_years`、`construction_period`、`operation_years`、`currency`、`price_basis`、`base_year`、`profile_id`、`discount_rate` |
| 建设与现金组成 | `construction_total`（及单位）、`annual_fixed_om`、`construction_units`、`payments_applied`、`net_cashflows` |
| 运营与税务明细 | `annual_operation`（含 `energy_net` / `variable_om` / `fixed_om` / `maintenance` / `demand_charge` / `net`）、`monthly_demand`、`tax_cash`、`annual_net_flows` |
| 比较与评价指标 | `comparison`（含冻结来源与差额流）、`evaluation`（逐年 `rows`、`npv`、`irr`/`irr_status`、`static_payback`/`dynamic_payback` 及 `_unit`/`_status`） |

所有金额均以十进制字符串保留原精度，页面展示与 Excel 导出直接投影该封存块。
