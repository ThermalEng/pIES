# 财务模型手算预期

> 文档状态：合成样例手算预期；对应[线性所得税 + 直线折旧模型](financial-model-linear-income-tax.yaml)、[累进所得税 + 双倍余额递减折旧模型](financial-model-progressive-tax-ddb.yaml)与[维护清单 + 指标消费模型](financial-model-maintenance-metrics.yaml)。
> 完全合成数据，不是税法推荐或隐式业务默认。
> 本页数值即事实夹具（`facts-*.yaml`）`expected` 的发布口径；本轮经两路复核对照：按完整模型文件驱动的复算器与不解析模型语法的直接手算公式（脚本仅在容器 `/tmp` 临时使用，不属生产代码）。Schema 校验只证明文件结构，语义以本页预期与两路复算为准。

## 时间语义（与[财务模型 YAML 契约](../financial-model-yaml.md)一致）

- `year=0`：开工年（建设期起点）；`year=K`：投产年（建设期末，第 0 个运营年）；`year=K+j`：第 j 个完整运营年（j=1..N，`op_year = year - K`）。
- 建设期与投产年（`year=0..K`）：运行事实（收入、成本）为 0，但建设付款按评价确认的付款计划取值、建设期非零；运营期（`year=K+1..K+N`）：有运行收益和成本。
- `active.from_op_year` / `active.to_op_year`：相对于运营年（1-based），不是绝对财务年；区间外的非状态关系式取 0，状态变量保持前值。
- `[year-1]` 只允许引用状态变量；参数与标量输入裸引用，年度序列输入必须带 `[year]`。

## 折现率与指标口径

- 折现率 `r` 取冻结评价确认的 `discount_rate`（样例 A/B r=0.08，样例 C r=0.10），模型不设默认。
- 指标输出（`kind: npv|irr|payback_static|payback_dynamic`）由 `source` 指向一条**已声明现金流**的序列输出；税前/税后口径由该序列的 `cashflow.tax_basis` 机器识别，不看输出 ID 命名、不解析 `description`。框架按声明计算：`npv = Σ_{y=0}^{K+N} cf[y] / (1+r)^y`；`irr` 为 `Σ cf[y] / (1+x)^y = 0` 的根；静态／动态回收期为累计（动态先贴现）首次非负、年内线性插值。
- 仅 `kind: npv` 输出可作规划目标，序列输出与 `irr`／回收期不可；系统不提供默认目标。

## 样例 A：线性所得税 + 直线折旧模型

### 完整输入

**时间参数**：建设期 K=1 年，运营期 N=3 年，评价期 K+N=4 年。

**参数值**（项目财务配置 `parameters_values`）：

- `income_tax_rate` = 0.25
- `depreciation_years` = 10

**输入绑定**（项目财务配置 `input_bindings`；同类分量以显式列表唯一定位，不隐式聚合）：

- `operating_revenue` ← [`tariff_binding: pv_export_1`]（`pv_export_1__period_energy_sale = -200,000`，取反后为 +200,000；未列出的 `pv_export_2` 不参与）
- `operating_cost` ← [`instance: pv_system_1`, `instance: pv_system_2`]（`pv_system_1__period_variable_om = 30,000` 与 `pv_system_2__period_variable_om = 20,000` 显式求和 = 50,000）

**事实输入**（同次求解/评价确认）：

- 建设总额 `initial_cost`（`construction_total`，求解前为决策表达式、此处为同次解的事实值）= 1,000,000 CNY
- 付款计划：t=0 付 60%（600,000），t=1 付 40%（400,000）
- `construction_payment[year] = construction_total × Σ ratio_t`：第 0 年 600,000，第 1 年 400,000，其余 0

### 手算过程

```
年折旧 = initial_cost / depreciation_years = 1,000,000 / 10 = 100,000 CNY
（active 区间 = 运营年 1..10，即 year=2..11，本评价期内为 year=2..4）

应税利润 = operating_revenue - operating_cost - 年折旧
        = 200,000 - 50,000 - 100,000 = 50,000 CNY

所得税 = max(0, 应税利润) × income_tax_rate = 50,000 × 0.25 = 12,500 CNY

税前现金流 = operating_revenue - operating_cost - construction_payment
税后现金流 = operating_revenue - operating_cost - 所得税 - construction_payment
          = 200,000 - 50,000 - 12,500 - construction_payment
```

### 完整输出（模型 `relations` 逐年值）

