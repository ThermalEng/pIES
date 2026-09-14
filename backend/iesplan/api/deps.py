"""API 公开请求依赖: 会话工厂与请求级数据库会话。

全部业务路由经本模块获取数据库会话, 不再直接绑定 ``iesplan.db`` 的全局
``SessionLocal``:

- ``get_session_factory``: 只读组合根装配结果
  ``request.app.state.bootstrap_context.session_factory``; 未装配或缺失时
  立即显式失败(抛 ``RuntimeError``), 无静默全局回退;
- ``get_request_db``: FastAPI 请求级依赖, 从上述工厂取会话, 请求结束关闭;
- ``DbSession``: ``Annotated[Session, Depends(get_request_db)]`` 别名,
  供各路由复用(与旧 ``Depends(get_db)`` 同形态, 覆盖点改为组合根装配)。
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.orm import Session


def get_session_factory(request: Request) -> Any:
    """公开依赖: 返回组合根装配的会话工厂。

    未装配(``app.state.bootstrap_context`` 缺失)或装配结果无
    ``session_factory`` 时抛 ``RuntimeError``: 显式失败, 不回退全局。
    """
    context = getattr(request.app.state, "bootstrap_context", None)
    factory = getattr(context, "session_factory", None)
    if factory is None:
        raise RuntimeError("API 未装配: bootstrap_context.session_factory 缺失, 拒绝提供数据库会话")
    return factory


def get_request_db(request: Request) -> Generator[Session, None, None]:
    """FastAPI 依赖: 提供请求级数据库会话, 请求结束自动关闭。

    会话来自组合根装配的 ``session_factory``(见 ``get_session_factory``);
    未装配时显式失败, 不使用任何全局会话工厂。
    """
    session = get_session_factory(request)()
    try:
        yield session
    finally:
        session.close()


#: 路由复用的请求会话注解(形态同旧 Depends(get_db), 覆盖点为组合根装配)。
DbSession = Annotated[Session, Depends(get_request_db)]
