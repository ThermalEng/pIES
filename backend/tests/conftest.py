"""pytest 全局测试环境配置。

- 固定 SQLite 内存数据库与内存队列/限速(单进程), 避免测试误连部署的
  Postgres/Redis, 并防止登录限速键在共享 Redis 中跨测试文件污染;
- 强制覆盖(非 setdefault): docker compose 会注入 IESPLAN_DB_URL 指向
  Postgres, setdefault 不生效会导致测试连到部署库(脏数据/约束差异);
- 默认初始化设备注册表(RR-P2-02: YAML 设备注册表是事实源, 测试均需消费),
  不初始化即抛 AppError(SYS-CFG-001) 已成常态。
"""

from __future__ import annotations

import os

os.environ["IESPLAN_DB_URL"] = "sqlite+pysqlite://"
os.environ["IESPLAN_QUEUE"] = "memory"

import pytest


@pytest.fixture(autouse=True)
def _init_device_registry():
    """所有测试自动初始化设备注册表(避免单个测试漏写 fixture)。"""
    from iesplan.devices import init_registry
    from iesplan.devices.pricing import load_price_book

    init_registry(book=load_price_book())
    yield