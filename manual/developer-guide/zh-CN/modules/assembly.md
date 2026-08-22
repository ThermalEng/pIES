# 装配与检查

> 文档状态：生效目标蓝图；代码边界：`backend/iesplan/assembly/`；文件契约：[装配 YAML](../formats/assembly-yaml.md)。

## 作用

`assembly` 是从可编辑业务状态进入计算世界的唯一同步闸门。它把项目设备、真实端口、连接、数据绑定、约束和计算选择整理为标准装配 YAML，证明这份输入完整、引用稳定且具备所选计算能力，然后签发不可变 `ValidatedAssemblyArtifact`。

装配的核心思想是“先证明这是什么、是否可以计算，再交给生成器决定怎样写求解器文件”。它不是可选的 GUI 预检查，也不应提前混入某个 solver 的对象和命令。

## 边界

模块负责：

- `ies.assembly` schema、解析、规范化和版本兼容；
- 把公开项目图导出为可手写、可审查的装配 YAML；
- 校验结构、引用、端口方向、载能、量纲、单位、数据、命令和整体可解性；
- 校验 calculation mode、GeneratorProvider、solver 和选项能力；
- 把相对资源解析成内容寻址引用，生成稳定规范装配文本；
- 签发摘要、依赖锁和校验回执。

模块不读取 ORM、用户会话、任务队列和 BlobStore 私有路径，不生成 MPS/LP 等求解器文件，不生成或执行命令，不选择未在装配中固定的 solver，也不修改项目草稿。

## 输入

装配有两个等价公开入口，最终进入同一 `AssemblySpec`：

| 输入方式 | 来源 | 进入条件 |
|---|---|---|
| 项目导出 | application/project | 设备、连接、数据和配置使用稳定 revision |
| 手写装配 YAML | 插件包、离线交换或开发样例 | 遵守 `ies.assembly` 安全格式 |

校验还需要显式注入：

| 依赖 | 所有者 | 进入条件 |
|---|---|---|
| 设备目录快照 | devices | model ID/精确版本及 descriptor 摘要完整 |
| 命令目录快照 | modeling | 所需 command ID/精确版本可解析 |
| 数据版本/资源 | dataset/storage | schema、摘要、时间轴、字段和质量明确 |
| 生成器与 solver descriptor | computation | mode、选项、命令和 executor 能力可查询 |

所有输入是公开 contract 或不可变快照，不是数据库行、前端表单状态和运行目录。

## 输出

成功输出 `ValidatedAssemblyArtifact`，由三部分共同组成：

1. 规范装配文本：schema `ies.assembly`、版本、UTC 时间、内容资源和稳定顺序；
2. `assembly_sha256`：规范字节的 SHA-256；
3. 校验回执：校验器 ID/版本、规范化算法版本、依赖锁、资源摘要和零阻断诊断。

产物保留明确业务单位。它保证单位和量纲兼容，但不转换为某个生成器的内部单位。下游 [GeneratorProvider](generators.md) 必须核验三件套，不能只接受裸 `dict` 或旧项目形状。

失败时输出有顺序、可定位的诊断集合，不产生部分 artifact、临时计划或可被 Worker 执行的对象。

## 四阶段开发思路

### 1. 结构阶段

解析 YAML 安全子集、schema、ID、必需字段、判别联合、精确版本和引用形状。重复键、未知核心字段、危险路径和可执行字段在这里拒绝。

### 2. 模型与数据阶段

核对设备模型、建模命令、参数、状态、数据列、单位、时间覆盖和资源摘要。设备端口和数据列完全来自公开 descriptor，装配不能发明。

### 3. 图与系统阶段

校验输出到输入、载能和量纲兼容、自环、重复边、悬空端口、网络连通、关键负荷供给、平衡和系统约束。

### 4. 计算兼容阶段

确认 calculation mode、GeneratorProvider、solver、选项、随机种子和输出请求能够协作。只查询 descriptor，不调用 generator，也不启动 solver。

只有当前结构足以可靠支撑下一阶段时才继续；同一阶段应尽可能聚合多个可修复问题。四阶段通过后，解析资源、规范化并签发产物。

