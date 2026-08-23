"""资源使用边界(0.2.0 批次A A4): 全局限流 + 上传配额 + dataset meta 白名单。

第三方攻击面侦察结论(0.2.0):
1. 除登录外的所有 API 无 IP/频控, 第三方可低成本刷任务/重复上传制造计算/存储压力。
2. dataset 上传的 provenance/fields/meta 由客户端 JSON 传入, 无 schema 白名单,
   可注入任意 JSON 污染质量报告展示。
3. 既有门禁: 登录限速(identity.py)、上传大小门禁(dataset 512MB / 包 2GB)、
   磁盘余量门禁(storage/service.py)、任务级 estimate_storage。

本模块只防第三方/外部请求(不做账户级配额重系统、不做管理员审批), 本地开发
默认宽松阈值可配置, 不误伤 e2e/本地开发。

实现:
- ``RateLimitMiddleware``: 按 IP 限流(内存 + Redis 降级, 复用登录限速模式)。
  只对非豁免路径做限流; 本地/e2e 默认阈值宽松(每窗口 120 次), 可经
  IESPLAN_RATE_LIMIT_MAX_REQUESTS 收紧。
- ``QuotaError`` / ``check_upload_quota``: 按用户/项目统计已用对象存储,
  超配额拒绝(复用 storage 的 usage 聚合; 对象内容寻址去重天然按实际占用计)。
- ``validate_upload_meta`` / ``validate_upload_fields``: dataset 上传的
  provenance/fields/meta schema 白名单, 拒绝未知键或畸形结构。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from iesplan.config import settings
from iesplan.core.errors import error_envelope

logger = logging.getLogger(__name__)

#: 限流错误信封(429; 与标准错误信封同构, 供前端按 message_key 渲染)
RATE_LIMIT_CODE: Final[str] = "API-RL-001"
RATE_LIMIT_MESSAGE_KEY: Final[str] = "ies.error.rate_limited"
RATE_LIMIT_FIX_HINT: Final[str] = "ies.fix.rate_retry"

#: 配额错误信封(413; 超过上传配额拒绝)
QUOTA_CODE: Final[str] = "API-QUOTA-001"
QUOTA_MESSAGE_KEY: Final[str] = "ies.error.upload_quota_exceeded"

#: 元数据白名单拒绝错误信封(400; 未知键/畸形结构)
META_CODE: Final[str] = "API-META-001"
META_MESSAGE_KEY: Final[str] = "ies.error.meta_invalid"


# ---------------------------------------------------------------------------
# 全局限速(进程内存 + Redis 降级, 复用登录限速模式)
#
# 局限说明: 内存限速为单进程状态 —— 多 Uvicorn Worker 下计数被分散, 进程重启
# 后状态丢失。生产多 Worker 部署应使用 Redis 原子计数; Redis 不可用(依赖缺失/
# 连接失败/运行期错误)自动降级内存限速并记 warning 日志, 不阻断 API。
# ---------------------------------------------------------------------------

try:
    import redis as _redis_module

    _REDIS_IMPORT_OK = True
except Exception:  # pragma: no cover - 环境缺 redis 依赖时降级内存限速
    _redis_module = None  # type: ignore[assignment]
    _REDIS_IMPORT_OK = False

#: Redis 全局限速键前缀(与登录限速键命名空间分开, 互不干扰)
_RATE_KEY_PREFIX = "iesplan:ratelimit:global"
#: 惰性初始化的 Redis 客户端(单例; 连接失败置 None 后不再重试, 保持内存降级)
_rate_redis_client: Any = None
#: 全局限速内存状态: ip -> [(monotonic, count), ...](窗口内时间戳)
_RATE_WINDOWS: dict[str, list[float]] = {}
_RATE_LOCK = threading.RLock()


def _rate_redis() -> Any | None:
    """尝试获取 Redis 客户端用于跨 Worker 限速; 不可用返回 None(降级内存)。

    与登录限速同策略: IESPLAN_QUEUE=memory(测试/单机模式)时直接跳过 Redis,
    避免测试环境共享 Redis 键造成跨测试/跨进程状态污染。
    """
    global _rate_redis_client
    if os.environ.get("IESPLAN_QUEUE", "auto").lower() == "memory":
        return None
    if _rate_redis_client is not None:
        return _rate_redis_client
    if not _REDIS_IMPORT_OK:
        return None
    try:
        client = _redis_module.Redis.from_url(
            settings.redis_url, decode_responses=True,
            socket_connect_timeout=1.0, socket_timeout=2.0,
        )
        client.ping()  # 探测连接, 失败抛异常
        _rate_redis_client = client
        logger.warning("全局限流使用 Redis 后端(跨 Worker 共享)")
    except Exception:  # noqa: BLE001 - 降级内存限速, 不阻断 API
        logger.warning("Redis 不可用, 全局限流降级为进程内存(单进程有效)")
        _rate_redis_client = None
    return _rate_redis_client


def _rate_key(ip: str) -> str:
    """Redis 限速键(IP; TTL 即窗口, INCR 幂等)。"""
    return f"{_RATE_KEY_PREFIX}:{ip}"


def _memory_check_and_count(ip: str) -> bool:
    """内存限速判定 + 计数: 窗口内请求数超过上限返回 False(429)。

    滑动窗口: 保留窗口内的时间戳, 清理过期时间戳; 计数为保留的时间戳数。
    使用单调时钟(monotonic), 与登录限速一致。拒绝时也记录本次请求,
    保证持续超限持续 429。
    """
    with _RATE_LOCK:
        now = time.monotonic()
        window_start = now - settings.rate_limit_window_seconds
        timestamps = _RATE_WINDOWS.get(ip, [])
        timestamps = [t for t in timestamps if t > window_start]
        timestamps.append(now)
        _RATE_WINDOWS[ip] = timestamps
        allowed = len(timestamps) <= settings.rate_limit_max_requests
        # 定期清理: 全表键数超过阈值时清空过期窗口(防内存无限增长)
        if len(_RATE_WINDOWS) > 10000:
            for key, ts in list(_RATE_WINDOWS.items()):
                kept = [t for t in ts if t > now - settings.rate_limit_window_seconds]
                if not kept:
                    _RATE_WINDOWS.pop(key, None)
        return allowed


def reset_rate_limit() -> None:
    """清空全局限速状态(测试与运维恢复用; Redis 键一并清除)。"""
    r = _rate_redis()
    if r is not None:
        try:
            for key in r.scan_iter(f"{_RATE_KEY_PREFIX}:*"):
                r.delete(key)
        except Exception:  # noqa: BLE001
            pass
    with _RATE_LOCK:
        _RATE_WINDOWS.clear()


def _client_ip(request: Request) -> str:
    """客户端 IP(代理部署时由反向代理注入 X-Forwarded-For, 取最左侧真实来源)。

    部署前提: 应用必须位于可信反向代理之后, 且代理必须覆盖/覆写
    X-Forwarded-For(见 manual/developer-guide/zh-CN/deployment.md)。若应用
    直接暴露或代理未覆盖该头, 客户端可伪造 X-Forwarded-For 前缀刷掉限流计数。
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _exempt_path(request: Request) -> bool:
    """豁免路径判定: 健康/就绪探针与登录接口除外。

    登录接口放行原因: 已有更严格的用户名级限速(identity.py), 全局限速可能
    误伤合法登录尝试, 且登录接口本身成本低。探针高频轮询不应计入业务限流。
    """
    exempt = {
        p.strip()
        for p in (settings.rate_limit_exempt_paths or "").split(",")
        if p.strip()
    }
    return request.url.path in exempt


