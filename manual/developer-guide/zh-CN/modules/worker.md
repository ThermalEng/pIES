# Worker（异步调度与临时容器编排）

> 代码边界：`backend/iesplan/worker/`；上位规范：[架构宪法](../ARCHITECTURE_CONSTITUTION.md)

`worker`（作为轻量化单进程异步调度层收敛于 `backend` 控制面）负责长时后台作业（全周期规划计算、敏感性/批量分析与导出/导入）的队列调度与状态机推进，基于唯一任务所有权（`TaskAttempt` 状态）确保单次有效执行与完整证据可追溯。

## 职责与边界

- **拥有**：待排队任务领取与唯一 `TaskAttempt` 所有权管理；按环节向容器代理 `job_runner` 下发断网临时计算容器启停指令并传递进程内取消/超时信号；计算任务各阶段编排（预测 → 组装 → 求解 → 极简子进程结果还原与后期财务评价 / 一体化算法）与已持久化阶段检查点。
- **不拥有**：修改项目草稿或回读未冻结配置；在控制面进程内直接执行求解或加载用户插件源码；拼接求解器 Shell 命令或解释装配设备语义；直接持有宿主机 Docker Socket 或绕过 `application.worker` 短事务命令操作数据库连接。
- **统一计算容器边界**：内置与第三方计算环节统一按 `ies.algorithm-plugin@1.0.0`（`/work/in → /work/out`）契约通过 `job_runner` 拉起独立断网临时容器执行，控制面自身永不解压或 `import` 用户源码。

## 执行状态机与容器隔离

```text
数据库待排队扫描 / 进程内唤醒
  ↓ 领取任务（分配唯一 TaskAttempt）
backend 单进程异步调度控制面（进程内槽位与停止信号；不持有 Docker Socket）
  │
  ├─ [阶段 1] prediction：经 job_runner 拉起 predictor 临时容器 ──→ 提交 prediction 检查点
  ├─ [阶段 2] generated：经 job_runner 拉起 generator 临时容器 ──→ 提交 generated 检查点
  ├─ [阶段 3] execution：经 job_runner 拉起 solver 临时容器 ──→ 提交 execution 检查点后清理临时区
  └─ [阶段 4] evaluation：经 job_runner 拉起 evaluator 临时容器（极简子进程还原 + 并联评价）
       │（一体化算法以单个 algorithm 容器覆盖阶段 2–3 后衔接评价；内部循环算法在单容器内自闭环）
       └─ 发布 Evidence、查询投影与任务终态
```

- **控制面与计算面彻底分离**：每个计算步骤在由 `job_runner` 启动的独立断网临时 Docker 容器中运行，控制面负责进程内有界线程池槽位调度、固定五项业务接口启停与结果持久化；`backend` 内部划分 HTTP 请求线程与后台调度、大文件 IO 及镜像构建线程，确保请求线程不卡死。
- **先落盘后清理与启动期收口**：临时容器结束后，先采集退出状态、容器限额/OOM 状态并持久化声明输出与检查点，再调用 `cleanup_stage` 清理作业目录（清理失败仅记告警，不污染业务结论）。`backend` 启动时一次性清理残留临时计算容器并将中断的在途 `TaskAttempt` 标为失败（不支持跨重启容器热接管）。
- **取消、退出与资源耗尽处置**：用户主动取消与部署超时由控制面通知 `job_runner` 立即终止临时容器，收集退出状态与资源观测后清理容器并释放槽位。明确检出 `MemoryError` 或容器 `State.OOMKilled` 的终止收口为资源耗尽失败并释放槽位，不自动在相同资源下重跑。

## 阶段检查点与恢复

- **阶段检查点（`ies.worker-checkpoint@1.0.0`）**：`prediction`、`generated` 与 `execution` 产物按阶段经 `storage` 持久化。发生中断并由用户重试时，可从已提交的有效检查点或原冻结快照重新执行（不支持恢复求解器内部搜索树）；检查点对象缺失或不可读时直接报错，不静默退回草稿。
- **单机取消与重试**：任务卡住时通过正常取消接口结束并重新提交或重跑，不再维护独立租约表、Fencing Token 或管理员解锁重排状态机。
- **轻量化结果索引**：证据发布同事务建立可重建的 `evidence_projection` 索引，供结果页摘要、财务面板与分窗逐时读取使用；必要时可通过 `docker compose exec backend python -m iesplan.cli.index_result --evidence-package-id <id>` 为历史大证据补建索引。

## 新增任务类型规范

1. 明确任务输入快照、结果契约、可选幂等键语义与资源池归属（`compute` 或 `io`）；
2. 在 `application.worker` 注册短事务状态推进与阶段执行命令；
3. 为长时步骤接入进程内取消与超时信号；
4. 在 Docker 环境下补齐正常提交、取消、超时与检查点执行测试。
