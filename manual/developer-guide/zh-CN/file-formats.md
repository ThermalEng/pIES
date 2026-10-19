# 文件格式标准

> 文档状态：生效目标契约；设备模型、设备数据与装配目标 schema 为 `2.0.0`，`FinanceProfile` 目标 schema 为 `2.0.0`、`FinanceOverrides` 目标 schema 为 `3.0.0`、`EffectiveFinanceConfig` 目标 schema 为 `3.0.0`，财务模型目标 schema 为 `1.0.0`，Solver Bundle 目标 schema 为 `1.0.0`，项目包目标 `format_version` 为 `4.0.0`。本章规定插件开发和离线交换必须共同遵守的文件边界；财务三件套的字段级定义见[财务 YAML 契约](formats/finance-yaml.md)，财务模型的字段级定义见[财务模型 YAML 契约](formats/financial-model-yaml.md)，项目包布局见[项目包](formats/project-package.md)。

目标不是把内部对象“导出成差不多能读的文件”，而是让开发者只看本章及对应格式页，就能手写、校验和交付一个不依赖当前代码目录的输入包。

任何目录 YAML、CSV 和装配对象都不因文件扩展名自动等同于本标准；只有通过对应版本的完整校验与规范化，才可声明兼容。

## 公共计算与财务文件契约

| 契约 | 文件 | 作用 | 直接消费者 |
|---|---|---|---|
| [设备模型 YAML](formats/device-model-yaml.md) | `*.device.yaml` | 描述设备身份、非时变技术常量、序列接口和声明式方程 | 设备目录、GUI schema、装配与技术模型校验 |
| [设备数据 CSV](formats/device-data-csv.md) | `*.data.csv` | 提供带 step、采样间隔、单位和来源语义的设备序列数据 | 数据导入器、装配校验、计算阶段序列物化 |
| [价格序列 CSV](formats/price-series-csv.md) | `*.price.csv` | 提供与全期时轴同轴的能源价格序列（`step` 从 0 连续，价格为有限 Decimal，正/零/负均可） | 数据导入器（同上传入口）、任务提交覆盖核对、计算阶段价格物化 |
| [地区 FinanceProfile YAML](formats/finance-yaml.md) | `*.finance-profile.yaml` / `finance_profile.yaml` | 注册地区财务基准：`region`/`currency`/`base_year`/`price_basis`/`cost_method`、按 `finance_type` 的时间口径成本分量、能源购售价格 | 财务合并器与装配校验 |
| [项目 FinanceOverrides YAML](formats/finance-yaml.md) | `*.finance-overrides.yaml` / `finance_overrides.yaml` | 稀疏原子覆盖：引用 `FinanceProfile {id}`，只替换既有 `finance_type` 分量与 constant 能源价格的金额；目标版本 `3.0.0` 增加 `model_ref` 与 `parameters_values` 覆盖 | 财务合并器 |
| [有效财务快照](formats/finance-yaml.md) | `*.effective-finance.yaml` / `effective_finance.yaml` | 合并器确定性产物：`profile_id` 与合并后的完整 `finance_types` / `energy_prices` | 装配校验、计算生成与求解 |
| [财务模型 YAML](formats/financial-model-yaml.md) | `financial_model.yaml` | 声明式财务模型：核算主体口径、参数槽位、年度时间轴关系式、状态结转、输出声明 | 财务计算、生成器能力核对、装配校验 |
| [装配 YAML](formats/assembly-yaml.md) | `*.assembly.yaml` | 固定项目计算基线、设备实例、规范数据与来源绑定、连接、指向有效财务快照的精确引用、`finance_binding`、`tariff_bindings`、规划配置 | 装配校验器 |
| [Solver Bundle](formats/solver-bundle.md) | 一个目录或不可变归档 | 固定求解器输入文件、受控命令、预期输出和结果适配器 | 求解运行时 |
| [计算配置](formats/calculation-config.md) | 计算提交输入（JSON 兼容映射） | 固定 mode、预测算法与参数、generator、solver、精度、收敛、时间限制、选项、种子和输出选择 | 生成器（生成前能力核对） |
| [项目包](formats/project-package.md) | `*.zip`（`manifest.json` 在根） | 离线交换：当前工作状态、包内历史版本及其精确财务/规划/数据依赖 | 项目包导入器 |

