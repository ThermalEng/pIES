"""Public template validation response must have a registered wrapper contract."""
import ast
from pathlib import Path

from iesplan.api.wrapper_keys import WRAPPER_KEYS


def test_candidate_template_validation_wrapper_is_registered():
    endpoint_name = "validate_template_yaml_endpoint"
    source = Path(__file__).resolve().parents[1] / "iesplan/api/model_templates.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    endpoint = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == endpoint_name
    )
    response = next(
        node.value for node in ast.walk(endpoint)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    )
    actual_keys = frozenset(key.value for key in response.keys)
    assert WRAPPER_KEYS.get(endpoint_name) == actual_keys, (
        "Register the existing public response; do not change its payload or "
        f"exempt the endpoint from the protocol gate: {endpoint_name} -> {sorted(actual_keys)}"
    )
