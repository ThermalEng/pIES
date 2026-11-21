# 网络边界与系统方程

> 所有者：`assembly`（端口边界与引用）、`modeling`（方程 AST 与数学贡献）；上位规范：[架构宪法](../ARCHITECTURE_CONSTITUTION.md)

本页定义装配 YAML 中的**完整网络边界语义**与可选 **`system_equations` 系统方程块**契约；基础连接与 `step_relation` 约束定义见[装配 YAML](assembly-yaml.md)。

## 适用边界

pIES 严格区分物理端口连接、端口关闭边界、设备内部转换与跨实例分配/来源记账：

- **公共母线**：按[装配连接](assembly-yaml.md#连接)对可连接端口形成理想混合母线；
- **系统方程（`system_equations`）**：用于表达跨实例能量分配、来源追踪、系统辅助账户及多设备间状态关联，复用 `modeling` 统一方程 AST 与单位体系，不引入价格或财务目标（财务引用辅助量须经 `amounts` 中的 `system_quantity` 显式声明）。

## 完整网络边界

| 端口 / 变量类别 | 网络语义 |
|---|---|
| 已连接 `in` / `out` / `bidirectional` | 加入所属载体母线分区并按每一步流量守恒（`out` 为正供给、`in` 为负吸收、`bidirectional` 按有符号净供给） |
| 未连接 `in` / `out` / `bidirectional` | 独立关闭边界，在每个运营步强制流量等于 `0`，不自动补入外部源汇 |
| `predefined` | 保持外部绑定数据源序列，不参与母线守恒，不因未连接置零 |
| `blind` 与设备内部变量 | 保持设备内部方程语义，不对外连接也不因未连线置零 |

- **关闭边界语义**：端口未连线属于明确数学边界（零流量约束），不直接视作输入校验错误；若与设备或负荷方程冲突，由统一分析或求解器判定可行性。外部能源输入与外送均应连接显式的源/汇设备模型。
- **有效域分离**：同一理想母线上不同端口的有效区间无需两两相交，由母线总量守恒与各自模型方程共同约束。

## `system_equations` 字段规范

装配顶层可选字段 `system_equations`（省略等价于 `{}`）为按块 ID（`lower_snake_case`）索引的映射，每个块可定义局部常数、引用、辅助变量与方程关系：

| 字段 | 规则与类型 |
|---|---|
| `constants` | 局部已知技术系数映射 `name -> {value, unit}`（不可为待优化变量） |
| `references` | 局部别名映射，指向装配中已有设备实例的 `interface`、`property` 或内部 `variable`（见下表） |
| `variables` | 本块辅助时序变量映射 `name -> {unit, valid_range, initial?}`；`valid_range` 与 `initial` 复用设备方程规则并可引用本块同量纲常数或 `property` 引用 |
| `relations` | 有序关系数组 `[{id, expression}]`；复用设备方程受限语法，允许 `[t]` 与本块带初值状态变量的 `[t-1]`（禁用 `step_duration` 别名冲突） |

`references` 各项为以下三选一判别联合：

| `kind` | 完整结构与绑定对象 |
|---|---|
| `interface` | `{kind: interface, instance, interface}`；引用设备实例真实接口（`predefined` 为物化输入，其余为模型变量） |
| `property` | `{kind: property, instance, property}`；引用实例技术属性（被规划变量接管时绑定同一规划变量） |
| `variable` | `{kind: variable, instance, variable}`；引用设备内部 `equations.variables` 变量 |

不同系统方程块彼此独立，不支持跨块直接读取内部变量；复杂跨实例关系应声明在同一块内。

### 分配与来源记账示例

系统方程提供构建定向分配与来源能量账户的通用原语，所需守恒、效率与互斥须由模型显式写出（仅省略母线连线不能证明某来源不会通过母线间接替代其他来源外送）：

```yaml
system_equations:
  delivery_account:
    constants: {}
    references:
      supply: {kind: interface, instance: s, interface: power_out}
      demand: {kind: interface, instance: l, interface: power_in}
    variables:
      delivered:
        unit: kW
        valid_range: {minimum: 0, maximum: null}
    relations:
      - {id: source_total, expression: "supply[t] = delivered[t]"}
      - {id: sink_total, expression: "demand[t] = delivered[t]"}
```

## 保存、冻结与公开输出

- **编辑与冻结**：工作区“系统技术方程”与草稿命令共同管理 `{system_equations, outputs}` 完整片段；草稿、项目版本、规范化装配与 `CalcSnapshot` 完整保留所有块定义及序列化 AST，不按表单白名单裁剪。
- **辅助序列输出**：通过 `outputs.system_series: [{block, variable}]`（省略等价于 `[]`）声明需要发布的系统辅助曲线；其输出身份、分页读取与 Excel 导出协议见[系统序列映射](solver-bundle.md#系统序列映射)与[求解步序列读取](../contracts.md#求解步序列读取)。
- **能力与诊断**：任务提交依实际使用特性检查 `network.closed_ports`、`modeling.system_equations` 与 `outputs.system_series` 能力；不合法的字段或引用在装配输入边界定位到块与关系 ID，转换条件不足由生成阶段按模型来源定位报告。
