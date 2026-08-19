"""应用配置(pydantic-settings)。

所有字段均可通过 ``IESPLAN_`` 前缀环境变量覆盖(如 ``IESPLAN_DB_URL``),
与 docker-compose 中 backend/worker 服务的环境变量命名一致。
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

#: 默认签名密钥(仅限开发环境; 生产必须通过 IESPLAN_SECRET_KEY 覆盖, 见 README)
DEFAULT_DEV_SECRET_KEY = "dev-only-secret-change-me"


class Settings(BaseSettings):
    """全局配置模型。"""

    model_config = SettingsConfigDict(env_prefix="IESPLAN_", extra="ignore")

    #: 数据库连接串(SQLAlchemy URL, psycopg 驱动)
    db_url: str = "postgresql+psycopg://iesplan:iesplan_dev_password@localhost:5432/iesplan"
    #: Redis 连接串(队列/心跳/可重建状态)
    redis_url: str = "redis://localhost:6379/0"
    #: 对象存储根目录(内容寻址对象落盘位置)
    data_dir: Path = Path("/data")
    #: 签名密钥(会话令牌、下载授权 HMAC 等; 生产必须通过 IESPLAN_SECRET_KEY
    #: 覆盖, 默认值仅限开发环境 —— 启动校验见下方 model_validator(C-04))
    secret_key: str = DEFAULT_DEV_SECRET_KEY
    #: 对外服务地址(用于生成链接)
    app_url: str = "http://localhost:8080"
    #: worker 类型: compute(计算) | io(导入导出等 IO 任务)
    worker_type: str = "compute"
    #: 计算并发槽数
    compute_slots: int = 2
    #: 任务超时(小时), 超时按 timed_out 处理
    task_timeout_hours: int = 8
    #: 会话 TTL(分钟), 默认 8 小时
    session_ttl_minutes: int = 480
    #: 首启种子管理员初始密码(仅首次种子使用, 首登后强制改密)
    default_admin_password: str = "iesplan-admin-initial"
    #: 对象存储最小剩余空间阈值(字节), 低于该值拒绝写入(2GB 安全阈值)
    storage_min_free_bytes: int = 2000000000
    #: 调试模式(详细日志、异常透出等)
    debug: bool = False

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