人工 authoring：设备模型 YAML、设备数据 CSV、`FinanceProfile`、`FinanceOverrides`、财务模型 YAML 与装配 YAML 允许人工编写。`EffectiveFinanceConfig` 只能由合并器生成，可导出、导入和进入快照，不能人工 authoring（导入时连同来源 Profile 与 Overrides 重新合并验证）。Solver Bundle 必须由生成器产生，不作为用户手写的项目输入。

## 扩展交付契约

[算法插件包](formats/algorithm-plugin-package.md)使用 `*.algorithm.zip` 交付装配加工程序、算法程序、依赖锁、结果声明和测试样例。它是用户扩展的分发与隔离执行契约，不是第五种核心计算文件，也不能代替 Solver Bundle。包由开发者组装并上传，只能由插件校验器和隔离运行器处理。

## 从手写文件到结果

```text
地区 FinanceProfile（人工 authoring，注册地区财务基准）
项目 FinanceOverrides（人工 authoring，精确引用 Profile revision）
财务 FinancialModel（人工 authoring，由 Overrides 精确引用 ID + revision）
        │ 三者并列
        ▼
merger（确定性合并 + 完整校验）──→ EffectiveFinanceConfig（不可人工 authoring）
                                          │ 只消费该不可变快照
                                          ▼
设备模型 YAML / 设备数据 CSV / 规划配置 → 校验与规范化 → 装配 YAML ──→ ValidatedAssemblyArtifact
                                                          │             │
                                          assembly.finance │精确引用     ▼
                                          （精确引用，不内联）    物化序列 → GeneratorProvider → Solver Bundle
                                                          ▼
                                          SolverRuntime 执行 → ExecutionReceipt → ComputeResult
```

装配 YAML 只表达业务装配、规划意图、指向有效财务快照的精确引用与财务绑定，不内联完整财务配置，也不包含 generator、solver、精度、算法选项、shell、可执行文件路径或 Python 模块路径。生成器只接受通过校验的规范装配产物和独立计算配置；运行时只接受 Solver Bundle，不再解释设备、项目或装配规则。计算只消费不可变 `EffectiveFinanceConfig`，不运行期继承 Profile、不读取最新地区价格、不静默默认值。

## 通用书写规则

所有公共文本格式共同遵守：

- UTF-8 编码；规范输出使用 LF 换行；
- ID 使用 ASCII 小写命名空间字符串，例如 `acme.device.pv`；局部 ID 使用 `lower_snake_case` 或短横线形式，但同一文件内保持一致；
- `schema_version`、插件版本和依赖版本使用带引号的 `MAJOR.MINOR.PATCH`；单个设备没有语义版本；
- 数值必须有限，禁止 `NaN`、`Infinity` 和依赖语言实现的特殊标量；财务金额使用 `{value, unit}` 原子十进制定点字符串，禁止 `null` 与部分金额；
- 计算序列以 `step` 表达，不携带时间戳和时区；原始输入与项目基线使用相同分辨率并保持文件内 `step` 连续；`data_repeat` 的完整来源序列可为完整日、周或年，整体作为重复基线且不另设周期字段，`data_full` 则必须原样覆盖全周期全部点数；装配前不做重采样、插值、聚合或融合，全周期计算序列只在计算阶段生成并统一对齐；
- 单位必须显式，且来自[公共契约](contracts.md)规定的单位词汇；
- 文件路径一律相对所属包，禁止绝对路径、`..`、符号链接逃逸和隐式当前目录；
- 密钥、令牌、数据库 ID、宿主机路径、ORM 字段和实现模块路径不得进入公共文件；
- 未知字段默认拒绝。插件私有扩展只能放在命名空间化的 `extensions` 下，且不能改变核心字段语义。

