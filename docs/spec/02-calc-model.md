# 02 计算引擎数学模型规格

> 版本: 0.1(草案)
> 状态: 设计规格
> 适用范围: IES Plan 综合能源规划软件(backend 计算引擎 / 求解器适配层)
> 配套文档: docs/spec/ 系列(01 数据库 schema、03 任务调度、04 受控注册表与诊断体系)

---

## 0. 范围、约定与术语

### 0.1 文档定位

本文档定义 IES Plan 计算引擎的**数学模型规格**,是求解器适配层(Solver Adapter Layer)直接实现依据。本文只定义"模型是什么",不定义"用哪个求解器";所有公式均按标准 MILP 构造给出,适配层只需把 §11 的构建管线映射到具体求解器 API。

### 0.2 版本 1 设备范围

| 编号 | 设备 | 输入输出 | 备注 |
|---|---|---|---|
| D1 | 电网连接 | 购电/售电 | 分时电价、需量费、并网容量、可禁止反送电 |
| D2 | 光伏 | 直流电→交流电 | 温度、辐照(法向/水平/散射)、朝向、倾角 |
| D3 | 电池储能 | 充/放电 | 充放互斥、SOC、容量衰减、循环寿命、更换 |
| D4 | 热泵 | 电→热/冷 | 供热/供冷双模式、COP 随环境温度变化 |
| D5 | 燃气锅炉 | 气→热 | 效率、天然气消耗 |
| D6 | 电制冷机 | 电→冷 | COP/能效 |
| D7 | 输配环节 | 热/冷输配 | 输配损耗、传输容量、泵耗电 |

存量设备(容量固定,只优化运行)与新增设备(容量为优化变量)均需支持。

### 0.3 精度等级

沿用注册表文档的精度等级定义:

| 等级 | 名称 | 说明 |
|---|---|---|
| P1 | 简化线性 | 线性效率/COP 常数、损耗比例常数,全部约束线性(默认) |
| P2 | 标准 | COP 随温度、设备部分负荷率(PWL 分段线性近似) |
| P3 | 详细非线性 | 完整传热/衰减模型(非线性,仅用于后验校验,不进入 MILP) |

本文公式默认给出 P1 形式;非线性项在 §5 中标注 P2/P3 的处理方式。

> 精度代号与 04 受控注册表与诊断体系文档 §7.1 一一对应:等级 1 ↔ P1(`linear_simplified`)、2 ↔ P2(`standard`)、3 ↔ P3(`detailed_nonlinear`)。

### 0.4 通用符号约定

- 功率单位 W(J/s),能量单位 J 或 kWh,温度内部用 K,金额用 CNY。
- 时间步索引:$\tau$ 为全局步, $y$ 为年份, $t$ 为年内步(见 §1)。
- 下标 $k$ 表示同类设备实例编号;版本 1 默认每种设备单个实例,$k$ 保留以备多台扩展。
- 所有时变外部输入(辐照、气温、电价、负荷)均为**已知参数序列**,因此设备模型的时变系数(如 COP(τ))在每步都是**常数系数**,这是整个 MILP 构造线性性的基础(§5.9)。

---

## 1. 时间轴语义

### 1.1 基本规则

- 标准非闰年,每年固定 365 天;不接受 366 天日历(输入含 2 月 29 日数据报错 `DATA-TS-003`,已在 04 文档登记)。
- 固定 UTC 偏移 $z_{off}$(默认 +08:00),**无夏令时**;所有时间序列一律以 UTC 存储(数据库时间列统一 `TIMESTAMPTZ`,与 01 数据库 schema 全局约定一致),按项目固定偏移 $z_{off}$ 解释为项目本地时间,对外输出同时给本地时间与 UTC。
- 支持 3 种步长:

| 步长 | Δt(s) | 每日步数 m | 年步数 N | 说明 |
|---|---|---|---|---|
| 60 min | 3600 | 24 | 8760 | 默认 |
| 30 min | 1800 | 48 | 17520 | |
| 15 min | 900 | 96 | 35040 | |

### 1.2 时间索引映射

规划期 $Y$ 年,全局步集 $\mathcal{T}_G = \{\tau : \tau = 0, \dots, N\cdot Y - 1\}$;年内步集 $\mathcal{T} = \{0, \dots, N-1\}$。映射:

$$
\tau = y \cdot N + t, \qquad y = \left\lfloor \tau / N \right\rfloor \in \{0,\dots,Y-1\}, \qquad t = \tau \bmod N
$$

日历字段(以步长 $\Delta t$ 秒、每日 $m$ 步):

$$
d(t) = \left\lfloor t / m \right\rfloor \quad(\text{年内第 } d+1 \text{ 天},\ 0\le d \le 364)
$$

$$
p(t) = t - m \cdot d(t) \quad(\text{日内第 } p \text{ 个时段},\ 0\le p \le m-1)
$$

$$
w(d) = (d + w_0) \bmod 7 \quad(\text{星期},\ w_0 \text{ 为第 1 天的星期偏移})
$$

绝对时间戳(UTC):

$$
ts(\tau) = t_0 + \tau \cdot \Delta t,\qquad t_0 = \text{2024-01-01T00:00:00Z(可配置基准年)}
$$

本地显示时间 $ts_{loc}(\tau) = ts(\tau) + z_{off}$。内部一律用整数 $\tau$ 索引,禁止在模型内做时间字符串运算。

### 1.3 模式分类(电价/时段/季节)

- **季节分类** $s(d) \in \{春, 夏, 秋, 冬\}$:按年内天数分段(默认:春 3-5 月、夏 6-8 月、秋 9-11 月、冬 12-2 月,可配置)。
- **星期类型** $q(w) \in \{工作日, 周末\}$(节假日按周末处理,可配置节假日表)。
- **电价时段** $g(p) \in \{谷, 平, 峰, 尖峰\}$:由 (季节, 星期, 日内时段) 查表得到。
- 分时电价序列 $\pi_{buy}(\tau) = \pi_{buy}\big(s(d(t)), q(w(d(t))), g(p(t))\big)$:为已知常数序列,可直接预计算为数组。

### 1.4 多年时间序列与跨年边界

- 规划期 $Y$ 年:第 $y$ 年复用"年度模板"(辐照、气温、电价),或以逐年输入的时间序列覆盖。
- 跨年边界:电池 SOC 在每年年初复位到 $E(yN) = soc_{init} \cdot E_{cap}(y)$(见 §5.4);年度统计与费用在 $y$ 边界切分。
- 时间序列插值规则:气象/负荷序列以小时为基准网格,插值到模型步长(线性插值;辐照禁止负值,插值后 clamp 到 ≥ 0);电价按模式查表,不插值。

### 1.5 伪代码:时间索引构建

```text
function build_time_index(dt_s, Y, t0, w0, tariff_table, holiday_set):
    m   <- 86400 // dt_s                    # 每日步数 96/48/24
    N   <- 365 * m                          # 年步数 35040/17520/8760
    T   <- N * Y                            # 全局步数
    idx <- []
    for tau in 0..T-1:
        y  <- tau // N
        t  <- tau mod N
        d  <- t // m
        p  <- t mod m
        w  <- (d + w0) mod 7
        q  <- WEEKEND if (w in {5,6} or d+1 in holiday_set) else WORKDAY
        s  <- season(d)
        g  <- tariff_lookup(s, q, p, tariff_table)
        idx[tau] = (y, t, d, p, w, q, s, g, ts(tau))
    return idx
```

**求解器适配要点**:时间索引构建为纯确定性函数,必须按同一逻辑生成,保证模型与后处理(结果报表)的时间字段完全一致;结果文件中的时间列一律由本函数生成,不允许各自推导。

---

## 2. 单位系统

### 2.1 基准单位与量纲

内部基准单位(SI + 货币):

