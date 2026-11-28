# 求解运行时

> 代码边界：`backend/iesplan/computation/runtime/`；交接格式：[Solver Bundle](../formats/solver-bundle.md)

`SolverRuntime` 负责在受控临时容器中执行阶段计算命令，采集原始输出、进程状态与容器资源状态，生成不可变的 `ExecutionReceipt`。

## 职责与边界

- **负责**：Bundle manifest 与声明输出相对路径边界校验；经 `job_runner` 代理以断网临时容器执行计算命令；取消与超时响应；声明输出检查、日志限长与 `ExecutionReceipt` 签发。
- **不负责**：解析装配 YAML 或设备语义、构造数学模型、选择求解器、隐式重试、解释数学最优性或裁决任务终态。

## 执行与隔离流水线

```text
接收阶段输入 / Solver Bundle + AttemptContext
  ↓ schema / manifest / 声明路径边界校验
经 job_runner 启动断网临时计算容器（只读挂载 /work/in）
  ↓ 监控超时 / 用户取消信号
  ↓ 收集退出状态、容器限额与 OOM 状态、声明输出并限长采集 stdout/stderr
  ↓ 签发 ExecutionReceipt 并持久化阶段输出后清理临时目录
```

- **执行前阻断**：任何 manifest 或路径校验失败均不启动容器，直接返回拒收状态回执（`input_rejected` / `policy_rejected`）。
- **分块资产交接**：遵循[Bundle 分块内容协议](../formats/solver-bundle.md#分块内容与生命周期)，输出经持久化接收接口提交执行检查点并确认成功后再清理临时目录；接收失败时保留未交付产物并明确报错。

## 命令与安全规则

- **无 Shell 执行**：执行入口与参数以受控列表方式传递，禁止拼接不可信 Shell 字符串与 `..` 路径逃逸。
- **路径与网络隔离**：默认 `--network=none`，仅按步骤只读挂载 `/work/in` 与可写挂载 `/work/out`，计算容器无数据库、Redis、对象存储凭证与宿主机代码目录权限。

## 取消、超时、资源与输出边界

- **中止与回收**：收到取消或超时信号时通知 `job_runner` 立即终止临时容器并清理；取消、超时、OOM 与进程崩溃分别记录对应的回执终止状态，迟到退出不改写为成功。
- **声明输出交接**：按清单交接 manifest `outputs` 声明的相对路径文件；正常退出却缺少 `required: true` 输出时判为 `output_protocol_failed`，进程退出码为 `0` 不直接等同于数学求解有解。
- **临时容器代理衔接**：在全步骤临时计算容器流水线中，调度器通过统一契约调用 `job_runner` 固定业务操作（`start_stage`、`inspect_stage`、`stop_stage`、`cleanup_stage`），以 `/work/in`（只读）与 `/work/out`（可写）交接资产；在**先采集容器退出状态、OOM/限额状态并持久化声明输出后**，调用 `cleanup_stage` 清理作业目录（清理失败仅记告警）；控制面启动时一次性清理遗留临时容器。
- **本地开发与测试**：真实求解器与临时容器集成测试统一在 Docker 容器环境执行。
