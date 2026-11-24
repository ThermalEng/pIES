"""Tests for YAML 1.2 safe-subset parser and serializer."""

from __future__ import annotations

import pytest

from pies_sdk.errors import SafeYamlError
from pies_sdk.yaml_safe import dump_safe_yaml, load_safe_yaml


def test_parse_and_roundtrip_safe_yaml() -> None:
    doc = {
        "schema": "ies.algorithm-plugin",
        "schema_version": "1.0.0",
        "entrypoint": "main:solve",
        "compatibility": {"pies": ">=1.1.5,<2.0.0"},
        "flags": [True, False, None, 42, 3.14],
        "nested": [{"id": "a", "items": [1, 2]}, {"id": "b", "items": []}],
        "empty_map": {},
    }
    dumped = dump_safe_yaml(doc)
    assert "\t" not in dumped
    assert "&" not in dumped
    assert "*" not in dumped
    assert load_safe_yaml(dumped) == doc


def test_unquoted_colon_in_scalar_preserved() -> None:
    yaml_text = """
entrypoint: main:solve
url_list:
  - https://example.com/a:b
  - main:solve
"""
    parsed = load_safe_yaml(yaml_text)
    assert parsed == {
        "entrypoint": "main:solve",
        "url_list": ["https://example.com/a:b", "main:solve"],
    }


def test_flow_mapping_quoted_colon_and_unicode_escapes() -> None:
    yaml_text = r'''
schema_inline: {"type":"object","additionalProperties":false,"items":["\u4e2d\u6587","\x41","\U0001F600"]}
'''
    parsed = load_safe_yaml(yaml_text)
    assert parsed == {
        "schema_inline": {
            "type": "object",
            "additionalProperties": False,
            "items": ["中文", "A", "😀"],
        }
    }
    with pytest.raises(SafeYamlError, match="Invalid"):
        load_safe_yaml('bad: "\\u12ZZ"\n')


@pytest.mark.parametrize(
    ("bad_yaml", "match"),
    [
        ("a:\n\tb: 1\n", "Tab"),
        ("a: &anc 1\nb: *anc\n", "anchor"),
        ("a: *alias\n", "alias"),
        ("a: !custom 1\n", "tag"),
        ("base:\n  a: 1\nchild:\n  <<: *base\n", "merge"),
        ("a: 1\na: 2\n", "Duplicate"),
        ("flow: {a: 1, a: 2}\n", "Duplicate"),
        ("a: 1\n---\nb: 2\n", "Multiple"),
        ("a: .nan\n", "Non-finite"),
        ("a: .inf\n", "Non-finite"),
        ("a: -Infinity\n", "Non-finite"),
        ("a: |\n  multiline\n", "Block scalar"),
    ],
)
def test_rejects_unsafe_yaml_constructs(bad_yaml: str, match: str) -> None:
    with pytest.raises(SafeYamlError, match=match):
        load_safe_yaml(bad_yaml)