## 规范化

规范化是公开纯过程：

- 文件级本地时间按明确偏移转换为 UTC `Z`；
- `relative_file` 解析为内容 ID、媒体类型和摘要；
- 映射和无业务顺序集合按稳定键排序；
- 数值使用唯一有限表示；
- 注释、YAML 表示差异和显示空白不进入语义摘要；
- 设备、命令、generator、solver 和所有资源形成完整依赖锁。

相同语义必须产生相同规范文本和摘要。规范化算法变化必须版本化；历史 artifact 不能用当前算法悄悄重算后覆盖。

## 增加装配字段或规则

1. 确认字段的唯一所有者；若属于设备模型、CSV、命令或 generator 选项，先修改其权威 contract；
2. 说明字段的类型、单位、空值、顺序、默认和版本兼容；
3. 判断新规则属于结构、模型与数据、图与系统还是计算兼容阶段；
4. 分配稳定诊断码、严重度、位置和修复提示键；
5. 更新装配 schema、手写示例、规范化和摘要测试；
6. 增加合法、单点非法、多点非法和边界拓扑测试；
7. 若核心字段语义不兼容，先走 ADR、升 schema MAJOR 并提供迁移器。

不要用装配字段暴露 generator 的工作路径、solver 参数字符串或实现模块名。确需 solver 选项时，进入 `calculation.options` 并由 GeneratorDescriptor schema 管理。

## 失败语义

| 问题 | 结果 |
|---|---|
| schema、重复键、引用形状或版本错误 | 结构诊断，阻断 |
| 设备、命令、参数、数据或单位不完整 | 模型/数据诊断，阻断 |
| 端口方向、载能、拓扑或系统约束非法 | 图/系统诊断，阻断 |
| generator/solver/mode/options 不匹配 | 计算兼容诊断，阻断 |
| 资源摘要不符或路径逃逸 | 完整性/安全诊断，阻断 |
| 非关键质量建议 | warning/info，写入回执后可继续 |

禁止在缺端口时创建默认双向端口、删除非法边后继续、用零填充缺失数据、把未知 generator 换成内置实现，或在异常后构造 `_direct_plan`。

## 必须遵循的规范

- 边连接真实端口，不连接设备抽象类型；
- 损耗、延迟和非同时性通过明确设备或约束建模；
- 业务单位在 artifact 中明确，装配只证明兼容，不执行 solver 内部换算；
- 装配 YAML 不含 shell、executable、函数路径、环境变量、凭证或宿主机路径；
- 所有设备、命令、generator、solver、schema 和资源版本进入依赖锁；
- 参与摘要的顺序和序列化唯一；
- artifact 不包含 GUI、HTTP、ORM 和存储 provider 私有形状；
- 只有 `ValidatedAssemblyArtifact` 可以进入 generator。

## 与生成器的交接

装配器承诺“业务输入有效且规范”；生成器承诺“从该规范输入确定地产生 Solver Bundle”。二者之间不共享可变 builder、solver 对象或进程上下文。

生成器入口必须重新核验 assembly 摘要和回执，但不得重新解释原始 YAML、访问最新项目或补做本应由装配阶段完成的业务校验。这样开发 assembly 时只需理解文件 schema 与业务规则，开发 generator 时只需理解规范 artifact 与代码。

## 完成标准

- 手写装配和项目导出进入同一 schema/校验路径；
- 四阶段都有独立、组合和稳定诊断测试；
- 非法输入没有任何旁路进入 generator；
- 相同语义输入产生相同规范文本、摘要和依赖锁；
- 新设备只要满足设备模型和建模命令契约即可装配，不增加类型分支；
- 装配模块测试不生成求解器文件、不运行命令。

代码阅读从 assembly 公开门面、`AssemblySpec`、诊断和 `ValidatedAssemblyArtifact` contract 开始，再按 parser、validator、canonicalizer、receipt signer 的数据流阅读；文件字段以[装配 YAML 标准](../formats/assembly-yaml.md)为准。