| 量纲 | 基准单位 | 符号 |
|---|---|---|
| 长度 | 米 | m |
| 质量 | 千克 | kg |
| 时间 | 秒 | s |
| 温度 | 开尔文 | K |
| 能量 | 焦耳 | J(功率 W = J/s) |
| 货币 | 人民币元 | CNY |
| 排放 | 千克 CO2 当量 | kgCO2e |

每个数值在内部必须携带 `(value, unit, dim)` 三元组;量纲用 7 维指数向量表示
$\dim = (M, L, T, \Theta, I, N, J) \in \mathbb{Z}^7$。例如功率 $\dim(W) = (0,2,-3,0,0,0,0)$。

### 2.2 单位换算表(接口↔内部)

| 量 | 接口单位 | 内部换算 | 默认显示精度 |
|---|---|---|---|
| 功率 | kW, MW | 1 kW = 1e3 W;1 MW = 1e6 W | 0.1 kW |
| 能量 | kWh, MWh | 1 kWh = 3.6e6 J;1 MWh = 3.6e9 J | 0.1 kWh |
| 温度 | °C | $T[K] = \theta[°C] + 273.15$ | 0.1 °C |
| 电费 | CNY/kWh | 内部为 CNY/J,$\times 3.6e6$ | 0.0001 CNY/kWh |
| 燃气 | m³ | 体积↔能量按 LHV:1 m³ = 35.9 MJ(可配置) | 0.01 m³ |
| 排放因子 | kg/kWh, kg/m³ | 与能量单位同步换算 | 0.001 |
| 金额 | 元, 万元 | 1 万元 = 1e4 CNY | 0.01 CNY |

换算规则:所有换算系数集中在单位注册表(Unit Registry,见 04 文档),模型内部禁止出现裸数值,所有魔法数字必须带单位。

### 2.3 数值表示策略:金额精确十进制,矩阵浮点

| 层级 | 表示 | 规则 |
|---|---|---|
| 输入/输出 | `Decimal`(28 位) | 金额、单价、费率、税率的解析与显示 |
| 模型内部(系数与变量) | `float64` | 能量/功率/容量等物理量 |
| 目标函数金额 | 求解器内用 `float64` | 精度损失受 MILP 容差约束(§10) |
| 对外报表 | `Decimal` | 由 float64 结果 × Decimal 单价在 Decimal 上下文重算,四舍五入 half-even,金额保留 2 位小数 |

边界规则:

1. 单价(CNY/kWh 等)存 `Decimal`;求解前统一转成 CNY/J 的 float64 系数并**记录转换后数值**,供逆向核对。
2. 求解结果(能量流 float64)→ 逐项与 Decimal 单价相乘得金额 Decimal,禁止"先汇总再舍入"传播误差。
3. 矩阵与目标函数一律 float64;MILP 整数容差与可行性容差见 §10.1。

### 2.4 量纲校验规则

- 每个参数 schema 声明 `unit` 与量纲;注册表校验(见 04 文档 §3)拒绝量纲不匹配的参数。
- 平衡方程构建时做量纲一致性断言:所有求和项量纲必须相同,不一致报错 `PARAM-UNIT-003`(已在 04 文档登记)。
- 温度转换必须显式(内部 K,接口 °C);摄氏度的"差值"与"绝对温度"不同量纲处理(差值无 273.15 偏置)。

---

## 3. 能量平衡模型

### 3.1 能量节点与守恒原则

版本 1 采用**单一母线(节点)电平衡 + 单一热母线 + 单一冷母线**,不做网络潮流。守恒方程以功率形式给出,能量形式由功率 × Δt 得到。输配损耗按"供应侧系数"建模(§3.5)。

### 3.2 电平衡

每步 $\tau$:

$$
\underbrace{\sum_k P_{pv,k}(\tau) + P_{buy}(\tau) + \sum_k P_{dis,k}(\tau)}_{\text{供给}}
=
\underbrace{L_e(\tau) + P_{sell}(\tau) + \sum_k P_{ch,k}(\tau) + \sum_k P_{hp,k}(\tau) + \sum_k P_{chl,k}(\tau) + P_{pump}(\tau)}_{\text{需求}}
\tag{E-BAL}
$$

其中 $P_{buy}(\tau)$ 购电功率、$P_{sell}(\tau)$ 售电功率、$P_{pv,k}$ 光伏注入、$P_{dis,k}/P_{ch,k}$ 电池放电/充电、$P_{hp,k}$ 热泵耗电、$P_{chl,k}$ 电制冷机耗电、$P_{pump}$ 输配泵耗电、$L_e(\tau)$ 电负荷(已知)。可选扩展项(启用削减时):$+ q_{shed,e}(\tau) - q_{curt,pv}(\tau)$,含义见 §3.7。

### 3.3 热平衡

$$
\sum_k Q_{b,k}(\tau) + \sum_k Q_{hp,h,k}(\tau) = Q_{sup,h}(\tau)
\tag{H-SUP}
$$

$$
Q_{sup,h}(\tau) = (1 + \lambda_h) \cdot Q_{del,h}(\tau),\qquad Q_{del,h}(\tau) = Q_{Lh}(\tau) - q_{shed,h}(\tau)
\tag{H-BAL}
$$

其中 $Q_{b,k}$ 锅炉产热、$Q_{hp,h,k}$ 热泵供热,$Q_{Lh}$ 热负荷(已知),$q_{shed,h} \ge 0$ 热负荷削减量(默认 = 0),$\lambda_h \ge 0$ 热输配损耗率(默认 0.05)。热输配容量约束:

$$
Q_{sup,h}(\tau) \le C_{tr,h}(\tau)
\tag{H-TR}
$$

### 3.4 冷平衡

$$
\sum_k Q_{chl,k}(\tau) = Q_{sup,c}(\tau),\qquad
Q_{sup,c}(\tau) = (1 + \lambda_c) \cdot (Q_{Lc}(\tau) - q_{shed,c}(\tau))
\tag{C-BAL}
$$

冷输配容量约束 $Q_{sup,c}(\tau) \le C_{tr,c}(\tau)$。$\lambda_c$ 默认 0.08。

### 3.5 输配损耗与泵耗电

- 损耗统一采用**供应侧比例系数**(P1):供给 = (1+λ)×净需求;损耗能量 $Q_{loss}(\tau) = \lambda \cdot Q_{del}(\tau)$ 逐时计入报表。
- P2:损耗率随传输流量变化的分段线性函数(近似 P1 的系数表)。
- 泵耗电(电平衡需求侧):

$$
P_{pump}(\tau) = c_{ph} \cdot Q_{sup,h}(\tau) + c_{pc} \cdot Q_{sup,c}(\tau),\qquad c_{ph}, c_{pc} \ge 0
\tag{PUMP}
$$

默认 $c_{ph} = c_{pc} = 20\ \text{W}_e / \text{kW}_{th}$(可配置)。P2 可加泵启停二进制与最小流量。

### 3.6 并网约束与禁止反送电

$$
0 \le P_{buy}(\tau) \le C_{imp},\qquad 0 \le P_{sell}(\tau) \le C_{exp}
\tag{GRID-CAP}
$$

- $C_{imp}$ 并网购电容量, $C_{exp}$ 售电容量(并网协议值,可配置)。
- **禁止反送电**:模型层面直接置 $P_{sell}(\tau) = 0$ 的等式约束(或从模型删除 $P_{sell}$ 变量),**绝不使用惩罚项软化**;状态位 `reverse_feed_allowed = false` 记录于方案并显著展示。

### 3.7 负荷满足与削减

- **默认:不允许削减**(版本 1 默认),即 $q_{shed,e} = q_{shed,h} = q_{shed,c} = 0$,平衡方程为等式,负荷必须全额满足。
- **允许削减时**(用户显式开启):
  - 削减量 $q_{shed,\cdot}(\tau) \ge 0$ 为连续变量,目标函数中加惩罚项 $\pi_{shed} \cdot \sum q_{shed}(\tau)$($\pi_{shed}$ 默认 = 5 × 最高购电单价,保证"被迫削减"才出现)。
  - **显著报告义务**:削减的每一步都在结果中标记(逐时表 `shed_*` 列),汇总 KPI 给出削减能量、削减率、事件列表(连续削减时段),并在方案报告中以醒目方式(如顶部警告条)呈现;削减率 = 削减能量/需求能量。
