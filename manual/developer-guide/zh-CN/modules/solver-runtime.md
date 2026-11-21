# 求解运行时

> 代码边界：`backend/iesplan/computation/runtime/`；交接格式：[Solver Bundle](../formats/solver-bundle.md)

`SolverRuntime` 负责在受控隔离环境中校验并执行 Solver Bundle 声明的结构化命令，采集原始输出、进程状态与资源统计，生成不可变的 `ExecutionReceipt`。

## 职责与边界

- **负责**：Bundle manifest、相对路径与命令策略校验；`ExecutorProvider` 发现与受信任 `executable` 解析；独立工作目录建立、分块输入写入与受控命令执行；取消、超时与管理员撤销（fencing）响应及进程组回收；声明输出检查、日志限长与 `ExecutionReceipt` 签发。
- **不负责**：解析装配 YAML 或设备语义、构造数学模型、选择求解器、隐式重试、解释数学最优性或裁决任务终态。

## 执行与隔离流水线

```text
接收 Solver Bundle + AttemptContext
  ↓ schema / manifest / 路径越界校验
  ↓ ExecutorProvider allowlist 与部署策略校验
创建独立工作目录并按块写入 Bundle 输入
  ↓ 以参数数组启动受控进程（禁止 shell）
监控超时 / 用户取消 / 租约 fencing 信号
  ↓ 等待回收完整进程组
核对声明输出与限长采集 stdout/stderr
  ↓ 签发 ExecutionReceipt 并持久化阶段输出后清理临时目录
```

- **执行前阻断**：任何 manifest、路径或策略校验失败均不启动外部进程，直接返回拒收状态回执（`input_rejected` / `policy_rejected`）。
- **分块资产交接**：遵循[Bundle 分块内容协议](../formats/solver-bundle.md#分块内容与生命周期)，输入按块流式写入隔离目录，输出经持久化接收接口提交执行检查点并确认成功后再清理临时目录；接收失败时保留未交付产物并明确报错。

## 命令与安全规则

- **无 Shell 执行**：`executable` 必须命中部署 allowlist 中的受信任 ID，`arguments` 以字符串参数数组逐项传递给操作系统进程 API，禁止 `sh -c`、管道、重定向与命令替换。
- **路径与网络隔离**：工作目录及声明输入/输出约束在隔离目录内；默认 `network: false`，运行身份无业务数据库、对象存储凭证与宿主机代码区权限；每次尝试使用独立工作目录，进程组完全收拢后才交接输出。

## 取消、超时、资源与输出边界

- **中止与回收**：收到取消、超时或管理员撤销（fencing）信号时先发送受控终止信号，宽限后强制终止完整进程组并回收子进程；取消、超时、OOM 与进程崩溃分别记录对应的回执终止状态，迟到退出不改写为成功。
- **声明输出交接**：按清单交接 manifest `outputs` 声明的相对路径文件，在工作目录中按块读取并交给对象存储；正常退出却缺少 `required: true` 输出时判为 `output_protocol_failed`，进程退出码为 `0` 不直接等同于数学求解有解。
- **本地开发与测试**：可通过 `ExecutorProvider` 契约隔离测试安全拒收、进程中止与输出采集；真实求解器集成测试在 Docker 容器环境执行。