| year | op_year | 阶段 | construction_payment | operating_revenue | operating_cost | depreciation | taxable_income | income_tax | before_tax_cashflow | after_tax_cashflow |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | — | 开工 | 600,000 | 0 | 0 | 0 | 0 | 0 | -600,000 | -600,000 |
| 1 | 0 | 投产 | 400,000 | 0 | 0 | 0 | 0 | 0 | -400,000 | -400,000 |
| 2 | 1 | 运营1 | 0 | 200,000 | 50,000 | 100,000 | 50,000 | 12,500 | 150,000 | 137,500 |
| 3 | 2 | 运营2 | 0 | 200,000 | 50,000 | 100,000 | 50,000 | 12,500 | 150,000 | 137,500 |
| 4 | 3 | 运营3 | 0 | 200,000 | 50,000 | 100,000 | 50,000 | 12,500 | 150,000 | 137,500 |

### NPV 标量

```
r = 0.08（冻结 discount_rate，无默认）

税前 NPV（before_tax_npv）=
  -600,000/1.08^0 - 400,000/1.08^1 + 150,000/1.08^2 + 150,000/1.08^3 + 150,000/1.08^4
  = -612,440.233253 CNY

税后 NPV（after_tax_npv）=
  -600,000 - 400,000/1.08 + (137,500/1.08^2 + 137,500/1.08^3 + 137,500/1.08^4)
  = -642,267.744679 CNY
```

### 规划目标示例

```yaml
# planning-linear-income-tax.yaml（节选）
objective:
  sense: maximize
  expression: after_tax_npv      # 模型 outputs 的 kind: npv 标量；序列输出不可作目标，系统不提供默认目标
```

---

## 样例 B：累进所得税 + 双倍余额递减折旧 + 亏损结转模型

### 完整输入

**时间参数**：建设期 K=1 年，运营期 N=5 年，评价期 K+N=6 年。

**参数值**（项目财务配置 `parameters_values`）：

- `tax_bracket1_rate` = 0.15，`tax_bracket1_limit` = 50,000 CNY，`tax_bracket2_rate` = 0.25
- `ddb_rate` = 0.2，`salvage_value` = 50,000 CNY，`depreciation_years` = 3

**输入绑定**：与样例 A 相同（`operating_revenue` ← [`pv_export_1`]；`operating_cost` ← [`pv_system_1`, `pv_system_2`] 显式求和）。

**事实输入**（同次求解/评价确认）：建设总额 1,000,000 CNY；付款计划 t=0 付 60%（600,000）、t=1 付 40%（400,000）；年收入 200,000 CNY；年成本 50,000 CNY。

### 手算过程

```
年折旧 = min(ddb_rate × book_value[year-1], book_value[year-1] - salvage_value)
（active 区间 = 运营年 1..3，即 year=2..4；book_value 初值 = year=-1 存量 = 1,000,000）

book_value[year] = book_value[year-1] - 年折旧（状态；区间外不更新，保持前值）

第 1 运营年（year=2）：
  折旧 = min(0.2 × 1,000,000, 1,000,000 - 50,000) = 200,000；book_value = 800,000
  税前利润 = 200,000 - 50,000 - 200,000 = -50,000（亏损）
  亏损新增 = max(0, -税前利润) = 50,000；亏损使用 = min(0, max(0, 税前利润)) = 0
  亏损余额 = 50,000；应税利润 = -50,000；所得税 = 0

第 2 运营年（year=3）：
  折旧 = min(0.2 × 800,000, 800,000 - 50,000) = 160,000；book_value = 640,000
  税前利润 = -10,000；亏损新增 = 10,000；亏损使用 = 0；亏损余额 = 60,000
  应税利润 = -10,000；所得税 = 0

第 3 运营年（year=4）：
  折旧 = min(0.2 × 640,000, 640,000 - 50,000) = 128,000；book_value = 512,000
  税前利润 = 22,000；亏损新增 = 0；亏损使用 = min(60,000, max(0, 22,000)) = 22,000
  亏损余额 = 38,000；应税利润 = 22,000 - 22,000 = 0；所得税 = 0

第 4 运营年（year=5，超过折旧截止）：
  折旧 = 0；book_value = 512,000（保持）
  税前利润 = 150,000；亏损使用 = min(38,000, 150,000) = 38,000；亏损余额 = 0
  应税利润 = 150,000 - 38,000 = 112,000
  所得税 = 50,000 × 0.15 + (112,000 - 50,000) × 0.25 = 7,500 + 15,500 = 23,000

第 5 运营年（year=6，超过折旧截止）：
  折旧 = 0；book_value = 512,000（保持）
  税前利润 = 150,000；亏损余额 = 0；应税利润 = 150,000
  所得税 = 50,000 × 0.15 + (150,000 - 50,000) × 0.25 = 7,500 + 25,000 = 32,500

税前现金流 = operating_revenue - operating_cost - construction_payment
税后现金流 = operating_revenue - operating_cost - 所得税 - construction_payment
```

