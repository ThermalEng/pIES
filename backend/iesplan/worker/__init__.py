"""计算 Worker 与 I/O Worker 共用框架(03-task-scheduling.md)。

包含:
- executors.py     结果检查与 I/O 任务入口;
- runner.py        任务执行分派与状态收拢;
- main.py          Worker 入口(队列订阅、心跳续租、槽门禁、信号处理)。

租约/提交/收拢直接消费 ``iesplan.application.worker`` 阶段网关,
本层不设转发模块。
"""

from __future__ import annotations

__all__ = ["runner", "executors", "main"]
