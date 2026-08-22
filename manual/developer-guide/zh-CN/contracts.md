# 公共契约

> 文档状态：生效蓝图；规范版本：1.0.0；上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)

本章定义跨进程、跨模块和扩展可依赖的稳定语义。具体 HTTP 路径与字段表由运行版本的 OpenAPI/schema 生成，不在手册中维护第二份易漂移副本。

## JSON 与数据类型

- UTF-8、`application/json`；
- 字段统一使用 `snake_case`；
- 枚举值使用小写 `snake_case`；
- 对外实体 ID 作为不透明十进制字符串传输，不能做算术；
- 稳定扩展 ID 使用命名空间字符串；
- 精确金额和费率结算值使用十进制定点字符串；
- 连续物理量使用 JSON number，拒绝 `NaN` 和无穷值；
- HTTP、事件、数据库和规范快照中的时间戳使用带 `Z` 的 ISO 8601 UTC；人工 CSV 可按[设备数据 CSV](formats/device-data-csv.md)使用显式文件级固定偏移，规范化后仍为 UTC；
- 长期结构必须声明 `schema_version`。

一个字段只能有一种未标记形态。缺失、空值和加载失败必须区分，不能用默认值或空数组掩盖错误。

## 成功与错误

资源成功响应只有一种外层形态：

```json
{"data": {}, "meta": {}}
```

列表在 `data` 中返回数组，分页、版本和追踪信息放在 `meta`。无正文成功响应使用正确 HTTP 语义，不返回伪造 JSON。

错误使用结构化诊断：

```json
{
  "error": {
    "code": "DOMAIN-CATEGORY-001",
    "message_key": "ies.error.example",
    "severity": "error",
    "blocking": true,
    "params": {},
    "location": null,
    "fix_hint_key": null,
    "ref_ids": []
  }
}
```

`code` 用于稳定分类，`message_key + params` 用于本地化，`location` 用于定位资源或字段。客户端不得接收堆栈、SQL、宿主机路径或凭证。

## HTTP 语义

- GET 只读且可重试；
- POST 创建资源或执行命令；
- PUT 完整替换，PATCH 显式部分更新；
- DELETE 执行明确生命周期操作；
- 创建使用 201，异步接受使用 202，无正文使用 204；
- 认证、授权、不存在、冲突和校验失败使用一致的标准状态码；
- 可重试写操作使用作用域明确的幂等键；
- 并发编辑使用 revision、ETag 或等价乐观锁。

上传必须声明媒体类型、大小和摘要约束。下载返回受控资源或短期授权，客户端不拼接存储路径。

## 单位边界

系统区分：

1. GUI 展示单位；
2. HTTP/业务契约的规范单位；
3. 生成器/求解器内部单位。

前端 mapper 完成表单字符串、百分比和简单展示换算；后端校验单位、量纲、范围和领域规则；装配阶段证明单位兼容并保留明确业务单位，业务单位到求解器内部单位只在 GeneratorProvider 边界发生，结果反向换算只在 ResultAdapter 边界发生。建模命令、运行时和业务服务中不得散落隐式换算常量。

## 时间与集合

项目时区用固定 UTC 偏移分钟数表达。时间序列必须声明起点、分辨率、点数和单位，数组长度与时间轴一致。

列表是否有序必须显式。参与哈希的集合按稳定键规范化；JSON 字段顺序没有业务语义。大型逐时数组通过字段、时间范围或对象引用读取，不无条件嵌入普通 DTO。

## 快照与异步契约

长时间计算、分析、导入和报告生成使用任务。任务请求携带幂等信息，消费不可变快照，并以不可变证据或对象提交结果。

Worker 只有在命令可解析、租约有效且写入资格仍成立时才能提交进度和结果。失败保留结构化诊断；取消、超时和重试遵守公开状态转换。

## 兼容与演进

产品正式发布前，错误的旧字段、响应并集和静默 fallback 应一次性删除。正式发布后：

