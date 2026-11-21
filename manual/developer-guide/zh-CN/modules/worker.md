# Worker

> 代码边界：`backend/iesplan/worker/`；上位规范：[架构宪法](../ARCHITECTURE_CONSTITUTION.md)

`worker` 是执行长时后台作业（全周期规划计算、敏感性/批量分析、项目包与大型报表导出）的常驻守护进程，确保任务在并发领取、进程中断、超时、取消及迟到写入等场景下依然保持单次有效提交与完整证据可追溯。

## 职责与边界

- **拥有**：任务队列发现与原子领取；Attempt 租约持有、续租与 Fencing Token 校验；隔离任务子进程控制与停止信号传递；计算任务各阶段编排（物化 → 生成 → 运行 → 适配 → 财务评价）与阶段检查点恢复；过期租约回收。
- **不拥有**：修改项目草稿或回读未冻结配置；拼接求解器 Shell 命令或解释装配设备语义；直接操作数据库连接提交事务（所有持久化读写均经 `application.worker` 短事务命令完成）。
- **算法插件载荷边界（`1.1.0` 目标）**：用户算法插件通过通用沙箱 Provider 转交独立容器 `plugin_runner` 执行，Worker 进程本身不解压或 `import` 用户源码。

## 执行状态机与进程隔离

```text
队列提示 / 数据库待排队扫描
  ↓ 原子领取（分配 Attempt + Lease + Fencing Token）
控制进程（负责心跳、续租、到期回收与停止监听）
  │
  └─ 启动独立任务子进程（经组合根装配依赖，无锁加载 CalcSnapshot）
       ↓ [阶段 1] prediction：序列预测物化 ──→ 提交 prediction 检查点
       ↓ [阶段 2] generated：GeneratorProvider 生成 Bundle ──→ 提交 generated 检查点
       ↓ [阶段 3] execution：SolverRuntime 隔离执行 ──→ 提交 execution 检查点后清理临时区
       ↓ [阶段 4] ResultAdapter 适配 + 财务评价
       ↓ 核验租约仍有效（Fencing）
       └─ 原子发布 Evidence、查询投影与任务终态
```

- **控制面与执行面分离**：每个 Attempt 在独立解释器子进程中运行，防止长时构模或后处理停顿阻塞控制进程的心跳续租与停止检测。
- **取消与资源耗尽处置**：用户主动取消、租约丢失与部署超时分别通过独立停止通道传递；明确检出 `MemoryError` 或伴随 cgroup OOM 计数上升的进程终止收口为资源耗尽失败并释放槽位，不自动在相同资源下机械重跑。任务 detail 中展示的 `worker_resources` 反映当前部署容器的 cgroup v2 观测事实，不作为单任务独享配额承诺。

## 阶段检查点与过期恢复

- **阶段检查点（`ies.worker-checkpoint@1.0.0`）**：`prediction`、`generated` 与 `execution` 产物按阶段经 `storage` 持久化并登记尝试 Owner 引用。发生故障后新建的 Attempt 凭新租约继续：已有 `execution` 产物则直接恢复结果适配，已有 `generated` 产物则跳过构模重跑求解，缺少检查点时按原快照重新执行（不支持恢复求解器内部搜索树）。检查点对象实际丢失时直接报错，不静默重算或退回草稿。
- **过期租约回收**：Worker 在槽位准入前定期回收数据库中已到期且仍占槽的活动租约，同一短事务内将旧 Attempt 置为 `stopped`、释放槽位并按剩余重试次数将任务重新入队（已请求取消的任务收拢为 `cancelled`），确保旧 Token 永不迟到写回。
- **轻量化结果索引**：证据发布同事务建立可重建的 `evidence_projection` 索引，供结果页摘要、财务面板与分窗逐时读取使用；升级后可通过运维命令 `docker compose exec backend python -m iesplan.cli.index_result --evidence-package-id <id>` 为历史大证据补建索引。

## 新增任务类型规范

1. 明确任务输入快照、结果契约、幂等键作用域与资源池归属（`compute` 或 `io`）；
2. 在 `application.worker` 注册短事务状态推进与阶段执行命令；
3. 为长时步骤接入取消与租约 Fencing 检查点；
4. 在 Docker 环境下补齐正常提交、重复消息、取消、超时、进程强杀及租约过期并发恢复测试。
