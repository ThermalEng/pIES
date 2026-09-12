"""身份域 repository 协议（users/roles/user_roles/credentials/window_sessions/app_settings/auth_events）。

实现规则（切片 4 落实）：
- 只做查询、写入、flush，必要时用 savepoint；绝不 commit/rollback；
- secret_hash/cost_params/session_token 明文永不经本协议进出；
  会话按 token 哈希查找，调用方先哈希（哈希算法归属见 services.identity 现状，
  切片 4 迁移时收敛到本域内部函数，不经过公开契约）；
- 密码验算、登录策略等规则不在 repository，在领域服务/application。
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.orm import Session

from iesplan.identity.contracts import (
    AppSettingRecord,
    AuthEventRecord,
    CredentialRecord,
    RoleRecord,
    UserRecord,
    UserRoleRecord,
    WindowSessionRecord,
)


class IdentityRepository(Protocol):
    """身份聚合 repository 协议（无状态方法组，db 由调用方事务拥有）。"""

    def get_user(self, db: Session, user_id: int) -> UserRecord | None: ...

    def get_user_by_username(self, db: Session, username: str) -> UserRecord | None: ...

    def get_user_by_email(self, db: Session, email: str) -> UserRecord | None: ...

    def list_users(self, db: Session) -> list[UserRecord]: ...

    def create_user(
        self,
        db: Session,
        *,
        username: str,
        display_name: str,
        email: str | None = None,
        status: str = "active",
        is_system: bool = False,
        public_namespace: str | None = None,
    ) -> UserRecord:
        """创建用户裸行（校验/哈希/授权由调用方先完成）；重名/重复邮箱/命名空间重复抛错。"""
        ...

    def get_user_by_namespace(self, db: Session, namespace: str) -> UserRecord | None:
        """按公开命名空间取用户；不存在返回 None。"""
        ...

    def set_user_status(self, db: Session, user_id: int, status: str) -> UserRecord: ...

    def set_public_namespace(self, db: Session, user_id: int, namespace: str) -> UserRecord:
        """分配公开命名空间（终身一次；重复分配抛 IdentityConflictError）。"""
        ...

    def touch_login(self, db: Session, user_id: int) -> UserRecord:
        """更新最后登录时间与凭证版本推进以外的时间戳。"""
        ...

    def get_active_credential(self, db: Session, user_id: int) -> CredentialRecord | None:
        """取用户当前有效密码凭证元数据（无哈希）。"""
        ...

    def get_active_password_secret(self, db: Session, user_id: int) -> str | None:
        """取用户当前有效密码哈希（仅供验算；永不装入 DTO、不落日志）。"""
        ...

    def bump_credential_version(self, db: Session, user_id: int) -> UserRecord:
        """递增用户凭证版本（改密/重置时使旧会话失效）并刷新 updated_at。"""
        ...

    def add_credential(
        self,
        db: Session,
        *,
        user_id: int,
        credential_type: str,
        secret_hash: str,
        algorithm: str | None = None,
        strength_score: int = 0,
        requires_change: bool = True,
        created_by: int | None = None,
        rotated_at: str | None = None,
    ) -> CredentialRecord:
        """写入新凭证行（哈希由领域服务生成后传入，repository 不做密码学）。"""
        ...

    def revoke_credentials(
        self, db: Session, user_id: int, *, credential_type: str = "password"
    ) -> int:
        """吊销用户指定类型的有效凭证（默认仅密码凭证，与旧 services 语义一致），返回行数。"""
        ...

    def get_user_by_auth_subject(self, db: Session, subject: str) -> UserRecord | None:
        """按外部主体(sub)取已绑定用户；未绑定返回 None。"""
        ...

    def bind_auth_subject(self, db: Session, user_id: int, subject: str) -> UserRecord:
        """绑定外部主体（唯一键；冲突抛 IdentityConflictError，调用方回滚）。"""
        ...

    def ensure_role(self, db: Session, code: str, name: str) -> RoleRecord:
        """取或建全局角色（并发建重以 savepoint 化为读取）。"""
        ...

    def user_roles(self, db: Session, user_id: int) -> list[str]:
        """列出用户当前有效角色 code。"""
        ...

    def grant_role(self, db: Session, *, user_id: int, role_id: int, granted_by: int) -> UserRoleRecord: ...

    def revoke_role(self, db: Session, *, user_id: int, role_id: int, revoked_by: int) -> None: ...

    def get_session(self, db: Session, session_id: int) -> WindowSessionRecord | None: ...

    def get_session_by_token_hash(self, db: Session, token_hash: str) -> WindowSessionRecord | None:
        """按 token 哈希取会话（本方法只接受哈希，不接受明文 token）。"""
        ...

    def list_active_sessions(
        self, db: Session, user_id: int, *, exclude_session_id: int | None = None
    ) -> list[WindowSessionRecord]:
        """用户非终态会话（active/takeover_pending），按 id 升序。"""
        ...

    def create_session(
        self,
        db: Session,
        *,
        user_id: int,
        token_hash: str,
        credential_version_at_issue: int,
        expires_at: str,
        status: str = "active",
    ) -> WindowSessionRecord: ...

    def set_session_status(
        self,
        db: Session,
        session_id: int,
        status: str,
        *,
        revoked_by: int | None = None,
        replaced_by_session_id: int | None = None,
    ) -> WindowSessionRecord: ...

    def extend_session(
        self, db: Session, session_id: int, *, expires_at: str
    ) -> WindowSessionRecord: ...

    def heartbeat_session(self, db: Session, session_id: int) -> WindowSessionRecord:
        """刷新会话 last_seen（高频调用，只 flush 不提交）。"""
        ...

    def get_app_setting(self, db: Session, key: str) -> AppSettingRecord | None: ...

    def set_app_setting(
        self, db: Session, key: str, value: dict[str, Any], updated_by: int | None = None
    ) -> AppSettingRecord: ...

    def record_auth_event(
        self,
        db: Session,
        *,
        event_type: str,
        user_id: int | None = None,
        session_id: int | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuthEventRecord:
        """追加认证审计事件（仅 INSERT；ip 由调用方先白名单化）。"""
        ...
