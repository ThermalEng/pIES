# 求解运行时

> 文档状态：生效目标蓝图；目标代码边界：`backend/iesplan/computation/runtime/`。

## 作用

SolverRuntime 在受控环境中执行 Solver Bundle 声明的一个命令，并生成可审计回执。它把进程隔离、资源限制、取消和文件完整性集中在一个通用边界，使生成器不需要自行管理 subprocess，Worker 也不需要理解每种 solver 的命令参数。

## 边界

模块负责：

- Solver Bundle 的 schema、摘要、路径和命令策略校验；
- ExecutorProvider protocol、受信任 executable 解析和能力检查；
- 隔离工作目录、最小权限、资源上限与网络策略；
- 单进程启动、取消、超时和终止；
- stdout/stderr、退出状态、资源统计和声明输出采集；
- 不可变 `ExecutionReceipt`。

模块不负责装配 YAML、设备/命令解析、数学问题构造、求解器选择、隐式重试、业务状态映射、财务分析和任务最终状态裁决。

## 输入

| 输入 | 进入条件 |
|---|---|
| Solver Bundle | schema、manifest 和每个输入摘要一致 |
| ExecutorProvider | 精确版本已由组合根装配，支持所声明 executable |
| AttemptContext | attempt ID、租约 fencing、取消信号、证据目标明确 |
| DeploymentPolicy | executable/environment allowlist 和硬资源上限 |

运行时不接受命令字符串、裸参数 dict 或 Python 函数路径。Worker 只能提交完整 Bundle。

## 输出

成功或失败都尽量形成 ExecutionReceipt，记录：

- 实际使用的 Bundle、executor 和 executable 版本；
- 开始/结束时间、退出码、信号和终止原因；
- 请求与实际施加的 CPU、内存、时间、文件和网络策略；
- stdout、stderr、声明输出及其摘要；
- 完整性、策略、取消、超时、OOM 和执行错误状态。

如果进程尚未启动即因 manifest 或策略失败，仍生成“未执行”回执；只有无法建立可信证据时才向 Worker 返回内部基础设施故障。

## 执行流程

```text
接收 Bundle
  ↓ schema / hash / path 校验
  ↓ executor + executable + policy 校验
建立隔离工作目录并复制只读输入
  ↓
以参数数组启动一个受控命令
  ↓ 监控取消、租约、超时与资源
终止并等待进程
  ↓
核对输出边界、计算摘要、封存日志
  ↓
签发 ExecutionReceipt
```

任何执行前校验失败都不得启动进程。进程退出后先封存原始证据，再由 ResultAdapter 解释业务状态。

## ExecutorProvider

ExecutorProvider 把稳定 `executable` ID 映射到部署允许的实际运行方式。descriptor 至少声明：

- executor ID/版本和支持的平台能力；
- executable ID/版本及镜像或二进制的受控解析规则；
- 允许的参数模式、环境变量和媒体类型；
- 可实施的 CPU、内存、超时、文件、进程和网络隔离；
- 取消与强制终止语义；
- readiness 自检和版本证明。

实际绝对路径、容器 runtime 细节和凭证留在 provider 配置中，不写入 Bundle 或任务结果。

## 命令安全

- executable 必须是 allowlist 中的稳定 ID；
- arguments 逐项传给进程 API，不经过 shell；
- 禁止 `sh -c`、管道、重定向、替换、通配和响应文件逃逸；
- 工作目录和所有输入/输出路径必须位于隔离根内；
- 禁止符号链接、设备文件、socket 和未声明文件；
- 环境从空白基线建立，只注入 allowlist 字段；
- `network: false` 为默认，普通 solver provider 必须完全断网；
- 运行身份无业务数据库、对象存储和宿主机工作区权限。

即使 Bundle 来自已注册生成器，运行时仍必须独立校验，不能把 provider 信任替代输入验证。

## 取消、超时和租约

Worker 拥有任务和 attempt 生命周期；runtime 只执行传入上下文：

1. 收到取消或租约失效后，先发受控终止；
2. 在固定宽限期后强制终止整个进程组；
3. 等待进程回收并封存当前输出；
4. 回执标记取消、超时或 fencing，不把迟到退出当成功；
5. 是否重试由 Worker 创建新 attempt 决定。

runtime 不延长业务租约，不在后台留下脱管求解器，也不复用上个 attempt 的可写目录。

## 输出边界

- 只接受 manifest 中声明的文件；
- 输出路径、数量和总大小受限；
- 必需输出缺失即协议失败；
- 未声明输出按策略拒绝并记录，不能悄悄加入结果；
- stdout/stderr 独立限长、脱敏和内容寻址；
- 输出只读封存后才允许 ResultAdapter 访问。

退出码零只说明命令按进程协议结束；业务求解状态必须读取声明输出。

## 增加 ExecutorProvider

1. 定义稳定 executor ID、版本、平台能力和威胁模型；
2. 定义 executable 解析、参数/environment allowlist 和资源限制；
3. 实现隔离启动、取消、强制终止和进程回收；
4. 实现输入只读、输出白名单和路径逃逸防护；
5. 用 fake solver 覆盖成功、非零、超时、取消、OOM 和异常输出；
6. 在 Docker 环境验证实际 solver 版本和隔离能力；
7. 通过组合根原子注册，失败时实例不承接计算任务。

## 失败语义

| 问题 | runtime 状态 |
|---|---|
| Bundle/输入摘要错误 | integrity_failed，未执行 |
| executable 或参数不在 allowlist | policy_rejected，未执行 |
| 无法建立隔离或资源限制 | infrastructure_failed，未执行 |
| 超时/取消/OOM | 对应终止状态，保存回执 |
| 非零退出或信号崩溃 | process_failed，保存日志与已有输出 |
| 输出缺失、越界或超限 | output_protocol_failed |

不得因一个 executor 失败而自动使用宿主机进程或另一个 solver。

## 完成标准

- runtime 单独测试不需要设备目录、装配器和数据库；
- 所有执行前拒绝路径均证明进程没有启动；
- 超时、取消和异常退出后无孤儿进程和可写残留；
- Bundle 外路径、shell 注入、环境泄漏和未声明网络被拒绝；
- 回执足以重建“运行了什么、使用什么限制、产生什么文件”；
- 新 generator 或 solver 不要求修改 runtime 核心分支。

代码阅读从 Bundle/ExecutionReceipt contract 和策略校验器开始，再读 executor protocol、隔离实现、进程生命周期和输出封存；真实 solver 只在 Docker 集成测试中调用。