- 光伏弃光 $q_{curt,pv}(\tau) = P_{pv}^{avail}(\tau) - P_{pv}(\tau) \ge 0$(逆变器限制导致的物理弃光,默认允许,计入弃光率 KPI;若"禁止弃光"则要求 $P_{pv}(\tau) = P_{pv}^{avail}(\tau)$)。

### 3.8 平衡残差定义(后验审计)

逐时残差(§10 使用):

$$
r_e(\tau) = \big|\ \text{E-BAL 左式} - \text{E-BAL 右式}\ \big|,\quad
r_h(\tau) = \big| \sum Q_{sup,h}(\tau) - (1+\lambda_h) Q_{del,h}(\tau) \big|,\quad
r_c(\tau) = \big| \sum Q_{sup,c}(\tau) - (1+\lambda_c) Q_{del,c}(\tau) \big|
$$

---

## 4. 设备数学模型

### 4.1 通用设备建模框架

每类设备定义:功率/能量流变量(连续)、启停/模式变量(二进制)、容量变量(存量固定/新增决策)、效率或转换系数(时变常数参数)。**存量(现有)设备**:容量为给定常数;新增设备:容量为连续优化变量 + 建设二进制(§5.3)。

### 4.2 电网连接(§3.6 已含)

补充需量费(电费构成):

$$
D_{y,m} \ge P_{buy}(\tau),\ \forall \tau \in \text{月 } m \text{ 的第 } y \text{ 年},\qquad
C_{dem,y} = \sum_{m} \pi_{dem}(m) \cdot D_{y,m}
\tag{DEMAND}
$$

$D_{y,m}$ 为连续变量(epigraph 形式,线性)。固定费 $C_{fix,y}$(基本电费/容量费)为常数项。

### 4.3 光伏(PV)

**输入参数**:$G_{ghi}(\tau), G_{dni}(\tau), G_{dhi}(\tau)$(水平总辐照/法向直射/水平散射,W/m²)、环境温度 $T_a(\tau)$、阵列方位角 $\gamma_a$、倾角 $\beta$、组件标称效率 $\eta_{pv}$、温度系数 $\beta_T$(默认 0.004 /K)、NOCT(默认 45 °C)、逆变器容量 $P_{inv}$。

**有效辐照(倾斜面转换,含朝向/倾角)**:

$$
G_{eff}(\tau) = G_{dni}(\tau)\cos\theta_i(\tau) + G_{dhi}(\tau)\frac{1+\cos\beta}{2} + \rho_g\, G_{ghi}(\tau)\frac{1-\cos\beta}{2}
\tag{PV-G}
$$

$$
\cos\theta_i(\tau) = \sin\alpha(\tau)\cos\beta + \cos\alpha(\tau)\sin\beta\cos\big(\gamma(\tau) - \gamma_a\big),\quad \cos\theta_i < 0 \Rightarrow 0
$$

其中 $\alpha(\tau)$ 太阳高度角、$\gamma(\tau)$ 太阳方位角(由天文算法计算,视为已知参数序列),$\rho_g$ 地面反射率(默认 0.2)。P1 简化:按 (朝向,倾角,月份) 预计算月度系数表 $F(\gamma_a,\beta,d)$,$G_{eff}(\tau) = G_{ghi}(\tau)\cdot F(\cdot)$(离线标定,禁止逐时算天文公式)。

**电池温度(组件温度)**:

$$
T_c(\tau) = T_a(\tau) + \frac{NOCT - 20}{800} \cdot G_{eff}(\tau)
\tag{PV-T}
$$

**可用出力(线性于容量)**:

$$
P_{pv}^{avail}(\tau) = P_{pv}^{cap} \cdot \frac{G_{eff}(\tau)}{G_{STC}} \cdot \Big[1 - \beta_T\big(T_c(\tau) - T_{STC}\big)\Big],\quad G_{STC}=1000\ \text{W/m}^2,\ T_{STC}=25\ ^\circ\text{C}
\tag{PV-P}
$$

**约束**:

$$
0 \le P_{pv}(\tau) \le P_{pv}^{avail}(\tau),\qquad P_{pv}(\tau) \le P_{inv}
\tag{PV-C}
$$

新增光伏:$P_{pv}^{cap}$ 为连续决策变量(组件面积 $A = P_{pv}^{cap}/(\eta_{pv}\cdot G_{STC})$ 由注册表推导,不单独建模)。朝向/倾角为**输入参数**,不做优化变量。

### 4.4 电池储能

**变量与参数**:$E_{cap0}$ 额定容量(J)、$E(\tau)$ 储存能量、$P_{ch}/P_{dis}$、$u_{ch}/u_{dis} \in \{0,1\}$、$\eta_{ch}/\eta_{dis}$(默认 0.95)、SOC 上下限 $[soc_{min}, soc_{max}]$(默认 0.10/0.90)、充放电功率上限 $P_{ch}^{max}/P_{dis}^{max}$。

**SOC 递推**:

$$
E(\tau+1) = E(\tau) + \eta_{ch}P_{ch}(\tau)\Delta t - \frac{P_{dis}(\tau)}{\eta_{dis}}\Delta t
\tag{BAT-SOC}
$$

**充放互斥(二进制)**:

$$
u_{ch}(\tau) + u_{dis}(\tau) \le 1,\qquad
P_{ch}(\tau) \le u_{ch}(\tau)P_{ch}^{max},\qquad
P_{dis}(\tau) \le u_{dis}(\tau)P_{dis}^{max}
\tag{BAT-MU}
$$

**SOC 界(用当年有效容量)**:

$$
soc_{min}\cdot E_{cap}(y) \le E(\tau) \le soc_{max}\cdot E_{cap}(y),\ \forall \tau \in y
\tag{BAT-BND}
$$

**跨年/终态**:$E(yN) = soc_{init}\cdot E_{cap}(y)$(每年年初复位);规划期末 $E(YN) \ge soc_{init}\cdot E_{cap}(Y-1)$。

**循环寿命累计(等效全循环)**:

$$
c_{eq}(y) = \frac{\sum_{\tau \in y}\big(P_{ch}(\tau) + P_{dis}(\tau)\big)\Delta t}{2 E_{cap0}},\qquad
C_{cum}(y) = C_{cum}(y-1) + c_{eq}(y) - R_{cyc}\cdot r_y
\tag{BAT-CYC}
$$

**容量衰减(线性,线性于累计循环)**:

$$
E_{cap}(y) = E_{cap0}\cdot\big(1 - \kappa_d\, C_{cum}(y)\big),\qquad \kappa_d = \frac{1 - EOL}{R_{cyc}}
\tag{BAT-FADE}
$$

默认:寿命 $R_{cyc}=6000$ 循环,EOL=80%(衰减到 80% 视为寿命终),$\kappa_d = 3.33\times10^{-5}$/循环。

**更换决策(二进制)**:

$$
r_y \in \{0,1\},\quad \sum_{y} r_y \le R_{max},\quad \text{更换成本 } REPL_y = c_{repl}\cdot r_y
$$

更换当年 $C_{cum}$ 复位(式 BAT-CYC),容量恢复至 $E_{cap0}(1-\kappa_d\cdot C_{cum}(y))$。P2:衰减曲线用分段线性凹函数近似(PWL);P3:非线性老化模型仅用于后验报告,不进入 MILP。

**线性性说明**:$c_{eq}, C_{cum}, E_{cap}$ 均为变量的线性函数 → BAT-BND 为线性界。这是"衰减+更换可入 MILP"的关键。

### 4.5 热泵(供冷/供热双模式)

