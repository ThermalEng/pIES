"""Result read model: small frozen facts, separate from complete solver evidence.

The stored projection is a rebuildable index, not a second authority. Evidence
without this index is read incrementally from the same immutable object.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import partial

from sqlalchemy.orm import Session

from iesplan import tasks
from iesplan.core.content import GeneratedContent, json_chunks
from iesplan.core.errors import NotFoundError
from iesplan.core.jsonstream import ArrayReader, ObjectFields, project_json, project_value
from iesplan.results import get_evidence
from iesplan.storage import add_ref, find_refs_by_owner, get_object, get_object_content, put_content


def evidence_selection(planning_names) -> ObjectFields:
    names = set(planning_names)
    solution = ObjectFields(
        {
            "variables": ArrayReader(lambda values: [item for item in values if item["name"] in names]),
        },
        other=True,
    )
    compute = ObjectFields(
        {
            "outputs": ObjectFields(
                {
                    "solver_summary": True,
                    "solution": solution,
                }
            )
        },
        other=True,
    )
    fields = {"compute_result": compute}
    selection = ObjectFields(fields, other=True)
    fields["content"] = selection
    return selection


def snapshot_planning_names(snapshot):
    receipt = snapshot.assembly_receipt if snapshot is not None else None
    return (receipt or {}).get("planning", {}).get("variables", ())


def read_evidence_projection(db: Session, package) -> dict:
    refs = find_refs_by_owner(db, "evidence_projection", package.id, "evidence_packages")
    if refs:
        return json.loads(get_object(db, refs[0]["object_id"]))
    snapshot = tasks.get_snapshot(db, package.calc_snapshot_id) if package.calc_snapshot_id else None
    return project_json(
        get_object_content(db, package.object_id),
        evidence_selection(snapshot_planning_names(snapshot)),
    )


def store_evidence_projection(db: Session, package_id: int, payload: Mapping, snapshot) -> int:
    """Flush-only, in the same publication transaction as the immutable evidence."""
    projected = project_value(payload, evidence_selection(snapshot_planning_names(snapshot)))
    obj = put_content(
        db,
        GeneratedContent(partial(json_chunks, projected)),
        "application/json",
        source_category="evidence_projection",
    )
    add_ref(
        db,
        obj.id,
        "evidence_projection",
        package_id,
        ref_entity_type="evidence_packages",
        purpose="Rebuildable evidence read model",
    )
    return obj.id


def index_evidence_projection(db: Session, package_id: int) -> int:
    """Deployment command for existing evidence; no re-solve or evidence rewrite.

    Repeated invocations reuse the same read index. Only this explicit command
    commits; normal GET requests remain read-only.
    """
    package = get_evidence(db, package_id)
    if package is None:
        raise NotFoundError("证据包不存在", params={"evidence_package_id": package_id})
    refs = find_refs_by_owner(db, "evidence_projection", package.id, "evidence_packages")
    if refs:
        return refs[0]["object_id"]
    try:
        snapshot = tasks.get_snapshot(db, package.calc_snapshot_id) if package.calc_snapshot_id else None
        projected = read_evidence_projection(db, package)
        object_id = store_evidence_projection(db, package.id, projected, snapshot)
        db.commit()
        return object_id
    except Exception:
        db.rollback()
        raise
