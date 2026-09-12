"""解耦重构切片 5: configuration / tasks / results 域 persistence 与 services 迁移测试。

- 直接覆盖 `iesplan.configuration` 门面:
  Profile 登记/按行读取、Overrides/Effective/Planning revision 追加与当前值;
- 直接覆盖 `iesplan.tasks` 门面: 快照/任务/尝试/租约创建与读取(含 fencing 取租约);
- 覆盖迁移后的 `iesplan.services.config_revisions` 读面(经 configuration 域);
- 覆盖迁移后的 `iesplan.services.tasks` 归属校验(经 tasks 域);
- 覆盖迁移后的 `iesplan.services.results` 结果写入面(经 results/tasks 域):
  证据提交(fencing)→四维评估→索引转交(同证据挂接/新证据转交)→选中→归档读取;
- package 归档读取经 results/configuration 域门面(证据清单/评估清单/索引清单/Profile 行)。
运行环境与切片 4 一致: SQLite 内存库 + 临时 data_dir(对象存储)。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from iesplan import configuration as configuration_domain
from iesplan import project as project_domain
from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.config import settings
from iesplan.core.errors import NotFoundError
from iesplan.db import Base
from iesplan.services import config_revisions as config_service
from iesplan.services import results as results_service
from iesplan.services import tasks as tasks_service
from iesplan.storage import put_object


@pytest.fixture()
def engine() -> Iterator[sa.Engine]:
    eng = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine: sa.Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture(autouse=True)
def _clean_tables(engine: sa.Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(settings, "data_dir", d)
    return d


def _store(db: Session, payload: dict, category: str = "test") -> int:
    obj = put_object(db, json.dumps(payload).encode("utf-8"), "application/json", source_category=category)
    return obj.id


def _make_project(db: Session, name: str = "slice5-proj"):
    return project_domain.create_project(db, name=name, owner_id=7, created_by=7)


def _make_version(db: Session, project_id: int, name: str = "v1") -> int:
    obj_id = _store(db, {"draft": True}, "version_content")
    version = project_domain.create_version(
        db,
        project_id=project_id,
        name=name,
        reason="slice5",
        created_by=7,
        content_object_id=obj_id,
    )
    return version.id


def _make_snapshot(db: Session, version_id: int) -> int:
    snapshot = tasks_domain.create_snapshot(
        db,
        project_version_id=version_id,
        dataset_version_ids=[],
        calc_config_snapshot={},
        random_seed=42,
        created_by=7,
    )
    return snapshot.id


def _make_task(db: Session, project_id: int, snapshot_id: int | None = None) -> int:
    task = tasks_domain.create_task(
        db, project_id=project_id, type="calc", requested_by=7, calc_snapshot_id=snapshot_id
    )
    return task.id


def _claim(db: Session, task_id: int) -> tuple[int, str]:
    attempt = tasks_domain.create_attempt(db, task_id=task_id)
    token = str(uuid4())
    tasks_domain.acquire_lease(db, attempt_id=attempt.id, lease_token=token)
    return attempt.id, token


def _evidence_payload(snapshot_id: int, hourly_object_id: int, *, seed: int = 42) -> dict:
    content = {
        "algorithm": "slice5",
        "seed": seed,
        "stop_condition": {"status": "OPTIMAL", "gap_threshold_pct": 0.1},
        "solve": {"solver_status": "OPTIMAL", "gap": 0.01, "feasible": True},
        "candidate_indices": [0],
        "metrics": {"npv": 1.0},
        "hourly_refs": [{"object_id": hourly_object_id, "fields": ["p"], "rows": 2}],
        "residuals": {
            "all_passed": True,
            "items": [{"name": "energy", "passed": True, "normalized": 0.0, "tol": 1e-6}],
        },
        "constraints": {},
        "financial": {"irr": 0.08, "irr_status": "unique", "npv": 1.0},
        "reliability": {"executed": True, "total_samples": 30, "valid_samples": 30},
        "candidates": [{"index": 0, "capacities": {}, "irr": 0.08, "npv": 1.0}],
        "content": {},
    }
    return {
        "snapshot_id": snapshot_id,
        "algorithm": content["algorithm"],
        "seed": seed,
        "stop_condition": content["stop_condition"],
        "solve": content["solve"],
        "candidate_indices": [0],
        "metrics": content["metrics"],
        "hourly_refs": content["hourly_refs"],
        "content": content,
        "created_by": 7,
    }


# ---------------------------------------------------------------------------
# configuration 域
# ---------------------------------------------------------------------------


def test_configuration_domain_profile_and_revisions(db: Session) -> None:
    project = _make_project(db)
    obj_id = _store(db, {"profile": True}, "profile")

    row = configuration_domain.register_profile(
        db, profile_id="p1", region="R", content={"k": "v"}, object_id=obj_id, created_by=7
    )
    fetched = configuration_domain.get_profile_row(db, row.id)
    assert fetched is not None and fetched.content == {"k": "v"}
    assert configuration_domain.get_profile_row(db, 999999) is None

    configuration_domain.append_overrides(
        db, project_id=project.id, revision=1, content={"o": 1}, profile_id="p1", created_by=7
    )
    current = configuration_domain.get_current_overrides(db, project.id)
    assert current is not None and current.revision == 1
    assert configuration_domain.next_overrides_revision(db, project.id) == 2

    configuration_domain.append_effective(
        db, project_id=project.id, revision=1, content={"e": 1}, profile_id="p1", created_by=7
    )
    effective = configuration_domain.get_current_effective(db, project.id)
    assert effective is not None and effective.revision == 1

    configuration_domain.append_planning(
        db, project_id=project.id, revision=1, content={"pl": 1}, created_by=7
    )
    planning = configuration_domain.get_current_planning(db, project.id)
    assert planning is not None and planning.revision == 1


def test_config_service_lists_profiles_through_domain(db: Session) -> None:
    obj_id = _store(db, {"profile": True}, "profile")
    configuration_domain.register_profile(
        db, profile_id="p9", region="R9", content={}, object_id=obj_id, created_by=7
    )
    items = config_service.list_finance_profiles(db)
    assert any(item["profile_id"] == "p9" and item["region"] == "R9" for item in items)
    assert config_service.get_finance_overrides(db, _make_project(db, "slice5-empty").id) == (
        None,
        None,
    )


# ---------------------------------------------------------------------------
# tasks 域
# ---------------------------------------------------------------------------


def test_tasks_domain_snapshot_task_attempt_lease(db: Session) -> None:
    project = _make_project(db)
    version_id = _make_version(db, project.id)
    snapshot_id = _make_snapshot(db, version_id)
    assert tasks_domain.get_snapshot(db, snapshot_id) is not None

    task_id = _make_task(db, project.id, snapshot_id)
    task = tasks_domain.get_task(db, task_id)
    assert task is not None and task.calc_snapshot_id == snapshot_id
    assert [t.id for t in tasks_domain.list_tasks(db, project.id)] == [task_id]

    attempt_id, token = _claim(db, task_id)
    assert tasks_domain.get_attempt(db, attempt_id) is not None
    lease = tasks_domain.get_lease_by_token(db, token)
    assert lease is not None and lease.attempt_id == attempt_id and lease.status == "active"
    assert tasks_domain.get_lease_by_token(db, "not-a-token") is None
    assert tasks_domain.get_lease_by_token(db, str(uuid4())) is None


def test_tasks_service_belongs_through_domain(db: Session) -> None:
    project = _make_project(db)
    other = _make_project(db, "slice5-other")
    task_id = _make_task(db, project.id)
    task = tasks_service.ensure_task_belongs(db, project.id, task_id)
    assert task.id == task_id
    with pytest.raises(NotFoundError):
        tasks_service.ensure_task_belongs(db, other.id, task_id)
    with pytest.raises(NotFoundError):
        tasks_service.ensure_task_belongs(db, project.id, 999999)


# ---------------------------------------------------------------------------
# results 域 + services.results 写入面
# ---------------------------------------------------------------------------


def _submit(db: Session, task_id: int, snapshot_id: int):
    hourly_id = _store(db, {"hourly": True}, "hourly")
    attempt_id, token = _claim(db, task_id)
    payload = _evidence_payload(snapshot_id, hourly_id)
    package = results_service.submit_evidence(db, task_id, attempt_id, token, payload)
    assert package.status == "complete"
    assert package.task_id == task_id and package.attempt_id == attempt_id
    return package


def test_results_submit_assess_index_select_flow(db: Session, data_dir: Path) -> None:
    project = _make_project(db)
    version_id = _make_version(db, project.id)
    snapshot_id = _make_snapshot(db, version_id)
    task_id = _make_task(db, project.id, snapshot_id)

    package = _submit(db, task_id, snapshot_id)
    assert results_domain.get_evidence(db, package.id) is not None
    assert results_domain.latest_evidence_for_task(db, task_id).id == package.id
    assert results_service.latest_evidence(db, task_id).id == package.id

    assessment = results_service.run_assessment(db, package.id)
    assert assessment.evidence_package_id == package.id
    assert assessment.assessor == "system"
    assert assessment.detail["checked"] == ["physical", "optimality", "financial", "reliability"]
    assert results_domain.latest_assessment(db, package.id).id == assessment.id

    view = results_service.assessment_to_dict(db, assessment)
    assert view["id"] == assessment.id and view["evidence_package_id"] == package.id
    assert set(view["dimensions"]) == {"physical", "optimality", "financial", "reliability"}
    assert view["created_at"] == assessment.created_at

    history = results_service.list_assessments(db, task_id)
    assert [a.id for a in history] == [assessment.id]

    # 同证据挂接: 不新增索引行, 只更新评估指针
    index = results_service.update_result_index(db, task_id, assessment.id)
    assert index.evidence_package_id == package.id and index.assessment_id == assessment.id
    assert results_service.latest_index(db, tasks_domain.get_task(db, task_id)).id == index.id

    assessment2 = results_service.run_assessment(db, package.id, assessment_type="physical")
    index2 = results_service.update_result_index(db, task_id, assessment2.id)
    assert index2.id == index.id and index2.assessment_id == assessment2.id

    # 新证据发布: 转交最新标记并插入新行
    package2 = _submit(db, task_id, snapshot_id)
    assessment3 = results_service.run_assessment(db, package2.id)
    index3 = results_service.update_result_index(db, task_id, assessment3.id)
    assert index3.id != index.id and index3.evidence_package_id == package2.id
    assert results_domain.get_index(db, index.id).is_latest is False
    assert results_domain.latest_index_for_version(db, index3.project_version_id).id == index3.id

    # 选中追加式(仅消费 user.id; 归属与角色判定经 project/identity 域)
    user = SimpleNamespace(id=7)
    selection = results_service.select_result(db, user, task_id, 0, "adopt", reason="slice5")
    assert selection.is_current is True
    assert results_service.current_selection(db, project.id).id == selection.id
    diff = results_service.selection_diff(db, project.id)
    assert diff is not None and diff["solution_id"] == 0


def test_results_package_archive_reads_through_domain(db: Session, data_dir: Path) -> None:
    project = _make_project(db)
    version_id = _make_version(db, project.id)
    snapshot_id = _make_snapshot(db, version_id)
    task_id = _make_task(db, project.id, snapshot_id)
    package = _submit(db, task_id, snapshot_id)
    assessment = results_service.run_assessment(db, package.id)
    results_service.update_result_index(db, task_id, assessment.id)

    packages = results_domain.list_evidence_for_tasks(db, tasks_domain.list_task_ids(db, project.id))
    assert [p.id for p in packages] == [package.id]
    assert tasks_domain.list_task_ids(db, 999999) == []
    assert results_domain.list_evidence_for_tasks(db, []) == []
    assert [a.id for a in results_domain.list_package_assessments(db, package.id)] == [assessment.id]
    assert [r.evidence_package_id for r in results_domain.list_package_index(db, package.id)] == [package.id]

    obj_id = _store(db, {"profile": True}, "profile")
    configuration_domain.register_profile(
        db, profile_id="pp", region="R", content={"k": "v"}, object_id=obj_id, created_by=7
    )
    profile_row = configuration_domain.get_profile_row(
        db,
        configuration_domain.register_profile(
            db, profile_id="pp2", region="R", content={"k2": "v2"}, object_id=obj_id, created_by=7
        ).id,
    )
    assert profile_row is not None and profile_row.content == {"k2": "v2"}
