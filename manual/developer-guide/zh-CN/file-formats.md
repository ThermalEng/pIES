# 文件格式标准

> 文档状态：生效目标契约；设备模型、设备数据与装配目标 schema 为 `2.0.0`，财务三件套（`FinanceProfile` / `FinanceOverrides` / `EffectiveFinanceConfig`）目标 schema 为 `1.0.0`，Solver Bundle 目标 schema 为 `1.0.0`。本章规定插件开发和离线交换必须共同遵守的文件边界；财务三件套的字段级定义见[财务 YAML 契约](formats/finance-yaml.md)。

目标不是把内部对象“导出成差不多能读的文件”，而是让开发者只看本章及对应格式页，就能手写、校验和交付一个不依赖当前代码目录的输入包。

任何目录 YAML、CSV 和装配对象都不因文件扩展名自动等同于本标准；只有通过对应版本的完整校验与规范化，才可声明兼容。

## 公共计算与财务文件契约

| 契约 | 文件 | 作用 | 直接消费者 |
|---|---|---|---|
| [设备模型 YAML](formats/device-model-yaml.md) | `*.device.yaml` | 描述设备身份、非时变技术常量、序列接口和声明式方程 | 设备目录、GUI schema、装配与技术模型校验 |
| [设备数据 CSV](formats/device-data-csv.md) | `*.data.csv` | 提供带 step、采样间隔、单位和来源语义的设备序列数据 | 数据导入器、装配校验、计算阶段序列物化 |
| [地区 FinanceProfile YAML](formats/finance-yaml.md) | `*.finance-profile.yaml` / `finance_profile.yaml` | 注册地区财务基准：`region`/`currency`/`base_year`/`price_basis`/`cost_method`、按 `finance_type` 的时间口径成本分量、能源购售价格 | 财务合并器与装配校验 |
| [项目 FinanceOverrides YAML](formats/finance-yaml.md) | `*.finance-overrides.yaml` / `finance_overrides.yaml` | 稀疏原子覆盖：引用 `FinanceProfile {id}`，只替换既有 `finance_type` 分量与 constant 能源价格的金额 | 财务合并器 |
| [有效财务快照](formats/finance-yaml.md) | `*.effective-finance.yaml` / `effective_finance.yaml` | 合并器确定性产物：`profile_id` 与合并后的完整 `finance_types` / `energy_prices` | 装配校验、计算生成与求解 |
| [装配 YAML](formats/assembly-yaml.md) | `*.assembly.yaml` | 固定项目计算基线、设备实例、规范数据与来源绑定、连接、指向有效财务快照的精确引用、`finance_binding`、`tariff_bindings`、规划配置 | 装配校验器 |
| [Solver Bundle](formats/solver-bundle.md) | 一个目录或不可变归档 | 固定求解器输入文件、受控命令、预期输出和结果适配器 | 求解运行时 |

人工 authoring：设备模型 YAML、设备数据 CSV、`FinanceProfile`、`FinanceOverrides` 与装配 YAML 允许人工编写。`EffectiveFinanceConfig` 只能由合并器生成，可导出、导入和进入快照，不能人工 authoring（导入时连同来源 Profile 与 Overrides 重新合并验证）。Solver Bundle 必须由生成器产生，不作为用户手写的项目输入。

## 扩展交付契约

[算法插件包](formats/algorithm-plugin-package.md)使用 `*.algorithm.zip` 交付装配加工程序、算法程序、依赖锁、结果声明和测试样例。它是用户扩展的分发与隔离执行契约，不是第五种核心计算文件，也不能代替 Solver Bundle。包由开发者组装并上传，只能由插件校验器和隔离运行器处理。

## 从手写文件到结果

```text
地区 FinanceProfile（人工 authoring，注册地区财务基准）
项目 FinanceOverrides（人工 authoring，精确引用 Profile 摘要）
        │ 两者并列
        ▼
merger（确定性合并 + 完整校验）──→ EffectiveFinanceConfig（不可人工 authoring，三摘要）
                                          │ 只消费该不可变快照
                                          ▼
设备模型 YAML / 设备数据 CSV / 规划配置 → 校验与规范化 → 装配 YAML ──→ ValidatedAssemblyArtifact
                                                          │             │
                                          assembly.finance │精确引用     ▼
                                          （摘要+血缘，不内联）    物化序列 → GeneratorProvider → Solver Bundle
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
- 计算序列以 `step` 表达，不携带时间戳和时区；原始输入与项目基线使用相同分辨率并保持文件内 `step` 连续，但不要求不同来源在物化前具有相同点数；`data_repeat` 的完整来源序列可为完整日、周或年，整体作为重复基线且不另设周期字段；装配前不做重采样、插值、聚合或融合，全周期计算序列只在计算阶段生成并统一对齐；
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

## 声明式配置与 YAML 边界

人工编写、导入导出、进入快照的声明式配置统一使用 YAML：`device` / `assembly` / `finance`（`FinanceProfile`、`FinanceOverrides`、`EffectiveFinanceConfig`）/ `planning` / `calculation` 与项目包内配置均为 `.yaml`，按安全子集解析、规范化与确定性摘要生成，不保留 `finance_config.json` 别名或 JSON/YAML 双格式兼容。HTTP JSON DTO、数据库内部行/列存储、求解器专用输入和大结果可继续使用对应域的原生格式，不强制转 YAML。

## 人工编写与规范化

人工文件可以保留注释和友好顺序。进入快照前，校验器必须生成唯一规范形态：

1. 解析安全子集并拒绝重复键、非法标量和未知核心字段；
2. 解析设备内容摘要、规范数据与预定义来源引用、规划配置、有效财务快照引用与财务绑定（`finance_binding`/`tariff_bindings`），确认技术方程与业务输入完整；
3. 核对项目计算基线、各输入来源的分辨率、连续 `step` 和对应模式的覆盖要求，将路径资源解析为内容寻址对象；
4. 按格式规定排序并移除注释、别名和非语义空白；
5. 生成规范字节与外部资源引用；
6. 生成包含校验器 ID、版本、依赖锁和零阻断诊断的校验回执。

财务三件套的规范化遵循[财务 YAML 契约](formats/finance-yaml.md)：解析 YAML（安全子集）→ 校验 → 规范化。`FinanceOverrides` 引用 `profile {id}`，`EffectiveFinanceConfig` 不携带内容摘要，文本文件按字头校验。

只有“规范装配文本 + 摘要 + 校验回执”共同组成的 `ValidatedAssemblyArtifact` 可以进入生成器。文件被修改后必须重新校验，旧回执不得复用。

人工编辑或模板实例化产生的设备 YAML 在进入项目正式模型目录前也必须通过其完整文件契约。候选模型字节可以送入校验器，但只有校验成功的规范模型、摘要与回执才能原子保存；失败候选不能先落入项目目录再等待异步清理。项目模型保存不接收配套数据文件；数据文件由数据集流程独立保存，并在装配中显式绑定。

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
4. 规范化和摘要是否变化；
5. 是否需要离线迁移器及迁移回执；
6. 手写示例、JSON Schema/CSV schema 与契约测试是否同步。

文件格式属于公共设计。实现内部重构不得顺带改变它；确需不兼容修改时，必须先有 ADR、迁移规则和版本升级。
