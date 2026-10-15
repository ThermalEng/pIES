"""手动重试 PG 回归: 终态触发器放行授权重试, 其余终态迁出仍阻断。

默认跳过; 设置 IESPLAN_TEST_PG_URL(指向隔离 disposable PG)即运行。
conftest 把默认引擎钉死在 sqlite, 本文件自建 PG 引擎。每个测试创建唯一
测试专属 schema(固定前缀 + 随机后缀), 建表与真实生产触发器部署均经该
schema 的 search_path 落入其中; 结束只删该专属 schema, 从不碰 public 或
其他既有 schema。任务准备复用 test_tasks_api 的公开辅助, 不复制应用流程。
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

PG_URL = os.environ.get("IESPLAN_TEST_PG_URL")

pytestmark = pytest.mark.skipif(not PG_URL, reason="需 IESPLAN_TEST_PG_URL 指向隔离 PG 空库")

#: 本文件专属 schema 前缀(避开 PG 保留的 pg_ 前缀)；每次测试生成唯一后缀，
#: 清理只删该专属 schema。
OWN_SCHEMA_PREFIX = "rt_retry_"


def _owned_schema() -> str:
    """生成本测试拥有的 schema 名(固定前缀 + 随机后缀，仅安全字符)。"""
    name = f"{OWN_SCHEMA_PREFIX}{uuid.uuid4().hex[:8]}"
    assert re.fullmatch(r"rt_retry_[0-9a-f]{8}", name) is not None
    return name


# 注册全部领域表(create_all 需要被引表; 与组合根加载范围一致)
import test_tasks_api as T  # noqa: E402
from auth_helpers import login_headers, make_user  # noqa: E402

import iesplan.audit.persistence  # noqa: E402,F401
import iesplan.configuration.persistence  # noqa: E402,F401
import iesplan.dataset.persistence  # noqa: E402,F401
import iesplan.identity.persistence  # noqa: E402,F401
import iesplan.model.persistence  # noqa: E402,F401
import iesplan.package.persistence  # noqa: E402,F401
import iesplan.project.persistence  # noqa: E402,F401
import iesplan.results.persistence  # noqa: E402,F401
import iesplan.storage.persistence  # noqa: E402,F401
from iesplan import tasks as tasks_domain  # noqa: E402
from iesplan.api import projects as projects_api  # noqa: E402
from iesplan.api import tasks as tasks_api  # noqa: E402
from iesplan.api.config import config_router  # noqa: E402
from iesplan.api.deps import get_request_db  # noqa: E402
from iesplan.bootstrap import collect_trigger_statements  # noqa: E402
from iesplan.config import settings  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.main import create_app  # noqa: E402
from iesplan.tasks import queue  # noqa: E402
from iesplan.tasks.persistence import CalcSnapshot  # noqa: E402


def _deploy_production_triggers(eng: Engine) -> None:
    """经本测试专属 schema 的 search_path 部署真实生产触发器(不可变与终态)。"""
    stmts = [s for s in collect_trigger_statements() if s and s.strip()]
    assert stmts, "生产触发器定义为空，拒绝在无触发器库上伪装验证"
    with eng.begin() as conn:
        for stmt in stmts:
            conn.execute(sa.text(stmt))


@pytest.fixture()
def pg_client(tmp_path: Path) -> Iterator[tuple[TestClient, Session]]:
    """每测隔离 PG 客户端：创建唯一专属 schema 并经其 search_path 建表/装触发器。

    结束只删该专属 schema，从不碰 public 或其他既有 schema；依赖覆盖到同一
    会话，对象存储指临时目录。
    """
    assert PG_URL and PG_URL.startswith("postgresql")
    schema = _owned_schema()
    eng = create_engine(str(PG_URL), connect_args={"options": f"-csearch_path={schema}"})
    with eng.begin() as conn:
        conn.execute(sa.text(f"CREATE SCHEMA {schema}"))
    try:
        Base.metadata.create_all(eng)
        _deploy_production_triggers(eng)
        factory = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
        db = factory()
        settings.data_dir = tmp_path
        queue.force_memory()
        app = create_app()
        app.include_router(projects_api.router)
        app.include_router(tasks_api.router)
        app.include_router(config_router)
        app.dependency_overrides[get_request_db] = lambda: db
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, db
        db.close()
    finally:
        # 只删本测试创建的专属 schema。
        with eng.begin() as conn:
            conn.execute(sa.text(f"DROP SCHEMA {schema} CASCADE"))
        eng.dispose()


def _submit(client: TestClient, db: Session, user, key: str) -> tuple[int, int, int, str]:
    """提交优化任务，返回(项目 id, 任务 id, 快照 id, 幂等键)。"""
    pid = T._prepare_project(client, db, user)
    rev = T._save_config(client, pid, user)
    code, body = T._submit_task(client, pid, user, config={"config_revision": rev}, idempotency_key=key)
    assert code in (200, 201), body
    task = body["task"]
    return pid, task["id"], task["calc_snapshot_id"], task["idempotency_key"]


def _retry(client: TestClient, pid: int, task_id: int, headers: dict) -> tuple[int, dict]:
    resp = client.post(f"/api/projects/{pid}/tasks/{task_id}/retry", headers=headers)
    return resp.status_code, resp.json()


def test_retry_failed_task_pg(pg_client: tuple[TestClient, Session]) -> None:
    """失败任务 PG 重试全链路：200 回到排队、同快照/同幂等键、新尝试编号 2、历史保留。"""
    client, db = pg_client
    owner = make_user(db, "pg_rt_failed")
    headers = login_headers(client, owner)
    pid, task_id, snapshot_id, idem_key = _submit(client, db, owner, "pg-rt-failed")
    T._claim(db, task_id)
    T._fail_task(db, task_id)
    db.commit()
    assert tasks_domain.get_task(db, task_id).status == "failed"
    frozen_before = tasks_domain.get_snapshot(db, snapshot_id)
    assert frozen_before is not None

    code, body = _retry(client, pid, task_id, headers)
    assert code == 200, body
    retried = body["task"]
    assert retried["id"] == task_id
    assert retried["status"] == "queued"
    assert retried["business_outcome"] is None
    assert retried["calc_snapshot_id"] == snapshot_id
    assert retried["idempotency_key"] == idem_key
    # 冻结输入值本身不变，而不仅是快照 id 相同。
    frozen_after = tasks_domain.get_snapshot(db, snapshot_id)
    assert frozen_after is not None
    assert dict(frozen_after.evaluation) == dict(frozen_before.evaluation)
    assert dict(frozen_after.calc_config_snapshot) == dict(frozen_before.calc_config_snapshot)
    assert frozen_after.dataset_version_ids == frozen_before.dataset_version_ids
    assert frozen_after.canonical_assembly_text == frozen_before.canonical_assembly_text

    claim = T._claim(db, task_id, "pg-exec-2")
    assert claim.attempt_no == 2
    attempts = tasks_domain.list_attempts(db, task_id)
    assert [(a.attempt_no, a.status) for a in attempts] == [(1, "failed"), (2, "running")]
    T._complete_task(db, task_id, outcome=tasks_domain.map_business_outcome("TIME_LIMIT_WITH_INCUMBENT"))
    db.commit()
    assert tasks_domain.get_task(db, task_id).business_outcome == "restricted_results"
    snapshots = db.execute(select(CalcSnapshot)).scalars().all()
    assert len(snapshots) == 1  # 快照复用，无新增

    # 已完成任务重试 → 409，无变更
    code, body = _retry(client, pid, task_id, headers)
    assert code == 409
    assert body["error"]["code"] == "TASK-STATE-001"
    done = tasks_domain.get_task(db, task_id)
    assert (done.status, done.business_outcome) == ("completed", "restricted_results")
    assert queue.queue_position(task_id, "compute") is None


@pytest.mark.parametrize("source, first_claim_no", [("timed_out", 2), ("cancelled", 1)])
def test_retry_terminal_states_pg(
    pg_client: tuple[TestClient, Session], source: str, first_claim_no: int
) -> None:
    """超时/已取消任务 PG 重试：200 回到排队、同快照；排队中取消者首领为第 1 次尝试。"""
    client, db = pg_client
    owner = make_user(db, f"pg_rt_{source}")
    headers = login_headers(client, owner)
    pid, task_id, snapshot_id, idem_key = _submit(client, db, owner, f"pg-rt-{source}")
    if source == "timed_out":
        # 超时落终态经权威迁移放置(生产尚无任务级超时写入器，迁移本身合法)；
        # 尝试按约束记失败-超时，不伪造尝试状态枚举。
        T._claim(db, task_id)
        task = tasks_domain.get_task(db, task_id)
        tasks_domain.check_transition(task, "timed_out")
        T._finish_attempt(db, task, status="failed", stop_reason="timeout")
        tasks_domain.set_task_status(db, task_id, "timed_out")
        db.commit()
    else:
        resp = client.post(f"/api/projects/{pid}/tasks/{task_id}/cancel", headers=headers)
        assert resp.status_code == 200
        db.commit()
    assert tasks_domain.get_task(db, task_id).status == source

    code, body = _retry(client, pid, task_id, headers)
    assert code == 200, body
    assert body["task"]["id"] == task_id
    assert body["task"]["status"] == "queued"
    assert body["task"]["calc_snapshot_id"] == snapshot_id
    assert body["task"]["idempotency_key"] == idem_key
    claim = T._claim(db, task_id, "pg-exec-2")
    assert claim.attempt_no == first_claim_no


def test_completed_reopen_blocked_by_trigger_pg(pg_client: tuple[TestClient, Session]) -> None:
    """触发器仍阻断已完成任务的直接迁出：无异常即测试失败，不静默通过。"""
    client, db = pg_client
    owner = make_user(db, "pg_rt_guard")
    pid, task_id, _, _ = _submit(client, db, owner, "pg-rt-guard")
    T._run_task(db, task_id)
    assert tasks_domain.get_task(db, task_id).status == "completed"

    with pytest.raises(sa.exc.DBAPIError, match="终态任务不可迁移状态"):
        tasks_domain.set_task_status(db, task_id, "running")
        db.commit()
    db.rollback()
    assert tasks_domain.get_task(db, task_id).status == "completed"