### 完整输出（模型 `relations` 逐年值）

| year | op_year | 阶段 | depreciation | book_value | taxable_income_before_loss | loss_carryforward_used | loss_carryforward_balance | taxable_income | income_tax | before_tax_cashflow | after_tax_cashflow |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | — | 开工 | 0 | 1,000,000 | 0 | 0 | 0 | 0 | 0 | -600,000 | -600,000 |
| 1 | 0 | 投产 | 0 | 1,000,000 | 0 | 0 | 0 | 0 | 0 | -400,000 | -400,000 |
| 2 | 1 | 运营1 | 200,000 | 800,000 | -50,000 | 0 | 50,000 | -50,000 | 0 | 150,000 | 150,000 |
| 3 | 2 | 运营2 | 160,000 | 640,000 | -10,000 | 0 | 60,000 | -10,000 | 0 | 150,000 | 150,000 |
| 4 | 3 | 运营3 | 128,000 | 512,000 | 22,000 | 22,000 | 38,000 | 0 | 0 | 150,000 | 150,000 |
| 5 | 4 | 运营4 | 0 | 512,000 | 150,000 | 38,000 | 0 | 112,000 | 23,000 | 150,000 | 127,000 |
| 6 | 5 | 运营5 | 0 | 512,000 | 150,000 | 0 | 0 | 150,000 | 32,500 | 150,000 | 117,500 |

注：`loss_carryforward_new` 在 year=2 为 50,000、year=3 为 10,000，其余为 0；`book_value` 是状态变量，折旧截止后不再更新、保持 512,000。

### NPV 标量

```
r = 0.08（冻结 discount_rate，无默认）

税前 NPV（before_tax_npv）=
  -600,000 - 400,000/1.08 + 150,000 × (1/1.08^2 + 1/1.08^3 + 1/1.08^4 + 1/1.08^5 + 1/1.08^6)
  = -415,827.309665 CNY

税后 NPV（after_tax_npv）=
  -600,000 - 400,000/1.08 + 150,000 × (1/1.08^2 + 1/1.08^3 + 1/1.08^4) + 127,000/1.08^5 + 117,500/1.08^6
  = -451,961.236071 CNY
```

### 规划目标示例

```yaml
# planning-progressive-tax-ddb.yaml（节选）
objective:
  sense: maximize
  expression: after_tax_npv      # 换模型即换计算方法；目标显式引用模型标量输出
```

---

## 样例 C：维护清单 + 指标消费模型

### 完整输入

**时间参数**：建设期 K=1 年，运营期 N=3 年，评价期 K+N=4 年；折现率 r=0.10。

**参数值**（项目财务配置 `parameters_values`）：`income_tax_rate` = 0.20，`depreciation_years` = 10。

**输入绑定**（与 A 同为显式列表）：`operating_revenue` ← [`tariff_binding: pv_export_1`]（`pv_export_1__period_energy_sale = -700,000`，取反后为 +700,000）；`operating_cost` ← [`instance: pv_system_1`, `instance: pv_system_2`]（60,000 + 40,000 = 100,000）。

**事实输入**（同次求解/评价确认）：建设总额 1,000,000 CNY；付款计划 t=0 付 60%（600,000）、t=1 付 40%（400,000）；维护清单：year=2 发生 40,000、year=4 发生 20,000（其余年份 0，见 `materialized.evaluation.maintenance_expenses`）。

### 手算过程

```
年折旧 = 1,000,000 / 10 = 100,000 CNY（active 运营年 1..10，本评价期内为 year=2..4）

应税利润 = operating_revenue - operating_cost - maintenance_expense - 年折旧
税前净流（gross_project_flow）= operating_revenue - operating_cost - maintenance_expense - construction_payment
税后净流（net_project_flow） = 税前净流 - 所得税

第 1 运营年（year=2）：应税 = 700,000 - 100,000 - 40,000 - 100,000 = 460,000；税 = 92,000
  税前净流 = 700,000 - 100,000 - 40,000 - 0 = 560,000；税后净流 = 468,000
第 2 运营年（year=3）：应税 = 700,000 - 100,000 - 0 - 100,000 = 500,000；税 = 100,000
  税前净流 = 600,000；税后净流 = 500,000
第 3 运营年（year=4）：应税 = 700,000 - 100,000 - 20,000 - 100,000 = 480,000；税 = 96,000
  税前净流 = 580,000；税后净流 = 484,000
```

