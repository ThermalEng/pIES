# 系统架构与模块边界

> 状态：生效目标架构；当前代码正在迁移
> 上位规范：[架构宪法](ARCHITECTURE_CONSTITUTION.md)

IES Plan 的核心流程为：设备定义 → 建模命令 → 装配与检查 → 计算 → 财务和结果分析。存储、任务、身份和审计提供基础能力，application 层负责跨模块用例编排，API 和 Worker 是外部适配器。

模块只通过公开门面、Protocol、不可变 contract 或事件交互。禁止跨模块调用私有函数、读取 registry 字典、导入 loader/repository/persistence 或查询对方 ORM。

## 核心模块

| 模块 | 权威职责 | 不负责 |
|---|---|---|
| `core` | 无状态错误、诊断、单位、时间和纯类型 | 注册表、业务默认值、数据库 |
| `devices` | 设备规格、端口、能力和 provider 注册 | 建模执行、项目实例、UI |
| `modeling` | 标准命令与建模 provider | 扫描设备内部目录 |
| `assembly` | 边—端装配、同步检查、计算输入转换 | fallback 直接 plan |
| `engines` | 纯计算与求解器适配 | HTTP、ORM、单位猜测 |
| `finance` | 现金流和财务指标 | UI、数据库编排 |
| `analysis` | 敏感性、批量和评估 | 任务租约和 HTTP |
| `storage` | 内容寻址、完整性、引用和 provider | 理解项目、证据等业务表 |
| `application` | 权限后的用例、事务和跨模块编排 | 穿透模块内部实现 |
| `api` | HTTP DTO 与状态码 | ORM 和领域计算 |
| `worker` | 租约、执行、重试和结果提交 | 缺命令时降级执行 |

## 开放扩展

设备、建模、装配和存储分别拥有模块内注册状态，不建立跨模块全局注册表。组合根选择 provider 并验证公开 ID/版本；候选注册表完整构建后一次性发布。正式发布前不实现运行期热加载。

当前迁移差距和实施顺序记录在根目录 `docs/`，不属于本指南的稳定外部契约。
