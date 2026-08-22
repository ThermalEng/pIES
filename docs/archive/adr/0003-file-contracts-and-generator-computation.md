# ADR-0003：文件契约与生成器式计算边界

> 状态：已批准、文档已实施、代码待 Roadmap 迁移、已归档  
> 决策日期：2026-08-22  
> 批准依据：项目所有者明确要求  
> 修订条款：架构宪法第 3、4、5、6、7、12、13、14、15、16、19 节

## 问题

现有设备 YAML、数据 CSV 和装配对象尚未形成适合外部插件与人工编写的统一公共契约；计算边界同时承担装配、问题构造、求解执行和结果解释，具体实现入口或求解器细节可能穿透到设备描述和 Worker。这样新增插件需要理解多个内部目录，也难以独立验证求解命令的安全与可复现性。

## 决策

1. 建立四种独立版本化文件契约：`ies.device-model`、`ies.device-data`、`ies.assembly`、`ies.solver-bundle`，首个目标 schema 均为 `1.0.0`。
2. 设备模型 YAML、数据 CSV 和装配 YAML 必须可由开发者按正式指南直接手写并经过统一校验；当前文件仅作为迁移输入，不自动宣称兼容。
3. 装配成功输出“规范装配文本 + SHA-256 + 校验回执”的 `ValidatedAssemblyArtifact`，不再输出面向具体求解器的 `ComputePlan`。
4. 计算拆为 GeneratorProvider、SolverRuntime 和 ResultAdapter：生成器把规范装配转成求解器输入与结构化命令；运行时只执行；结果适配器只解释声明输出。
5. 业务单位在装配阶段验证，在 GeneratorProvider 边界转换到求解器内部单位；反向换算由 ResultAdapter 完成。
6. 装配 YAML 禁止命令、脚本、可执行路径和实现模块路径。Solver Bundle 的命令使用受信任 executor/executable ID、参数数组、相对工作目录、环境 allowlist 和硬资源/网络策略，禁止 shell 字符串。
7. 生成器是确定性纯转换，不访问数据库、对象服务、网络或环境，不启动求解器。运行时不理解项目、设备和装配业务。

## 影响

- 目标代码边界由单一 `engines/` 收敛为 `computation/generators`、`computation/runtime` 和 `computation/result_adapters`；现有代码按 Roadmap 迁移。
- 任务快照增加规范装配、资源、generator、solver、executor 和 result adapter 的精确版本；attempt 证据增加 Solver Bundle 与 ExecutionReceipt。
- 插件可以围绕稳定文件与 provider contract 独立开发；通用 runtime 不为每种设备或 solver 增加业务分支。
- 文件 schema、provider 版本和产品版本独立演进，不兼容变化必须版本化并提供迁移。

## 被否决方案

- 在现有设备 YAML 中继续保存 Python `package/entry`：把公共设备规格绑定到实现目录并扩大动态执行面。
- 在装配 YAML 中允许 shell/solver 命令：让用户输入直接跨越业务校验与执行安全边界。
- 每个算法自行创建 subprocess：重复实现超时、取消、资源限制、日志和路径防护。
- runtime 直接读取装配并判断 solver 类型：会重新耦合业务语义、问题构造和执行。
- 继续以内部 `dict`/`ComputePlan` 作为长期插件格式：无法提供稳定手写、规范化和内容摘要契约。

## 迁移与回滚

Roadmap 的 `0.2.0` 先实现 schema、校验器、规范 artifact 和 provider contract，再引入 generator/runtime/result adapter，最后删除旧函数路径和直连引擎入口。迁移期只允许在明确边界把旧输入一次性转换到新契约，不能双写或静默 fallback。

若某个 solver 迁移失败，可以暂缓该 solver provider 的发布，但不得回滚公共安全边界、恢复 shell 字符串或让未校验装配进入计算。本 ADR 的稳定规则已进入架构宪法和开发者指南，后续以两者为权威。