def _error_body(code: str, message_key: str, status: int, **params: object) -> JSONResponse:
    """构造标准错误信封(与全局异常处理同构, 供中间件/配额/白名单直接返回)。

    fix_hint_key 透出: 限流给 ies.fix.rate_retry, 其余 None。
    """
    fix_hint = RATE_LIMIT_FIX_HINT if code == RATE_LIMIT_CODE else None
    return JSONResponse(
        status_code=status,
        content=error_envelope(
            code=code,
            message_key=message_key,
            severity="error",
            blocking=True,
            params=params,
            fix_hint_key=fix_hint or "",
        ),
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """全局限流中间件: 对 API 请求按 IP 限流(窗口内超上限返回 429)。

    只限制高成本端点或全局限流合理阈值; 健康/就绪探针与登录豁免。
    关闭开关(IESPLAN_RATE_LIMIT_ENABLED=false)时透明放行。
    Redis 可用时跨 Worker 计数; 不可用时降级进程内存(单进程有效)。
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        if not settings.rate_limit_enabled or _exempt_path(request):
            return await call_next(request)
        ip = _client_ip(request)
        if not _check_and_count(ip):
            return _error_body(
                RATE_LIMIT_CODE, RATE_LIMIT_MESSAGE_KEY, 429,
                retry_after=settings.rate_limit_window_seconds,
            )
        return await call_next(request)


def _check_and_count(ip: str) -> bool:
    """限流判定 + 计数, 返回是否放行(False = 429)。

    Redis 可用走原子 INCR(跨 Worker 一致); 不可用走进程内存滑动窗口。
    """
    r = _rate_redis()
    if r is not None:
        try:
            count = r.incr(_rate_key(ip))
            if count == 1:
                r.expire(_rate_key(ip), settings.rate_limit_window_seconds)
            return int(count) <= settings.rate_limit_max_requests
        except Exception:  # noqa: BLE001 - 运行期错误降级内存
            return _memory_check_and_count(ip)
    return _memory_check_and_count(ip)


# ---------------------------------------------------------------------------
# 上传配额(每用户/每项目)
# ---------------------------------------------------------------------------

class QuotaError(Exception):
    """上传配额超限(API 层转换为 413 标准错误信封)。"""

    def __init__(self, *, used_bytes: int, quota_bytes: int, scope: str, owner_id: int) -> None:
        super().__init__(f"上传配额超限: used={used_bytes} quota={quota_bytes}")
        self.used_bytes = used_bytes
        self.quota_bytes = quota_bytes
        self.scope = scope
        self.owner_id = owner_id


def _dataset_files_bytes(db, project_ids: list[int]) -> int:
    """项目集合内数据集版本文件占用之和(dataset_files.size_bytes)。

    统计口径 = 逻辑分配量: 内容寻址去重时同内容对象被多个版本引用, 其
    dataset_file 行会重复计列大小 —— 门禁目的(防重复上传刷存储)由逻辑累计
    即满足, 且语义清晰、无跨表 join、不依赖对象去重实现。
    """
    if not project_ids:
        return 0
    import sqlalchemy as sa

    from iesplan.models.dataset import Dataset, DatasetFile, DatasetVersion

    total = (
        sa.select(sa.func.coalesce(sa.func.sum(DatasetFile.size_bytes), 0))
        .select_from(DatasetFile)
        .join(DatasetVersion, DatasetVersion.id == DatasetFile.dataset_version_id)
        .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
        .where(Dataset.project_id.in_(project_ids))
    )
    return int(db.execute(total).scalar_one() or 0)


def _project_ids_for_user(db, user_id: int) -> list[int]:
    """用户作为所有者或成员(未撤销)的未删除项目 id 列表。"""
    import sqlalchemy as sa

    from iesplan.models.project import Project, ProjectMember

    owned = sa.select(Project.id).where(Project.owner_id == user_id, Project.status != "deleted")
    member = sa.select(ProjectMember.project_id).where(
        ProjectMember.user_id == user_id, ProjectMember.revoked_at.is_(None)
    )
    rows = db.execute(owned.union(member)).scalars().all()
    return list(rows)


def check_upload_quota(
    db,
    *,
    user_id: int,
    project_id: int | None = None,
    incoming_bytes: int = 0,
) -> None:
    """上传配额门禁: 用户已用(所属项目数据集文件) + 本次请求大小 > 配额即拒绝。

    只启用显式配置的配额(0 = 不限):
    - ``upload_quota_bytes``: 每用户配额(全局);
    - ``project_quota_bytes``: 每项目配额(叠加)。
    本地开发默认双 0(不限), 不误伤 e2e/本地开发。
    项目包导入无目标项目(新项目身份), 此时 project_id=None 只应用用户级配额。

    参数:
        db: 数据库会话(请求级; 只读统计, 不提交)。
        user_id: 当前用户。
        project_id: 目标项目(可为 None, 如项目包导入)。
        incoming_bytes: 本次上传数据字节数(配额判断包含本次请求, 防逐次小额
            上传逐步逼近上限; 已知大小时传入)。
    异常:
        QuotaError: 超过任一配额(API 层转换为 413)。
    """
    if settings.upload_quota_bytes <= 0 and settings.project_quota_bytes <= 0:
        return

    if settings.upload_quota_bytes > 0:
        used = _dataset_files_bytes(db, _project_ids_for_user(db, user_id))
        if used + incoming_bytes > settings.upload_quota_bytes:
            raise QuotaError(
                used_bytes=used, quota_bytes=settings.upload_quota_bytes,
                scope="user", owner_id=user_id,
            )
    if settings.project_quota_bytes > 0 and project_id is not None:
        project_used = _dataset_files_bytes(db, [project_id])
        if project_used + incoming_bytes > settings.project_quota_bytes:
            raise QuotaError(
                used_bytes=project_used, quota_bytes=settings.project_quota_bytes,
                scope="project", owner_id=project_id,
            )


# ---------------------------------------------------------------------------
# dataset 上传 meta/fields/provenance schema 白名单
# ---------------------------------------------------------------------------

#: meta 顶层允许的键(source_category/license/provenance/created_reason)
_META_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"source_category", "license", "provenance", "created_reason"}
)

#: provenance 允许的键(合法溯源字段; 拒绝任意嵌套污染)
#: 与内置样例(builtin_sample)生成的溯源形状对齐: 含 time_range 嵌套对象。
_PROVENANCE_ALLOWED_KEYS: frozenset[str] = frozenset(
    {"source_category", "generator", "region", "region_name", "resolution",
     "utc_offset_minutes", "seed", "description_zh", "origin", "tag", "time_range"}
)

#: provenance 允许的嵌套值类型(str / int / float / bool / None / list[str] / dict[str, scalar])
_SCALAR_TYPES = (str, int, float, bool, type(None))


def _is_provenance_value(value: Any, depth: int = 0) -> bool:
    """provenance 值类型白名单: 标量 / 字符串列表 / 简单字典(递归最多 2 层)。"""
    if depth > 2:
        return False
    if isinstance(value, _SCALAR_TYPES):
        return True
    if isinstance(value, list):
        return all(_is_provenance_value(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_provenance_value(v, depth + 1) for k, v in value.items())
    return False


def _validate_provenance(prov: Any, path: str = "meta.provenance") -> list[str]:
    """校验 provenance: 必须是 dict, 键在白名单内, 值类型受控。"""
    errors: list[str] = []
    if prov is None:
        return errors
    if not isinstance(prov, dict):
        errors.append(f"{path}: 必须是 JSON 对象")
        return errors
    for key in prov:
        if key not in _PROVENANCE_ALLOWED_KEYS:
            errors.append(f"{path}.{key}: 未知溯源字段")
        elif not _is_provenance_value(prov[key]):
            errors.append(f"{path}.{key}: 值类型非法")
    return errors


def validate_upload_meta(meta: dict | None) -> list[str]:
    """校验上传 meta 白名单: 拒绝未知顶层键与畸形 provenance/created_reason。

    返回错误信息列表(空 = 合法); 不影响 source_category/license 等合法字段。
    """
    meta = dict(meta or {})
    errors: list[str] = []
    for key in meta:
        if key not in _META_ALLOWED_KEYS:
            errors.append(f"meta.{key}: 未知元信息字段")
    if "source_category" in meta and not isinstance(meta["source_category"], str):
        errors.append("meta.source_category: 必须为字符串")
    if "license" in meta and not isinstance(meta["license"], str):
        errors.append("meta.license: 必须为字符串")
    if "created_reason" in meta and not isinstance(meta["created_reason"], str):
        errors.append("meta.created_reason: 必须为字符串")
    if "provenance" in meta:
        errors.extend(_validate_provenance(meta["provenance"]))
    return errors


def validate_upload_fields(fields: dict | None) -> list[str]:
    """校验上传 fields 白名单: 键为列名(≤200), 值为 {unit: str} 形态。

    合法声明示例: {"e_load": {"unit": "kWh"}} 或 {"e_load": "kWh"}。
    拒绝未知字段形状(如嵌套任意 JSON、数组、非字符串 unit)与超大字段字典。
    """
    fields = dict(fields or {})
    errors: list[str] = []
    if len(fields) > 200:
        errors.append("fields: 字段数超过上限 200")
        return errors
    for key, value in fields.items():
        if not isinstance(key, str) or not key:
            errors.append("fields: 字段名必须为非空字符串")
            continue
        if isinstance(value, dict):
            for vk, vv in value.items():
                if vk != "unit":
                    errors.append(f"fields.{key}: 未知字段属性 {vk!r}(仅允许 unit)")
                elif not isinstance(vv, str):
                    errors.append(f"fields.{key}.unit: 必须为字符串")
        elif isinstance(value, str):
            pass  # 简写 {"e_load": "kWh"}
        else:
            errors.append(f"fields.{key}: 字段声明必须为 {{'unit': str}} 或字符串")
    return errors
