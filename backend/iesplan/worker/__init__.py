"""计算 Worker 与 I/O Worker 共用框架(03-task-scheduling.md)。

包含:
- solver_process.py 隔离求解器子进程封装(资源限制/超时/取消/孤儿清理);
- executors.py     各任务类型执行函数(结果检查与 I/O 占位; 计算经阶段网关);
- runner.py        任务执行分派(快照输入装配 → 阶段网关 → 证据包/评估/结果索引);
- main.py          Worker 入口(队列订阅、心跳续租、槽门禁、信号处理)。

租约/提交/收拢直接消费 ``iesplan.application.worker`` 阶段网关,
本层不设转发模块。
"""

from __future__ import annotations

__all__ = ["runner", "executors", "solver_process", "main"]
