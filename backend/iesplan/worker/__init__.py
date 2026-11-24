"""嵌入式后台调度器与任务执行框架(ADR-0016)。

包含:
- scheduler.py     收敛于 backend 的轻量级嵌入式调度器与隔离执行池;
- executors.py     结果检查与 I/O 任务入口;
- runner.py        任务执行分派与状态收拢。

租约/提交/收拢直接消费 ``iesplan.application.worker`` 阶段网关,
本层不设转发模块。独立常驻 ``io_worker``/``compute_worker`` 进程已退场。
"""

from __future__ import annotations

from iesplan.worker.scheduler import EmbeddedScheduler

__all__ = ["EmbeddedScheduler", "runner", "executors"]
