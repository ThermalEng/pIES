# 命名金额、支付与资产关系

> 所有者：finance；上位规范：[架构宪法](../ARCHITECTURE_CONSTITUTION.md)。
> 状态：目标契约，新增部分尚未交付。它扩展同一套财务三件套 `1.0.0`，不建立第二种财务文件、公式执行器或版本调度。
> 本页是命名明细、`amounts`、`cash_movements`、`assets` 及派生税额引用的唯一字段级正文。基础成本、能源、VAT、所得税和旧有来源种类仍见[财务 YAML](finance-yaml.md)。评价基准归[公共契约](../contracts.md#评价范围与冻结基准)。

## 金额、发生与用途三层

1. **金额定义**说明怎样取得一个数值，不自动产生现金、资产或扣除。
2. **经济发生项**具有独立身份，例如某实例某期运维、某笔维护、某命名收付或某税务结算。引用金额数值不等于引用同一笔经济发生项。
3. **用途关系**分别说明支付、计税、资本化、折旧、回收及评价。相同发生项可以合法参与不同用途，但不能重复付款或同时全额费用化与折旧扣除。

例如以初建成本的60%计算第10年换新，是新发生项，不能因为其数值依赖初建就拒绝；把同一笔初建再登记成额外付款才是重复。平台按声明的发生身份检查，不用金额相等、表达式相似或费用名字猜业务事实。

## 命名成本明细

每个现有成本分量 `finance_types.<type>.<component>` 增加可选 `items` 映射。省略等价于 `{}`。component 仍为 upfront_capex／annual_fixed_om／period_variable_om，时间含义不变。

| 字段 | 规则 |
| --- | --- |
| `items.<item_id>` | 一个独立命名费用项，item_id 为稳定局部 ID，不是 driver，也不是显示名称 |
| `items.<item_id>.fixed` | 可选 Money；upfront 为币种、annual 为币种/年；period_variable_om 仍不接受 fixed |
| `items.<item_id>.linear` | 可选 `driver → {unit_cost: Money}`，复用原 driver 绑定与单位规则 |

每项至少一个非空 fixed／linear。不同 item 可以引用同一个 driver，并保留不同费率、税务范围及发生条件。分量金额为其原 fixed、原 linear 与 items 各项的**加法**；这些是不同费用，不是同一费用的新旧两种写法。不能在原 linear 和 items 中重复登记同一笔费用后宣称程序会去重。

税基路径扩展为 `<type>.<component>.items.<item_id>`，再可选 `.fixed` 或 `.linear.<driver>`。原整分量范围覆盖全部实际叶子，包括命名项；原 `.fixed` 和 `.linear.<driver>` 只指原基础叶子。整项与子项、相交范围仍按真实叶子识别重复税基，不仅比较字符串。

装配 drivers 对实际计入的所有明细取所需 driver 的并集；一个 driver 只绑定一次，不因两项费用共用容量而造两个技术属性。实例 cost_occurrence 仍为“路径字符串→条件”的映射，新增路径 `<component>.items.<item_id>` 及其 `.fixed`／`.linear.<driver>` 叶子；条件沿用 `{kind: always}` 或 `{kind: construction_unit, unit_ref}`，不增加嵌套写法或 unit_id 别名。未覆盖的叶子为 always；整分量范围包含其全部items，整项与子项不能重叠配置。条件作用于原金额，沿用已有精确激活转换，不重复乘状态、不猜 epsilon。实例／分量的沉没成本规则不变。

下面是完整分量块；capacity_kw 必须在该类别实例的 drivers 中绑定。两项都属于 annual_fixed_om，但身份及税率可以不同：

```yaml
annual_fixed_om:
  items:
    materials:
      linear:
        capacity_kw: {unit_cost: {value: "2", unit: CNY/kW/a}}
    service:
      fixed: {value: "10", unit: CNY/a}
      linear:
        capacity_kw: {unit_cost: {value: "5", unit: CNY/kW/a}}
```

## 通用金额定义 `amounts`

Profile 与 Effective 增加可选 `amounts` 映射，省略规范化为 `{}`。每项形态唯一，不接受字符串公式别名：

| 字段 | 规则 |
| --- | --- |
| `unit` | 必填，输出单位；币种与本文件一致，可为币种或币种/年等明确费率单位 |
| `axis` | 必填，scalar／financial_year／tax_month；金额单位与期间轴分别保留，不能因按月索引就把金额误作月费率 |
| `constant` | 必填 Money，单位与 unit 一致；零常数也明确填写 |
| `terms` | 必填映射 `term_id → {coefficient: Money, source: Reference}`，无项为 `{}`；coefficient 为有限已知十进制量，可带符号，单位乘来源单位须等于输出 unit |

金额为 `constant + sum(coefficient × source)`，terms 为无序命名集合，规范化按 ID 排序。来源可以是规划决策量，系数不能是决策量。定义允许零和带符号结果；资产非负、费用幅值等约束由具体消费关系明确，不把所有金额一律截断为正数。

引用的身份、范围与期间完全显式：

| `kind` | 必需字段与含义 |
| --- | --- |
| `driver` | `finance_type, driver, scope, period`；取得绑定后的实际规模或运行聚合量，聚合沿用该 driver 声明 |
| `system_quantity` | `block, variable, aggregation, period`；引用system_equations的真实辅助量，aggregation只为integrate_positive／sum_positive，沿现有非负计量与真实步长规则在指定运营期间取量 |
| `construction_unit` | `unit_ref, period: {kind: scalar}`；引用本装配明确建设单元的同次0/1状态，单位为1；不根据容量阈值重判 |
| `cost` | `path, scope, basis, period`；path 为上节及原财务格式定义的真实成本分量/叶子路径 |
| `tariff` | `price, bindings, basis, period`；bindings 为明确非空计费 binding_id 集合，不按价格名猜范围 |
| `demand` | `charge, bindings, basis, period`；bindings 为明确非空需量 billing_id 集合 |
| `maintenance` | `year, position, basis`；原评价清单同年从1起的发生身份，不复制金额 |
| `amount` | `id, period`；引用本映射其他金额，不能直接或间接自引用 |
| `movement` | `id, basis, period`；引用下节独立经济发生项的原额、含税额、进项或净基础，不是按实付时点截断的现金总额 |
| `asset` | `id, measure, period`；measure 为 basis／depreciation／book_value，引用本页资产关系的同次值 |
| `tax` | `source, measure, period`；source 和 measure 见税额引用表 |
| `baseline` | `id, period`；引用评价确认中冻结的具名基准事实，Profile 不保存任务或其他项目指针 |

`scope` 沿用 `{kind: instances, instances: [...]}` 或 `{kind: all_new_instances}`，所属类别及实例归属在项目公开边界解析；all_new_instances 仅指本次装配该财务类别的新建实例，不包含既有资产。bindings／instances 须非空、拒绝重复 ID，按稳定 ID 排序。显式all_new_instances在该项目没有匹配实例时产生零，不借用首个实例。

amount中的driver引用也构成实际使用，须提供真实绑定；不能因为该实例的初建分量属于沉没成本就让这个已被新公式使用的driver缺席。source的kind限定自身表中的字段，未知核心字段和其他kind的字段均拒绝。

system_quantity绑定原技术变量，不从已导出的序列结果回流。它不创建计费点或自动授予交易身份；有符号量先由技术模型明确分解为可计量非负量。该引用另需modeling.system_equations能力，不要求用户为了费用计算额外请求发布曲线。

`basis` 恰为 raw／gross／deductible_input／net_basis／net_revenue：raw沿来源的原额口径（原成本/计费沿Profile.price_basis，维护为原实际含税付款，movement沿其显式price_basis），gross 是实际含税额，deductible_input 是明确规则产生的可抵进项，net_basis 为支出的gross减该可抵额，net_revenue为收入gross减明确销项税。后两者分别用于支出基础与收入，不混用；非适用方向的basis在公开输入边界拒绝。含税输入无规则时gross等于raw，可抵额/销项为零；不含税输入且无适用税则时不凭空加税，gross同样等于raw。net_revenue不是税后净利润，也不表示已扣所得税。tariff引用为raw_charge及其对应含税额（不预乘sale的负号），购售现金符号由价格的显式direction决定；引用净记账贡献时必须在金额系数中明确符号，不能重复翻转。

`period` 是以下判别联合，不通过数组位置或公历年猜索引：

- `{kind: scalar}`：无期间量，如初建总额、容量、年度费率。
- `{kind: current}`：仅绑定当前 amount.axis 的同轴实际期间；scalar 定义不能使用。
- `{kind: financial_year, year}` 或 `{kind: tax_month, month}`：指定实际评价年／月，整数从1开始；引用含时点0的movement或基准CF时允许financial_year=0，不把它当成运行年。
- `{kind: year_months, year}`：明确汇总该评价年的12个税务月；year 可为正整数或字面值 `current`，后者只用于 financial_year 定义。
- `{kind: range, axis, first, last}`：同轴闭区间之和，axis 为 financial_year／tax_month，first、last 为正整数。

成本引用选择scalar时，upfront为一次性金额，annual_fixed_om为年费率；选择financial_year时annual_fixed_om为该运营年金额、非运营年为零；period_variable_om选择具体期间则按真实步范围聚合。tariff/demand为指定期间账单，不能选scalar费率。driver的property绑定仅为scalar；interface绑定按其已声明聚合在所选运营期间取量，不假设单年复制。

asset的basis只用scalar，depreciation/book_value只用financial_year，不把年度折旧插值为月度量。税额引用按上表所属轴取值；只有显式year_months/range才作汇总，余额/应收的期间值仍是存量，不因可引用就自动产生现金。construction_unit用于明确的条件金额；amount.constant保持无条件，不把所有固定金额统一乘建设状态。

费率乘单位为year、值为"1"的已知系数可形成每个运营年的金额；功率先按 driver 的真实 step_duration 积分。不隐式年化、不把年费率当一次性付款、不以平均价格乘电量。期间是否落在本次 E=K+N 范围内在提交边界核对一次。

### 税额输出引用

| source | measure | 单位与含义 |
| --- | --- | --- |
| `{kind: rule, id}`，VAT规则 | output_tax／deductible_input | 本规则该月确认的销项／可抵进项，币种；不是实缴 |
| `{kind: pool, id}` | paid／refund_confirmed／refund_received／credit_balance／receivable | 池的实缴、确认退款、到账退款、留抵或应收；币种，按实际期间分别引用 |
| `{kind: rule, id}`，所得税规则 | taxable_base／loss_used／assessed／paid | 当年亏损抵扣前基础、亏损使用、应计及实缴所得税；当前无支付滞后时后两者数值相同，但含义不合并 |
| `{kind: rule, id}`，派生税费 | assessed／paid | 下节规则的本期应计或实际支付 |

共享 VAT 池实缴只归池，不复制为每个 VAT 规则的实缴税额。余额与应收不是现金，不因进入金额公式就自动支付或回收。允许已知费率乘税额输出，不按税目名称生成计算分支。

assessed按应计期引用，paid按实际支付期引用；派生规则有payment_lag时不能把原应计期的数额当作当期paid。对应期没有该项实际支付为已知零，不以未发布的规则/量补零。

每个 source/measure 必须有同次构模声明及公开封存出口，逐项对应关系以[税额引用与封存字段](solver-bundle.md#税额引用与封存字段)为唯一正文；本次金额引用和基准税额选择器使用同一身份、轴及期间规则。规则级销项/进项不能从共享池实缴反推，池应收也不能在读取时用退款清单或当前配置重算。未发布对应事实的证据不可作为该量的基准来源。

命名金额及冻结基准进入规划约束的最小入口见[金额引用约束](assembly-yaml.md#金额引用约束)：只将明确期间的值绑定为本条标量关系的局部名称，不把财务引用放进技术系统方程。

## 具名收付 `cash_movements`

Profile 与 Effective 增加可选映射，省略为 `{}`。每个 movement_id 定义一个独立经济发生项，其金额定义与付款计划分开：

| 字段 | 必需与语义 |
| --- | --- |
| `amount` | 必填 Reference，仅 kind: amount；指向 quote 量纲为本币金额的 scalar 或逐期金额定义 |
| `price_basis` | 必填 tax_inclusive／tax_exclusive；明确该发生项quote的含税口径，不从金额公式或别的来源的gross/net字段猜测 |
| `direction` | 必填 inflow／outflow；决定现金正向。金额为负时保留其反向效果，不取绝对值 |
| `recognition` | 必填 `{kind: at, at: TimePoint}` 或 `{kind: each, axis, first, last}`；后者 axis 为 financial_year／tax_month、整数闭区间，每期形成独立发生项 |
| `payments` | 必填有序 `[{offset, ratio}]`；offset 为从每笔recognition起算的整数期间，负值明确表示预付款，单位沿其轴；ratio 为非负十进制字符串，精确合计1 |

TimePoint 恰为 `{kind: financial_year, year}` 或 `{kind: tax_month, month}`；financial_year 此处是现金时点，允许0。each 的期间从1开始。月末支付 p 汇入 CF[ceil(p/12)]；年时点 y 汇入 CF[y]，不改写建设付款的原有时点规则。金额 axis 必须为 scalar 或与 recognition 同轴，逐期定义按 current 绑定。payments 即使单笔立即支付也填写 `[{offset: 0, ratio: "1"}]`。

实际发生身份为 `(movement_id, recognition轴, recognition期间)`，不只是 movement_id。at 产生一个身份，each 为闭区间每期各产生一个身份；同额、同一数值配方或同一付款月不会合并身份。结果 `cash_movements` 每行的 `(id, axis, period)` 保留该身份，付款、进项确认及资产引用均绑定它。财务 Reference 中的 movement.period 选择发生期，不选择实付期或开票期。

recognition须在评价期内，付款计算所得年时点不得小于0、税务月不得小于1。约定付款超出评价期时作为明确期末应付／应收封存，不进入期内现金，也不自动变成终期回款；完整义务金额不因现金评价截断而消失。引用本币，不新增换汇系统。

movement 不自动费用化、资本化或免税。税务规则和资产关系引用它的发生身份；原有成本、计量账单、维护与税款仍由各自关系拥有现金，不为了接入新目录复制成 movement。纯数值配方被另一笔换新或服务复用时是新发生项；输入不得以多个用途把同一发生项再支付一遍。

amounts本身只是数值关系，不自动取得新的开票/纳税身份。movement的price_basis决定其原额如何形成含税收付和税基，已得到gross的来源若用于同一发生项不得再次加税；若声明为另一笔真实交易，则按新交易明确的口径处理，不按金额相等推断。

原 `net_cashflows` 继续只接收明确的税后净现金。其 amount 联合增加 `{kind: amount_ref, id}`，引用本页 scalar 本币金额，负值不允许；原 fixed/source_share 不变。它不进入 VAT/CIT，也不能用于绕过尚未声明的税务处理。

## 资本化与计提 `assets`

Profile 与 Effective 增加可选 assets 映射，省略为 `{}`。每项具有稳定 asset_id，不从 finance_type 或名称推断资产：

| 字段 | 规则 |
| --- | --- |
| `basis_from` | 必填 Reference：kind为cost（path限定upfront_capex及其子项）、maintenance或movement，basis必须net_basis；亦可引用amount组合，但其全部支出根须为上述净基础并保留实际发生身份及份额，不接收税款余额、主营利润或人为重填的基础 |
| `capitalized_at` | 必填 TimePoint，资本化时点；不是付款时点 |
| `available_from` | 必填正整数运营年，表示可开始计提的最早运营年 |

年度税务粒度下，年时点c资本化须满足`c < K+available_from`，月p资本化须满足`p <= 12*(K+available_from-1)`；同年年末资本化不能取得当年全年折旧。资产本身无逐期定义，basis_from不能使用current，须指定明确发生期。资产基础 B 非负，并保留各来源发生身份；同一来源的资产分配份额合计不得超过1，不接受未知或决策型分摊系数。不同时点形成的独立资产不能仅为求和而抹去计提起点。

用于组合资产基础的amount须constant为零、terms为净基础引用乘非负无量纲已知系数；递归展开后按实际发生身份计算分配份额。它不提供“手填基础金额”或任意利润资本化入口。固定金额可以先声明真实movement，再由该发生项的net_basis进入资产；金额公式自身不替代支出身份。

所得税 depreciation.base_from 增加 `{kind: asset, id}`。method、life_years、residual、first_year 沿原契约；first_year 仍为运营年，不早于 available_from。初建/维护原来源继续合法，但同一实际基础只能被认领一次，不得用 asset、类别总额和来源子集重复计提。期间冲突在可取得本次输入的公开边界检查。

固定货币残值与比例残值并存，沿用已有精确 B=0／B>0 语义；资产账面余值不自动变成现金回收。资产形成、付款、进项确认和折旧各有期间：某具名资本性发生项100，K=1，40%在时点0、60%在时点2付尾款，资产在时点1形成并从运营第1年计提时，不能把首年折旧基础缩成已付款40，也不能在计提时再扣一次现金。这不改变原初建construction_payments限于0..K的规则；延期尾款须用本节明确发生项/资产关系表达，不能暗扩原字段范围。

下面给出该例的完整新增字段块，嵌入已有合法Profile；上下文明示币种CNY、K=1、E至少为2、无该项VAT，不是工程默认配置：

```yaml
amounts:
  purchase_quote:
    unit: CNY
    axis: scalar
    constant: {value: "100", unit: CNY}
    terms: {}
cash_movements:
  purchase_invoice:
    amount: {kind: amount, id: purchase_quote, period: {kind: scalar}}
    price_basis: tax_inclusive
    direction: outflow
    recognition: {kind: at, at: {kind: financial_year, year: 0}}
    payments:
      - {offset: 0, ratio: "0.4"}
      - {offset: 2, ratio: "0.6"}
assets:
  asset_a:
    basis_from:
      kind: movement
      id: purchase_invoice
      basis: net_basis
      period: {kind: financial_year, year: 0}
    capitalized_at: {kind: financial_year, year: 1}
    available_from: 1
```

### 派生税费

tax_rules 增加 `kind: derived_tax`，完整字段为 `label_ref, axis, base_from, rate, payment_lag`：

- axis 必填 tax_month／financial_year；base_from 为本页 kind: tax 引用，period 必须同轴 current，或显式的跨轴 year_months 汇总。
- rate 为已知非负有限 Money，单位 `"1"`；应计额＝base_from × rate。它不是“税种名→计算方法”的注册表。
- payment_lag 为非负整数，单位沿 axis；支付期＝应计期＋滞后。期外应付不算期内现金，税费自身不再写第二笔 movement。
- 所得税规则增加 `movement_revenue`、`movement_cost`、`derived_tax_cost` 三个可选 ID 列表；各自明确收入、可扣费用及可扣派生税费，省略为空。movement收入按net_revenue入发生年度税基，费用按net_basis在发生年度扣除，不随收付款计划移动；税务月p归评价年ceil(p/12)，年时点y>=1归年y，开工瞬间y=0属于第1税务年度而现金仍在CF[0]。派生税费按应计年度扣除，不把实缴日期暗作权责发生日期。
- VAT规则增加可选 `movement_input_tax: [{movement, rate, deductible_ratio, confirmation}]`、`movement_output_tax: [{movement, rate, confirmation}]`。movement 指定定义ID，输入对应outflow、输出对应inflow；该条目覆盖此定义的全部实际发生项，确认方式见下节。

同一来源只确认一次；分期付款不重复开票计税。没有相应规则则不猜税额、抵扣或扣除资格。资本化的movement不得同时列为全额movement_cost；零现金、免税、不可抵扣与不可费用扣除是不同事实。

引用图按原金额、税额、净基础、资产及当期/跨期依赖构造。同期间循环（例如所得税引用一个又依赖该所得税的可扣费）属于输入引用错误，不按字段顺序求值；现有抵扣池与亏损递推继续由原税务关系表达，不开放任意金融状态脚本。

### 收付发生项的VAT确认

movement条目的confirmation为以下判别联合；仅用于本页新增收付，不改变原建设进项确认字段：

条目的rate、deductible_ratio沿原VAT规则使用无量纲Money；rate非负，deductible_ratio为0..1，不混用裸数或字符串比例别名。下表schedule中的ratio则沿原确认清单使用十进制字符串。

| method | 完整形态与语义 |
| --- | --- |
| `recognition_period` | 仅 `{method: recognition_period}`；逐发生项在其recognition对应税务月确认。月p映射p，年y>=1映射12y；年时点0不得使用 |
| `explicit_schedule` | `{method: explicit_schedule, occurrences: [{at: TimePoint, schedule: [{period, ratio}]}]}`；at明确选择本movement的一个实际发生项，每项schedule只分配该次发生的税额，不分配整个movement的合计额 |

explicit_schedule对at和each均采用同一形态。occurrences须恰好覆盖recognition产生的全部发生身份各一次，不接受缺项、额外at或重复at；按at期间排序。每份schedule非空，period为绝对税务月1..12E且不重复，按period排序；非负十进制ratio逐份精确合计1，不能将多次发生的ratio合计一次。确认可晚于发生或付款；不把发生年当作固定开票年。年时点0同样用明确的at及开票月，不夹成月1。

例如E至少为3，service在评价年1、2各发生一笔不含税支出100，税率0.1、全额可抵，立即付款；两次进项分别在月13、25确认。新增VAT条目为：

```yaml
movement_input_tax:
  - movement: service
    rate: {value: "0.1", unit: "1"}
    deductible_ratio: {value: "1", unit: "1"}
    confirmation:
      method: explicit_schedule
      occurrences:
        - at: {kind: financial_year, year: 1}
          schedule: [{period: 13, ratio: "1"}]
        - at: {kind: financial_year, year: 2}
          schedule: [{period: 25, ratio: "1"}]
```

每次含税支付110、可抵额10、净基础100；本规则月13、25的deductible_input各10，其余月为零。发生项结果中的可抵总额仍分别为10，不因支付或确认分期复制；确认月进入规则级VAT事实，再汇入所属池，不把两次总额20重复用于每个开票月。

重复认领按实际发生身份检查：同一方向的发生项只能由一个VAT条目拥有，不能在同规则列表、其他规则或另一池再确认；不是按金额相等判重。本契约不引入分票工作流，分期比例只在该发生项自己的schedule内表达。Overrides仍按所属rule、条目列表名和movement_id匹配，不能用数组下标或开票月另造条目身份；具体覆盖权限见下节。

## 合并、冻结与能力边界

| 输入 | 覆盖纪律 |
| --- | --- |
| 命名成本 items | Profile 声明身份；Overrides 只原子替换既有 fixed/linear 单价，不增删 item 或 driver |
| amounts | Profile 声明项、轴、单位、term及来源种类；Overrides 可替换既有 constant/coefficient Money、明确实例/binding范围和固定期间；不得改单位、轴、来源 kind/path/id 或增删 term |
| cash_movements | Profile 声明身份、方向、price_basis及金额引用；Overrides 可整体替换 recognition 与 payments，不能换方向、金额口径或来源 |
| assets | Profile 声明身份与来源；Overrides 可整体替换来源中的项目实例范围、capitalized_at 与 available_from；不能替换来源种类或把无关金额变为资本化来源 |
| derived_tax | Overrides 只替换既有 rate 与 payment_lag；税基、来源、轴和规则身份不变 |

movement的VAT条目在所属rule及movement_input_tax/movement_output_tax列表内按movement_id匹配，只可替换rate/deductible_ratio及既有explicit_schedule的完整occurrences数组；confirmation.method、movement定义ID与池归属不变，不逐开票行拼接。合并后的occurrences须对应合并后recognition产生的发生集合；若Overrides改变each范围，须一并给出匹配的完整occurrences，否则在项目采用/提交公开边界报告具体缺失或多余的发生期。不在冻结读取或Worker中重复匹配。所得税的movement_revenue/movement_cost/derived_tax_cost是Profile范围，Overrides不增删；本页资产basis_from的实例范围覆盖不改变这一点。

省略覆盖保留原项；不接受 null 删除、逐元素列表拼接或“最新”来源。Effective 保存完整命名结构和经明确覆盖的参数。Profile 无法确认的项目实例/期间在项目采用或提交的真实输入边界绑定；内部冻结和恢复不再校验。修改方法、范围类型或新增费用身份时发布 Profile 新 revision，而非偷偷改变 Overrides 权限。

实际计入实例成本的命名项、实际消费的金额关系、收付、资产与派生税分别要求 `finance.named_items`、`finance.amount_references`、`finance.cash_movements`、`finance.assets`、`finance.derived_tax`；还要求同次 financial_facts 恢复。闲置财务类别或金额定义不自动计现金/税，也不单独触发其执行能力；其文件内引用仍须合法。引用movement/asset/tax/baseline等来源时同时要求其实际关系能力，不能只凭amount_references标记认定全部已接通。执行能力发布必须覆盖输入→合并→冻结→主模型→同次恢复，缺失沿 TASK-REQ-007 返回触发字段，不静默忽略后按旧金额求解。

全部关系进入现有关系汇集和统一分析，条件转换只查询实际所需界；不另设金额预求解或参考容量代入。固定成本可能是零、无条件、随建设发生，或随规模变化，均按声明处理，不增加案例名和币种名分支。

## 最小可复核关系

以下为手算约束，不是工程默认值或独立上传文件：

| 输入 | 应得到的事实 |
| --- | --- |
| q=3kW，上述两项年费；materials进项10%全抵，service无进项 | 两项分别6、25 CNY/年，总额31；不得合并单价后对31整项抵扣 |
| 同一100CNY发生项，支付40%/60%，净基础100，直线5年、残值0 | 现金40和60各一次，折旧每年20；支付进度不缩小已形成基础 |
| 已确认实缴税额10，派生税费率0.04 | 应计0.4；引用销项、留抵或退款都不能代替paid；滞后改变支付期而不改应计额 |
| 净现金回收引用9CNY/kW×3kW | 支付/回收金额27随同次规模变化，账面残值不额外产生第二笔回款 |

## 结果所有权

成本、金额、发生项、资产及税额均以自己的稳定身份封存，不按显示名称或 solver 列名恢复。具体字段归 [Bundle关系事实](solver-bundle.md#关系事实映射)，这里不复制输出表。页面与导出只消费同一证据；已付款、应付、净基础、折旧和账面值分别展示，不把余额计现金，不在解后重建税务或资本化关系。
