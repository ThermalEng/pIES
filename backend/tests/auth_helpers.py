"""测试共享认证辅助: 创建带凭证用户 + 真实窗口会话登录。

业务 API 已统一改为窗口会话认证(iesplan.api.auth, Bearer/Cookie),
测试以 POST /api/auth/login 获取窗口凭证, 不再使用 X-User-Id 模拟头。

注意: 每用户单活动窗口(重复登录会使旧凭证降级 takeover_pending),
因此 login_headers 在同一 TestClient 实例内按用户缓存 token。
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from iesplan.models.identity import User
from iesplan.services import identity

#: 测试用户统一密码(与 make_user 创建的凭证一致)
DEFAULT_PASSWORD = "Test12345"


def make_user(
    db: Session, username: str, role: str = "engineer", password: str = DEFAULT_PASSWORD
) -> User:
    """创建带密码凭证的测试用户(首登不强制改密, 可真实登录)。"""
    user = identity.create_user(
        db, username, password, role=role, force_password_change=False, display_name=username
    )
    user._test_password = password  # type: ignore[attr-defined]  # 测试辅助, 不入库
    return user


def login_headers(client: TestClient, user: User) -> dict[str, str]:
    """以窗口会话登录并返回 Bearer 头(同一 client 内按用户缓存)。

    缓存避免同用户重复登录触发单活动窗口接管(旧凭证立即失效)。
    """
    try:
        cache: dict[int, str] = client._auth_tokens  # type: ignore[attr-defined]
    except AttributeError:
        cache = client._auth_tokens = {}  # type: ignore[attr-defined]
    token = cache.get(user.id)
    if token is None:
        password = getattr(user, "_test_password", DEFAULT_PASSWORD)
        resp = client.post(
            "/api/auth/login", json={"username": user.username, "password": password}
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["token"]
        cache[user.id] = token
    return {"Authorization": f"Bearer {token}"}
