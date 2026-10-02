"""应用配置：环境层 + 部署文件层，单一类型化入口 ``Settings``。

- 环境层（``IESPLAN_`` 前缀环境变量）：配置文件路径、秘密、容器编排
  信息（数据库/缓存/对象目录/对外地址/进程角色/认证/调试开关）。
- 部署文件层（TOML，唯一权威）：API 与各 Worker 的非秘密应用运行参数
  （并发槽、会话、存储阈值、限流、配额、求解策略上限、计算表单初值）。
  默认值只在 ``backend/deploy/pies.toml`` 维护，程序内不复制第二份；
  部署者以挂载文件覆盖（``IESPLAN_DEPLOY_CONFIG_FILE`` 指向挂载路径）。

缺文件、非法 TOML、未知节/键或缺少必需项一律在 ``Settings()`` 构造
（各进程启动入口）明确失败，不回退另一套默认值。文件只在该边界解析
一次；模块之间传递已装配的类型化值，不重复校验配置。
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

#: 默认签名密钥(仅限开发环境; 生产必须通过 IESPLAN_SECRET_KEY 覆盖, 见 README)
DEFAULT_DEV_SECRET_KEY = "dev-only-secret-change-me"

#: 部署配置文件路径环境变量(未设置时使用包内默认文件)。
DEPLOY_CONFIG_ENV_VAR = "IESPLAN_DEPLOY_CONFIG_FILE"

#: 包内默认部署配置文件(仓库 backend/deploy/pies.toml，镜像 /app/deploy/pies.toml)。
DEFAULT_DEPLOY_CONFIG_FILE = Path(__file__).resolve().parent.parent / "deploy" / "pies.toml"

#: 允许的 TOML 节与键(未知节/键即非法输入，防拼写漂移)。
_ALLOWED_DEPLOY_SECTIONS: dict[str, frozenset[str]] = {
    "worker": frozenset({"compute_slots"}),
    "session": frozenset({"ttl_minutes"}),
    "storage": frozenset({"min_free_bytes"}),
    "rate_limit": frozenset({"enabled", "window_seconds", "max_requests", "exempt_paths"}),
    "quotas": frozenset({"upload_bytes", "project_bytes"}),
    "solver": frozenset({
        "max_timeout_seconds",
        "max_memory_mib",
        "max_cpu_cores",
        "allow_network",
        "max_output_files",
        "max_output_bytes",
        "allowed_executables",
        "allowed_environment_vars",
    }),
    "calculation": frozenset({"time_limit_seconds"}),
}

#: Settings 应用运行字段 ← (TOML 节, 键)。
_APP_TOML_MAP: tuple[tuple[str, str, str], ...] = (
    ("compute_slots", "worker", "compute_slots"),
    ("session_ttl_minutes", "session", "ttl_minutes"),
    ("storage_min_free_bytes", "storage", "min_free_bytes"),
    ("rate_limit_enabled", "rate_limit", "enabled"),
    ("rate_limit_window_seconds", "rate_limit", "window_seconds"),
    ("rate_limit_max_requests", "rate_limit", "max_requests"),
    ("rate_limit_exempt_paths", "rate_limit", "exempt_paths"),
    ("upload_quota_bytes", "quotas", "upload_bytes"),
    ("project_quota_bytes", "quotas", "project_bytes"),
    ("solver_max_timeout_seconds", "solver", "max_timeout_seconds"),
    ("solver_max_memory_mib", "solver", "max_memory_mib"),
    ("solver_max_cpu_cores", "solver", "max_cpu_cores"),
    ("solver_allow_network", "solver", "allow_network"),
    ("solver_max_output_files", "solver", "max_output_files"),
    ("solver_max_output_bytes", "solver", "max_output_bytes"),
    ("solver_allowed_executables", "solver", "allowed_executables"),
    ("solver_allowed_environment_vars", "solver", "allowed_environment_vars"),
    ("calc_time_limit_seconds", "calculation", "time_limit_seconds"),
)


def _read_deploy_file(path: Path) -> dict[str, dict[str, Any]]:
    """读取并做形状检查的部署 TOML（非法输入直接抛 ValueError）。"""
    if not path.is_file():
        raise ValueError(
            f"部署配置文件缺失: {path}"
            f"(以挂载文件覆盖时设置 {DEPLOY_CONFIG_ENV_VAR})"
        )
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"部署配置文件无法解析: {path}: {exc}") from exc
    unknown_sections = set(data) - set(_ALLOWED_DEPLOY_SECTIONS)
    if unknown_sections:
        raise ValueError(
            f"部署配置文件 {path} 含未知节: {sorted(unknown_sections)}"
        )
    checked: dict[str, dict[str, Any]] = {}
    for section, allowed in _ALLOWED_DEPLOY_SECTIONS.items():
        node = data.get(section) or {}
        if not isinstance(node, dict):
            raise ValueError(f"部署配置文件 {path} 节 [{section}] 须为表")
        unknown_keys = set(node) - set(allowed)
        if unknown_keys:
            raise ValueError(
                f"部署配置文件 {path} 节 [{section}] 含未知键: {sorted(unknown_keys)}"
            )
        checked[section] = node
    return checked


def _required_deploy_value(
    data: dict[str, dict[str, Any]], path: Path, section: str, key: str
) -> Any:
    """取必需部署项；缺失即启动失败，不补默认。"""
    if key not in data[section]:
        raise ValueError(f"部署配置文件 {path} 缺少必需项 [{section}] {key}")
    return data[section][key]


class Settings(BaseSettings):
    """全局配置模型（唯一类型化入口；应用运行值来自部署文件）。"""

    model_config = SettingsConfigDict(env_prefix="IESPLAN_", extra="ignore")

    # --- 环境层：编排 / 秘密 / 进程角色（可由部署环境提供）---
    #: 数据库连接串(SQLAlchemy URL, psycopg 驱动)
    db_url: str = "postgresql+psycopg://iesplan:iesplan_dev_password@localhost:5432/iesplan"
    #: Redis 连接串(队列/心跳/可重建状态)
    redis_url: str = "redis://localhost:6379/0"
    #: 对象存储根目录(对象落盘位置)
    data_dir: Path = Path("/data")
    #: 签名密钥(会话令牌、下载授权 HMAC 等; 生产必须通过 IESPLAN_SECRET_KEY
    #: 覆盖, 默认值仅限开发环境 —— 启动校验见下方 model_validator(C-04))
    secret_key: str = DEFAULT_DEV_SECRET_KEY
    #: 对外服务地址(用于生成链接)
    app_url: str = "http://localhost:8080"
    #: worker 类型: compute(计算) | io(导入导出等 IO 任务)
    worker_type: str = "compute"
    #: 首启种子管理员初始密码(仅首次种子使用, 首登后强制改密)
    default_admin_password: str = "iesplan-admin-initial"
    #: 外部认证提供方: disabled(内置账号) | oidc(OpenID Connect 单点登录)
    auth_provider: str = "disabled"
    #: OIDC 提供方配置(仅 auth_provider=oidc 时生效, 经 IESPLAN_ 前缀环境变量覆盖)
    oidc_discovery_url: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    #: 调试模式(详细日志、异常透出等)
    debug: bool = False
    #: 部署配置文件路径(包内默认文件，或以挂载文件覆盖)
    deploy_config_file: Path = DEFAULT_DEPLOY_CONFIG_FILE

    # --- 应用运行层：部署文件唯一权威（禁环境变量，禁程序内默认）---
    #: 计算并发槽数
    compute_slots: int = Field(exclude=True)
    #: 会话 TTL(分钟)
    session_ttl_minutes: int = Field(exclude=True)
    #: 对象存储最小剩余空间阈值(字节), 低于该值拒绝写入
    storage_min_free_bytes: int = Field(exclude=True)
    #: 全局限流开关
    rate_limit_enabled: bool = Field(exclude=True)
    #: 全局限流窗口(秒); 单 IP 在窗口内超过上限即 429
    rate_limit_window_seconds: int = Field(exclude=True)
    #: 全局限流每 IP 请求上限(窗口内)
    rate_limit_max_requests: int = Field(exclude=True)
    #: 全局限流豁免路径(逗号分隔)
    rate_limit_exempt_paths: str = Field(exclude=True)
    #: 用户上传配额(字节); 0 = 不限
    upload_quota_bytes: int = Field(exclude=True)
    #: 项目级上传配额(字节); 0 = 不限
    project_quota_bytes: int = Field(exclude=True)
    #: 求解单次超时上限(秒)：策略上限，非单次计算默认请求
    solver_max_timeout_seconds: int = Field(exclude=True)
    #: 求解内存允许上限(MiB)：策略上限，非硬件保证
    solver_max_memory_mib: int = Field(exclude=True)
    #: 求解 CPU 允许上限(核)：策略上限，非硬件保证
    solver_max_cpu_cores: int = Field(exclude=True)
    #: 求解是否允许联网(普通 solver 默认断网)
    solver_allow_network: bool = Field(exclude=True)
    #: 声明输出文件数上限
    solver_max_output_files: int = Field(exclude=True)
    #: 声明输出总量上限(字节)
    solver_max_output_bytes: int = Field(exclude=True)
    #: 默认允许的 executable 精确引用
    solver_allowed_executables: tuple[str, ...] = Field(exclude=True)
    #: 允许注入的环境变量名(空 = 不允许任何环境变量)
    solver_allowed_environment_vars: tuple[str, ...] = Field(exclude=True)
    #: 新建计算配置表单 time_limit_seconds 初值(秒)；保存后属项目 revision
    calc_time_limit_seconds: int = Field(exclude=True)

    def __init__(self, **values: Any) -> None:
        raw_path = (
            values.get("deploy_config_file")
            or os.environ.get(DEPLOY_CONFIG_ENV_VAR)
            or DEFAULT_DEPLOY_CONFIG_FILE
        )
        path = Path(raw_path)
        values["deploy_config_file"] = path
        file_values = _read_deploy_file(path)
        for field_name, section, key in _APP_TOML_MAP:
            values.setdefault(
                field_name, _required_deploy_value(file_values, path, section, key)
            )
        try:
            super().__init__(**values)
        except ValidationError as exc:
            raise ValueError(f"部署配置文件非法: {path}: {exc}") from exc

    @property
    def sqlalchemy_url(self) -> str:
        """SQLAlchemy 引擎 URL(与 db_url 一致, 供引擎构造使用)。"""
        return self.db_url

    @model_validator(mode="after")
    def _warn_on_default_secret_key(self) -> Settings:
        """启动校验(C-04): 签名密钥仍为默认开发值时记录 warning 日志。

        不阻断开发环境启动; 生产部署必须通过 IESPLAN_SECRET_KEY 覆盖,
        否则下载授权等 HMAC 签名可被伪造(详见 README 安全部署说明)。
        """
        if self.secret_key == DEFAULT_DEV_SECRET_KEY:
            logger.warning(
                "IESPLAN_SECRET_KEY 仍为默认开发值(%r), 生产环境必须设置强随机密钥, "
                "否则 HMAC 下载授权等签名可被伪造",
                DEFAULT_DEV_SECRET_KEY,
            )
        return self


#: 模块级单例; 由 db.py 等模块直接引用
settings = Settings()
