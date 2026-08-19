"""pytest 全局测试环境配置。

- 固定 SQLite 内存数据库与内存队列/限速(单进程), 避免测试误连部署的
  Postgres/Redis, 并防止登录限速键在共享 Redis 中跨测试文件污染;
- 各测试模块自身的 setdefault 不会覆盖本文件已设置的值(conftest 先导入)。
"""

from __future__ import annotations

import os

os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")
os.environ.setdefault("IESPLAN_QUEUE", "memory")
