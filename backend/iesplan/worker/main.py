"""计算与 I/O Worker 入口：领取任务、发布心跳和监督任务进程。"""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import os
import signal
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any

from iesplan.application import worker as worker_app
from iesplan.config import settings
from iesplan.worker import runner

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 5.0
POLL_INTERVAL = 1.0


class Worker:
    """后台任务 Worker 主循环；计算 Worker 需组合根装配执行面后启动。"""

    def __init__(
        self,
        worker_type: str = "compute",
        worker_id: str | None = None,
        *,
        session_factory: Any,
        compute: Any = None,
        poll_interval: float = POLL_INTERVAL,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
    ) -> None:
        if worker_type not in ("compute", "io"):
            raise ValueError(f"非法 worker_type: {worker_type!r}(可选 compute/io)")
        if session_factory is None:
            raise TypeError("Worker 缺少必需的 session_factory(由组合根装配显式传入)")
        if worker_type == "compute" and compute is None:
            raise TypeError("计算 Worker 缺少必需的执行面 compute(由组合根装配显式传入)")
        self.worker_type = worker_type
        self.pool = "compute" if worker_type == "compute" else "io"
        self.compute = compute
        self.worker_id = worker_id or self._default_worker_id()
        # 会话工厂只能由 bootstrap 装配的 ApplicationContext 显式提供(main 传入
        # ctx.session_factory); 无缺省、无回退, 缺失即构造失败。
        self.session_factory = session_factory
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self._stop = threading.Event()
        self._process_context = multiprocessing.get_context("spawn")
        self._task_process: Any = None
        self._cancel_event: Any = None
        self._fence_event: Any = None
        self._current_task_id: int | None = None
        self._claim: worker_app.Claim | None = None
        self._resource_start: Any = None
        self._last_heartbeat = 0.0

    # -- 生命周期 ---------------------------------------------------------

    def run(self) -> None:
        """主循环: 心跳与领取执行, 直至收到停止信号。"""
        self._install_signal_handlers()
        logger.info("Worker 启动: id=%s type=%s pool=%s pid=%s",
                    self.worker_id, self.worker_type, self.pool, os.getpid())
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - 单次 tick 异常不影响进程存活
                logger.exception("Worker tick 异常")
            time.sleep(self.poll_interval)
        # 停止领取，等待已领取任务完成。
        while self._task_process is not None:
            try:
                self._tick()
            except Exception:
                logger.exception("退出前收拢任务失败")
            time.sleep(self.poll_interval)
        self._clear_heartbeat()
        logger.info("Worker 退出: %s", self.worker_id)

    def stop(self) -> None:
        """请求优雅退出(信号处理回调)。"""
        self._stop.set()

    def _tick(self) -> None:
        """单轮: 心跳、进程收拢与空闲时领取。"""
        now = time.monotonic()
        if now - self._last_heartbeat >= self.heartbeat_interval:
            self._heartbeat()
            self._last_heartbeat = now
            self._check_task_control()
        if self._task_process is not None:
            if self._task_process.is_alive():
                return
            self._reap_task_process()
            return
        # 槽门禁(03 §5.2 第 1 步): 无空槽则等待
        if not self._slot_gate():
            return
        task_id = worker_app.dequeue_task(self.pool)
        if task_id is None:
            with self.session_factory() as db:
                task_id = worker_app.find_queued_task(db, self.pool)
            if task_id is None:
                return
        self._claim_and_run(task_id)

    # -- 子流程 ------------------------------------------------------------

    def _heartbeat(self) -> None:
        """Worker 心跳(Redis, 可重建视图; 03 §7.1: 间隔 5 s, TTL 15 s)。"""
        payload = {
            "worker_id": self.worker_id,
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "pool": self.pool,
            "alive_at": datetime.now(UTC).isoformat(),
            "running_attempts": (
                [{"task_id": self._current_task_id}] if self._current_task_id else []
            ),
            "load": {"cpu": None, "mem_mb": None},
        }
        worker_app.publish_heartbeat(self.worker_id, payload, ttl=int(self.heartbeat_interval * 3))

    def _clear_heartbeat(self) -> None:
        """退出时清除心跳(供管理界面判定失联)。"""
        worker_app.publish_heartbeat(self.worker_id, {}, ttl=1)

    def _check_task_control(self) -> None:
        if self._claim is None or not self._task_process.is_alive():
            return
        with self.session_factory() as db:
            reason = worker_app.attempt_stop_reason(db, self._claim)
        if reason == "fenced":
            self._fence_event.set()
        elif reason == "cancelled":
            self._cancel_event.set()

    def _slot_gate(self) -> bool:
        """槽门禁(03 §5.2): 池内存在可用槽才尝试领取。"""
        with self.session_factory() as db:
            available = worker_app.slot_available(db, self.pool)
        if not available:
            time.sleep(self.poll_interval)  # 无空槽: 保持排队, 等待下一轮
        return available

    def _claim_and_run(self, task_id: int) -> None:
        """领取(槽 + 尝试 + 租约 + token)并在独立解释器中执行。

        领取事务由 application.worker 用例提交, 返回后租约/尝试状态即刻
        可见; 本层不调用 commit/rollback。
        """
        with self.session_factory() as db:
            claim = worker_app.acquire_attempt(db, task_id, self.worker_id)
        if claim is None:
            logger.info("领取失败(无槽/非 queued): task=%s", task_id)
            return
        self._current_task_id = task_id
        self._claim = claim
        self._resource_start = worker_app.observe_local_resources()
        self._cancel_event = self._process_context.Event()
        self._fence_event = self._process_context.Event()
        self._task_process = self._process_context.Process(
            target=run_claimed_task,
            args=(self.worker_type, self.worker_id, claim, self._fence_event, self._cancel_event),
            name=f"task-{task_id}",
        )
        try:
            self._task_process.start()
        except Exception as exc:
            self._task_process.close()
            self._task_process = None
            self._cancel_event = None
            self._fence_event = None
            self._current_task_id = None
            self._claim = None
            self._resource_start = None
            with self.session_factory() as db:
                worker_app.fail_attempt(
                    db, claim, code="TASK-EXEC-001", message=f"任务进程启动失败：{exc}",
                )
            raise
        logger.info("任务领取: task=%s attempt=%s worker=%s", task_id, claim.attempt_id, self.worker_id)

    def _reap_task_process(self) -> None:
        if self._task_process is None:
            return
        self._task_process.join()
        if self._task_process.exitcode:
            logger.error("任务进程退出: task=%s exit_code=%s",
                         self._current_task_id, self._task_process.exitcode)
            if self._claim is not None:
                with self.session_factory() as db:
                    worker_app.report_task_process_exit(
                        db, self._claim, exit_code=self._task_process.exitcode,
                        before=self._resource_start,
                        after=worker_app.observe_local_resources(),
                    )
        self._task_process.close()
        self._task_process = None
        self._cancel_event = None
        self._fence_event = None
        self._current_task_id = None
        self._claim = None
        self._resource_start = None

    # -- 工具 ----------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        """SIGTERM/SIGINT → 优雅退出(仅主线程可注册信号; 嵌入线程场景跳过)。"""
        if threading.current_thread() is not threading.main_thread():
            return
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, _signum: int, _frame: Any) -> None:  # noqa: ANN001
        self.stop()

    @staticmethod
    def _default_worker_id() -> str:
        """默认 Worker id: <类型前缀>-<主机名>-<pid>。"""
        prefix = "cw"  # compute worker
        return f"{prefix}-{socket.gethostname()}-{os.getpid()}"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def run_claimed_task(
    worker_type: str, worker_id: str, claim: worker_app.Claim,
    stop_event: Any, cancel_event: Any,
) -> None:
    """任务进程入口：在组合根重新装配，不继承连接、provider 对象或大文件。

    进程间只传递领取身份及停止事件；输入、阶段检查点和终态仍通过原有
    application 公开门面与短事务交接。该进程只执行已领取任务。
    """
    from iesplan.bootstrap import assemble_task_runtime

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ctx = assemble_task_runtime(worker_type)
    compute = ctx.compute_execution_plane() if worker_type == "compute" else None
    runner.run_task(
        ctx.session_factory, claim, worker_id=worker_id,
        stop_event=stop_event, cancel_event=cancel_event, compute=compute,
    )


