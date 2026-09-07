"""0.6.5 退役证据测试: 旧 sequence_prep 路径不再存在、不可导入、无符号残留。

验证退役收尾(0.6.5 条目 3-4)的静态证据: 模块/目录删除、public 符号删除、
诊断码删除、device-data 2.0 契约不再暴露 day/week 模板时代助手, 且后端
源码中不存在任何指向已退役模块的导入(无运行时双读)。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 辅助(纯 AST/路径, 不 import 业务模块, 与 test_architecture_gates 同风格)
# ---------------------------------------------------------------------------

_BACKEND_SRC = Path(__file__).resolve().parents[1] / "iesplan"


def _src_modules() -> list[Path]:
    return sorted(p for p in _BACKEND_SRC.rglob("*.py"))


def _module_imports_sequence_prep(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "sequence_prep" in alias.name.split("."):
                    return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            if "sequence_prep" in node.module.split("."):
                return True
    return False


class TestRetiredModulesAndSymbols:
    def test_sequence_prep_package_removed(self) -> None:
        assert not (_BACKEND_SRC / "sequence_prep").exists()
        assert not (_BACKEND_SRC / "application" / "sequence_prep.py").exists()

    def test_sequence_prep_not_importable(self) -> None:
        for dotted in ("iesplan.sequence_prep", "iesplan.application.sequence_prep"):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(dotted)

    def test_no_src_module_imports_sequence_prep(self) -> None:
        offenders = [str(p) for p in _src_modules() if _module_imports_sequence_prep(p)]
        assert offenders == []

    def test_project_service_prep_symbols_removed(self) -> None:
        from iesplan.services import project as project_service

        for name in (
            "record_sequence_prep_refs",
            "load_latest_prepared_sequences",
            "load_draft_content",
            "rollback_prepared_sequences",
        ):
            assert not hasattr(project_service, name), name

    def test_prep_diagnostic_codes_removed(self) -> None:
        from iesplan.core.diagnostics import DIAG_FIX_HINT_KEYS, DIAG_MESSAGE_KEYS, NEW_DIAG_CODES

        legacy_codes = ["PROJ-PREP-001", "DATA-PREP-001", "DATA-PREP-004", "DATA-PREP-007"]
        for code in legacy_codes:
            assert code not in NEW_DIAG_CODES, code
            assert code not in DIAG_MESSAGE_KEYS, code
            assert code not in DIAG_FIX_HINT_KEYS, code
        assert not any(code.startswith("DATA-PREP-") for code in NEW_DIAG_CODES)

    def test_datacontract2_no_periodic_rows_helper(self) -> None:
        import iesplan.devices.datacontract2 as dc2

        assert not hasattr(dc2, "periodic_rows")
        assert not hasattr(dc2, "PERIOD_VALUES")

    def test_prepared_sequences_key_only_in_retirement_cleanup(self) -> None:
        """新代码不读取旧清单键: 除退役清理(services/project.py)与其离线 CLI
        文档外, 任何源码不得再提及 ``prepared_sequences``(无运行时双读)。"""
        from iesplan.services.project import LEGACY_PREP_DRAFT_KEY

        assert LEGACY_PREP_DRAFT_KEY == "prepared_sequences"
        allowed = {"services/project.py", "cli/purge_sequence_prep.py"}
        references = [
            str(p.relative_to(_BACKEND_SRC))
            for p in _src_modules()
            if LEGACY_PREP_DRAFT_KEY in p.read_text(encoding="utf-8")
        ]
        assert set(references) <= allowed, sorted(set(references) - allowed)