**COP 随环境温度**(P1 线性或卡诺修正;P2 用回归多项式/查表):

$$
COP_h(\tau) = \operatorname{clip}\!\Big(\eta_{hp}\cdot\frac{T_{cnd}}{T_{cnd} - T_a(\tau) + \Delta T_{ev}}, \ [COP_h^{min}, COP_h^{max}]\Big)
\tag{HP-COPh}
$$

$$
COP_c(\tau) = \operatorname{clip}\!\Big(\eta_{hp}\cdot\frac{T_{ev}}{T_a(\tau) + \Delta T_{cd} - T_{ev}}, \ [COP_c^{min}, COP_c^{max}]\Big)
\tag{HP-COPh}
$$

默认 $T_{cnd}=318\,\text{K}$、$T_{ev}=280\,\text{K}$、$\Delta T_{ev}=5\,\text{K}$、$\Delta T_{cd}=10\,\text{K}$、$\eta_{hp}=0.45$、clip 范围供热 [2.0, 5.5]、供冷 [2.5, 6.5];亦可直接给回归 $COP(\tau) = a_0 + a_1 T_a(\tau)$。

**出力与耗电**:

$$
Q_{hp,h,k}(\tau) = P_{hp,k}(\tau)\cdot COP_h(\tau),\qquad
Q_{hp,c,k}(\tau) = P_{hp,k}(\tau)\cdot COP_c(\tau)
\tag{HP-P}
$$

**模式互斥与容量**:

$$
u_{h}(\tau) + u_{c}(\tau) \le 1,\qquad
0 \le P_{hp}(\tau) \le P_{hp}^{cap}
$$

$$
Q_{hp,h}(\tau) \le P_{hp}^{cap}\cdot \delta_h(\tau)\cdot u_h(\tau),\qquad
Q_{hp,c}(\tau) \le P_{hp}^{cap}\cdot \delta_c(\tau)\cdot u_c(\tau)
\tag{HP-CAP}
$$

$\delta_h(\tau), \delta_c(\tau)$ 为温度导致的出力衰减系数(参数,如结霜衰减,默认 1)。COP 为常数系数 → HP-P 线性;模式互斥保证同一时刻只供热或只供冷。新增热泵:$P_{hp}^{cap} \in [P_{min}, P_{max}]$ 连续决策 + 建设二进制(§5.3)。

### 4.6 燃气锅炉

**效率与气耗**(LHV 基准):

$$
Q_{b,k}(\tau) = \eta_b \cdot P_{gas,k}(\tau),\qquad
V_{gas,k}(\tau) = \frac{P_{gas,k}(\tau)\cdot\Delta t}{LHV_V}
\tag{B-P}
$$

$P_{gas}$ 燃气输入功率(W),$LHV_V$ 体积低位热值(默认 35.9 MJ/m³,可配置)。**启停与爬坡下限**:

$$
u_b(\tau)\cdot P_{gas}^{min} \le P_{gas}(\tau) \le u_b(\tau)\cdot P_{gas}^{max}
\tag{B-C}
$$

$P_{gas}^{min}$ 默认 = 30% × $P_{gas}^{max}$(低负荷保护)。新增锅炉:$P_{gas}^{max}$ 为连续决策,对应产热容量 $Q_{b}^{cap} = \eta_b P_{gas}^{max}$。

### 4.7 电制冷机

P1(常数能效):

$$
Q_{chl,k}(\tau) = P_{chl,k}(\tau)\cdot COP_{chl,k}(\tau)
\tag{C-P}
$$

启停与最小负荷:

$$
u_{chl}(\tau)\cdot Q_{chl}^{min} \le Q_{chl}(\tau) \le u_{chl}(\tau)\cdot Q_{chl}^{cap}
\tag{C-C}
$$

P2:COP 随部分负荷率变化,输入-输出曲线 $Q_{chl} = f(P_{chl})$ 用分段线性凹函数(PWL,λ-凸组合)建模;P3:EER/COP 随冷凝温度完整计算,仅后验。新增电制冷机:$Q_{chl}^{cap}$ 连续决策 + 建设二进制。

### 4.8 存量 vs 新增的统一差异

| 方面 | 存量设备(现有) | 新增设备 |
|---|---|---|
| 容量 | 常数 $C^{fix}$ | 连续变量 $C \in [C_{min}, C_{max}]$ |
| 建设 | 无 | 二进制 $z \in \{0,1\}$,$C_{min}z \le C \le C_{max}z$ |
| 投资成本 | 无(沉没) | $F_i z + c_i C$(固定费+单位容量费) |
| 运行约束 | 相同 | 相同(容量替换为变量) |

### 4.9 线性性总结(求解器适配关键)

除 P3 外,所有设备约束中:**时变效率/COP/辐照系数均为已知参数序列**,与决策变量只做乘-加运算,且容量变量与功率变量之间的耦合均为乘积形式中的"系数 × 变量"(如 PV-P、HP-CAP、B-P),因此:

- 全部约束为线性(等式/不等式);
- 目标为线性(§5);
- 决策变量中二进制数量有限(启停、模式、互斥、建设、更换)。

→ 模型整体为**标准 MILP**,可直接映射到任意 MILP 求解器。P2 的 PWL 需引入 SOS2 或 λ 变量,适配层按 PWL 接口抽象(§11)。

---

## 5. 优化问题构建(MILP)

### 5.1 决策变量总表

| 类别 | 变量 | 类型 | 说明 |
|---|---|---|---|
| 功率流 | $P_{buy}, P_{sell}, P_{pv,k}, P_{ch,k}, P_{dis,k}, P_{hp,k}, P_{chl,k}, P_{pump}$ | 连续 ≥ 0 | 逐时功率(W) |
| 能量 | $E_k(\tau)$(电池)、$Q_{hp,h,k}, Q_{hp,c,k}, Q_{b,k}, Q_{chl,k}$ | 连续 ≥ 0 | 逐时 |
| 热/冷供给 | $Q_{sup,h}, Q_{sup,c}$ | 连续 ≥ 0 | 逐时 |
| 削减 | $q_{shed,e}, q_{shed,h}, q_{shed,c}, q_{curt,pv}$ | 连续 ≥ 0 | 默认禁用 |
| 需量 | $D_{y,m}$ | 连续 ≥ 0 | 月最大购电 |
| 容量(新增) | $C_i \in [C_{min}, C_{max}]$ | 连续 | 各新增设备 |
| 衰减 | $c_{eq}(y), C_{cum}(y), E_{cap}(y)$ | 连续 | 电池 |
| 启停/模式 | $u_{b}, u_{chl}, u_{h}, u_{c}$ | 二进制 | 逐时 |
| 互斥 | $u_{ch}, u_{dis}$ | 二进制 | 逐时 |
| 建设 | $z_i$ | 二进制 | 每新增设备 |
| 更换 | $r_y$ | 二进制 | 电池每年 |

### 5.2 目标函数:税后项目投资 IRR 的转化

**直接最大化 IRR 不可行**:IRR 是现金流序列的隐含贴现率,是决策变量(现金流)的非线性隐函数,且 IRR 约束非线性。版本 1 采用**两阶段转化**(§5.6):

1. **阶段 1(代理目标)**:固定贴现率 $r$(默认 8%,= 加权平均资本成本 WACC),最大化税后净现值 NPV 的线性代理;
2. **阶段 2(IRR 硬约束)**:对代理解计算真实 IRR;若低于下限 $\rho_{min}$,用 NPV 在 $\rho_{min}$ 处的线性化约束/投资乘子搜索强制满足(§5.6)。

**IRR 硬约束的线性化**:标准符号型现金流(首年为负投资,后续年份现金流无再反转,即最多一次变号)下,NPV 关于贴现率严格递减,于是

$$
IRR(x) \ge \rho_{min} \iff NPV(x)\big|_{\rho=\rho_{min}} \ge 0
\tag{IRR-LIN}
$$