- 兼容新增按次版本演进；
- 缺陷修复按修订版本演进；
- 不兼容公开契约变化提升主版本，并提供迁移说明；
- schema、文件格式、provider 和产品版本各自拥有独立版本，不能机械同步。

详细规则见[版本化与发布](versioning-and-release.md)。

## 公共文件契约

插件和离线交换使用四种版本化契约：设备模型 YAML、设备数据 CSV、装配 YAML 和 Solver Bundle，完整标准见[文件格式标准](file-formats.md)。

- 前三种允许人工编写，但必须先经过对应 schema 校验和规范化；
- 装配成功产出 `ValidatedAssemblyArtifact`，不能把原始 YAML `dict` 直接交给计算；
- GeneratorProvider 只从规范装配生成 Solver Bundle；
- SolverRuntime 只按 Bundle 的结构化命令执行，不解释业务字段；
- 文件中禁止实现模块路径、shell 字符串、宿主机绝对路径和凭证。

四种 schema 独立版本化。不能识别的 MAJOR 必须拒绝，不得以“尽可能解析”方式继续任务。

## 设计一个新 contract

不要从现有 ORM 或页面表单直接复制字段。按下面顺序设计：

1. 写出调用方要完成的动作和接收方承诺的结果；
2. 为 contract 指定唯一所有者和版本；
3. 区分命令、查询、领域结果、应用结果和 HTTP DTO；
4. 对每个字段说明类型、单位、空值、顺序和稳定性；
5. 定义成功、业务失败、冲突和内部失败；
6. 决定幂等、revision、分页或异步语义；
7. 用前后端/模块协议测试证明关键值完整往返；
8. 再从权威 schema 生成具体字段文档。

如果同一字段需要“旧字符串或新对象都能用”，应先完成调用方迁移，再一次性采用唯一形态，而不是把并集写进正式 contract。

## Contract 在各层的变化

| 边界 | 输入 | 输出 | 不应跨越的内容 |
|---|---|---|---|
| GUI ↔ feature | FormState | FrontendModel | 原始 HTTP 与业务数据库概念 |
| feature ↔ API | Request/Response Contract | JSON/文件 | React 状态和显示单位 |
| API ↔ application | Actor + Command | ApplicationResult | Cookie、Response、ORM |
| application ↔ domain | 领域命令/值对象 | 领域结果/诊断 | 跨模块私有实现 |
| assembly ↔ generator | `ValidatedAssemblyArtifact` + 内容资源 | Solver Bundle | 原始 YAML、项目草稿、ORM、存储路径 |
| generator ↔ runtime | Solver Bundle | `ExecutionReceipt` + 原始输出 | 设备私有实现、数据库、shell 字符串 |
| runtime ↔ result adapter | 只读 Bundle、回执、声明输出 | `ComputeResult` | 进程句柄、当前项目、隐式重试 |
| Worker ↔ results | attempt + evidence contract | 写入回执 | 无 fencing 的裸结果 |

同一个业务概念可以在不同边界有不同类型，但转换必须存在于明确 mapper 中，不能依靠字段名碰巧相同。

## 示例：保存计算配置

前端 FormState 可保存百分数文本和未完成输入；提交 mapper 把它变为规范比例与明确枚举的请求 DTO。API 只校验传输结构并形成应用命令；application 校验权限与 revision；配置领域校验目标、变量和算法兼容性；成功后返回新的配置修订和诊断摘要。任一失败都保持原表单，并通过标准错误或诊断回到对应字段。

这个流程中不存在“后端替前端除以 100”、路由直接写 ORM、页面接受两种响应形状或配置失败后返回旧数据。

## 完成标准

- contract 的所有者、调用方和版本明确；
- 输入输出、单位、空值、失败和并发语义完整；
- 具体字段由 schema 生成，不在多份手册重复；
- 关键 ID、金额、单位、枚举和错误参数有跨边界往返测试；
- 旧形态已迁移删除，没有静默 fallback。
