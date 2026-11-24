# 应用用例

> 代码边界：`backend/iesplan/application/`；上位规范：[架构宪法](../ARCHITECTURE_CONSTITUTION.md)

`application` 编排跨模块的完整用户业务动作，持有数据库事务边界、业务级授权检查与跨资源恢复逻辑，同时为 `api`（同步请求）与 `worker`（后台任务分阶段命令）提供与传输协议无关的公开用例入口。

## 职责与边界

- **拥有**：接收 `ActorContext` 与用例命令；校验业务权限、乐观锁修订（`expected_revision`）与幂等键；协调多个领域公开门面完成业务操作；持有数据库 `commit`/`rollback` 边界；协调对象引用、任务创建与不可变审计日志。
- **不拥有**：设备方程或财务数学计算、底层 ORM 表定义跨模块导出、HTTP Cookie/Response 细节、以及长时计算任务自身的执行状态机。
- **Worker 分阶段接口（`application.worker`）**：长时任务不能把整个求解过程包裹进单一长数据库事务。`application.worker` 向 Worker 守护进程暴露任务领取、进度上报、阶段检查点提交、写入资格核对、管理员解锁及调用计算/分析领域能力的短事务公开命令；文件流式扫描与耗时计算期间不占用数据库连接。

## 用例执行标准模型

```text
接收 ActorContext + Command
  ↓ 加载目标资源并执行后端授权校验
  ↓ 核对乐观锁 revision / 幂等键 / 生命周期状态
  ↓ 调用相关领域模块公开门面（devices / modeling / assembly / finance / storage）
  ↓ 协调持久化、ObjectOwner 引用与脱敏审计
提交数据库事务并返回 ApplicationResult
```

Repository 仅执行 `flush` 或局部 `savepoint`，严禁下层覆盖调用方全局事务；同一用例应可同时被 HTTP 路由与后台任务/CLI 复用。

## 主要用例族

| 用例族 | 职责摘要 |
|---|---|
| `identity` | 用户认证、多会话管理与空闲续期、改密、角色与管理员账号管理 |
| `projects` | 项目创建、草稿读写、手动固化/恢复版本、归档与删除确认 |
| `datasets` | 数据集及价格 CSV 上传校验、质量诊断、版本管理与项目绑定 |
| `modeling` | 项目设备实例、参数覆盖、连接拓扑、模型候选校验保存与原子编号分配 |
| `planning` / `finance` / `calculation` | 规划配置、财务三件套合并与有效快照引用、计算配置版本化修订管理 |
| `validation` / `tasks` / `results` | 全项目装配校验、快照冻结与幂等任务创建、结果投影与方案应用 |
| `exports` / `packages` | Excel 报表生成、离线项目包导出与导入提案两阶段确认 |
| `operations` | 系统服务健康聚合、存储容量查看与管理端审计查询 |
| `customizations`（`1.2.0` 目标） | 本地模型/插件管理及 pIES Market（本地/联网双模式）生命周期编排 |

## 关键跨模块约束

- **项目模型保存一致性**：候选模型字节交由 `devices` 解析并校验；同一事务内原子分配递增不复用后缀并保存规范 YAML、校验回执与目录清单，校验失败则整事务回滚且不占用正式编号。
- **计算提交与队列可靠性**：提交用例在事务内完成装配检查、冻结 `CalcSnapshot`、创建幂等 `Task` 与建立快照对象引用；事务提交后再发布队列消息，队列消息丢失时 Worker 仍可从数据库权威任务表重新发现。
- **跨存储中断收敛**：数据库与对象存储之间的已发生中断使用显式操作 ID 实现幂等重试，不静默忽略或事后全盘扫描其他无关对象。
