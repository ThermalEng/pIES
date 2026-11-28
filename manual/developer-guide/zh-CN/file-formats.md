# 文件格式标准

> 文档状态：生效契约；规范版本：`1.2.0`；上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)

pIES 的核心业务文档与离线交换包使用独立、可验证的公共格式，字段级定义以 `formats/` 下对应权威章节为准。

## 公共文件契约总览

| 格式契约 | 推荐文件名 / 后缀 | 职责 | 主要消费者 |
|---|---|---|---|
| [设备模型 YAML](formats/device-model-yaml.md) | `*.device.yaml` | 设备身份、非时变技术参数、五类序列接口与声明式方程 | 设备目录、前端表单 Schema、装配与方程解析 |
| [设备数据 CSV](formats/device-data-csv.md) | `*.data.csv` | 带连续 `step`、分辨率与单位的设备时序输入 | 数据导入、装配绑定核对、序列物化 |
| [价格序列 CSV](formats/price-series-csv.md) | `*.price.csv` | 能源全期或日内价格曲线 | 数据导入、任务提交核对、价格物化 |
| [财务 YAML 契约](formats/finance-yaml.md) | `finance_profile.yaml` / `finance_overrides.yaml` / `effective_finance.yaml` | 地区财务基准、项目稀疏覆盖与确定性合并快照 | 财务合并器、装配校验、生成与评价 |
| [装配 YAML](formats/assembly-yaml.md) | `*.assembly.yaml` | 项目基线、设备实例、来源绑定、连接拓扑、财务绑定与规划配置 | 装配校验器 |
| [网络边界与系统方程](formats/network-relations.md) | 装配内的 `system_equations` | 跨实例分配、辅助技术变量与未连接端口关闭语义 | 装配校验、公共技术贡献、生成器 |
| [命名金额、支付与资产](formats/finance-relations.md) | 财务三件套内扩展节 | 命名成本明细、通用金额定义、收付发生、资产折旧与派生税 | 财务合并器、生成器、结果适配 |
| [Solver Bundle](formats/solver-bundle.md) | `bundle.yaml` + 输入输出目录 | 求解器输入文件、结构化命令、输出清单与结果映射 | 求解运行时、结果适配器 |
| [计算配置](formats/calculation-config.md) | 计算配置映射 | 计算模式、预测配置、生成器/求解器版本、精度、时限与输出请求 | 提交校验、生成器 |
| [项目包](formats/project-package.md) | `*.zip`（根含 `manifest.json`） | 项目工作区、历史版本与依赖的离线导出导入 | 项目包导入导出器 |
| [算法插件包](formats/algorithm-plugin-package.md) | `plugin.yaml` + `*.algorithm.zip` | 统一计算环节（预测/组装/求解/评价/一体化算法）与插件包契约（`1.2.0` 生效） | `backend` 调度器、`job_runner` 与临时计算容器 |

**可人工编写与不可人工编写的界线**：设备模型 YAML、数据 CSV、`FinanceProfile`、`FinanceOverrides` 与装配 YAML 支持人工编写；`EffectiveFinanceConfig` 必须由确定性合并器生成（导入时从来源重新合并验证）；Solver Bundle 只能由生成器产出。

## 从手写文件到计算结果

```text
FinanceProfile（地区基准） + FinanceOverrides（项目覆盖）
        │
        ▼ 确定性合并
EffectiveFinanceConfig（不可变快照）
        │ 精确引用
        ▼
设备 YAML + 数据 CSV + 装配 YAML ──→ ValidatedAssemblyArtifact
                                              │
                     计算配置 + 评价确认 ─────┼→ 序列物化 → GeneratorProvider → Solver Bundle
                                                                                      │
                                                ComputeResult ← ExecutionReceipt ← SolverRuntime
```

装配 YAML 聚焦系统结构、绑定与规划意图，不包含计算配置选项（生成器、求解器、精度等）或进程/路径信息。

## 通用书写规则与 YAML 安全子集

所有公开文本格式共同遵守以下规则：

- **编码与换行**：UTF-8 编码，规范输出采用 LF 换行。
- **命名与标识**：扩展 ID 使用 ASCII 小写命名空间字符串（如 `ies.device.pv`）；局部标识使用 `lower_snake_case` 或短横线并保持文件内一致。
- **版本号**：`schema_version`、插件版本与依赖版本采用带引号的 `MAJOR.MINOR.PATCH`；单个设备模型不设语义版本。
- **数值与金额**：禁止 `NaN`、`Infinity` 或依赖语言实现的特殊标量；财务金额使用 `{value, unit}` 十进制定点字符串原子。
- **时间与单位**：计算与数据序列统一使用从 `0` 开始连续递增的 `step` 与显式 `resolution`，不携带时区；单位必须来自公共单位词汇。
- **路径与安全边界**：文件内路径必须为相对路径，禁止绝对路径、`..`、符号链接逃逸、宿主机路径、密钥凭证及实现模块路径。
- **严格校验**：未知核心字段默认拒绝；扩展字段须放在命名空间化的 `extensions` 下且不得修改核心语义。

YAML 统一使用 **YAML 1.2 安全子集**：两空格缩进，禁止 Tab、自定义 tag、anchor、alias、合并键与重复键。

### 单一当前契约与项目包依赖

产品 2.0 正式发布前，自有文件契约标记统一固定为 `1.0.0`：

- 开发迭代直接同步更新当前 Schema、生产者、消费者、样例与测试，不维护多版本兼容矩阵、旧字头分发或运行期自动迁移。
- 历史冻结快照与已封存结果按原记录保留，不强制用当前程序重新求解历史格式。
- 项目包可移植依赖要求：
  - 每个数据集版本在包内拥有独立元数据与文件目录；
  - 包须自包含当前草稿及包内历史版本所需的精确财务三件套、规划配置与项目模型规范文本，携带集合须与实际引用集合完全一致；
  - 确认导入时通过正式保存路径在目标库重建新身份并重映射全部内部引用，不复用源库数据库 ID 或修订号。

## 前端表单与规范化

1. **统一入口**：对于以文件契约为权威输入的配置（如设备模型、财务文档、规划完整文档），前端结构化表单提交前必须序列化为完整文档并送入同一后端正式解析入口，避免前后端各自维护第二套解析逻辑。
2. **唯一规范形态**：进入装配产物前，校验器执行安全解析、依赖解析、稳定键排序与注释剥离，形成唯一规范字节与校验回执；两者共同构成进入生成器的唯一凭证 `ValidatedAssemblyArtifact`。

## 插件与离线交付最小集

交付一套新的设备技术内容时，应提供可独立通过校验的最小闭环：

1. `*.device.yaml` 设备技术定义；
2. 最小合法与典型 `*.data.csv` 样例；
3. 引用该设备的最小 `*.assembly.yaml`；
4. 正向及常见非法样例的契约测试与可复核计算基线。
