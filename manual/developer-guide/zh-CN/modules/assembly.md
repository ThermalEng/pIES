# 装配与检查

> 代码边界：`backend/iesplan/assembly/`；文件契约：[装配 YAML](../formats/assembly-yaml.md)、[网络边界与系统方程](../formats/network-relations.md)

`assembly` 是从可编辑业务状态进入计算求解世界的同步校验闸门。它接收项目计算基线、设备实例、来源声明与数据引用、端口连接、系统方程、规划配置与不可变有效财务快照 `EffectiveFinanceConfig`，执行结构、模型、网络与财务完整性检查，签发不可变的 `ValidatedAssemblyArtifact`。

## 职责与边界

- **输入来源**：项目导出（已固定版本与配置）与手写 `ies.assembly` YAML 共享同一解析模型 `AssemblySpec`；同时注入设备目录快照、方程契约与规范数据版本引用。
- **输出产物**：`ValidatedAssemblyArtifact`（由规范化装配文本与携带完整依赖锁的校验回执组成），保留精确业务单位；任一阶段存在阻断错误则输出有序诊断集合，不产生半成品。
- **严格隔离**：不读取 ORM 或会话，不生成求解器输入文件或运行命令，不选择 `generator`/`solver`/计算精度（这些归独立的 `CalculationConfig`）。
- **有效性与可解性区分**：装配通过代表输入完整且领域规则合法，不保证特定生成器必定支持全部数学形式或数学问题一定有最优解。

## 四阶段校验与规范化

1. **结构校验**：YAML 1.2 安全子集解析、`schema` 字头、ID 命名空间与必填字段检查。
2. **模型与数据校验**：核对设备实例 `properties`、`equations`、`predefined` 接口唯一来源绑定（`constant` / `data_repeat` / `data_full` / `data_predict`）、采样分辨率一致性与连续 `step` 校验（`data_full` 与全期价格长度在获得确认运营年限的任务提交边界进一步核对）。
3. **图与系统校验**：检查端口连接方向、载体与量纲兼容性，禁止自环、重复边及 `blind`/`predefined` 端口连线；验证 `system_equations` 与 `planning.constraints`（含 `step_relation` 与 `amount_relation`）引用合法性。
4. **规划与财务完整性校验**：确认实例 `asset_origin`（`existing` / `new`）、显式设备成本绑定（`costed` / `none` 及按增量语义的 `driver` 齐全性）、`tariff_bindings` 与 `demand_bindings` 合法性、规划目标逐项财务标识符与量纲兼容性。

四阶段通过后，校验器对映射与无序集合按稳定键排序，剥离非语义空白与注释，连同设备、方程与有效财务快照版本锁一起签发规范产物。

### 端口级网络语义

装配域拥有统一的端口网络分区能力，检查阶段与生成器消费同一分区事实，禁止维护设备级隐式短接或各自计算连通图：

- **理想公共母线**：通过 `connections` 中的 `{from, to}` 对真实可连接端口（`in` / `out` / `bidirectional`）的传递闭包划分公共母线；同一实例的不同端口不因同属一台设备或相同载体而自动合并。每条母线逐 `step` 满足流量守恒（`out` 为正供给、`in` 为负吸收、`bidirectional` 按模型有符号净供给）。
- **未连接端口关闭语义**：未接入任何连接的可连接端口构成独立关闭边界（每步流量约束为零），不自动补边、不作为免费外源或未计量排放。仅有源或仅有汇的母线不直接判定为输入非法，供需是否可行由实际方程与求解结果判定。
- **来源分配与传输边界**：物理连接、定向分配与跨载体转换是独立概念。需要限定某一电源的去向或建立来源能量账户时，须通过[网络边界与系统方程](../formats/network-relations.md)中的 `system_equations` 显式声明辅助流量与守恒方程，不得仅凭缺少直连边推断防逆流语义。
