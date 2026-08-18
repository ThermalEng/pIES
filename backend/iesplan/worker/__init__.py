"""计算 Worker 与 I/O Worker 共用框架(03-task-scheduling.md)。

包含:
- lease.py         租约与 fencing token 协议(领取/续租/提交/释放/迟到拒绝);
- solver_process.py 隔离求解器子进程封装(资源限制/超时/取消/孤儿清理);
- executors.py     各任务类型执行函数(调用引擎与指标, 进度/取消检查点);
- runner.py        任务执行分派(快照输入装配 → 执行器 → 证据包/评估/结果索引);
- main.py          Worker 入口(队列订阅、心跳续租、槽门禁、信号处理)。
"""

from __future__ import annotations

__all__ = ["lease", "runner", "executors", "solver_process", "main"]