右式为线性约束(贴现系数为常数)。若现金流多次变号(更换成本造成),该等价关系不严格成立,退化到 §5.6 的乘子搜索,并在报告中提示"IRR 存在多个根,已按 NPV 单调假设近似处理"。

### 5.3 基准方案与收益定义

**基准方案 = 不建设任何新增设备、存量设备按现状运行**。基准年运行成本 $C_{base,y}$ 通过求解"容量全为零"的运行优化问题得到(与 §7 同构):

$$
C_{base,y} = \min\ \big\{\text{购电+气费+需量费+固定费}\big\}_{x_{cap}=0}
$$

若存量系统无法满足负荷(默认不允许削减):基准定义失败,报错 `TASK-SOLVE-003`(已在 04 文档登记),提示用户需新增设备(或显式开启削减并以 $\pi_{shed}$ 计价,该惩罚价必须醒目展示)。

**方案收益(相对基准)**:

$$
NB_y = C_{base,y} - C_{op,y} + R_{sell,y}
\tag{NB}
$$

$$
C_{op,y} = \sum_{\tau \in y}\Big[\pi_{buy}(\tau)E_{buy}(\tau) + \pi_{gas}(\tau)V_{gas}(\tau)\Big] + C_{dem,y} + C_{fix,y}
$$

$$
R_{sell,y} = \sum_{\tau \in y}\pi_{sell}(\tau)E_{sell}(\tau),\qquad E_{buy}(\tau) = P_{buy}(\tau)\Delta t / 3.6\times10^6\ [\text{kWh}]
$$

### 5.4 现金流与税收(税后)

**投资与运维**($i$ 遍历新增设备):

$$
CAPEX_0 = \sum_i \big(F_i z_i + c_i C_i\big)
\tag{CAPEX}
$$

$$
OM_y = \sum_i \Big(c_{fix,i}\, C_i + c_{var,i}\, E_{out,i,y}\Big)
\tag{OM}
$$

$E_{out,i,y}$ 为第 $i$ 台设备第 $y$ 年产出能量(光伏发电量/电池放电量/供热量等,由运行变量线性表达)。

**直线折旧**:$DEP_y = CAPEX_0 / L_{dep}$(第 $y=1..L_{dep}$ 年;$L_{dep}$ 默认 10,可配置;残值默认 0)。

**税后现金流**(企业所得税率 $t_c$,默认 25%):

$$
ATCF_0 = -CAPEX_0
$$

$$
ATCF_y = (1 - t_c)\big(NB_y - OM_y - DEP_y\big) + DEP_y - REPL_y,\quad y = 1..Y
\tag{ATCF}
$$

**阶段 1 目标**(最大化):

$$
\max\ J(x) = ATCF_0 + \sum_{y=1}^{Y}\frac{ATCF_y}{(1+r)^y}
\tag{OBJ}
$$

J 为线性目标(所有项都是变量的线性组合或常数)。

### 5.5 可行域(约束清单)

模型 = 式 (E-BAL),(H-SUP),(H-BAL),(H-TR),(C-BAL),(PUMP),(GRID-CAP),(DEMAND),(PV-C),(BAT-SOC/MU/BND/CYC/FADE),(HP-P/HP-CAP),(B-P/B-C),(C-P/C-C),§4.8 的容量-建设耦合,以及变量上下界。削减默认关闭;开启时含削减变量与惩罚项 $- \pi_{shed}\sum q_{shed}$(进入目标)。

### 5.6 两阶段求解算法(含 IRR 硬约束)

```text
function solve_capacity_design(instance, IRR_min, r):
    # 阶段 1:代理目标 NPV 最大化
    mip1 <- build_milp(instance, objective = NPV@r, investment_cost_scale = 1.0)
    (x1, status1, gap1) <- solve(mip1)
    if status1 == NO_FEASIBLE_FOUND: return NO_FEASIBLE_FOUND
    cf1  <- cashflow(x1, instance)            # ATCF_0..ATCF_Y
    irr1 <- irr_root(cf1)                     # 二分/牛顿,见下
    if irr1 >= IRR_min: return (x1, irr1)     # 满足硬约束,接受

    # 阶段 2:IRR 硬约束补救 —— 投资成本乘子 θ 二分
    # 提高投资成本 → 现金流变差 → IRR 下降;降低 θ 等价于放宽投资
    lo, hi <- 0.1, 1.0
    for iter in 1..8:                          # 默认 8 次,可配置
        th  <- (lo + hi) / 2
        x_t <- solve(build_milp(instance, objective = NPV@r, cost_scale = th))
        if x_t 可行 and irr_root(cashflow(x_t)) >= IRR_min:
            hi <- th                            # 保持可行,继续收紧
            best <- x_t
        else:
            lo <- th
    if best 存在: return (best, irr_root(cashflow(best)))
    else: return INFEASIBLE_BY_IRR_FLOOR(IRR_min)   # 建议降低 IRR_min 或放宽约束(状态码见 §11.4)

function irr_root(ATCF):                        # 现金流 → IRR
    # NPV(rho) 从 rho=0.001 到 1e4 扫描变号(最多 64 段)
    # 标准符号型:二分至 |NPV| < 1e-6 * |ATCF0|,返回 rho
    # 多次变号:返回所有根列表,告警,取最小正根为保守值
```

约束性说明:当代理目标(NPV@r)与 IRR 硬约束冲突时,以 IRR 硬约束为准(阶段 2 保证)。报告中同时输出 NPV、IRR、两者与约束的关系,不可静默吞并。

### 5.7 变量类型与默认变量集

| 类型 | 变量 | 说明 |
|---|---|---|
| 连续 | 全部功率/能量/容量/需量/衰减 | 物理量 |
| 整数 | (版本 1 无整型决策,保留扩展位:如电池簇数、台数) | |
| 枚举 | 设备模式(供热/供冷/停机)、电价时段(参数) | 模式用二进制编码,不引入枚举变量 |
| 布尔 | $u_{b}, u_{chl}, u_{h}, u_{c}, u_{ch}, u_{dis}, z_i, r_y$ | 二进制 |

**默认变量集(用户未指定时)**:所有版本 1 设备**可选**;新增设备容量为**连续变量**;建设二进制自动引入;运行二进制(启停/模式/互斥)自动引入。用户可冻结(固定)任意设备/参数 → 对应变量降为常数(§7)。

### 5.8 求解规模与规模控制策略

全 20 年逐时 MILP(每小时约 5-6 个二进制)二进制数约百万级,超出常规求解能力,因此采用**双层分解**(默认策略):

1. **容量层(外层)**:在**代表性时段集**上求解完整 MILP(含容量决策)。代表性时段:每年选取 $n_{typ}$ 个典型日(默认 12,各 24 步),由 k-means 对 (辐照, 气温, 负荷, 电价) 向量聚类得到,以聚类权重加权进入目标;20 年 × 288 步 ≈ 5760 步,二进制约 3 万 — 常规求解器可解。
2. **运行层(内层)**:容量固定后,对**全年 8760 步**求解运行优化(§7)做校验与费用精算;结果与典型日加权结果的偏差(按年费用相对偏差,默认 5%)超限时,增加典型日数重算(自适应)。

该策略使"逐时模型"与"可求解模型"解耦;适配层对求解器只暴露 MILP 实例,分解逻辑在模型层实现。

---

## 6. 多目标方法

### 6.1 目标集与规范化

版本 1 可选目标(用户勾选,默认只选税后 IRR 最大):

| 编号 | 目标 | 方向 | 说明 |
|---|---|---|---|
| f1 | 税后项目投资 IRR | max | 主目标,默认 |
| f2 | 税后 NPV | max | 代理线性目标 |
| f3 | 年 CO2 排放 | min | 电网+燃气排放 |
| f4 | 年购能费用 | min | 购电+购气+需量费 |
| f5 | 光伏自用率/自给率 | max | 定义见 §9.2 |