def main(argv: list[str] | None = None) -> int:
    """Worker 进程入口(python -m iesplan.worker.main)。

    全部运行时能力经 bootstrap 组合根装配: 按 worker_type 选择
    assemble_compute_worker()/assemble_io_worker(), Daemon 的会话工厂、
    计算执行面与能力取自装配好的 ApplicationContext。计算执行面缺失时
    assemble_compute_worker 直接抛异常，Daemon 不启动、不领取任务。
    """
    parser = argparse.ArgumentParser(description="pIES 计算/I/O Worker")
    parser.add_argument(
        "--worker-type", choices=("compute", "io"), default=None,
        help="Worker 类型(缺省取 IESPLAN_WORKER_TYPE 环境变量或配置)",
    )
    parser.add_argument("--worker-id", default=None, help="Worker 标识(缺省自动生成)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker_type = args.worker_type or os.environ.get("IESPLAN_WORKER_TYPE") or settings.worker_type
    if worker_type not in ("compute", "io"):
        raise ValueError(f"非法 worker_type: {worker_type!r}(可选 compute/io)")
    from iesplan.bootstrap import assemble_compute_worker, assemble_io_worker

    assemble = assemble_compute_worker if worker_type == "compute" else assemble_io_worker
    ctx = assemble()
    compute = ctx.compute_execution_plane() if worker_type == "compute" else None
    Worker(
        worker_type=worker_type,
        worker_id=args.worker_id,
        session_factory=ctx.session_factory,
        compute=compute,
    ).run()
    return 0


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main(sys.argv[1:]))
