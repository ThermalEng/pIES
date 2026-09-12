"""项目包域纯内容（包格式解析/下载授权签名/常量，归属 package）。

跨域传输编排（导出/导入提案与确认、Excel 报告）已上移
``application.packages.transfers``；本模块仅保留无 IO 的包格式解析纯函数、
HMAC 下载授权与格式常量。财务/规划配置读经 configuration 域，内容对象
读写经 application 层 content_objects 用例。

依据架构宪法 §10/§12 与 domain-model §快照、任务和结果/§对象生命周期 及 contracts §公共文件契约：

- export_package: 仅所有者；版本化清单(格式版本/清单/对象清单)，流式导出
  模型/配置/版本/数据集版本与溯源/历史结果证据与评估引用；
  含当前生效规划/财务配置 revision 快照(0.6.5 事项 3)；
  不含账号/权限/会话/全局配置/密钥（domain-model §对象生命周期、架构宪法 §16）；
  对象清单只登记路径/大小/类型(不登记内容摘要)；
- import_proposal: 导入前校验(格式/兼容性/清单/完整性，对象清单与包内文件
  一一对应 + 大小一致；包内规划/财务配置严格恢复 + 领域校验，缺失 = 导入后
  无配置，不静默默认)；暂存对象 + 拟创建项目快照 + 分区提交内容 + 校验报告；
- confirm_import: 提交导入 — 每次导入创建新项目身份(不覆盖已有)，导入者成为
  所有者，原授权关系不迁移，历史结果作为证据来源保留(不伪造本地任务)；
- export_excel: 固定模板(标题中英双语，默认中文)，固定引用证据包与评估，
  不重新求解；查看者可导出 Excel，仅所有者可导出项目包；
- 下载授权: 短期单对象授权(HMAC 签名 token 含 object_id + 过期，过期 5 分钟)。

本层服务不主动 commit，事务边界由 API 层控制。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import Any

from iesplan.config import settings
from iesplan.core.contracts import (
    PlanningConfig,
    PlanningConfigError,
)
from iesplan.core.diagnostics import SEVERITY_ERROR
from iesplan.core.errors import AppError
from iesplan.finance import (
    EffectiveFinanceConfig,
    FinanceOverrides,
    FinanceProfile,
    FinanceTripletError,
)
from iesplan.planning.contracts import validate_planning_domain

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 项目包格式版本(主版本号兼容判定: 只接受 1.x)
PACKAGE_FORMAT_VERSION = "1.0"
#: 项目包媒体类型
PACKAGE_MEDIA_TYPE = "application/zip"
#: Excel 报告媒体类型
EXCEL_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
#: 下载授权有效期(秒, 短期单对象授权, 默认 5 分钟)
DOWNLOAD_TOKEN_TTL_SECONDS = 300
#: 包内禁止出现的清单键(账号/权限/会话/全局配置/密钥不得随包导出，见 domain-model §对象生命周期)
FORBIDDEN_MANIFEST_KEYS: frozenset[str] = frozenset(
    {"accounts", "permissions", "sessions", "global_config", "secrets"}
)
#: 项目包必需文件
REQUIRED_PACKAGE_FILES: tuple[str, ...] = ("manifest.json", "project.json", "draft.json")
#: 数据集版本内容媒体类型映射(format 校验)
_FORMAT_BY_MEDIA: dict[str, str] = {
    "text/csv; charset=utf-8": "csv",
    "text/csv": "csv",
    "application/json": "json",
}


def media_file_kind(media_type: str | None) -> str:
    """媒体类型 → 包内文件扩展名 kind(csv 或 json)，供导出装配使用。"""
    return "csv" if _FORMAT_BY_MEDIA.get(media_type or "", "") == "csv" else "json"

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ImportValidationError(AppError):
    """项目包校验失败(格式/兼容性/清单/完整性, HTTP 400)。

    携带 params.reasons 逐项说明失败原因; 校验失败即拒绝, 不创建任何记录。
    """

    code = "PKG-IMP-001"
    http_status = 400
    severity = SEVERITY_ERROR
    message_key = "ies.diag.pkg.invalid"

    def __init__(self, reasons: list[str], message: str = "") -> None:
        self.reasons = list(reasons)
        super().__init__(message or f"项目包校验失败: {'; '.join(reasons)}")


class DownloadTokenError(AppError):
    """下载授权无效/过期(HTTP 400)。"""

    code = "EXPORT-TOKEN-001"
    http_status = 400
    severity = SEVERITY_ERROR
    message_key = "ies.diag.export.token_invalid"


class PackageSizeError(AppError):
    """项目包超限(上传字节/条目数/单条目解压大小/总解压大小, HTTP 413)。

    在任何解压读取之前完成门禁, 防止 ZIP Bomb 消耗内存与 CPU。
    """

    code = "PKG-SIZE-001"
    http_status = 413
    severity = SEVERITY_ERROR
    message_key = "ies.diag.pkg.too_large"


# ---------------------------------------------------------------------------
# 域门面组合(本服务只经公开域接口访问数据,不直接导入 ORM 或跨服务调用)
# ---------------------------------------------------------------------------

#: 审计动作(与 services.audit.AUDIT_ACTION_CATALOG 同值; 包导出/导入审计经
#: audit 域门面直接记录, 不再经 services.audit 转调)。



# ---------------------------------------------------------------------------
# 下载授权: 短期单对象签名 token(绑定项目与签发用户 + 过期, 过期 5 分钟)
# ---------------------------------------------------------------------------

#: 项目包上传字节上限(压缩后; 与 Nginx client_max_body_size 对齐为 2GB)
MAX_PACKAGE_BYTES: int = 2 * 1024 * 1024 * 1024
#: 项目包 zip 最大条目数(含目录; 超限拒绝, 防数十万小文件)
MAX_PACKAGE_ENTRIES: int = 5000
#: 单条目解压后大小上限(512MB, 防单文件巨大膨胀)
MAX_PACKAGE_ENTRY_BYTES: int = 512 * 1024 * 1024
#: 全部条目解压后总大小上限(4GB, 防整体 ZIP Bomb)
MAX_PACKAGE_TOTAL_BYTES: int = 4 * 1024 * 1024 * 1024


def _token_sign(payload: str) -> str:
    """HMAC-SHA256 签名(密钥取 settings.secret_key)。"""
    return hmac.new(settings.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def create_download_token(
    object_id: int,
    kind: str,
    *,
    project_id: int,
    user_id: int,
    ttl_seconds: int = DOWNLOAD_TOKEN_TTL_SECONDS,
) -> str:
    """签发短期单对象下载授权 token(绑定项目与签发用户，架构宪法 §16)。

    token = base64url(payload) + "." + hmac 签名; payload 含 object_id/kind/
    project_id/user_id/exp, 缺省 5 分钟过期。下载时必须验证 token 绑定的项目
    与当前会话用户一致, 防止跨项目对象下载。
    """
    exp = int(datetime.now(UTC).timestamp()) + max(int(ttl_seconds), 1)
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "object_id": int(object_id),
                "kind": str(kind),
                "project_id": int(project_id),
                "user_id": int(user_id),
                "exp": exp,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).decode("ascii")
    return f"{payload}.{_token_sign(payload)}"


def verify_download_token(token: str, *, expected_kind: str | None = None) -> dict[str, Any]:
    """校验下载授权 token: 格式/签名/过期, 返回 {object_id, kind, project_id, user_id}。

    签名比较使用 hmac.compare_digest(常量时间, 防时序侧信道);
    签名不符/格式非法/已过期一律抛 DownloadTokenError。
    """
    if not isinstance(token, str) or "." not in token:
        raise DownloadTokenError("", params={"reason": "bad_token"})
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(_token_sign(payload), sig):
        raise DownloadTokenError("", params={"reason": "bad_signature"})
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, UnicodeDecodeError) as exc:
        raise DownloadTokenError("", params={"reason": "bad_payload"}) from exc
    if not isinstance(data, dict) or "object_id" not in data or "exp" not in data:
        raise DownloadTokenError("", params={"reason": "bad_payload"})
    if int(data["exp"]) < int(datetime.now(UTC).timestamp()):
        raise DownloadTokenError("", params={"reason": "expired"})
    if expected_kind is not None and data.get("kind") != expected_kind:
        raise DownloadTokenError("", params={"reason": "kind_mismatch", "expected": expected_kind})
    # 旧版(未绑定项目/用户)token 一律视为无效
    if data.get("project_id") is None or data.get("user_id") is None:
        raise DownloadTokenError("", params={"reason": "bad_payload"})
    return {
        "object_id": int(data["object_id"]),
        "kind": data.get("kind"),
        "project_id": int(data["project_id"]),
        "user_id": int(data["user_id"]),
    }


# ---------------------------------------------------------------------------
# 项目包导出(架构宪法 §12、domain-model §对象生命周期)
# ---------------------------------------------------------------------------


class PackageExport:
    """项目包导出结果(对象记录 + 下载授权 + 清单)。"""

    __slots__ = (
        "object_id",
        "oid",
        "size_bytes",
        "media_type",
        "file_name",
        "manifest",
        "token",
        "expires_at",
    )

    def __init__(
        self,
        *,
        object_id: int,
        oid: str,
        size_bytes: int,
        media_type: str,
        file_name: str,
        manifest: dict,
        token: str,
        expires_at: datetime,
    ) -> None:
        self.object_id = object_id
        self.oid = oid
        self.size_bytes = size_bytes
        self.media_type = media_type
        self.file_name = file_name
        self.manifest = manifest
        self.token = token
        self.expires_at = expires_at

    def to_dict(self) -> dict[str, Any]:
        """API 序列化(不含包内容)。"""
        return {
            "object_id": self.object_id,
            "oid": self.oid,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "file_name": self.file_name,
            "token": self.token,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "manifest": self.manifest,
        }


def bound_dataset_ids(content: dict) -> list[int]:
    """从项目内容取出绑定的数据集版本 id 清单。"""
    return [
        int(binding["dataset_version_id"])
        for binding in content.get("dataset_bindings", [])
        if binding.get("dataset_version_id") is not None
    ]










# ---------------------------------------------------------------------------
# 项目包导入(校验 → 提案 → 确认; 每次导入创建新项目身份，domain-model §对象生命周期/§快照、任务和结果)
# ---------------------------------------------------------------------------


def parse_package(data: bytes) -> tuple[dict, dict[str, bytes]]:
    """解析项目包 zip: (manifest, {entry_path: bytes}); 校验失败抛 ImportValidationError。

    前置门禁(任何解压读取之前执行, 防 ZIP Bomb 内存/CPU 耗尽):
    - 上传字节上限(MAX_PACKAGE_BYTES, 2GB);
    - zip 文件头与条目数预检(MAX_PACKAGE_ENTRIES, 5000);
    - 单条目解压大小(按 zip 头声明的 file_size 预检, MAX_PACKAGE_ENTRY_BYTES);
    - 总解压大小上限(MAX_PACKAGE_TOTAL_BYTES)。
    超限抛 PackageSizeError(PKG-SIZE-001, 413)。

    校验项:
    - 格式: 合法 zip, 无路径穿越条目;
    - 兼容性: 主版本号与当前格式兼容(1.x);
    - 清单: manifest.json 存在, package_type=project, 无账号/权限/会话/全局配置/密钥;
    - 完整性: 对象清单与包内文件一一对应 + 大小一致(传输完整性由 zip 条目 CRC 承担,
      不做内容摘要比对);
    - 必需文件: project.json / draft.json 存在。
    """
    reasons: list[str] = []
    if len(data) > MAX_PACKAGE_BYTES:
        raise PackageSizeError(
            "",
            params={"reason": "package_too_large", "max_bytes": MAX_PACKAGE_BYTES, "actual_bytes": len(data)},
        )
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ImportValidationError(["文件不是合法的 zip 项目包"]) from exc
    with zf:
        # 条目数与单条目/总解压大小预检(zip 头部声明值, 不做真实解压)
        infos = zf.infolist()
        if len(infos) > MAX_PACKAGE_ENTRIES:
            raise PackageSizeError(
                "",
                params={
                    "reason": "too_many_entries",
                    "max_entries": MAX_PACKAGE_ENTRIES,
                    "actual_entries": len(infos),
                },
            )
        total_uncompressed = 0
        for info in infos:
            if info.is_dir():
                continue
            if info.file_size > MAX_PACKAGE_ENTRY_BYTES:
                raise PackageSizeError(
                    "",
                    params={
                        "reason": "entry_too_large",
                        "entry": info.filename,
                        "max_bytes": MAX_PACKAGE_ENTRY_BYTES,
                        "actual_bytes": info.file_size,
                    },
                )
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_PACKAGE_TOTAL_BYTES:
                raise PackageSizeError(
                    "",
                    params={
                        "reason": "total_uncompressed_too_large",
                        "max_bytes": MAX_PACKAGE_TOTAL_BYTES,
                        "actual_bytes": total_uncompressed,
                    },
                )
        entries: dict[str, bytes] = {}
        for info in infos:
            if info.is_dir():
                continue
            name = info.filename
            if name.startswith("/") or ".." in name.split("/"):
                reasons.append(f"条目路径非法(路径穿越): {name}")
                continue
            entries[name] = zf.read(info)
    if reasons:
        raise ImportValidationError(reasons)
    if "manifest.json" not in entries:
        raise ImportValidationError(["包内缺少 manifest.json 清单"])
    try:
        manifest = json.loads(entries["manifest.json"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ImportValidationError(["manifest.json 无法解析"]) from exc
    if not isinstance(manifest, dict):
        raise ImportValidationError(["manifest.json 结构非法"])

    # 兼容性: 格式主版本
    format_version = manifest.get("format_version")
    if not isinstance(format_version, str) or not format_version.split(".", 1)[0] == "1":
        reasons.append(f"格式版本不兼容: {format_version!r}, 期望 1.x")
    if manifest.get("package_type") != "project":
        reasons.append(f"包类型非法: {manifest.get('package_type')!r}, 期望 project")
    # 禁止携带账号/权限/会话/全局配置/密钥
    forbidden = FORBIDDEN_MANIFEST_KEYS & set(manifest)
    if forbidden:
        reasons.append(f"包包含禁止内容({', '.join(sorted(forbidden))}), 拒绝导入")

    # 完整性: 对象清单与包内文件一一对应 + 大小一致
    objects_manifest = manifest.get("objects")
    if not isinstance(objects_manifest, list):
        reasons.append("清单缺少 objects 对象清单")
    else:
        seen_paths: set[str] = set()
        for entry in objects_manifest:
            if not isinstance(entry, dict):
                reasons.append("对象清单条目非法")
                continue
            path = entry.get("path")
            expected_size = entry.get("size_bytes")
            if not isinstance(path, str) or path not in entries:
                reasons.append(f"对象清单条目缺失文件: {path}")
                continue
            if path in seen_paths:
                reasons.append(f"对象清单重复条目: {path}")
                continue
            seen_paths.add(path)
            actual = entries[path]
            if not isinstance(expected_size, int) or len(actual) != expected_size:
                reasons.append(f"对象大小不符: {path}")
        # 反向: 包内文件(除清单)必须全部在对象清单中
        for path in entries:
            if path == "manifest.json" or path in seen_paths:
                continue
            reasons.append(f"包内文件未在对象清单中: {path}")

    for required in REQUIRED_PACKAGE_FILES:
        if required not in entries:
            reasons.append(f"缺少必需文件: {required}")
    if reasons:
        raise ImportValidationError(reasons)
    return manifest, entries




def parse_config_files(entries: dict[str, bytes], manifest: dict) -> dict:
    """解析包内财务三件套/规划配置 YAML(0.6.5 条目 1-2), 严格校验。

    返回 {"profile": FinanceProfile, "overrides": FinanceOverrides,
          "effective": EffectiveFinanceConfig, "planning": PlanningConfig | None};
    包未携带配置时返回 {}(导入后项目无配置, 不静默默认)。

    校验(任一失败 → ImportValidationError, 拒绝整个导入):
    - files.configs 声明的路径必须存在且为合法安全 YAML(yamlmini 子集);
    - 三件套必须齐全(Profile + Overrides + Effective 一并导入, 缺一拒绝);
    - 内容严格恢复(FinanceProfile/FinanceOverrides/EffectiveFinanceConfig/
      PlanningConfig.from_dict: 拒未知/缺失字段);
    - Overrides 对 Profile 结构校验(profile_ref 匹配、只许既有叶子、
      禁改单位/carrier/direction/tax、禁新增 finance_type/price_id);
    对象字节完整性由 _parse_package 的对象清单一一对应 + 大小校验承担
    (外部包入口边界); 领域层不做本地内容重算比对(2.6)。
    """
    files_meta = manifest.get("files") or {}
    configs_meta = files_meta.get("configs") or {}
    if not configs_meta:
        return {}
    if not isinstance(configs_meta, dict):
        raise ImportValidationError(["清单 files.configs 结构非法(期望映射)"])
    reasons: list[str] = []

    profile_path = configs_meta.get("finance_profile")
    overrides_path = configs_meta.get("finance_overrides")
    effective_path = configs_meta.get("effective_finance")
    planning_path = configs_meta.get("planning_config")
    required = {
        "finance_profile": profile_path,
        "finance_overrides": overrides_path,
        "effective_finance": effective_path,
    }
    for field, path in required.items():
        if path is None:
            reasons.append(f"清单 files.configs 缺少 {field} 条目")
    if planning_path is not None and profile_path is None:
        reasons.append("包内携带规划配置但缺少财务三件套(规划必须引用已生成的有效财务快照)")
    if reasons:
        raise ImportValidationError(reasons)

    def _load_yaml(package_field: str, path: str) -> dict:
        if not isinstance(path, str) or path not in entries:
            raise ImportValidationError([f"清单 files.configs.{package_field} 指向的包内文件缺失: {path}"])
        try:
            from iesplan.core.yamlmini import load as yaml_load

            doc = yaml_load(entries[path].decode("utf-8"))
        except Exception as exc:
            raise ImportValidationError([f"包内 {path} 无法解析为安全 YAML: {exc}"]) from exc
        if not isinstance(doc, dict):
            raise ImportValidationError([f"包内 {path} 结构非法(期望对象)"])
        return doc

    profile: FinanceProfile | None = None
    overrides: FinanceOverrides | None = None
    effective: EffectiveFinanceConfig | None = None
    try:
        profile = FinanceProfile.from_dict(_load_yaml("finance_profile", profile_path))
    except FinanceTripletError as exc:
        reasons.append(f"包内 FinanceProfile 非法: {exc}")
    try:
        overrides = FinanceOverrides.from_dict(
            _load_yaml("finance_overrides", overrides_path), profile=profile
        )
    except FinanceTripletError as exc:
        reasons.append(f"包内 FinanceOverrides 非法: {exc}")
    try:
        effective = EffectiveFinanceConfig.from_dict(_load_yaml("effective_finance", effective_path))
    except FinanceTripletError as exc:
        reasons.append(f"包内 EffectiveFinanceConfig 非法: {exc}")

    planning: PlanningConfig | None = None
    if planning_path is not None:
        planning_doc = _load_yaml("planning_config", planning_path)
        try:
            planning = PlanningConfig.from_dict(planning_doc)
        except PlanningConfigError as exc:
            reasons.append(f"包内规划配置非法: {exc}")
        else:
            for d in validate_planning_domain(planning):
                reasons.append(f"包内规划配置领域校验失败: {d.params.get('detail') or d.code}")
    if reasons:
        raise ImportValidationError(reasons)
    result: dict = {
        "profile": profile,
        "overrides": overrides,
        "effective": effective,
    }
    if planning is not None:
        result["planning"] = planning
    return result










# ---------------------------------------------------------------------------
# Excel 报告导出(U15/U14, domain-model §快照、任务和结果 /
# contracts §公共文件契约 / REQ-EXPORT-001: 固定模板, 固定引用, 不重新求解)
# ---------------------------------------------------------------------------


def parse_evidence_content(raw: bytes | None) -> dict | None:
    """解析证据对象内容(JSON 字典), 解析失败返回 None。"""
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None














__all__ = [
    "ImportValidationError",
    "DownloadTokenError",
    "PackageExport",
    "PACKAGE_FORMAT_VERSION",
    "DOWNLOAD_TOKEN_TTL_SECONDS",
    "create_download_token",
    "verify_download_token",
    "parse_package",
    "parse_config_files",
    "parse_evidence_content",
    "bound_dataset_ids",
    "media_file_kind",
]