**规范化(付费表 payoff table)**:对每个目标单独求解得到 $f_j^{min}, f_j^{max}$;归一化值:

$$
\hat{f}_j(x) = \frac{f_j(x) - f_j^{min}}{f_j^{max} - f_j^{min}} \in [0,1]\ (\text{max 方向}),\qquad
\hat{f}_j(x) = \frac{f_j^{max} - f_j(x)}{f_j^{max} - f_j^{min}}\ (\text{min 方向})
$$

### 6.2 通用硬约束(所有方法必须遵守)

**最低税后 IRR 硬约束**:每个子问题的求解都附加

$$
NPV(x)\big|_{\rho=\rho_{min}} \ge 0\ \ (\text{线性约束, §5.2 的 IRR-LIN})\quad \text{或经 §5.6 阶段 2 强制}
$$

该约束**不可被任何权重/优先级/ε 值抵消**:加权法中即使权重为 0,IRR 下限仍生效;ε-约束法中 ε 扫描不能松弛此约束。若某 ε 网格点导致不可行,报告"该点因 IRR 硬约束不可行",不允许静默删除。

### 6.3 加权法(Weighted Sum)

$$
\max_x\ \sum_j w_j \hat{f}_j(x),\qquad \sum_j w_j = 1,\ w_j \ge 0
$$

权重由用户给定(提供预设:经济优先、减排优先、均衡)。每个权重向量求解一次 MILP(线性目标,代价同单目标)。对凹 Pareto 前沿,加权法能覆盖前沿;非凹区域(版本 1 常见于整数决策导致的离散前沿)改用 ε-约束法。

### 6.4 优先级法(Lexicographic)

按用户指定顺序 $f_{(1)} \succ f_{(2)} \succ \cdots$:

```text
X <- 原始可行域 + IRR 硬约束
for k in 1..K:
    x_k <- solve(max f_(k)(x) over X)
    f_k* <- f_(k)(x_k)
    X <- X ∩ { f_(k)(x) >= f_k* - eps_k }     # eps_k 默认 1% 目标区间
return x_K
```

每阶段都必须含 IRR 硬约束;eps 防止数值退化。输出各阶段目标值。

### 6.5 ε-约束法(ε-constraint)

以 $f_1$(默认税后 IRR,但其硬约束形式特殊,故实际操作以 NPV 或排放为首要目标做 ε 扫描,IRR 恒为硬约束)为例:

$$
\max\ f_1(x)\quad \text{s.t.}\quad f_j(x) \ge \varepsilon_j\ (\max\text{方向})\ \text{或}\ f_j(x) \le \varepsilon_j\ (\min\text{方向}),\ j=2..K
$$

ε 网格:$n_{grid}$ 等分点(默认 10)在 $[f_j^{min}+\delta,\ f_j^{max}-\delta]$ 上($\delta$ 为 5% 区间宽,防止退化端点),全网格笛卡尔积求解。所有子问题含 IRR 硬约束。

### 6.6 Pareto 解集

- 收集全部候选解(ε-约束法为主、加权法补充),做**支配过滤**(O(K²) 逐对比较),输出非支配解集;
- 结果含:每点目标值向量、对应容量方案摘要(各设备容量、建设标志)、IRR(确认 ≥ 下限)、求解状态;
- 前端呈现 Pareto 前沿散点(默认二维,第三维可选);可选指标:解点数、均匀度、超体积(版本 1 可选)。

### 6.7 多目标工作流伪代码

```text
function multi_objective_search(instance, objs, method, IRR_min, r):
    payoffs <- payoff_table(instance, objs)          # 各单目标最优
    add_hard_constraint(NPV@IRR_min >= 0)            # 全局硬约束
    if method == WEIGHTED:
        sols <- [ solve(weighted(obj, w)) for w in weight_grid ]
    elif method == LEXICOGRAPHIC:
        sols <- [ lexicographic_solve(instance, objs) ]
    elif method == EPSILON:
        sols <- [ solve(epsilon(obj, eps_vec)) for eps_vec in grid(payoffs) ]
    front <- dominance_filter(sols)                  # 非支配解集
    return front with IRR checks, report infeasible grid points
```

---

## 7. 任意方案评价(固定容量运行优化)

### 7.1 问题定义

输入:完整方案(设备列表、**全部容量已固定**、参数齐全,可以是"任意方案":用户手工组合、非最优方案、多目标前沿上的候选点)。输出:该方案下的最优逐时运行。

$$
\min_{u} \Big\{ C_{op}(\bar C, u) + \text{惩罚项} \Big\} \quad \text{s.t. 平衡与设备约束(容量为常数 } \bar C)
\tag{EVAL}
$$

- 只优化运行变量(§5.1 中除容量、建设之外的连续与二进制变量);
- 容量变量替换为常数 $\bar C$;建设变量 $z_i$ 按方案给定;更换变量 $r_y$ 保留(运行中可更换电池);
- 默认不允许削减;允许时按 §3.7 惩罚。

### 7.2 LP 松弛与二进制处理

运行问题含二进制(启停、充放互斥、模式)。已知性质:充放互斥在 $\eta_{ch}<1<\eta_{dis}^{-1}$ 且电价非负时,LP 松弛的最优解天然满足互斥(同时充放只增加损耗);但**电价为零/负、光伏弃光免费等退化情形下 LP 松弛可能给出同时充放伪解**。策略:

1. 默认:保留二进制,求解 MILP(精确);
2. 用户选择快速模式:解 LP 松弛后**校验互斥**;出现同时充放时,对冲突时段施加 big-M 互斥约束重解一次,并报告"使用近似求解";
3. 后验审计(§10)强制校验 SOC 递推与互斥,保证输出无伪解。

### 7.3 固定方案的财务结果

- 运行成本 $C_{op,y}$、相对基准收益 $NB_y$、税后现金流 $ATCF_y$(投资按方案容量计算 $CAPEX_0$ 已知)、**该方案的税后 IRR**(直接用方案现金流求根);
- 方案级 KPI(§9.2)与逐时结果(§9.1)。

### 7.4 伪代码

```text
function evaluate_scheme(scheme, instance):
    assert scheme.capacities 全部已固定
    mip <- build_milp(instance, capacities = scheme.capacities, objective = min C_op)
    (x, status, gap) <- solve(mip)
    if status != OPTIMAL and status != TIME_LIMIT_WITH_INCUMBENT: return NO_FEASIBLE_FOUND  # 状态码见 §11.4
    audit_residuals(x)                          # §10.1 残差审计
    hourly <- export_hourly(x, instance)        # §9.1
    kpis   <- aggregate_kpis(hourly)            # §9.2
    irr    <- irr_root(cashflow(scheme, hourly))
    return {hourly, kpis, irr, cost_breakdown}
```

---

## 8. 逐时结果内容

### 8.1 逐时输出表(字段定义)

以下字段以全局步 τ 为行输出(单位、来源列标注入结果 schema):

| 分组 | 字段 | 单位 | 来源 |
|---|---|---|---|
| 时间 | year, step, datetime_local, datetime_utc, season, weekday_type, tariff_period | — | §1.2 时间索引 |
| 电 | P_buy, P_sell | kW | 变量 |
| | P_pv, P_pv_avail, q_curt_pv(弃光) | kW | 变量/参数 |
| | P_ch, P_dis, SOC(%) | kW, % | 变量 |
| | P_hp, P_chl, P_pump | kW | 变量 |
| | L_e(输入)、q_shed_e(削减) | kW | 参数/变量 |
| 热 | Q_b, Q_hp_h, Q_sup_h, Q_Lh, Q_loss_h, q_shed_h | kW | 变量/参数 |
| 冷 | Q_chl, Q_sup_c, Q_Lc, Q_loss_c, q_shed_c | kW | 变量/参数 |
| 气 | V_gas | m³ | 变量 |
| 费用 | cost_buy, cost_gas, revenue_sell, cost_demand(月分摊), cost_fixed, cost_total_step, cost_total_cum | CNY | Decimal 重算 |
| 排放 | co2_grid, co2_gas, co2_total, co2_total_cum | kgCO2e | 排放因子 × 能耗 |
| 状态 | u_b, u_chl, u_h, u_c, u_ch, u_dis | 0/1 | 变量 |