YAML 采用 YAML 1.2 的安全子集：两个空格缩进，禁止 Tab、自定义 tag、anchor、alias、合并键和重复键。解析器不得构造任意语言对象。

## 版本与兼容

每种契约独立版本化，不与产品版本或插件版本机械同步：

- PATCH：只澄清或增加不改变验证结果的说明；
- MINOR：增加旧消费者可以安全忽略的可选能力；
- MAJOR：删除、重命名、改变默认值或改变同一输入的业务语义。

消费者必须先识别 `schema`，再按自己声明支持的 `schema_version` 校验。不能识别的 MAJOR 必须拒绝；不得猜测字段、静默降级或把旧格式当作新格式继续执行。

### 项目包的可移植历史依赖

项目包现行目标格式为 `4.0.0`，以 `format_version` 声明；该目标改变历史依赖的携带与引用语义，不保留旧 `1.x` / `2.x` / `3.x` 解析路径。目标 `4.0.0` 增加 `financial_model.yaml` 必需文件与 `version_configs.financial_model` 键；当前实现仍是 `3.0.0`，本次修订只定义目标契约，`4.0.0` 导出/导入与完整模型的 GUI 尚未实现。完整布局见[项目包](formats/project-package.md)。

- 数据集元数据和数据文件按数据版本唯一定位，同一数据集的不同版本不能共用一个元数据条目。
- 包须携带当前工作状态及包内历史版本恢复所需的精确财务、规划与数据依赖，包括当前已不再启用、但仍被历史引用的依赖；不导出无关配置历史或全库内容。
- 每组财务依赖包含其精确来源的 `FinanceProfile`、`FinanceOverrides`、`EffectiveFinanceConfig` 和财务模型 YAML；不能要求导入环境预先注册历史 Profile，也不能用导入环境的同名或最新 Profile 替代包内来源。
- 草稿与包内全部历史版本内容引用的项目已保存模型必须随包携带（仅历史引用、当前草稿已不引用的同样携带），携带集合与引用集合必须恰好相等：引用未携带或多携带未引用模型均拒绝导入；只被冻结证据引用的设备类型串属于证据原文，不参与集合。
- 财务模型 YAML 随包携带，`effective_finance.yaml` 的 `model_ref` 指向该模型；导入时校验模型 ID 与 revision 匹配，不静默替换为新模板。
- 项目模型随包携带其**规范模型文本原样**（规范化存储形态的紧凑 JSON 字节），条目按来源编号升序；导入时按来源基础 ID 经正式保存路径重建本项目模型行（新编号、新行 id），再把草稿与历史版本内容里的模型引用与设备类型串映射为新 ID。用户侧编写模型仍使用正式设备模型 YAML 契约。
- 声明式配置仍使用各自正式 YAML 契约；包内引用只用于定位包内来源，不能作为目标数据库的身份或修订号直接复用。确认导入时建立来源到新项目依赖的明确映射，再发布引用这些依赖的版本；缺席保持缺席，不用当前配置替代历史。
- 外部上传入口一次性校验文件及依赖关系，失败原因通过标准错误信封的 `params.reasons` 返回；后续确认消费已接受的提案，不新增内部摘要比对或重复语义校验。

项目独立身份、历史恢复及证据来源原则见[领域模型与追溯链](domain-model.md)。

## 声明式配置与 YAML 边界

人工编写、导入导出、进入快照的声明式配置统一使用 YAML：`device` / `assembly` / `finance`（`FinanceProfile`、`FinanceOverrides`、`EffectiveFinanceConfig`）/ `planning` / `calculation` 与项目包内配置均为 `.yaml`，按安全子集解析与规范化，不保留 `finance_config.json` 别名或 JSON/YAML 双格式兼容。HTTP JSON DTO、数据库内部行/列存储、求解器专用输入和大结果可继续使用对应域的原生格式，不强制转 YAML。

