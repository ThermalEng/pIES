"""计算 Worker / I/O Worker 入口(03-task-scheduling.md §1.1/§4/§5/§7)。

main() 职责:
- 按 worker_type(compute|io) 订阅对应队列(Redis 可重建视图; 不可用自动降级
  内存后端, 单进程模式);
- 循环: 槽门禁 → 出队 → 领取(acquire_attempt: 建尝试 + 租约 + fencing token)
  → 执行(runner.run_task, 计算引擎在隔离子进程)→ 提交/失败/取消收拢;
- 心跳(Redis heartbeat:{worker_id}, 5 s)与租约续期(PG, 15 s; 0 行 → 立即
  取消当前任务并停止一切写回, 03 §4.4 自毁契约);
- 信号处理: SIGTERM/SIGINT 优雅退出(置停止事件 → 取消当前任务 → 等待退出)。

并发模型: 单任务线程 + 主循环轮询(心跳/续租/取消); 槽并发由 compute_slots
约束(每池 capacity 行, 03 §5.2), 本进程同一时刻至多执行一个任务(多 Worker
进程由部署编排)。
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
from datetime import UTC, datetime
from typing import Any

from iesplan.config import settings
from iesplan.db import SessionLocal
from iesplan.services import queue
from iesplan.worker import lease, runner

logger = logging.getLogger(__name__)

#: 默认参数(03 附录 A: 心跳 5 s、续租 15 s、租约 TTL 60 s)
HEARTBEAT_INTERVAL = 5.0
RENEW_INTERVAL = 15.0
POLL_INTERVAL = 1.0
#: 优雅退出等待当前任务的最长秒数
SHUTDOWN_GRACE = 30.0


class Worker:
    """Worker 主循环(compute/io 共用框架; 03 §1.1 计算与 I/O Worker 职责)。"""

    def __init__(
        self,
        worker_type: str = "compute",
        worker_id: str | None = None,
        session_factory: Any = None,
        *,
        poll_interval: float = POLL_INTERVAL,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
        renew_interval: float = RENEW_INTERVAL,
        isolate: bool = True,
    ) -> None:
        if worker_type not in ("compute", "io"):
            raise ValueError(f"非法 worker_type: {worker_type!r}(可选 compute/io)")
        self.worker_type = worker_type
        self.pool = "compute" if worker_type == "compute" else "io"
        self.worker_id = worker_id or self._default_worker_id()
        self.session_factory = session_factory or SessionLocal
        self.poll_interval = poll_interval
        self.heartbeat_interval = heartbeat_interval
        self.renew_interval = renew_interval
        self.isolate = isolate
        self._stop = threading.Event()
        self._task_thread: threading.Thread | None = None
        self._cancel_event: threading.Event | None = None
        self._current_task_id: int | None = None
        self._last_heartbeat = 0.0
        self._last_renew = 0.0

    # -- 生命周期 ---------------------------------------------------------

    def run(self) -> None:
        """主循环: 心跳/续租/领取执行, 直至收到停止信号。"""
        self._install_signal_handlers()
        logger.info("Worker 启动: id=%s type=%s pool=%s pid=%s",
                    self.worker_id, self.worker_type, self.pool, os.getpid())
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:  # noqa: BLE001 - 单次 tick 异常不影响进程存活
                logger.exception("Worker tick 异常")
            time.sleep(self.poll_interval)
        # 优雅退出: 取消当前任务并等待其收拢(03 §6.1 子进程 SIGTERM → SIGKILL)
        if self._task_thread is not None and self._task_thread.is_alive():
            logger.info("优雅退出: 等待当前任务收拢(task=%s)", self._current_task_id)
            if self._cancel_event is not None:
                self._cancel_event.set()  # 执行器检查点 + 隔离子进程取消
            self._task_thread.join(timeout=SHUTDOWN_GRACE)
            if self._task_thread.is_alive():
                logger.warning("当前任务未在 %s s 内收拢, 强制退出", SHUTDOWN_GRACE)
        self._clear_heartbeat()
        logger.info("Worker 退出: %s", self.worker_id)

    def stop(self) -> None:
        """请求优雅退出(信号处理回调)。"""
        self._stop.set()

    def _tick(self) -> None:
        """单轮: 心跳 → 续租/取消 → 空闲时领取并执行新任务。"""
        now = time.monotonic()
        if now - self._last_heartbeat >= self.heartbeat_interval:
            self._heartbeat()
            self._last_heartbeat = now
        if self._task_thread is not None:
            if self._task_thread.is_alive():
                if now - self._last_renew >= self.renew_interval:
                    self._renew_or_cancel()
                    self._last_renew = now
                return  # 有任务在执行, 本 tick 不领取
            self._task_thread = None
            self._cancel_event = None
            self._current_task_id = None
            return
        # 槽门禁(03 §5.2 第 1 步): 无空槽则等待
        if not self._slot_gate():
            return
        task_id = queue.dequeue(self.pool)
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
        queue.set_heartbeat(self.worker_id, payload, ttl=int(self.heartbeat_interval * 3))

    def _clear_heartbeat(self) -> None:
        """退出时清除心跳(供管理界面判定失联)。"""
        queue.set_heartbeat(self.worker_id, {}, ttl=1)

    def _renew_or_cancel(self) -> None:
        """续租; 失败(0 行) → 租约失效 → 立即取消当前任务(03 §4.4 自毁契约)。"""
        task_id = self._current_task_id
        if task_id is None or self._cancel_event is None:
            return
        attempt_id = getattr(self, "_attempt_id", None)
        token = getattr(self, "_lease_token", None)
        if attempt_id is None or token is None:
            return
        with self.session_factory() as db:
            if not lease.renew_lease(db, attempt_id, token):
                logger.warning("租约失效, 取消当前任务: task=%s", task_id)
                self._cancel_event.set()  # 执行器检查点抛 TaskCancelled → 收拢

    def _slot_gate(self) -> bool:
        """槽门禁(03 §5.2): 池内存在可用槽才尝试领取。"""
        with self.session_factory() as db:
            available = lease.slot_available(db, self.pool)
        if not available:
            time.sleep(self.poll_interval)  # 无空槽: 保持排队, 等待下一轮
        return available

    def _claim_and_run(self, task_id: int) -> None:
        """领取(槽 + 尝试 + 租约 + token)并在独立线程中执行。"""
        with self.session_factory() as db:
            claim = lease.acquire_attempt(db, task_id, self.worker_id)
            db.commit()
        if claim is None:
            logger.info("领取失败(无槽/非 queued): task=%s", task_id)
            return
        self._attempt_id = claim.attempt_id
        self._attempt_no = claim.attempt_no
        self._lease_token = claim.lease_token
        self._current_task_id = task_id
        self._cancel_event = threading.Event()
        self._task_thread = threading.Thread(
            target=self._execute_task, args=(claim,), name=f"task-{task_id}", daemon=True,
        )
        self._task_thread.start()
        logger.info("任务领取: task=%s attempt=%s worker=%s", task_id, claim.attempt_id, self.worker_id)

    def _execute_task(self, claim: lease.Claim) -> None:
        """任务执行线程: runner.run_task 完成提交/失败/取消收拢(自带事务边界)。"""
        try:
            with self.session_factory() as db:
                runner.run_task(
                    db, claim, worker_id=self.worker_id, isolate=self.isolate,
                    stop_event=self._cancel_event,
                )
        except Exception:  # noqa: BLE001 - 线程边界兜底, 防止静默死亡
            logger.exception("任务线程异常: task=%s", claim.task_id)

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


def main(argv: list[str] | None = None) -> int:
    """Worker 进程入口(python -m iesplan.worker.main)。"""
    parser = argparse.ArgumentParser(description="IES Plan 计算/I/O Worker")
    parser.add_argument(
        "--worker-type", choices=("compute", "io"), default=None,
        help="Worker 类型(缺省取 IESPLAN_WORKER_TYPE 环境变量或配置)",
    )
    parser.add_argument("--worker-id", default=None, help="Worker 标识(缺省自动生成)")
    parser.add_argument("--no-isolation", action="store_true", help="禁用求解器子进程隔离(调试)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # 幂等建表(与 API 启动一致; Worker 可能先于 API 启动, 保证权威表就绪)
    from iesplan.db import init_db

    init_db()
    # 建模命令注册表(设备命令 + 计算引擎命令; 与 API 启动一致, 03 §5.2/§9.3)
    # 注册失败必须阻断启动: 计算 Worker 在空命令注册表上消费任务只会批量失败
    # (codex 二次审核 High-6), 不让其进入消费循环
    from iesplan.modeling.registry_loader import register_catalog_commands

    register_catalog_commands()
    worker_type = args.worker_type or os.environ.get("IESPLAN_WORKER_TYPE") or settings.worker_type
    Worker(
        worker_type=worker_type,
        worker_id=args.worker_id,
        isolate=not args.no_isolation,
    ).run()
    return 0


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main(sys.argv[1:]))
