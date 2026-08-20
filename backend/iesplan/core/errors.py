"""应用异常体系。

携带 code/severity/blocking/message_key/params/location,与诊断体系(04 §5)对齐,
后端只输出消息键与参数,文案由前端渲染(原则 P3)。
"""

from __future__ import annotations

from iesplan.core.diagnostics import (
    SEVERITIES,
    SEVERITY_BLOCKING,
    SEVERITY_ERROR,
)


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
        params: dict | None = None,
        location: dict | None = None,
    ) -> None:
        if severity is not None and severity not in SEVERITIES:
            raise ValueError(f"非法严重度: {severity!r}")
        self.code = code or self.code
        self.severity = severity or self.severity
        self.blocking = self.severity == SEVERITY_BLOCKING if blocking is None else blocking
        self.message_key = message_key or self.message_key
        self.params = dict(params or {})
        self.location = location
        super().__init__(message or self.message_key)

    def to_dict(self) -> dict:
        """序列化为 API 错误响应体(与诊断对象同构)。"""
        return {
            "error": True,
            "code": self.code,
            "severity": self.severity,
            "blocking": self.blocking,
            "message_key": self.message_key,
            "params": self.params,
            "location": self.location,
        }


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