前端结构化表单不是第二种配置协议。凡业务动作的权威输入是上述文件，前端必须直接生成该文件的完整字节并提交，后端必须交给同一个正式文件解析器读取；不得由前端或测试先解析成另一套简化 DTO 后绕过文件入口。人工文件与前端生成文件共享相同关键字、错误码和保存语义。

## 人工编写与规范化

人工文件可以保留注释和友好顺序。进入快照前，校验器必须生成唯一规范形态：

1. 解析安全子集并拒绝重复键、非法标量和未知核心字段；
2. 解析设备 revision、规范数据与预定义来源引用、规划配置、有效财务快照引用与财务绑定（`finance_binding`/`tariff_bindings`），确认技术方程与业务输入完整；
3. 核对项目计算基线、各输入来源的分辨率、连续 `step` 和对应模式的覆盖要求；设备数据关键字只解析为项目目录内 CSV 相对路径，并在实例化入口形成来源绑定；
4. 按格式规定排序并移除注释、别名和非语义空白；
5. 生成规范字节与外部资源引用；
6. 生成包含校验器 ID、版本、依赖锁和零阻断诊断的校验回执。

财务三件套的规范化遵循[财务 YAML 契约](formats/finance-yaml.md)：解析 YAML（安全子集）→ 校验 → 规范化。`FinanceOverrides` 引用 `profile {id}`，`EffectiveFinanceConfig` 不携带内容摘要，文本文件按字头校验。

只有“规范装配文本 + 校验回执”共同组成的 `ValidatedAssemblyArtifact` 可以进入生成器。用户修改配置后必须重新走入口校验，旧回执不得复用。

每种允许由前端生成的配置文件都必须有端到端往返契约测试：表单值 → 完整文件字节 → 正式后端解析器 → 领域值。测试必须逐项断言关键字对应的业务值，并覆盖未知关键字、缺失必填项、错误类型、非法枚举以及所引用项目 CSV 不存在、不可读或内容错误；这些失败必须返回登记错误码、字段路径和可用时的行列，不得成为 500、空对象、默认补值或部分保存。测试不得使用仅供测试的简化解析器替代正式入口。

人工编辑或模板实例化产生的设备 YAML 在进入项目正式模型目录前也必须通过其完整文件契约。候选模型字节可以送入校验器，但只有校验成功的规范模型与回执才能原子保存；失败候选不能先落入项目目录再等待异步清理。项目模型保存不接收配套数据文件；数据文件由数据集流程独立保存，并在装配中显式绑定。

## 插件交付最小集合

一个新设备技术内容至少交付：

1. 一份设备模型 YAML；
2. 一份最小合法数据 CSV 和一份典型数据 CSV；
3. 一份引用该设备的最小装配 YAML；
4. 格式契约测试、非法样例和预期诊断；
5. 使用现有方程 contract 与 generator 得到的可复核结果基线。

设备内容不自带专用方程解释器或 generator provider。只有现有公共方程/generator 能力确实无法表达目标算法时，才按[算法插件包](formats/algorithm-plugin-package.md)另行交付独立插件；插件能力不能藏进设备 YAML。

插件不能用 README 中的额外步骤弥补文件中的隐含字段。样例必须可以复制、改 ID 后直接通过相应版本的校验器。

## 格式变更流程

变更任何核心字段前，开发者必须回答：

1. 权威所有者是哪一种契约；
2. 旧文件能否保持原语义；
3. 需要升 PATCH、MINOR 还是 MAJOR；
4. 规范化是否变化；
5. 是否需要离线迁移器及迁移回执；
6. 手写示例、JSON Schema/CSV schema 与契约测试是否同步。

文件格式属于公共设计。实现内部重构不得顺带改变它；确需不兼容修改时，必须先有 ADR、迁移规则和版本升级。
