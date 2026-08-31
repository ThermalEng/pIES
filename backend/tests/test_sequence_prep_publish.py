"""序列预备事务式发布用例集成测试(0.6.5 事项 3)。

覆盖(modules/application.md「典型示例:预备项目计算序列」):
- data_repeat 模板预备成功: 预备产物对象/回执落盘、模型实例引用原子替换、
  项目草稿 revision 推进、草稿 prepared_sequences 清单闭合;
- 失败严格阻断: 任一接口预备失败不写对象、不替换引用、不推进 revision;
- 幂等替换: 重复发布解绑旧引用并绑定新引用(无引用泄漏);
- 回滚: 显式回滚恢复上一份已验证引用(新增引用解绑、清单恢复);
- data_predict 显式三输入(训练输入/训练目标/预测输入)经应用层解析附着
  文件并发布, 训练产物一并落盘;
- 权限与乐观锁: 非所有者 403, revision 冲突 409。

测试环境: SQLite :memory:(StaticPool 共享连接) + tmp 对象存储目录;
模型实例经 save_project_model 正式保存(编号/内容锁/附着引用为真实路径),
data_predict 的三个显式输入文件按 ``data:{ref}`` 附着(生产路径由后续
上传/绑定流程完成, 本切片只消费附着引用)。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

# 单文件运行安全网: 固定 SQLite, 避免 iesplan.main 启动期误连部署 Postgres
os.environ.setdefault("IESPLAN_DB_URL", "sqlite+pysqlite://")

import numpy as np  # noqa: E402
import pytest  # noqa: E402
from auth_helpers import make_user  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from iesplan.application.projects.model_save import (
    FINAL_OWNER_NAMESPACE,
    DataFileRef,
    save_project_model,
    upload_temp_data_file,
)
from iesplan.application.sequence_prep import (
    PrepPublishRejectedError,
    prepare_prepared_sequences,
    rollback_prepared_sequences,
)
from iesplan.config import settings  # noqa: E402
from iesplan.core import yamlmini  # noqa: E402
from iesplan.core.errors import ConflictError, ForbiddenError  # noqa: E402
from iesplan.db import Base  # noqa: E402
from iesplan.devices import content_sha256, parse_device_model_v2  # noqa: E402
from iesplan.models.identity import User  # noqa: E402
from iesplan.models.project import Project  # noqa: E402
from iesplan.models.project_model import ProjectModel  # noqa: E402
from iesplan.services import project as project_service  # noqa: E402
from iesplan.storage import attach, find_refs_by_owner, get_object, put_object  # noqa: E402

#: data_repeat 模型(与 test_project_model_save 同构; 最终保存后附带 _N 后缀)
DATA_REPEAT_MODEL_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.profile_load, names: {zh-CN: 曲线负荷, en-US: Profile Load}}
properties: {}
interfaces:
  electric_demand:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: 1000}
    source: {mode: data_repeat, data_ref: load_data}
equations: {variables: {}, relations: []}
"""

#: data_predict 模型(显式训练输入/训练目标/预测输入由应用层按 data:{ref} 解析)
DATA_PREDICT_MODEL_YAML = """
schema: ies.device-model
schema_version: "2.0.0"
device: {id: acme.device.pred_load, names: {zh-CN: 预测负荷, en-US: Pred Load}}
properties: {}
interfaces:
  power:
    type: predefined
    carrier: electricity
    unit: kW
    valid_range: {minimum: 0, maximum: 1000}
    source: {mode: data_predict, data_ref: predict_ref}
equations: {variables: {}, relations: []}
"""


def _model_document(yaml_text: str):
    parsed = parse_device_model_v2(yamlmini.load(yaml_text))
    assert parsed.document is not None
    return parsed.document


