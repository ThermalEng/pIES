"""应用异常体系。

携带 code/severity/blocking/message_key/params/location,与诊断体系(04 §5)对齐,
后端只输出消息键与参数,文案由前端渲染(原则 P3)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from iesplan.core.diagnostics import (
    SEVERITIES,
    SEVERITY_BLOCKING,
    SEVERITY_ERROR,
)


def error_envelope(
    *,
    code: str,
    message_key: str,
    severity: str = SEVERITY_ERROR,
    blocking: bool = True,
    params: Mapping[str, Any] | None = None,
    location: Mapping[str, Any] | None = None,
    fix_hint_key: str = "",
    ref_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """构造标准错误信封 {"error": {...}}(宪法 §8.3,与 Diagnostic 字段同构)。

    全库唯一错误体权威构造器: HTTP 异常处理器、AppError.to_dict、中间件
    手工响应一律经由本函数输出,字段集固定为
    code/severity/blocking/message_key/params/location/fix_hint_key/ref_ids。
    """
    return {
        "error": {
            "code": code,
            "message_key": message_key,
            "severity": severity,
            "blocking": blocking,
            "params": dict(params or {}),
            "location": dict(location) if location is not None else None,
            "fix_hint_key": fix_hint_key,
            "ref_ids": list(ref_ids or ()),
        }
    }


class AppError(Exception):
    """应用级错误基类。

    属性:
        code: 诊断码(域-类别-编号,如 PERM-DENIED-001)。
        severity: 严重度(blocking/error/warning/info)。
        blocking: 是否阻断当前操作(默认按严重度推导)。
        message_key: 文案键(默认 ies.msg.err.generic)。
        params: 文案插值参数(只含可序列化数据)。
        location: 对象定位字典(如 {object_type, object_id, field, row})。
    """

    code: str = "SYS-CFG-001"
    severity: str = SEVERITY_ERROR
    message_key: str = "ies.msg.err.generic"
    http_status: int = 500

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        severity: str | None = None,
        blocking: bool | None = None,
        message_key: str | None = None,
        params: Mapping[str, Any] | None = None,
        location: Mapping[str, Any] | None = None,
        fix_hint_key: str = "",
        ref_ids: Sequence[str] | None = None,
    ) -> None:
        if severity is not None and severity not in SEVERITIES:
            raise ValueError(f"非法严重度: {severity!r}")
        self.code = code or self.code
        self.severity = severity or self.severity
        self.blocking = self.severity == SEVERITY_BLOCKING if blocking is None else blocking
        self.message_key = message_key or self.message_key
        self.params: dict[str, Any] = dict(params or {})
        self.location: Mapping[str, Any] | None = (
            dict(location) if location is not None else None
        )
        self.fix_hint_key = fix_hint_key
        self.ref_ids: tuple[str, ...] = tuple(ref_ids or ())
        super().__init__(message or self.message_key)

    def to_dict(self) -> dict[str, Any]:
        """序列化为标准错误信封(error 对象内含 8 个契约字段)。"""
        return error_envelope(
            code=self.code,
            message_key=self.message_key,
            severity=self.severity,
            blocking=self.blocking,
            params=self.params,
            location=self.location,
            fix_hint_key=getattr(self, "fix_hint_key", ""),
            ref_ids=getattr(self, "ref_ids", ()),
        )


class ForbiddenError(AppError):
    """无权限执行操作(04 §9 表 F:PERM-DENIED-001)。"""

    code = "PERM-DENIED-001"
    severity = SEVERITY_BLOCKING
    message_key = "ies.diag.perm.denied"
    http_status = 403


class NotFoundError(AppError):
    """资源不存在(新码 RES-MISS-003,已在 diagnostics.NEW_DIAG_CODES 登记)。"""

    code = "RES-MISS-003"
    severity = SEVERITY_ERROR
    message_key = "ies.diag.res.not_found"
    http_status = 404


class ConflictError(AppError):
    """状态/资源冲突(如并发保存冲突,新码 SYS-STORE-004,已登记)。"""

    code = "SYS-STORE-004"
    severity = SEVERITY_ERROR
    message_key = "ies.diag.store.save_conflict"
    http_status = 409


def http_error(status: int, code: str, message_key: str, **params: object) -> AppError:
    """构造带指定 HTTP 状态码的 AppError(基类默认 500, 按需覆盖)。"""
    err = AppError(code=code, message_key=message_key, params=dict(params))
    err.http_status = status
    return err
