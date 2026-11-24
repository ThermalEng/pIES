# Worker（异步调度与临时容器编排）

> 代码边界：`backend/iesplan/worker/`；上位规范：[架构宪法](../ARCHITECTURE_CONSTITUTION.md)

`worker`（在 `1.1.5` 目标架构下作为轻量化异步调度层收敛于 `backend` 控制面）负责长时后台作业（全周期规划计算、敏感性/批量分析、结果检查与大型导出/导入）的队列调度与状态机推进，确保任务在并发领取、进程/容器中断、超时、取消及迟到写入等场景下依然保持单次有效提交与完整证据可追溯。

## 职责与边界

- **拥有**：任务队列发现与原子领取；Attempt 租约持有、Fencing Token 校验与管理员解锁恢复；按环节向容器代理 `job_runner` 下发断网临时计算容器启停指令并传递停止信号；计算任务各阶段编排（预测 → 组装 → 求解 → 结果适配与后期财务评价 / 一体化算法）与阶段检查点恢复。
- **不拥有**：修改项目草稿或回读未冻结配置；在控制面进程内直接执行求解或加载用户插件源码；拼接求解器 Shell 命令或解释装配设备语义；直接持有宿主机 Docker Socket 或绕过 `application.worker` 短事务命令操作数据库连接。
- **统一计算容器边界（`1.1.5` 目标）**：内置与第三方计算环节统一按 `ies.algorithm-plugin@1.0.0`（`/work/in → /work/out`）契约通过 `job_runner` 拉起独立断网临时容器执行，控制面自身永不解压或 `import` 用户源码。

## 执行状态机与容器隔离

```text
队列提示 / 数据库待排队扫描
  ↓ 原子领取（分配 Attempt + Lease + Fencing Token）
backend 异步调度控制面（发布心跳、停止监听与阶段推进；不持有 Docker Socket）
  │
  ├─ [阶段 1] prediction：经 job_runner 拉起 predictor 临时容器 ──→ 提交 prediction 检查点
  ├─ [阶段 2] generated：经 job_runner 拉起 generator 临时容器 ──→ 提交 generated 检查点
  ├─ [阶段 3] execution：经 job_runner 拉起 solver 临时容器 ──→ 提交 execution 检查点后销毁临时区
  └─ [阶段 4] evaluation：经 job_runner 拉起 evaluator 临时容器（适配 + 后期财务计算与评价）
       │（极简一体化算法以单个 algorithm 容器覆盖阶段 2–3 后衔接后期评价；内部循环算法在单容器内自闭环）
       ↓ 核验租约仍有效（Fencing）
       └─ 原子发布 Evidence、查询投影与任务终态
```

- **控制面与计算面彻底分离**：每个计算步骤在由 `job_runner` 启动的独立断网临时 Docker 容器中运行，控制面负责领取任务、发布心跳、通过固定五项业务接口下发启停指令与回收状态；`backend` 内部严格划分 HTTP 请求线程与后台调度、大文件 IO 及镜像构建线程/子进程并分别做并发限流，API 请求线程绝不承担长时 IO 或阻塞任务。
- **先落盘后清理与重启恢复**：临时容器结束后，必须先采集状态、资源指标并持久化声明输出与检查点，再调用 `cleanup_stage` 清理作业目录。`backend` 或 `job_runner` 重启后，直接基于既有 `TaskAttempt`、`Fencing Token`、阶段检查点与容器标签识别并收拢遗留容器，**绝不新建第二套任务状态机，也不重复启动同一阶段**；心跳缺失不改变任务领取状态和槽位，控制面短时不可用时已启动的临时计算容器继续执行，数据库恢复后凭原 Token 续交状态。
- **取消、退出与资源耗尽处置**：用户主动取消、管理员撤销与部署超时通过独立停止通道传递并由 `job_runner` 立即终止销毁临时容器；容器异常退出时监督逻辑记录退出码、最后阶段及观察到的 `/work/out/iterations.jsonl`（若有）并收拢尝试与槽位。明确检出 `MemoryError` 或临时容器 cgroup OOM 计数上升的终止收口为资源耗尽失败并释放槽位，不自动在相同资源下机械重跑。任务 detail 中展示的 `worker_resources` 反映执行容器的 cgroup v2 观测事实，不作为单任务无限配额承诺。

## 阶段检查点与恢复

- **阶段检查点（`ies.worker-checkpoint@1.0.0`）**：`prediction`、`generated` 与 `execution` 产物按阶段经 `storage` 持久化并登记尝试 Owner 引用（写盘期间不持有租约行锁，写入元数据与引用前再锁定核对写入资格）。发生中断并重试后，新建 Attempt 凭新 Token 从已提交检查点恢复：已有 `execution` 产物则直接恢复结果适配，已有 `generated` 产物则跳过构模重跑求解，缺少检查点时按原快照重新执行（不支持恢复求解器内部搜索树）。检查点对象实际丢失或不可读时直接报错，不静默重算或退回草稿。
- **管理员解锁与恢复**：整机停止等导致活动尝试遗留占槽时，由管理员在短事务内解锁以撤销旧 Token、结束旧尝试并释放槽位，随后按原快照重新排队；每次新领取产生新 Attempt 与新 Token，旧 Token 迟到写回一律拒绝。
- **轻量化结果索引**：证据发布同事务建立可重建的 `evidence_projection` 索引，供结果页摘要、财务面板与分窗逐时读取使用；升级后可通过运维命令 `docker compose exec backend python -m iesplan.cli.index_result --evidence-package-id <id>` 为历史大证据补建索引。

## 新增任务类型规范

1. 明确任务输入快照、结果契约、幂等键作用域与资源池归属（`compute` 或 `io`）；
2. 在 `application.worker` 注册短事务状态推进与阶段执行命令；
3. 为长时步骤接入取消与租约 Fencing 检查点；
4. 在 Docker 环境下补齐正常提交、重复消息、取消、超时、进程退出、控制面中断与重试恢复测试。