def _data_csv(
    *,
    dataset_id: str,
    device_id: str,
    device_sha: str,
    source_mode: str,
    resolution: str,
    columns: list[str],
    rows: list[list[float]],
    units: dict[str, str] | None = None,
    period: str | None = None,
) -> bytes:
    lines = [
        "# schema: ies.device-data",
        "# schema_version: 2.0.0",
        f"# dataset_id: {dataset_id}",
        f"# device_id: {device_id}",
        f"# device_content_sha256: {device_sha}",
        f"# source_mode: {source_mode}",
        f"# resolution: {resolution}",
    ]
    if period is not None:
        lines.append(f"# period: {period}")
    for column in columns:
        lines.append(f"# unit.{column}: {(units or {}).get(column, 'kW')}")
    lines.append("step," + ",".join(columns))
    for step, row in enumerate(rows):
        lines.append(",".join([str(step), *(str(v) for v in row)]))
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# 测试环境(与 test_project_model_save 同构)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    from iesplan.migrations import apply_migrations

    apply_migrations(eng)
    yield eng
    eng.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(engine: Engine) -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture()
def db_session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path):
    settings.data_dir = tmp_path
    yield tmp_path


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_engineer(db: Session, name: str = "engineer") -> User:
    return make_user(db, name)


def _make_project(db: Session, user: User) -> tuple[Project, int]:
    project = project_service.create_project(
        db, user, f"{user.username} 项目",
        baseline_resolution="1h", baseline_leap_year=False, baseline_scenario_mode="single",
    )
    db.commit()
    return project, 1  # 初始草稿 revision = 1


def _save_data_repeat_model(
    db: Session, user: User, project_id: int, expected_revision: int
) -> tuple[int, int]:
    """正式保存 data_repeat 模型(临时上传 → 门禁 → 内容锁附着)。"""
    from iesplan.application.projects.model_save import new_temp_upload_id

    document = _model_document(DATA_REPEAT_MODEL_YAML)
    assert document.device is not None
    raw = _data_csv(
        dataset_id="test.load.profile",
        device_id=document.device.id,
        device_sha=content_sha256(document),
        source_mode="data_repeat",
        resolution="15min",
        columns=["electric_demand"],
        rows=[[float(10 + step)] for step in range(96)],
        period="day",
    )
    upload_id = new_temp_upload_id()
    temp = upload_temp_data_file(
        db, user, project_id, content=raw, data_ref="load_data", upload_id=upload_id
    )
    ref = DataFileRef(
        data_ref="load_data",
        upload_id=upload_id,
        object_id=temp["temp_file"]["object_id"],
        sha256=temp["temp_file"]["sha256"],
    )
    result = save_project_model(
        db, user, project_id,
        model_yaml=DATA_REPEAT_MODEL_YAML,
        expected_revision=expected_revision,
        source="direct_yaml",
        data_files=(ref,),
    )
    return int(result["project_model"]["id"]), result["project_revision"]