### 8.2 汇总统计(KPI)

- 年度/生命周期:购电量、售电量、自发自用电量、弃光量、气耗、总费用、总收益、税后 IRR、NPV;
- 自给率 = (电负荷 − 购电)/电负荷(按年);自用率 = 自用光伏/光伏发电;
- 峰谷套利收益(相对无电池基准)、电池等效全循环数、累计衰减、更换事件列表;
- 逐月最大需量 D_m、并网利用率;
- 削减率与削减事件(若开启削减,必须显著报告,§3.7);
- 排放总量与单位能耗排放。

### 8.3 文件格式与元数据

输出为 CSV/Parquet 双格式 + JSON 摘要;文件元数据含:方案 ID、种子、求解器、版本、模型构建时间、求解时间、gap、状态码。时间列由 §1.2 时间索引函数生成。

---

## 9. 收敛与停止

### 9.1 逐物理量残差、容差与归一化

求解后强制执行**残差审计**(audit_residuals),每个物理量独立残差/容差/归一化:

| 物理量 | 残差定义 | 归一化尺度 S | 容差(默认) |
|---|---|---|---|
| 电平衡 | $r_e(\tau) = \|\text{E-BAL 左右差}\|$ | $S_e = \max(1,\ \max_\tau L_e(\tau))$ | 1e-6 |
| 热平衡 | $r_h(\tau) = \|Q_{sup,h} - (1+\lambda_h)Q_{del,h}\|$ | $S_h = \max(1,\ \max_\tau Q_{Lh})$ | 1e-6 |
| 冷平衡 | $r_c(\tau) = \|Q_{sup,c} - (1+\lambda_c)Q_{del,c}\|$ | $S_c = \max(1,\ \max_\tau Q_{Lc})$ | 1e-6 |
| SOC 递推 | $r_s(\tau) = \|E(\tau{+}1) - E(\tau) - \eta_{ch}P_{ch}\Delta t + P_{dis}\Delta t/\eta_{dis}\|$ | $S_s = E_{cap0}$ | 1e-6 |
| SOC 跨期 | $\|E(YN) - E(yN)\text{ 边界差}\|$ | $S_s$ | 1e-6 |
| 充放互斥 | $\max_\tau (u_{ch}+u_{dis}-1)_+$ | 1 | 0(整数) |
| 需量定义 | $\|D_{y,m} - \max_{\tau\in m} P_{buy}\|$ | $C_{imp}$ | 1e-4 |

判定:归一化残差 $\bar r = r/S$;任何 $\bar r > tol$ 即判定模型有误,报错(带 τ 定位),不进入结果输出。

### 9.2 MIP Gap 定义与默认值

$$
Gap = \frac{|UB - LB|}{\max\big(1,\ |UB|\big)} \times 100\%
$$

- UB = 最优上界(最大化问题;求解器原始-对偶界),LB = 当前最优可行解目标值;
- 默认停止条件:相对 MIP gap ≤ 0.1%(可配置);LP 子问题(gap = 0)直接最优;
- 可行性容差:原始可行性 1e-7(按约束尺度),整数容差 1e-5;金额目标绝对 gap 默认 1e-3 CNY(防止金额过小振荡)。

### 9.3 时间上限行为

- 参数 `time_limit`(默认 600 s)为硬上限;超时**立即停止**,必须返回:
  - 当前最优可行解(incumbent),若存在;
  - 最优性信息:UB、LB、Gap、已访问节点数、迭代数、停止原因(`time_limit`);
  - 状态码 `TIME_LIMIT_WITH_INCUMBENT` 或 `NO_FEASIBLE_FOUND`(无可行解时,同时返回不可行性证明信息/冲突约束分析,若求解器支持)。
- 可行解与最优性信息必须**持久化到结果文件**(§8.3),不允许静默丢弃;
- 时间上限下的解仍须通过 §9.1 残差审计;gap 非零在结果中显著标注。

### 9.4 可复现性

固定求解器种子(默认 42)、固定线程数;同一实例重复求解必须得到相同解(位级相同)。模型构建顺序确定(注册表顺序遍历设备)。

---

## 10. 固定方案可靠性 vs 重规划敏感性

### 10.1 两类问题的数学差异

**固定方案评估(here-and-now)**:容量与设备选择**在场景集合之外一次性固定**为 $\bar C^*$;每个场景只重优化运行变量:

$$
f(\bar C^*, \xi_s) = \min_{u}\ \big\{C_{op}(\bar C^*, u, \xi_s)\big\}\ \text{s.t. 运行约束(§3, §4)}
\tag{FIXED}
$$

**重规划(recourse)**:每个场景都重新优化容量+选择+运行:

$$
(\bar C^*(\xi_s), u^*(\xi_s)) = \arg\max\ J(x, \xi_s)\ \text{s.t. 完整 MILP(§5)}
\tag{REPLAN}
$$

数学差异:REPLAN 的解空间是 FIXED 解空间的超集(同一问题在容量维上松弛),故对同一场景 $f(\bar C^*(\xi_s), \xi_s) \le f(\bar C^*, \xi_s)$ 恒成立;REPLAN 给出的是"该场景下能获得的最优",FIXED 给出的是"既定投资在不确定性下的实际表现"。两者的差值即"重新规划的后悔值"。

### 10.2 场景采样与可复现种子

- 场景 $\xi_s = (G_{ghi}, T_a, \pi_{buy}, L_e, Q_{Lh}, Q_{Lc})$ 的扰动源:天气年型、电价倍率、负荷倍率(对数正态/历史抽样);
- 采样方法:默认**分层抽样**(年型拉丁超立方),可选 Sobol 拟随机;每场景独立随机数流:
  `rng_s = default_rng(seed_base + 1000 * s)`,`seed_base` 默认 42,所有种子记录在结果元数据;
- 默认样本数 S = 100(可配置);双层分解下建议先 S=10 筛选、再 S=100 细化;
- 同一 `seed_base` 下,任何一次运行的场景集合**逐位一致**(结果可比)。

### 10.3 固定方案的可靠性指标

| 指标 | 公式 | 含义 |
|---|---|---|
| 期望年费用 | $\mathbb{E}_s f(\bar C^*, \xi_s)$ | 平均表现 |
| 费用分位 | $P_{90}$、最差场景费用 | 尾部风险 |
| EENS | $\sum_\tau q_{shed,e}(\tau)\Delta t$(仅允许削减时) | 缺电量 |
| LOLP | 削减步数 / 总步数 | 缺电概率 |
| 年费用标准差 | $\sigma(f(\bar C^*,\cdot))$ | 稳健性 |

### 10.4 重规划的敏感性指标

| 指标 | 公式 | 含义 |
|---|---|---|
| 选型频率 | $f_i = \frac{1}{S}\sum_s z_i^*(\xi_s)$ | 设备 i 的建设概率 |
| 容量分布 | $\mu_i = \frac1S\sum_s \bar C_i^*(\xi_s)$, $\sigma_i$, P10/P90 | 容量对场景的敏感度 |
| 后悔值 | $R_s = f(\bar C^*, \xi_s) - f(\bar C^*(\xi_s), \xi_s)$;均值 $\bar R$ | 信息价值(类似 EVPI) |
| 不稳定指数 | $I = \frac{1}{S}\sum_s \frac{\|\bar C^*(\xi_s) - \boldsymbol\mu\|_1}{\|\boldsymbol\mu\|_1 + \epsilon}$ | 决策对样本的稳定性 |

报告要求:给出两套指标对照表,并用文字结论区分"方案本身不可靠(低可靠性)"与"方案对样本敏感(高不稳定)",避免混用。

