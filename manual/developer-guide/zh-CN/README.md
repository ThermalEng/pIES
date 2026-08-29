# pIES 开发者指南

本指南面向维护者、集成开发者、扩展开发者和贡献者。它不仅规定边界，也解释每个部分为什么存在、接收什么、产出什么，以及怎样在不破坏解耦的前提下继续开发。

开始一个局部开发任务时，先阅读架构宪法，再进入对应的模块手册。模块手册与该模块的公开代码和测试共同构成开发入口；一般不需要回到历史 Review 或旧规格寻找上下文。

## 规范与蓝图

1. [架构宪法](ARCHITECTURE_CONSTITUTION.md)：最高原则与冲突裁决；
2. [系统架构蓝图](architecture.md)：系统上下文、端到端流程、模块职责和依赖方向；
3. [文件格式标准](file-formats.md)：可手写的设备模型 YAML、数据 CSV、装配 YAML，以及生成器产出的 Solver Bundle；
4. [模块开发手册](module-development.md)：按模块选择开发入口、统一阅读方法和完成标准；
5. [领域模型与追溯链](domain-model.md)：项目、数据、配置、任务、结果和对象生命周期；
6. [公共契约](contracts.md)：HTTP、数据类型、单位、时间、诊断和异步语义；
7. [扩展体系](extensions.md)：设备技术内容、方程转换、生成器、运行时、存储和数据 provider；
8. [模型与算法](customization-center.md)：外部模型/插件的文件交付、版本、共享、安装和选择规则；
9. [前端与帮助中心](frontend.md)：前端输入输出、状态、GUI 开发流程与正式文档；
10. [部署与运行](deployment.md)：Docker 部署、安全、健康、备份和恢复；
11. [开发、测试与贡献](development.md)：变更流程、测试层次和完成标准；
12. [版本化与发布](versioning-and-release.md)：三段式版本、变更日志和发布门禁。

## 按任务进入

| 要开发的内容 | 首先阅读 |
|---|---|
| provider 发现、依赖注入或启动就绪 | [组合根与启动](modules/bootstrap.md) |
| 通用类型、诊断、单位或时间轴 | [Core 基础能力](modules/core.md) |
| 新设备或设备描述 | [设备目录](modules/devices.md) |
| 手写或校验设备模型 YAML | [设备模型 YAML](formats/device-model-yaml.md) |
| 手写或校验设备时序 CSV | [设备数据 CSV](formats/device-data-csv.md) |
| 设备方程如何形成声明式数学贡献 | [技术方程建模](modules/modeling.md) |
| 手写装配、项目图校验或规范产物 | [装配 YAML](formats/assembly-yaml.md)与[装配与检查](modules/assembly.md) |
| 从规范装配生成求解器输入和命令 | [计算生成器](modules/generators.md) |
| 安全执行求解器命令 | [求解运行时](modules/solver-runtime.md) |
| 计算全链路或结果适配 | [计算生成与求解](modules/engines.md) |
| 财务指标 | [财务计算](modules/finance.md) |
| 敏感性、批量或结果评估 | [结果分析](modules/analysis.md) |
| 对象、引用、清理或存储适配器 | [对象存储](modules/storage.md) |
| 跨模块业务流程 | [应用用例](modules/application.md) |
| HTTP 路由与 DTO | [API 适配](modules/api.md) |
| 后台任务、租约与结果提交 | [Worker](modules/worker.md) |
| repository、数据库约束或 migration | [持久化](modules/persistence.md) |
| 页面、表单、状态或帮助中心 | [前端与帮助中心](frontend.md) |
| 用户自定义模型、算法插件、共享安装或选择 | [模型与算法](customization-center.md) |

## 使用原则

- 架构宪法规定“为什么不能越界”；模块手册说明“这个部分怎样工作和怎样开发”。
- 稳定公共设计写在本指南；已经实现但未发布的变化写入更新日志 `Unreleased`，尚未实现的顺序写入 Roadmap，两者不重复。
- Review 只提供证据和历史上下文，不能成为实现的第二套权威说明。
- 文档标记为“生效目标契约”表示后续实现必须以此为目标，不代表当前代码已经兼容；当前成熟度以更新日志和 Roadmap 为准。

返回[正式文档入口](../../README.md)或[项目入口](../../../README.md)。
