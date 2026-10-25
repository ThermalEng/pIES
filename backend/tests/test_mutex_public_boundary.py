"""Focused red reproduction for the project generator's private lowering import.

The general architecture gate remains authoritative. Keep this reproduction
until the public lowering boundary and its numerical regressions are verified.
"""
from test_architecture_gates import _find_private_symbol_imports


def test_project_generator_uses_public_lowering_boundary():
    violations = [
        item for item in _find_private_symbol_imports()
        if item[0] == "iesplan.computation.generators.project"
    ]
    assert not violations, (
        "Expose the shared lowering operation through its owner public API; "
        "preserve finite-bound mutex semantics without compatibility aliases: "
        f"{violations}"
    )