维护费由模型关系式显式计入应税所得与现金流各一次（不在模型之外另扣）：year=3 无维护事实，维护输入为 0。

### 完整输出（模型 `relations` 逐年值）

| year | op_year | 阶段 | construction_payment | operating_revenue | operating_cost | maintenance_expense | depreciation | taxable_income | income_tax | gross_project_flow | net_project_flow |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | — | 开工 | 600,000 | 0 | 0 | 0 | 0 | 0 | 0 | -600,000 | -600,000 |
| 1 | 0 | 投产 | 400,000 | 0 | 0 | 0 | 0 | 0 | 0 | -400,000 | -400,000 |
| 2 | 1 | 运营1 | 0 | 700,000 | 100,000 | 40,000 | 100,000 | 460,000 | 92,000 | 560,000 | 468,000 |
| 3 | 2 | 运营2 | 0 | 700,000 | 100,000 | 0 | 100,000 | 500,000 | 100,000 | 600,000 | 500,000 |
| 4 | 3 | 运营3 | 0 | 700,000 | 100,000 | 20,000 | 100,000 | 480,000 | 96,000 | 580,000 | 484,000 |

### 指标值（均消费同一条已声明现金流）

```
r = 0.10（冻结 discount_rate，无默认）

gross_discounted_total（npv，source=gross_project_flow，cashflow.tax_basis=before_tax）=
  -600,000 - 400,000/1.10 + 560,000/1.10^2 + 600,000/1.10^3 + 580,000/1.10^4
  = 346,110.238372

net_discounted_total（npv，source=net_project_flow，cashflow.tax_basis=after_tax）=
  -600,000 - 400,000/1.10 + 468,000/1.10^2 + 500,000/1.10^3 + 484,000/1.10^4
  = 129,376.408715

net_yield_rate（irr，source=net_project_flow）= 0.155465（Σ net_project_flow[y]/(1+x)^y = 0）

capital_recovery_static（payback_static，source=net_project_flow）=
  累计 -600,000 / -1,000,000 / -532,000 / -32,000 / +452,000 → 3 + 32,000/484,000 = 3.066116

capital_recovery_discounted（payback_dynamic，source=net_project_flow）=
  贴现流 -600,000 / -363,636.363636 / 386,776.859504 / 375,657.400451 / 330,578.512396
  累计首次非负在第 4 年 → 3 + 201,202.103681/330,578.512396 = 3.608636
```

### 维护定向复核（改变某年维护金额 → 对应年现金流与 NPV 正确变化）

```
把 year=2 的维护从 40,000 改为 90,000（其余不变）：
  year=2 税前净流: 560,000 → 510,000（Δ = -50,000，仅一次扣减；若重复扣减会变成 460,000）
  year=2 所得税:   92,000 → 82,000（Δ = -10,000，维护在模型中显式税前扣除）
  year=2 税后净流: 468,000 → 428,000（Δ = -40,000）
  year=3/4 与 year=0/1 不变；gross_discounted_total Δ = -50,000/1.10^2 = -41,322.314050
  net_discounted_total Δ = -40,000/1.10^2 = -33,057.851240
```

### 规划目标示例

```yaml
# planning-maintenance-metrics.yaml（节选）
objective:
  sense: maximize
  expression: net_discounted_total   # 模型声明的 npv 输出；ID 不使用 before_tax/after_tax 命名，目标识别不看名字
```

## 说明

- 三样例的逐年表与指标值即对应事实夹具（`facts-*.yaml`）的 `expected`，逐字段一致。
- 折旧年限是参数：把样例 A 的 `depreciation_years` 改为 2、样例 B 改为 5，折旧截止与税金随之变化（定向验证见交付报告，脚本同样对照直接手算公式）。
- 封存评价块由模型输出驱动：逐年明细与指标值来自 `model_yearly`／`model_metrics`，口径按同任务冻结 `model_rules.outputs` 的 `cashflow.tax_basis`／`source` 识别；框架不再另存基础现金流或平行指标（样例 C 的输出 ID 刻意不使用 `before_tax`/`after_tax` 命名，即验证口径识别不依赖 ID）。
