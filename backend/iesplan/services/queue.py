"""Redis 队列与可重建状态服务(最小实现)。

设计约束见开发者指南 architecture.md 与 contracts.md: compute/io 两个逻辑队列(ZSET)、
秒级进度、Worker 心跳、取消信号。

一致性原则(规格 0.2/1.3): 队列/进度/心跳均为**可重建状态** —— 权威事实只在
PostgreSQL(tasks.status 等), Redis 只是视图; 丢失后可从 PG 重建, 不影响正确性。

降级策略: Redis 不可用(连接失败或运行期错误)时自动降级为进程内内存队列
(单进程模式), 并记录降级状态供运维观测(见 queue_status)。单机/测试场景可
通过环境变量 ``IESPLAN_QUEUE=memory`` 强制内存模式, 避免外部依赖。

消息格式(规格 5.1): ZSET member 为自足 JSON, score 为单调入队序号(INCR 生成);
排序语义由 PG 权威字段 (priority DESC, requested_at) 决定, 不编码进 score。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from iesplan.config import settings

logger = logging.getLogger(__name__)

try:
    import redis as _redis

    _REDIS_AVAILABLE = True
except Exception:  # pragma: no cover - 环境缺 redis 依赖时降级
    _redis = None  # type: ignore[assignment]
    _REDIS_AVAILABLE = False

#: 队列池名(与 compute_slots.pool_name / 规格 5.1 一致)
QUEUE_COMPUTE = "compute"
QUEUE_IO = "io"
#: 取消信号 TTL(秒, 规格 5.1: 10 min)
_CANCEL_TTL = 600
#: Redis 秒级进度键 TTL(秒, 任务结束后清理, 兜底防残留)
_PROGRESS_TTL = 3600


class _MemoryBackend:
    """进程内内存队列(单进程模式的降级后端)。

    仅当 Redis 不可用时启用; 状态可重建, 进程重启即丢失, 不影响权威事实。
    """

    def __init__(self) -> None:
        self.queues: dict[str, list[dict[str, Any]]] = {QUEUE_COMPUTE: [], QUEUE_IO: []}
        self.cancel: dict[int, str] = {}
        self.heartbeats: dict[str, dict[str, Any]] = {}
        self.progress: dict[tuple[int, int], dict[str, Any]] = {}

    # -- 队列 -------------------------------------------------------------
    def enqueue(self, msg: dict[str, Any], queue: str) -> None:
        self.queues.setdefault(queue, []).append(msg)

    def dequeue(self, queue: str) -> dict[str, Any] | None:
        items = self.queues.get(queue, [])
        return items.pop(0) if items else None

    def requeue(self, task_id: int, queue: str) -> None:
        """重新入队: 原消息若仍在队列则移到队尾(镜像 Redis zadd 替换语义),
        否则重建最小消息入队尾。"""
        items = self.queues.get(queue, [])
        existing = [m for m in items if m.get("task_id") == task_id]
        self.queues[queue] = [m for m in items if m.get("task_id") != task_id]
        if existing:
            self.queues[queue].append(existing[0])
        else:
            self.queues[queue].append(
                {"v": 1, "task_id": task_id, "pool": queue, "enqueued_at": datetime.now(UTC).isoformat()}
            )

    def remove(self, task_id: int, queue: str) -> None:
        self.queues[queue] = [m for m in self.queues.get(queue, []) if m.get("task_id") != task_id]

    def queue_position(self, task_id: int, queue: str) -> int | None:
        for index, msg in enumerate(self.queues.get(queue, [])):
            if msg.get("task_id") == task_id:
                return index
        return None

    def depth(self, queue: str) -> int:
        return len(self.queues.get(queue, []))

    # -- 心跳(带过期判定) ------------------------------------------------
    def set_heartbeat(self, worker_id: str, payload: dict[str, Any], ttl: int) -> None:
        self.heartbeats[worker_id] = {"payload": payload, "expires_at": time.monotonic() + ttl}

    def get_heartbeat(self, worker_id: str) -> dict[str, Any] | None:
        hb = self.heartbeats.get(worker_id)
        if hb is None:
            return None
        if hb["expires_at"] < time.monotonic():
            self.heartbeats.pop(worker_id, None)
            return None
        return hb["payload"]

    # -- 秒级进度 ---------------------------------------------------------
    def set_progress(self, key: tuple[int, int], progress: dict[str, Any]) -> None:
        self.progress[key] = progress

    def get_progress(self, key: tuple[int, int]) -> dict[str, Any] | None:
        return self.progress.get(key)

    # -- 取消信号 ---------------------------------------------------------
    def set_cancel(self, task_id: int, reason: str, ttl: int) -> None:
        self.cancel[task_id] = reason

    def get_cancel(self, task_id: int) -> str | None:
        return self.cancel.get(task_id)

    def clear_cancel(self, task_id: int) -> None:
        self.cancel.pop(task_id, None)


class _RedisBackend:
    """Redis 后端(ZSET 队列 + STRING/HASH 心跳/进度/取消信号, 规格 5.1)。"""

    def __init__(self, url: str) -> None:
        if not _REDIS_AVAILABLE:
            raise RuntimeError("redis 依赖未安装")
        self._r = _redis.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=1.0, socket_timeout=2.0
        )
        self._r.ping()  # 探测连接, 失败抛异常 → 由调用方降级

    @staticmethod
    def _msg_key(queue: str, task_id: int) -> str:
        """消息 JSON ↔ task_id 的索引键(用于定位 ZSET member 的 zrank/zrem)。"""
        return f"{queue}:msg:{task_id}"

    @staticmethod
    def _member(msg: dict[str, Any]) -> str:
        return json.dumps(msg, ensure_ascii=False, sort_keys=True)

    # -- 队列 -------------------------------------------------------------
    def enqueue(self, msg: dict[str, Any], queue: str) -> None:
        seq = self._r.incr(f"{queue}:queue:seq")
        self._r.zadd(f"{queue}:queue", {self._member(msg): seq})
        # 消息索引保留 7 天(队列本身无 TTL, 可重建)
        self._r.set(self._msg_key(queue, msg["task_id"]), self._member(msg), ex=7 * 86400)

    def dequeue(self, queue: str) -> dict[str, Any] | None:
        items = self._r.zrange(f"{queue}:queue", 0, 0, withscores=True)
        if not items:
            return None
        member, _score = items[0]
        self._r.zrem(f"{queue}:queue", member)
        try:
            return json.loads(member)
        except ValueError:
            return None

    def requeue(self, task_id: int, queue: str) -> None:
        member = self._r.get(self._msg_key(queue, task_id))
        if member is None:
            return  # 原消息不可见(已被领取/重建丢失): 权威状态在 PG, 由调度器重建
        seq = self._r.incr(f"{queue}:queue:seq")
        self._r.zadd(f"{queue}:queue", {member: seq})

    def remove(self, task_id: int, queue: str) -> None:
        member = self._r.get(self._msg_key(queue, task_id))
        if member is not None:
            self._r.zrem(f"{queue}:queue", member)
            self._r.delete(self._msg_key(queue, task_id))

    def queue_position(self, task_id: int, queue: str) -> int | None:
        member = self._r.get(self._msg_key(queue, task_id))
        if member is None:
            return None
        rank = self._r.zrank(f"{queue}:queue", member)
        return rank if rank is not None else None

    def depth(self, queue: str) -> int:
        return self._r.zcard(f"{queue}:queue")

    # -- 心跳 -------------------------------------------------------------
    def set_heartbeat(self, worker_id: str, payload: dict[str, Any], ttl: int) -> None:
        self._r.set(f"heartbeat:{worker_id}", json.dumps(payload, ensure_ascii=False), ex=ttl)

    def get_heartbeat(self, worker_id: str) -> dict[str, Any] | None:
        raw = self._r.get(f"heartbeat:{worker_id}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    # -- 秒级进度(HASH, 规格 7.1) -----------------------------------------
    def set_progress(self, key: tuple[int, int], progress: dict[str, Any]) -> None:
        task_id, attempt_no = key
        mapping = {
            field: (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
            for field, value in progress.items()
        }
        name = f"progress:{task_id}:{attempt_no}"
        self._r.hset(name, mapping=mapping)
        self._r.expire(name, _PROGRESS_TTL)

    def get_progress(self, key: tuple[int, int]) -> dict[str, Any] | None:
        task_id, attempt_no = key
        raw = self._r.hgetall(f"progress:{task_id}:{attempt_no}")
        if not raw:
            return None
        out: dict[str, Any] = dict(raw)
        for field in ("detail",):
            if isinstance(out.get(field), str):
                try:
                    out[field] = json.loads(out[field])
                except ValueError:
                    pass
        return out

    # -- 取消信号 ---------------------------------------------------------
    def set_cancel(self, task_id: int, reason: str, ttl: int) -> None:
        self._r.set(f"cancel:{task_id}", reason, ex=ttl)

    def get_cancel(self, task_id: int) -> str | None:
        return self._r.get(f"cancel:{task_id}")

    def clear_cancel(self, task_id: int) -> None:
        self._r.delete(f"cancel:{task_id}")


# ---------------------------------------------------------------------------
# 模块级后端选择(auto/memory/redis)与降级状态
# ---------------------------------------------------------------------------

_backend: _MemoryBackend | _RedisBackend | None = None
_degraded = False
_degraded_logged = False


def _degrade() -> None:
    """降级为内存后端并记录降级状态(Redis 连接失败或运行期错误)。"""
    global _backend, _degraded, _degraded_logged
    _backend = _MemoryBackend()
    _degraded = True
    if not _degraded_logged:
        _degraded_logged = True
        logger.warning("Redis 不可用, 队列已降级为内存后端(单进程模式)")


def _get_backend() -> _MemoryBackend | _RedisBackend:
    """惰性选择后端: IESPLAN_QUEUE=memory 强制内存; 其余先试 Redis, 失败降级。"""
    global _backend, _degraded
    if _backend is not None:
        return _backend
    mode = os.environ.get("IESPLAN_QUEUE", "auto").lower()
    if mode == "memory":
        _backend = _MemoryBackend()
        return _backend
    try:
        _backend = _RedisBackend(settings.redis_url)
    except Exception:
        _degrade()
    return _backend  # type: ignore[return-value]


def force_memory() -> None:
    """强制切换为内存后端(测试/单机场景, 等价 IESPLAN_QUEUE=memory)。"""
    global _backend, _degraded
    _backend = _MemoryBackend()
    _degraded = True


def _call(method: str, *args: Any) -> Any:
    """调用后端方法; Redis 运行期错误时降级内存后端并重试一次。"""
    backend = _get_backend()
    try:
        return getattr(backend, method)(*args)
    except Exception:
        if isinstance(backend, _MemoryBackend):
            raise
        _degrade()
        return getattr(_get_backend(), method)(*args)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def _build_message(
    task_id: int,
    queue: str,
    *,
    task_type: str | None = None,
    snapshot_id: int | None = None,
    priority: int = 0,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """构造队列消息(规格 5.1: 自足 JSON, 领取时凭 task_id 回 PG 取权威行)。"""
    return {
        "v": 1,
        "task_id": task_id,
        "type": task_type,
        "pool": queue,
        "snapshot_id": snapshot_id,
        "priority": priority,
        "enqueued_at": datetime.now(UTC).isoformat(),
        "trace_id": trace_id,
    }


def enqueue(
    task_id: int,
    queue: str = QUEUE_COMPUTE,
    *,
    task_type: str | None = None,
    snapshot_id: int | None = None,
    priority: int = 0,
    trace_id: str | None = None,
) -> None:
    """任务入队(compute/io 逻辑队列, 消息携带自足信息)。"""
    _call(
        "enqueue",
        _build_message(task_id, queue, task_type=task_type, snapshot_id=snapshot_id,
                       priority=priority, trace_id=trace_id),
        queue,
    )


def dequeue(queue: str = QUEUE_COMPUTE) -> int | None:
    """按入队序(FIFO)领取一个任务, 返回 task_id; 队列空返回 None。"""
    msg = _call("dequeue", queue)
    if msg is None:
        return None
    return msg.get("task_id")


def requeue(task_id: int, queue: str = QUEUE_COMPUTE) -> None:
    """任务重新入队(原消息回队尾; 排序语义由 PG 权威字段决定)。"""
    _call("requeue", task_id, queue)


def remove(task_id: int, queue: str = QUEUE_COMPUTE) -> None:
    """从队列移除任务(领取成功/直接取消/删除协调时调用)。"""
    _call("remove", task_id, queue)


def queue_position(task_id: int, queue: str = QUEUE_COMPUTE) -> int | None:
    """任务在队列中的位次(0 起); 不在队列返回 None(排队位次估算, 规格 9.1)。"""
    return _call("queue_position", task_id, queue)


def set_heartbeat(worker_id: str, payload: dict[str, Any], ttl: int = 15) -> None:
    """写 Worker 心跳(规格 7.1: 间隔 5 s, TTL = 3 × 间隔; 可重建视图)。"""
    _call("set_heartbeat", worker_id, payload, ttl)


def get_heartbeat(worker_id: str) -> dict[str, Any] | None:
    """读 Worker 心跳; 过期/不存在返回 None。"""
    return _call("get_heartbeat", worker_id)


def set_progress(
    task_id: int, attempt_no: int, percent: float, stage: str, detail: dict | None = None
) -> None:
    """写秒级进度(规格 7.1: 间隔 ~2 s; 丢失后由 PG 持久进度兜底)。"""
    progress = {
        "percent": percent,
        "stage": stage,
        "detail": detail or {},
        "updated_at": datetime.now(UTC).isoformat(),
    }
    _call("set_progress", (task_id, attempt_no), progress)


def get_progress(task_id: int, attempt_no: int) -> dict[str, Any] | None:
    """读秒级进度; 不存在返回 None。"""
    return _call("get_progress", (task_id, attempt_no))


def set_cancel(task_id: int, reason: str, ttl: int = _CANCEL_TTL) -> None:
    """广播取消信号(规格 6.1: Worker 轮询; Redis 信号丢失不影响正确性)。"""
    _call("set_cancel", task_id, reason, ttl)


def get_cancel(task_id: int) -> str | None:
    """读取消信号(含取消原因)。"""
    return _call("get_cancel", task_id)


def clear_cancel(task_id: int) -> None:
    """清除取消信号(任务终态后)。"""
    _call("clear_cancel", task_id)


def queue_status() -> dict[str, Any]:
    """队列服务状态: 后端类型/降级标记/各池深度(供 readyz 与运维观测)。"""
    return {
        "backend": "redis" if isinstance(_get_backend(), _RedisBackend) else "memory",
        "degraded": _degraded,
        "queues": {pool: _call("depth", pool) for pool in (QUEUE_COMPUTE, QUEUE_IO)},
    }
