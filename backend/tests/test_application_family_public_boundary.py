"""Focused red reproduction for cross-family implementation penetration.

Use existing owner facades; this does not authorize changing transactions,
device-model resolution, snapshot freezing, or model object ownership.
"""
from test_architecture_gates import _find_cross_application_family_impl_imports


def test_project_task_validation_use_owner_application_facades():
    callers = {
        "iesplan.application.projects.lifecycle",
        "iesplan.application.tasks.submissions",
        "iesplan.application.tasks.views",
        "iesplan.application.validations.precheck",
    }
    violations = {
        item for item in _find_cross_application_family_impl_imports()
        if item[0] in callers
    }
    assert not violations, (
        "Cross-family calls must use owner-exported operations, not implementation "
        f"modules; retain existing behavior and transaction ownership: {sorted(violations)}"
    )
