# 扩展体系

> 文档状态：生效蓝图；规范版本：1.1.0；上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)

## 总体模型

统一设备技术内容、方程转换、计算生成器、执行器、结果适配器、存储和可选数据来源通过所属模块的公开 contract 接入。每个模块独立拥有注册状态；不存在跨模块全局注册表。

正式 provider 在启动时发现和原子注册，不支持运行期热加载。设备 YAML 是安全声明内容，不是可执行 provider：内置或用户设备都使用同一个 schema、parser、validator 和方程语言，不为某个设备增加专用 Python 类、命令或语义版本。

网页“模型与算法”中，设备发布由 revision、规范内容摘要和校验回执固定；算法插件 ZIP 才使用独立语义版本，并作为不可变任务载荷交给系统内置通用沙箱 generator/executor/result adapter。上传插件不会把用户模块加入 API、普通 Worker 或 provider 注册表。详细生命周期见[模型与算法](customization-center.md)。

## 身份与版本规则

设备内容与可执行扩展使用不同身份机制：

| 对象 | 稳定身份 | 固定具体内容 | 兼容性版本 |
|---|---|---|---|
| 设备 YAML | `device.id` | 规范 SHA-256、发布 revision、校验回执 | 仅统一 `ies.device-model` schema 版本 |
| 算法插件/provider | 命名空间 ID | 包/实现摘要与发布记录 | 独立三段式语义版本 |
| 数据版本 | dataset ID | 规范内容摘要与变换记录 | 文件 schema 版本 |

禁止给单个设备添加 `version`，也禁止用同 ID 的当前内容解释历史摘要。设备 schema 的破坏性变化提升统一 schema MAJOR；可执行 provider 的破坏性 contract 变化提升其自身 MAJOR。

## 设备内容接入

设备公开 descriptor 只来自 [设备模型 YAML](formats/device-model-yaml.md)，至少包含：

- 稳定设备 ID 和本地化名称；
- 非时变纯技术 `properties`、单位和值域；
- `interfaces` 的 carrier、单位、有效区间和五类 type；
- `predefined` 的 `constant/data_repeat/data_predict` 来源声明；
- 受限声明式 `equations`；
- 统一 schema 版本、规范内容摘要和校验回执。

设备文件不得包含独立设备版本、价格、成本、税务、折旧、计算精度、算法选择、实现路径或可执行入口。前端按同一公开 descriptor 呈现 properties 和 interfaces；增加设备不能要求前端、assembly 或 generator 增加设备 ID 判断。

设备数据样例按[设备数据 CSV](formats/device-data-csv.md)提供，只能绑定精确设备内容中的 `predefined` interface。

## 方程转换能力

`modeling` 提供统一受限方程 parser、AST、语义校验和公共数学贡献 contract。它不是每设备一条命令的扩展点，不存在独立 `ModelCommand` 目录。

若需扩展方程语言或公共 AST，必须：

- 对所有设备保持同一语法和语义，不按设备 ID 分支；
- 明确操作符的类型、单位、索引、确定性和复杂度上限；
- 升级公共方程 contract，提供迁移与协议测试；
- 输出 solver 无关的变量、关系、状态、接口流和结果映射；
- 禁止动态执行、文件/网络访问和 solver 私有对象。

装配固定设备内容摘要和方程 contract；generator 在执行前验证能力匹配。

## 计算 provider

计算扩展拆成三个主要角色：

- GeneratorProvider：只接受 `ValidatedAssemblyArtifact`，生成 [Solver Bundle](formats/solver-bundle.md)；
- ExecutorProvider：把受信任 executable ID 映射为受控进程或容器执行方式；
- ResultAdapterProvider：把声明输出和执行回执映射为统一 `ComputeResult`。

GeneratorDescriptor 必须提前声明支持的装配 schema、计算 mode、方程 contract、solver、选项、计算精度、内部单位、确定性和配套 ResultAdapter。设备投资、O&M 和能源价格由规划经济配置提供；generator 可以据此形成目标，但不得修改或扩充设备技术 descriptor。

ExecutorDescriptor 声明 allowlist、资源隔离、取消和 readiness 能力。运行时只执行 Solver Bundle 中的结构化命令；装配 YAML 和设备 YAML 不得含 shell 或代码路径。

## 存储与数据 provider

存储适配器只实现字节级保存、读取、删除和健康能力。内容摘要、对象元数据、引用、配额、保留和恢复由 `storage` 领域统一管理；业务模块不能感知底层路径或凭证。

外部气象、价格或排放数据只能生成可审计的数据版本。技术序列按 predefined interface 绑定；价格序列进入规划经济配置，不进入设备技术 interface。数据必须先经过单位、时区、分辨率、缺失值、范围和来源检查，GeneratorProvider 与 SolverRuntime 不得在运行时访问外部服务。

## 可执行扩展的共同元数据

每个算法插件或 provider 至少声明：

- 稳定命名空间 ID 与三段式语义版本；
- 所需产品公共 contract 版本；
- 能力、依赖、输入输出 schema 和单位；
- 错误、本地化消息键、确定性、随机种子和资源要求；
- 契约测试、参考输入输出和容差；
- 许可证、供应者和迁移说明。

稳定 ID 不包含部署路径或实现类名。以上要求不应被误用于恢复单设备版本字段。

## Provider 开发流程

1. 先选择所属模块和单一 provider 角色；设备内容只走统一 YAML contract；
2. 阅读对应[模块开发手册](module-development.md)，确定输入输出和失败语义；
3. 为可执行 provider 选择稳定 ID 与语义版本；
4. 实现公开 Protocol，并把环境依赖封装在 provider 内；
5. 提供 descriptor、能力、配置 schema 和健康检查；
6. 使用所属模块统一契约测试集验证；
7. 在组合根注册候选，验证依赖后原子发布；
8. 用一个外部扩展证明核心模块和前端无需修改。

用户设备的开发流程则是“写统一 YAML/CSV → 安全校验 → 规范化 → 内容摘要 → 发布”，不执行上述 Python provider 注册步骤。

## 扩展验收

- 新设备不修改无关模块映射，也不增加独立版本或命令；
- 新 provider 的 ID、版本、依赖和能力在启动时可解析；
- 构建失败不污染已发布状态；
- 输入、输出、单位和诊断符合公共 contract；
- 历史任务按设备内容摘要或 provider 精确版本可解释；
- 归档解包不能路径逃逸或携带符号链接；
- 用户代码不会被 API 或普通 Worker import、执行或加入 `sys.path`；
- 所有测试在 Docker 中执行。

不得在失败时回退到同名内置实现、旧设备内容、默认 interface 或静态设备映射。

## 完成标准

- 设备可只按统一公共 YAML/CSV 开发，算法扩展可只依赖公开 contract 和包 schema 开发；
- 核心仓库无需增加扩展或设备 ID 判断；
- 合法与非法内容/provider 都通过统一协议测试；
- 快照和证据分别显示设备内容摘要及可执行 provider 精确版本；
- 升级、缺依赖和失败不会留下部分注册状态或历史语义漂移。
