"""pytest 全局测试环境配置。

- 固定 SQLite 内存数据库与内存队列/限速(单进程), 避免测试误连部署的
  Postgres/Redis, 并防止登录限速键在共享 Redis 中跨测试文件污染;
- 强制覆盖(非 setdefault): docker compose 会注入 IESPLAN_DB_URL 指向
  Postgres, setdefault 不生效会导致测试连到部署库(脏数据/约束差异)。
"""

from __future__ import annotations

import os

os.environ["IESPLAN_DB_URL"] = "sqlite+pysqlite://"
os.environ["IESPLAN_QUEUE"] = "memory"