def _save_predict_model(
    db: Session, user: User, project_id: int, expected_revision: int
) -> tuple[int, int]:
    """正式保存 data_predict 模型并附着三个显式输入文件(data:{ref})。

    模型保存门禁要求 ``data_ref`` 文件存在: 以训练目标文件作为绑定文件
    (目标列即接口列); 训练输入/预测输入为算法显式输入, 直接以存储门面
    附着(生产路径由后续上传/绑定流程完成, 本切片只消费附着引用)。
    """
    from iesplan.application.projects.model_save import new_temp_upload_id

    document = _model_document(DATA_PREDICT_MODEL_YAML)
    assert document.device is not None
    rng = np.random.default_rng(7)
    temp = rng.normal(20.0, 5.0, 24)
    ghi = np.clip(rng.normal(500.0, 200.0, 24), 0, None)
    power = 2.0 * temp + 0.5 * ghi / 100.0 + 10.0
    train_in = _data_csv(
        dataset_id="train.in", device_id=document.device.id,
        device_sha=content_sha256(document),
        source_mode="data_predict", resolution="1h", columns=["temp", "ghi"],
        rows=[[t, g] for t, g in zip(temp, ghi, strict=True)],
        units={"temp": "°C", "ghi": "W/m²"},
    )
    train_target = _data_csv(
        dataset_id="train.target", device_id=document.device.id,
        device_sha=content_sha256(document),
        source_mode="data_predict", resolution="1h", columns=["power"],
        rows=[[v] for v in power],
    )
    pred_in = _data_csv(
        dataset_id="pred.in", device_id=document.device.id,
        device_sha=content_sha256(document),
        source_mode="data_predict", resolution="1h", columns=["temp", "ghi"],
        rows=[[t, g] for t, g in zip(
            np.linspace(10.0, 30.0, 8760), np.linspace(100.0, 900.0, 8760), strict=True
        )],
        units={"temp": "°C", "ghi": "W/m²"},
    )
    upload_id = new_temp_upload_id()
    temp = upload_temp_data_file(
        db, user, project_id, content=train_target, data_ref="predict_ref", upload_id=upload_id
    )
    ref = DataFileRef(
        data_ref="predict_ref",
        upload_id=upload_id,
        object_id=temp["temp_file"]["object_id"],
        sha256=temp["temp_file"]["sha256"],
    )
    result = save_project_model(
        db, user, project_id,
        model_yaml=DATA_PREDICT_MODEL_YAML,
        expected_revision=expected_revision,
        source="direct_yaml",
        data_files=(ref,),
    )
    model_id, new_rev = int(result["project_model"]["id"]), result["project_revision"]
    # 附着训练输入/预测输入(存储公开门面; purpose 与模型保存的 data:{ref} 一致)
    for ref_key, content in (("train_in", train_in), ("pred_in", pred_in)):
        handle = put_object(db, content, "text/csv; charset=utf-8", source_category="sequence_prep_test")
        attach(db, handle.id, FINAL_OWNER_NAMESPACE, model_id,
               ref_entity_type=FINAL_OWNER_NAMESPACE, purpose=f"data:{ref_key}")
    db.commit()
    return model_id, new_rev


def _prepared_refs(db: Session, model_id: int, purpose_prefix: str) -> list[dict]:
    refs = find_refs_by_owner(db, FINAL_OWNER_NAMESPACE, model_id, FINAL_OWNER_NAMESPACE)
    return [ref for ref in refs if str(ref.get("purpose") or "").startswith(purpose_prefix)]


def _stored_model_document(db: Session, model_id: int):
    """读取项目模型清单引用的规范字节并重建最终文档(带 _N 后缀)。"""
    import json as _json

    model = db.get(ProjectModel, model_id)
    assert model is not None
    parsed = parse_device_model_v2(_json.loads(get_object(db, model.model_object_id).decode("utf-8")))
    assert parsed.document is not None
    return parsed.document


def _attached_purposes(db: Session, model_id: int) -> set[str]:
    refs = find_refs_by_owner(db, FINAL_OWNER_NAMESPACE, model_id, FINAL_OWNER_NAMESPACE)
    return {str(ref.get("purpose") or "") for ref in refs}


def _draft_prepared(db: Session, project_id: int) -> dict:
    project = db.get(Project, project_id)
    draft = project_service.get_current_draft(db, project)
    content = project_service.load_draft_content(db, draft)
    return dict(content.get("prepared_sequences") or {})


# ---------------------------------------------------------------------------
# data_repeat 发布
# ---------------------------------------------------------------------------


