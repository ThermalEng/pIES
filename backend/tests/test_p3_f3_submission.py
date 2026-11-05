"""Public API: exact accessible sealed evidence, immutable values and re-authoring."""
# ruff: noqa: F811

import json

from auth_helpers import make_user
from test_task_draft_load import (  # noqa: F401,F811
    _clean_state,
    _setup,
    client,
    db,
    engine,
)
from test_tasks_api import _claim, _complete_task, _h, _prepare_project, _save_config, _submit_task

from iesplan import results as results_domain
from iesplan import tasks as tasks_domain
from iesplan.application import packages as packages_uc
from iesplan.application.projects import load_content_object
from iesplan.finance import comparison_as_declared, freeze_comparison, parse_comparison
from iesplan.project import get_draft
from iesplan.storage import get_object, put_object


def _source(client, db):
    pid, tid, user = _setup(client, db, "f3")
    _claim(db, tid)
    _complete_task(db, tid, outcome="normal_completion")
    task = tasks_domain.get_task(db, tid)
    evidence = []
    for value in ("10", "999"):
        obj = put_object(
            db,
            json.dumps(
                {
                    "financial_evaluation": {
                        "status": "evaluated",
                        "currency": "CNY",
                        "evaluation_years": 1,
                        "annual_net_flows": ["-100", "60"],
                        "amounts": [
                            {
                                "id": "budget",
                                "axis": "scalar",
                                "unit": "CNY",
                                "values": [{"period": 0, "value": value}],
                            }
                        ],
                    }
                }
            ).encode(),
            "application/json",
            "eval_results",
        )
        evidence.append(
            results_domain.create_evidence(
                db,
                task_id=tid,
                calc_snapshot_id=task.calc_snapshot_id,
                object_id=obj.id,
                status="complete",
                created_by=user.id,
            )
        )
    db.commit()
    return pid, tid, user, evidence


def _evaluation(tid, evidence_id):
    return {
        "evaluation_years": 1,
        "construction_period": 0,
        "operation_years": 1,
        "discount_rate": 0.08,
        "construction_payments": [{"t": 0, "ratio": 1}],
        "maintenance_expenses": [],
        "comparison": {
            "mode": "difference",
            "baseline": {"kind": "task_result", "task_id": tid, "evidence_id": evidence_id},
            "facts": {"budget": {"kind": "amount", "id": "budget"}},
            "comparability": {"service_basis": "same service", "differences": "none"},
        },
    }


def test_submit_freezes_exact_evidence_not_latest_and_load_as_declared(client, db):
    pid, tid, user, evidence = _source(client, db)
    revision = client.get(f"/api/projects/{pid}/config", headers=_h(client, user)).json()["revision"]
    status, body = _submit_task(
        client,
        pid,
        user,
        config={"config_revision": revision},
        evaluation=_evaluation(tid, evidence[0].id),
        idempotency_key="f3-frozen",
    )
    assert status == 201, body
    task = body["task"]
    snapshot = tasks_domain.get_snapshot(db, task["calc_snapshot_id"])
    comparison = snapshot.evaluation["comparison"]
    assert comparison["facts"]["budget"]["values"] == ("10",)
    assert comparison["baseline_source"]["evidence_id"] == evidence[0].id
    assert comparison["baseline_source"]["object_id"] == evidence[0].object_id
    response = client.post(f"/api/projects/{pid}/tasks/{task['id']}/load-draft", headers=_h(client, user))
    assert response.status_code == 200, response.text
    draft = client.get(f"/api/projects/{pid}", headers=_h(client, user)).json()["draft"]["content"]
    loaded = draft["task_working_inputs"]["comparison"]
    assert loaded["baseline"]["kind"] == "declared"
    assert "task_id" not in loaded["baseline"]
    assert loaded["baseline"]["annual_net_flows"] == ["-100", "60"]
    assert loaded["baseline"]["facts"]["budget"]["values"] == ["10"]
    assert tasks_domain.get_snapshot(db, snapshot.id).evaluation == snapshot.evaluation