### 10.5 计算复杂度说明

FIXED 为 S 个运行 MILP(每年约 8760 步,可并行);REPLAN 为 S 个完整设计 MILP(典型日集,§5.8)。默认并行度 ≤ 4(可配置),总预算受 `time_limit` 约束;超时按 §9.3 处理,已完成的场景子集结果保留并标注完成率。

---

## 11. 求解器适配层实现要点(契约)

### 11.1 模型构建管线

```text
function build_milp(instance, mode):
    # mode ∈ {DESIGN(容量优化), EVALUATION(固定方案), BASELINE}
    idx    <- build_time_index(...)              # §1.5
    coefs  <- precompute_coefficients(instance)  # COP(τ), G_eff(τ), 可用率等参数序列
    m      <- new_model(sense = MAX, money_scale = CNY_per_J)
    add_vars(m, continuous = [...], binary = [...])      # §5.1
    for tau in all_steps:                        # 平衡约束(§3)
        add_constraint(m, ELECTRIC_BALANCE(tau))
        add_constraint(m, HEAT_BALANCE(tau)); add_constraint(m, COLD_BALANCE(tau))
        add_constraint(m, GRID_CAP(tau)); add_constraint(m, PUMP(tau))
    for dev in devices:                          # 设备约束(§4)
        for tau in all_steps: add_constraint(m, dev.constraints(tau))
    for y in years:                              # 跨年约束(§1.4, §5.3-5.5)
        add_constraint(m, SOC_RESET(y)); add_constraint(m, BAT_FADE(y))
        add_constraint(m, REPLACEMENT(y)); add_constraint(m, DEMAND_CHARGE(y))
    if mode == DESIGN: add_constraint(m, CAPEX_COUPLING); set_objective(m, NPV)
    if IRR_min: add_constraint(m, NPV_AT(IRR_min) >= 0)   # §5.2 IRR-LIN
    return m
```

### 11.2 系数预计算(参数化)

每步系数表(全部为常数数组,float64,附单位):COP_h/c(τ)、G_eff(τ)、T_c(τ)、P_pv^avail 的比例系数、δ_h/c(τ)、π_buy/sell/gas(τ)、排放因子、损耗 λ、泵系数、贴现因子 $(1+r)^{-y}$、IRR 贴现因子 $(1+\rho_{min})^{-y}$。

### 11.3 数据流与数值约定

- 输入参数(JSON,带单位)→ 校验/量纲检查(§2.4)→ 预计算系数(float64)→ 矩阵装配 → 求解 → Decimal 重算金额 → 审计(§9.1)→ 导出(§8)。
- 矩阵与目标 float64;二进制变量声明为整数 0/1;求解器种子/线程/时间上限按 §9 传参。
- 求解结果字段命名与 §8.1 表一一对应,禁止别名。

### 11.4 状态码

`OPTIMAL` / `TIME_LIMIT_WITH_INCUMBENT` / `NO_FEASIBLE_FOUND` / `INFEASIBLE_BY_IRR_FLOOR` / `BASE_INFEASIBLE` / `MODEL_AUDIT_FAIL`;每个状态码携带可读说明与建议动作。

---

## 附录 A 符号表

| 符号 | 含义 | 单位 |
|---|---|---|
| τ, y, t | 全局步、年、年内步 | — |
| N, Y, m, Δt | 年步数、年数、日步数、步长 | —, s |
| $P_{buy}, P_{sell}$ | 购/售电功率 | W |
| $P_{pv}, P_{pv}^{avail}, P_{pv}^{cap}$ | 光伏出力/可用/容量 | W |
| $P_{ch}, P_{dis}, E, E_{cap}$ | 电池充/放电、储能、容量 | W, J |
| $P_{hp}, Q_{hp,h}, Q_{hp,c}$ | 热泵电耗、供热、供冷 | W |
| $P_{gas}, Q_b, V_{gas}$ | 锅炉气功率、产热、气量 | W, m³ |
| $P_{chl}, Q_{chl}$ | 电制冷机电耗、产冷 | W |
| $P_{pump}$ | 泵耗电 | W |
| $Q_{sup,h}, Q_{sup,c}, Q_{Lh}, Q_{Lc}$ | 热/冷供给、负荷 | W |
| λ_h, λ_c | 热/冷输配损耗率 | — |
| $C_{imp}, C_{exp}, C_{tr}$ | 并网/售电/输配容量 | W |
| π_buy, π_sell, π_gas, π_dem, π_shed | 电价/气价/需量费/削减惩罚 | CNY/kWh 等 |
| $C_{base,y}, C_{op,y}, R_{sell,y}, NB_y$ | 基准成本、运行成本、售电收入、净收益 | CNY |
| CAPEX, OM, DEP, REPL, ATCF | 投资、运维、折旧、更换、税后现金流 | CNY |
| r, ρ, t_c, L_dep | 贴现率、IRR、税率、折旧年限 | — |
| COP_h, COP_c, η_b, COP_chl | 热泵/锅炉/制冷机能效 | — |
| β_T, NOCT, β, γ_a | 温度系数、标称工作温度、倾角、方位角 | /K, °C, ° |
| $u_{ch}, u_{dis}, u_h, u_c, u_b, u_{chl}$ | 运行二进制 | — |
| z_i, r_y | 建设、更换二进制 | — |
| $C_{cum}, c_{eq}, \kappa_d, R_{cyc}$ | 累计循环、年循环、衰减率、循环寿命 | —, /cycle, cycle |

## 附录 B 默认参数表(全部可配置)

| 参数 | 默认值 | 参数 | 默认值 |
|---|---|---|---|
| 步长 Δt | 3600 s | 规划期 Y | 20 年 |
| 贴现率 r | 8% | 最低 IRR ρ_min | 8% |
| 所得税率 t_c | 25% | 折旧年限 L_dep | 10 |
| 光伏 η_pv / β_T / NOCT | 0.20 / 0.004 /K / 45 °C | 逆变器 P_inv | = P_cap |
| 电池 η_ch / η_dis | 0.95 / 0.95 | SOC 范围 | 10%–90% |
| 电池寿命 R_cyc / EOL | 6000 循环 / 80% | 更换成本系数 | 0.8 × 单位容量投资 |
| 热泵 T_cnd / T_ev | 318 / 280 K | COP 上下限 | 供热 [2,5.5] 供冷 [2.5,6.5] |
| 锅炉 η_b / LHV_V | 0.90 / 35.9 MJ/m³ | 锅炉最小负荷 | 30% |
| 制冷机 COP_chl | 4.0 | 损耗 λ_h / λ_c | 0.05 / 0.08 |
| 泵系数 c_ph / c_pc | 20 W/kW_th | 削减惩罚 π_shed | 5 × 最高购电价 |
| 排放因子 电网 / 燃气 | 0.581 kg/kWh / 2.0 kg/m³ | 时间上限 | 600 s |
| MIP gap | 0.1% | 种子 seed_base | 42 |
| 典型日数 n_typ | 12/年 | 场景数 S | 100 |

## 附录 C 与求解器无关的交付物清单

1. 时间索引函数(§1.5)— 确定性、可单测;
2. 系数预计算器(§11.2)— 输入时间序列 → 常数系数数组;
3. MILP 装配器(§11.1)— 变量/约束/目标注册表;
4. IRR 求根器(§5.6)— 二分/牛顿 + 多根检测;
5. 两阶段求解器(§5.6)— NPV 代理 + IRR 硬约束补救;
6. 多目标调度器(§6.7)— 权重/优先级/ε/Pareto;
7. 残差审计器(§9.1)— 逐物理量独立容差;
8. 方案评估器(§7.4)— 固定容量运行优化;
9. 场景采样器(§10.2)— 可复现种子;
10. 逐时导出器与 KPI 聚合器(§8)。

> 本文档为求解器适配层的单一事实来源;任何与本规格不一致的求解器行为必须在结果元数据中声明差异。