class TestPublishDataRepeat:
    def test_publish_success_atomic(self, db_session: Session):
        db = db_session
        user = _make_engineer(db)
        project, rev = _make_project(db, user)
        model_id, rev2 = _save_data_repeat_model(db, user, project.id, rev)
        assert rev2 == 2

        result = prepare_prepared_sequences(
            db, user, project.id,
            expected_revision=rev2,
            model_id=model_id,
            prepared_specs={"load_data": {"semantics": {"electric_demand": "instantaneous"}}},
        )
        assert result["project_revision"] == 3
        prepared = result["prepared"]
        assert set(prepared) == {"electric_demand"}
        info = prepared["electric_demand"]
        assert info["source_mode"] == "data_repeat"
        assert info["content_sha256"] and info["receipt_sha256"]
        # 预备对象可读且摘要一致(内容寻址 + 引用)
        assert hashlib.sha256(get_object(db, info["object_id"])).hexdigest() == info["content_sha256"]
        # 引用替换: 模型实例挂上 canonical/receipt 引用
        canon_refs = _prepared_refs(db, model_id, "sequence_prep:canonical:")
        assert len(canon_refs) == 1
        assert canon_refs[0]["object_id"] == info["object_id"]
        # 草稿清单闭合
        draft_prepared = _draft_prepared(db, project.id)
        assert str(model_id) in draft_prepared
        assert draft_prepared[str(model_id)]["electric_demand"]["content_sha256"] == info["content_sha256"]

    def test_failure_rejects_without_side_effects(self, db_session: Session):
        db = db_session
        user = _make_engineer(db)
        project, rev = _make_project(db, user)
        model_id, rev2 = _save_data_repeat_model(db, user, project.id, rev)

        with pytest.raises(PrepPublishRejectedError) as exc:
            prepare_prepared_sequences(
                db, user, project.id,
                expected_revision=rev2,
                model_id=model_id,
                prepared_specs={"load_data": {"semantics": {"electric_demand": "bogus"}}},
            )
        diags = exc.value.params["diagnostics"]
        assert any(d["code"] == "DATA-PREP-001" for d in diags)
        # 无副作用: 无预备引用、草稿 revision 不变、清单为空
        assert _prepared_refs(db, model_id, "sequence_prep:") == []
        assert project_service.get_current_draft(db, project).revision == rev2
        assert _draft_prepared(db, project.id) == {}

    def test_republish_replaces_refs_without_leak(self, db_session: Session):
        db = db_session
        user = _make_engineer(db)
        project, rev = _make_project(db, user)
        model_id, rev2 = _save_data_repeat_model(db, user, project.id, rev)
        specs = {"load_data": {"semantics": {"electric_demand": "instantaneous"}}}
        prepare_prepared_sequences(
            db, user, project.id, expected_revision=rev2, model_id=model_id, prepared_specs=specs
        )
        prepare_prepared_sequences(
            db, user, project.id, expected_revision=3, model_id=model_id, prepared_specs=specs
        )
        canon_refs = _prepared_refs(db, model_id, "sequence_prep:canonical:")
        assert len(canon_refs) == 1  # 旧引用已解绑, 无泄漏
        assert project_service.get_current_draft(db, project).revision == 4

    def test_rollback_restores_previous_verified_refs(self, db_session: Session):
        db = db_session
        user = _make_engineer(db)
        project, rev = _make_project(db, user)
        model_id, rev2 = _save_data_repeat_model(db, user, project.id, rev)
        specs = {"load_data": {"semantics": {"electric_demand": "instantaneous"}}}
        first = prepare_prepared_sequences(
            db, user, project.id, expected_revision=rev2, model_id=model_id, prepared_specs=specs
        )
        first_object_id = first["prepared"]["electric_demand"]["object_id"]
        # 第二次发布(不同列语义 → 不同内容/摘要 → 新对象)
        specs2 = {"load_data": {"semantics": {"electric_demand": "state"}}}
        second = prepare_prepared_sequences(
            db, user, project.id, expected_revision=3, model_id=model_id, prepared_specs=specs2
        )
        second_object_id = second["prepared"]["electric_demand"]["object_id"]
        assert second_object_id != first_object_id
        # 回滚到上一份已验证状态(第一次发布)
        rollback = rollback_prepared_sequences(
            db, user, project.id, expected_revision=4, model_id=model_id
        )
        assert rollback["prepared"]["electric_demand"]["object_id"] == first_object_id
        canon_refs = _prepared_refs(db, model_id, "sequence_prep:canonical:")
        assert len(canon_refs) == 1
        assert canon_refs[0]["object_id"] == first_object_id  # 第二次引用已解绑
        assert project_service.get_current_draft(db, project).revision == 5


# ---------------------------------------------------------------------------
# data_predict 发布
# ---------------------------------------------------------------------------


