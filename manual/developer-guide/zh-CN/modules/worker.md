# Worker

> 文档状态：生效蓝图；代码边界：`backend/iesplan/worker/`

## 作用

`worker` 可靠地执行不能在 HTTP 请求中完成的工作，包括计算、规划、分析、数据处理和导出。它保证一个任务在重复领取、进程退出、超时和迟到提交等情况下仍只有可解释的权威结果。

Worker 负责“可靠执行”，领域模块负责“怎样计算”。

## 边界

模块负责：

- 从可重建队列发现待执行任务；
- 领取 attempt、获得租约和 fencing token；
- 加载并校验不可变快照；
- 对计算任务依次调用已注册 GeneratorProvider、SolverRuntime 和 ResultAdapter；
- 心跳、进度、取消、超时、重试和资源隔离；
- 在租约仍有效时提交证据、结果或明确失败。

模块不修改项目草稿、不补快照缺失输入、不选择最新数据、不实现设备公式，也不绕过 application/result 的写入资格检查。

用户算法插件是特例化执行载荷，不是 Worker 进程插件。普通 Worker 固定插件包与环境摘要，仍依次调用通用沙箱 GeneratorProvider、SolverRuntime/ExecutorProvider 和 ResultAdapterProvider；这些 provider 通过公开内部协议分阶段调用独立 `plugin_runner`。Worker 不得把 ZIP 解压到自身代码目录、加入 `sys.path`、import 入口或向 runner 传递数据库/对象存储凭证。

## 输入

| 输入 | 进入条件 |
|---|---|
| 任务 ID | 数据库中存在可领取的权威任务 |
| attempt 与 fencing token | 由原子领取产生，租约尚有效 |
| `CalcSnapshot` | 摘要、schema、对象和依赖版本完整 |
| provider 与内容目录 | 快照固定的设备内容、方程 contract、generator、executor、solver 与 result adapter 均可解析 |
| 执行策略 | 超时、资源限制、重试和取消检查点明确 |

Redis 队列消息只是定位提示，不能携带覆盖数据库任务事实的第二份状态。

## 输出

- attempt 的开始、心跳、进度和终态；
- 不可变证据或结果对象及摘要；
- 结构化失败诊断；
- 取消、超时、重试和资源统计；
- 与任务、attempt、快照和实际依赖版本的追溯关系。

只有持有当前有效 fencing token 的 attempt 可以提交权威结果。

## 执行流程

```text
队列提示
  ↓ 查询权威任务并原子领取
Attempt + Lease + Fencing Token
  ↓ 校验快照、规范装配与 provider 版本
加载不可变输入
  ↓ generator 产生并封存 Solver Bundle
  ↓ runtime 受控执行，持续检查租约/取消/超时
ExecutionReceipt + 原始输出
  ↓ result adapter 形成候选结果
  ↓ 再次校验租约与写入资格
提交证据和终态
```

心跳和秒级进度可以是可重建状态；attempt、租约资格、任务终态和证据索引必须在权威数据库中。

## 增加任务类型

1. 定义任务命令、快照输入、结果 contract 和幂等作用域；
2. 确定属于 compute 还是 I/O 资源池；
3. 注册公开任务 handler；计算任务只编排 generator/runtime/result adapter，并让 Worker 启动时验证精确版本可解析；
4. 明确取消检查点、超时、资源限制和可重试错误；
5. 通过公开模块门面执行，不在 Worker 复制领域逻辑；
6. 定义证据提交及迟到写入拒绝；
7. 测试正常、取消、超时、进程中断、租约过期、重复消息和重试。

## 失败语义

- 队列重复消息：权威领取保证不会产生两个有效 attempt；
- 设备内容、方程 contract、generator、executor、result adapter 或快照依赖缺失：Worker 不 ready，或任务以明确不可执行诊断失败；
- 租约过期：停止权威写入，迟到结果必须被拒绝；
- 可重试外部故障：新建 attempt，保留旧 attempt 原因；
- 取消或超时：执行隔离层终止工作，状态不可伪装成普通失败；
- 进程崩溃：租约到期后可恢复领取，已提交证据保持不可变。

## 必须遵循的规范

- Worker 只消费不可变快照，不读取当前草稿；
- 队列、心跳和缓存可重建，不是权威事实；
- 结果提交必须校验 task、attempt、token 和状态；
- 重试沿用同一逻辑输入，输入改变必须新建任务；
- 资源隔离、超时和取消不能依赖求解器自觉返回；
- Worker 不解释装配字段、不拼命令字符串、不根据 solver 名称添加分支；
- 每个 attempt 的 Solver Bundle、ExecutionReceipt 和原始输出都进入证据链；
- 诊断和日志不得包含凭证、宿主机路径或敏感原始数据。
- 用户插件 attempt 还必须固定包摘要、依赖锁、runner/runtime 摘要、实际资源限制和隔离执行回执；runner 失联按明确可重试策略处理，不能在普通 Worker 内降级执行。

## 完成标准

- 租约、续租、fencing、重复领取和迟到写入均有并发测试；
- 各任务类型有快照加载与公开命令协议测试；
- 取消、超时、崩溃和重试后状态可解释；
- 重建队列不会重复产生逻辑结果；
- 证据完整追溯到 task、attempt、snapshot 和版本化依赖。

代码阅读从 Worker 入口和任务分派开始，再按 lease、runner、executor、隔离执行与结果提交阅读；对应测试以 Worker lease/runner 测试和任务 API 测试为入口。
