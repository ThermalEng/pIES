# pIES 开发者指南

本指南面向项目维护者、扩展开发者与贡献者，说明系统架构边界、公共数据契约、文件格式标准、模块职责及部署验收方式。

进行具体设计与实现前，应先阅读[架构宪法](ARCHITECTURE_CONSTITUTION.md)了解全局约束，再按需查阅对应格式标准或模块文档；模块文档与对应模块的公开接口及测试共同构成开发依据。

## 指南目录

1. [架构宪法](ARCHITECTURE_CONSTITUTION.md)：全局强制原则与冲突裁决顺序。
2. [系统架构蓝图](architecture.md)：系统分层、端到端业务链、模块边界与依赖方向。
3. [文件格式标准](file-formats.md)：设备 YAML、数据 CSV、财务 YAML、装配 YAML、计算配置、Solver Bundle 与项目包规范。
4. [模块开发手册](module-development.md)：后端各业务模块的职责总览与跨模块协作规范。
5. [领域模型与追溯链](domain-model.md)：草稿、版本、快照、任务与证据的生命周期及不可变追溯关系。
6. [公共契约](contracts.md)：HTTP/JSON 规范、错误信封、单位与时间基准、异步任务与结果读取契约。
7. [扩展体系](extensions.md)：设备内容、方程能力、计算 Provider 与存储适配器的扩展机制。
8. [模型与算法](customization-center.md)：用户自定义设备模型、算法插件包与共享目录设计（规划中）。
9. [前端与帮助中心](frontend.md)：前端分层架构、状态管理、GUI 与后端契约边界、帮助中心机制。
10. [部署与运行](deployment.md)：Docker 容器化部署、统一配置、Worker 并发与求解隔离、健康排查及备份恢复。
11. [规范索引与文档边界](normative-index.md)：各文档权威归属与维护约定。

## 按开发任务索引

| 开发任务 | 对应参考文档 |
|---|---|
| 启动装配、Provider 注册与就绪检查 | [组合根与启动](modules/bootstrap.md) |
| 公共基础类型、诊断码、单位换算与时间轴 | [Core 基础能力](modules/core.md) |
| 新增内置设备或维护设备描述目录 | [设备目录](modules/devices.md)、[设备模型 YAML](formats/device-model-yaml.md) |
| 编写或校验设备/价格时序 CSV | [设备数据 CSV](formats/device-data-csv.md)、[价格序列 CSV](formats/price-series-csv.md) |
| 受限技术方程解析与数学贡献转换 | [技术方程建模](modules/modeling.md) |
| 装配图校验、端口网络边界与系统方程 | [装配与检查](modules/assembly.md)、[装配 YAML](formats/assembly-yaml.md)、[网络边界与系统方程](formats/network-relations.md) |
| 财务三件套、命名金额、税务规则与项目评价 | [财务计算](modules/finance.md)、[财务 YAML](formats/finance-yaml.md)、[命名金额、支付与资产](formats/finance-relations.md) |
| 评估范围、冻结基准与年度维护来源 | [公共契约](contracts.md#评价范围与冻结基准)、[年度维护金额来源](contracts.md#年度维护金额来源) |
| 数学问题构建与 Solver Bundle 生成 | [计算生成器](modules/generators.md)、[Solver Bundle](formats/solver-bundle.md) |
| 隔离执行求解命令与回执采集 | [求解运行时](modules/solver-runtime.md) |
| 计算全链路、预测算法与结果适配 | [计算生成与求解](modules/engines.md) |
| 敏感性、批量扫描与系统四维评估 | [结果分析](modules/analysis.md) |
| 不可变对象存储、引用生命周期与清理恢复 | [对象存储](modules/storage.md) |
| 跨模块业务编排与事务边界 | [应用用例](modules/application.md) |
| HTTP 路由、DTO 映射与错误响应 | [API 适配](modules/api.md) |
| 后台守护进程、租约、检查点与重试 | [Worker](modules/worker.md) |
| 领域表归属、Repository 与数据库迁移 | [持久化](modules/persistence.md) |
| 前端页面、Feature 分层、浏览器验收与帮助中心 | [前端与帮助中心](frontend.md) |
| 自定义模型库、算法插件与共享园地（`1.1.0` 目标） | [模型与算法](customization-center.md) |

返回[正式文档入口](../../README.md)或[项目入口](../../../README.md)。