class TestPublishDataPredict:
    PREDICT_SPEC = {
        "predict_ref": {
            "feature_columns": ["temp", "ghi"],
            "feature_semantics": {"temp": "instantaneous", "ghi": "instantaneous"},
            "training_input_ref": "train_in",
            "training_target_ref": "predict_ref",
            "prediction_input_ref": "pred_in",
        }
    }

    def test_publish_predict_with_explicit_inputs(self, db_session: Session):
        db = db_session
        user = _make_engineer(db)
        project, rev = _make_project(db, user)
        model_id, rev2 = _save_predict_model(db, user, project.id, rev)

        result = prepare_prepared_sequences(
            db, user, project.id,
            expected_revision=rev2,
            model_id=model_id,
            prepared_specs=self.PREDICT_SPEC,
        )
        assert result["project_revision"] == 3
        info = result["prepared"]["power"]
        assert info["source_mode"] == "data_predict"
        assert info["training_artifact"] is not None
        assert info["training_artifact"]["sha256"]
        # 预备输出: 全周期 8760 点连续 step, 回执固定算法契约(以最终 _N 文档校验)
        stored = _stored_model_document(db, model_id)
        verify = __import__(
            "iesplan.devices.datacontract2", fromlist=["canonicalize_device_data_v2"]
        ).canonicalize_device_data_v2(
            get_object(db, info["object_id"]),
            stored,
            expected_rows=8760,
            expected_data_ref="predict_ref",
        )
        assert not [d for d in verify.diagnostics if d.blocking]
        assert len(verify.rows) == 8760
        assert verify.column_order == ("step", "power")
        # 训练产物对象可读且摘要一致
        artifact_bytes = get_object(db, info["training_artifact"]["object_id"])
        assert hashlib.sha256(artifact_bytes).hexdigest() == info["training_artifact"]["sha256"]
        # 三个显式输入文件仍附着(摘要链追溯)
        purposes = _attached_purposes(db, model_id)
        assert {"data:train_in", "data:predict_ref", "data:pred_in"} <= purposes

    def test_missing_explicit_input_rejected(self, db_session: Session):
        db = db_session
        user = _make_engineer(db)
        project, rev = _make_project(db, user)
        model_id, rev2 = _save_predict_model(db, user, project.id, rev)

        with pytest.raises(PrepPublishRejectedError) as exc:
            prepare_prepared_sequences(
                db, user, project.id,
                expected_revision=rev2,
                model_id=model_id,
                prepared_specs={"predict_ref": {"feature_columns": ["temp", "ghi"]}},
            )
        diags = exc.value.params["diagnostics"]
        assert any("缺少显式输入" in d["params"]["detail"] for d in diags)
        assert _prepared_refs(db, model_id, "sequence_prep:") == []
        assert project_service.get_current_draft(db, project).revision == rev2


# ---------------------------------------------------------------------------
# 权限与乐观锁
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_non_owner_rejected(self, db_session: Session):
        db = db_session
        user = _make_engineer(db, "owner")
        outsider = _make_engineer(db, "outsider")
        project, rev = _make_project(db, user)
        model_id, rev2 = _save_data_repeat_model(db, user, project.id, rev)
        with pytest.raises(ForbiddenError):
            prepare_prepared_sequences(
                db, outsider, project.id,
                expected_revision=rev2,
                model_id=model_id,
                prepared_specs={"load_data": {"semantics": {"electric_demand": "instantaneous"}}},
            )

    def test_wrong_revision_rejected(self, db_session: Session):
        db = db_session
        user = _make_engineer(db)
        project, rev = _make_project(db, user)
        model_id, rev2 = _save_data_repeat_model(db, user, project.id, rev)
        with pytest.raises(ConflictError) as exc:
            prepare_prepared_sequences(
                db, user, project.id,
                expected_revision=99,
                model_id=model_id,
                prepared_specs={"load_data": {"semantics": {"electric_demand": "instantaneous"}}},
            )
        assert exc.value.params["expected_revision"] == 99