def test_other_users_baseline_is_not_accessible_and_failed_submission_creates_no_task(client, db):
    _, tid, _, evidence = _source(client, db)
    other = make_user(db, "f3_other")
    pid = _prepare_project(client, db, other, name="F3 other owner")
    revision = _save_config(client, pid, other)
    status, body = _submit_task(
        client,
        pid,
        other,
        config={"config_revision": revision},
        evaluation=_evaluation(tid, evidence[0].id),
        idempotency_key="f3-denied",
    )
    assert status == 403, body
    assert tasks_domain.list_task_ids(db, pid) == []


def test_unknown_selected_fact_reports_alias_and_does_not_guess_zero(client, db):
    pid, tid, user, evidence = _source(client, db)
    evaluation = _evaluation(tid, evidence[0].id)
    evaluation["comparison"]["facts"]["budget"]["id"] = "missing"
    revision = client.get(f"/api/projects/{pid}/config", headers=_h(client, user)).json()["revision"]
    status, body = _submit_task(
        client,
        pid,
        user,
        config={"config_revision": revision},
        evaluation=evaluation,
        idempotency_key="f3-missing",
    )
    assert status == 400, body
    assert body["error"]["code"] == "TASK-REQ-015"
    assert body["error"]["location"]["field"] == "evaluation.comparison.facts.budget"


def test_mismatched_evidence_has_user_visible_detail(client, db):
    pid, tid, user, evidence = _source(client, db)
    evaluation = _evaluation(tid, evidence[-1].id + 1)
    revision = client.get(f"/api/projects/{pid}/config", headers=_h(client, user)).json()["revision"]
    before = tasks_domain.list_task_ids(db, pid)
    status, body = _submit_task(
        client,
        pid,
        user,
        config={"config_revision": revision},
        evaluation=evaluation,
        idempotency_key="f3-wrong-evidence",
    )
    assert status == 400, body
    assert body["error"]["code"] == "TASK-REQ-015"
    assert body["error"]["location"]["field"] == "evaluation.comparison.baseline.evidence_id"
    assert body["error"]["params"]["detail"] == "基准证据不属于指定任务"
    assert tasks_domain.list_task_ids(db, pid) == before


def test_package_keeps_frozen_comparison_without_forging_local_tasks(client, db):
    pid, tid, user, evidence = _source(client, db)
    revision = client.get(f"/api/projects/{pid}/config", headers=_h(client, user)).json()["revision"]
    status, body = _submit_task(
        client,
        pid,
        user,
        config={"config_revision": revision},
        evaluation=_evaluation(tid, evidence[0].id),
        idempotency_key="f3-package",
    )
    assert status == 201, body
    task = body["task"]
    _claim(db, task["id"])
    _complete_task(db, task["id"], outcome="normal_completion")
    snapshot = tasks_domain.get_snapshot(db, task["calc_snapshot_id"])
    frozen = snapshot.evaluation["comparison"]
    from iesplan.core.jsonutil import jsonable

    sealed = jsonable(frozen)
    sealed["evaluation_net_flows"] = ["0", "0"]
    raw = json.dumps({"financial_evaluation": {"comparison": sealed}}).encode()
    obj = put_object(db, raw, "application/json", "eval_results")
    result = results_domain.create_evidence(
        db,
        task_id=task["id"],
        calc_snapshot_id=snapshot.id,
        object_id=obj.id,
        status="complete",
        created_by=user.id,
    )
    db.commit()
    exported = packages_uc.export_package(db, user, pid)
    importer = make_user(db, "f3_importer")
    proposal = packages_uc.propose_import(
        db,
        importer,
        get_object(db, exported.object_id),
        idempotency_key="f3-import",
    )
    imported = packages_uc.confirm_import(db, importer, proposal.id)
    draft = load_content_object(db, get_draft(db, imported.current_draft_id).content_object_id)
    source = next(row for row in draft["imported_evidence"] if row["package_id"] == result.id)
    payload = next(row for row in source["objects"] if row["path"].endswith("/result.json"))
    restored = json.loads(get_object(db, payload["object_id"]))["financial_evaluation"]["comparison"]
    assert restored == sealed
    assert tasks_domain.list_task_ids(db, imported.id) == []
    declared = comparison_as_declared(restored, years=1, currency="CNY")
    reauthored = freeze_comparison(parse_comparison(declared, years=1), years=1, currency="CNY")
    assert reauthored.to_dict()["facts"] == restored["facts"]
    assert reauthored.to_dict()["baseline_net_flows"] == restored["baseline_net_flows"]
    assert reauthored.baseline_source["kind"] == "declared"
