# 扩展体系

> 文档状态：生效蓝图；规范版本：1.1.0；上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)

pIES 将“声明式业务内容”与“可执行计算能力”分开扩展：设备模型以纯数据文档接入，计算生成、受控执行、结果适配与存储则通过所属模块的公开 Protocol 注册。

## 扩展角色划分

| 扩展类型 | 所属模块 | 固定内容与版本机制 | 发现与运行方式 |
|---|---|---|---|
| 设备技术模型 | `devices` | 稳定 `device.id` + 发布 `revision` + 统一 `ies.device-model` schema（无单设备语义版本） | 声明式 YAML 解析发布，所有设备共用统一解析器与方程 AST |
| 计算生成器 | `computation` | 命名空间 ID + 三段式语义版本 | 组合根启动发现并原子注册 |
| 求解执行器 | `computation` | 命名空间 ID + 三段式语义版本 | 组合根启动发现，映射到受控本地进程或容器运行方式 |
| 结果适配器 | `computation` | 命名空间 ID + 三段式语义版本 | 组合根启动发现，按 Bundle/回执精确引用解析 |
| 存储适配器 | `storage` | 命名空间 ID + 三段式语义版本 | 组合根注入，仅负责字节级存取与健康状态 |
| 用户算法插件（`1.1.0` 目标） | `customization` | 插件 ID + 三段式语义版本 + 离线依赖锁 | 打包为 `*.algorithm.zip`，由独立隔离容器运行，不加入 API/Worker 进程 |

正式可执行 Provider 仅在进程启动时由组合根完成发现、依赖校验并一次性原子发布，不支持运行期热加载，亦不存在跨模块的全局注册表。

## 设备内容接入

设备描述符仅来源于[设备模型 YAML](formats/device-model-yaml.md)，只定义非时变技术属性（`properties`）、五类时序接口（`interfaces`）及受限声明式方程（`equations`）。

- 设备定义不包含成本、价格、税务、计算精度或执行路径；
- `predefined` 接口在设备定义中仅声明槽位与单位/值域，项目级数据源（`constant` / `data_repeat` / `data_full` / `data_predict`）在装配实例绑定；
- 新增设备无需在前端、装配或生成器中编写设备 ID 特判分支。

## 方程能力扩展

`modeling` 维护统一的受限方程语法、公共 AST 与数学贡献契约，不设立“每设备一条命令”的扩展注册表。扩展方程操作符或函数时须满足：

1. 对全部设备保持统一语法、类型、量纲与时间索引规则；
2. 明确操作符的复杂度与确定性约束；
3. 输出与求解器无关的变量、约束与映射元数据；
4. 同步更新通用生成器降级能力与契约测试。

## 计算 Provider 体系

计算阶段通过三个独立角色解耦数学构模与进程执行：

- **GeneratorProvider**：直接消费已签发的 `ValidatedAssemblyArtifact`、设备数学贡献、全周期物化序列、`EffectiveFinanceConfig`、`CalculationConfig` 与冻结评价确认，执行唯一业务单位到求解内部单位转换，确定性产出 [Solver Bundle](formats/solver-bundle.md)。
- **ExecutorProvider**：维护受信任 executable 白名单、参数与环境过滤规则、资源与网络隔离策略，执行 Bundle 中的结构化命令并返回 `ExecutionReceipt`。
- **ResultAdapterProvider**：消费只读 Bundle、回执与原始求解输出，将求解器状态、目标、变量及财务事实还原为带规范业务单位的 `ComputeResult`。

每个 Provider 通过 Descriptor 显式声明支持的 Schema、模式、能力集合、Options JSON Schema（Draft 2020-12）及配套依赖版本。

## 存储与外部数据源

- **存储适配器**：仅封装底层 Blob 字节读写、删除与存在性/大小查询；对象身份、Owner 引用清单、保留策略与中断恢复统一由 `storage` 领域服务管理。
- **外部时序数据**：气象、负荷或价格数据先经过格式与单位校验形成不可变数据版本或价格对象，再在装配或财务文件绑定；数据准备阶段保持来源原始分辨率与连续 `step`，不执行隐式插值或重采样。

## 开发新 Provider 的标准流程

1. 确定目标所属模块与单一 Provider 角色，分配带命名空间的稳定 ID 与语义版本；
2. 实现对应模块的公开 Protocol 与 Descriptor（包含配置 Schema 与自检方法）；
3. 使用对应模块的契约测试集验证合法输入、非法输入、单位边界与确定性；
4. 在组合根注册候选 Provider，验证依赖解析与部分失败回滚行为；
5. 通过端到端测试证明在不修改 API、装配与前端类型映射的前提下完成接入。
