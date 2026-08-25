"""0.3.0 C5: 静态架构门禁测试(宪法 §14.3 前三条)。

宪法 §14.3 要求 CI 逐步加入并最终强制: 禁止 core 依赖业务模块、禁止跨模块
导入私有符号、禁止 API 直接导入 ORM。本文件建立 0.3.0 基线门禁:

  1. test_core_no_business_dependencies  — core 不允许 import 任何业务模块;
  2. test_no_cross_module_private_imports — 禁止 from X import _y / import X._y
     以及模块对象上的私有属性访问(如 config_service._row_to_config);
  3. test_api_no_direct_orm_imports      — api 不允许直接 import iesplan.models.*
     与 iesplan.db 的 Session/Base 等(get_db 依赖注入除外)。

策略: 现有违规列入文件头部的 WHITELIST_BASELINE 常量(白名单基线, 注释写明
整改 TODO), 新增违规直接断言失败。后续里程碑按注释逐条整改后移除白名单条目,
白名单清空后门禁转为硬强制。

实现约束: 纯标准库(ast/pathlib)读取源码文本, 绝不 import 业务模块。
"""

from __future__ import annotations

import ast
from pathlib import Path

#: backend/ 目录(本测试文件所在目录的上一级)
_BACKEND_DIR = Path(__file__).resolve().parents[1]
#: 业务包根目录 backend/iesplan/
_PKG_ROOT = _BACKEND_DIR / "iesplan"
#: 各门禁扫描根目录
_CORE_DIR = _PKG_ROOT / "core"
_API_DIR = _PKG_ROOT / "api"

#: 宪法 §14.3 判定为"业务模块"的 iesplan 顶层子包(core 一律禁止依赖)。
#: 判定规则见 _is_business_import: 允许根包 iesplan(仅 __version__)与
#: iesplan.core 子树, 其余 iesplan.* 子包(services/api/models/storage/worker/
#: engines/analysis/…)均视为业务模块。

# ---------------------------------------------------------------------------
# 门禁 1 白名单: core → 业务模块 依赖
# ---------------------------------------------------------------------------
# 基线核查(2026-08-23): backend/iesplan/core/ 全部 .py 仅 import 标准库、第三方
# 与 iesplan.core.*, 无业务模块依赖, 基线全绿。
# 若未来确需豁免, 需人工评审架构影响后在此登记 {(模块路径, 行号): 理由}。
WHITELIST_CORE_BUSINESS_DEPS: dict[tuple[str, int], str] = {}

# ---------------------------------------------------------------------------
# 门禁 2 白名单: 跨模块私有符号导入 (键 = (模块路径, 符号))
# ---------------------------------------------------------------------------
# 符号形式:
#   - 导入语句: from X import _y / import X._y(imported 名字以下划线开头,
#     排除 __dunder__(如 __version__ 属公开约定));
#   - 属性访问: 模块别名上的私有属性, 如 config_service._row_to_config。
# 每条目均需整改: 提升为公开 API 或改为公开等价调用, 整改后移除条目。
WHITELIST_PRIVATE_IMPORTS: dict[tuple[str, str], str] = {
    # ---- analysis 域内部: wrapper 私有财务/指标辅助, sensitivity 复用 ----
    ("iesplan.analysis.sensitivity", "_financial_to_dict"):
        "同域 wrapper 私有财务结果序列化辅助; TODO: 提升为 analysis 公开 API 后移除。",
    ("iesplan.analysis.sensitivity", "_jsonable_kpi"):
        "同域 wrapper 私有 KPI 可 JSON 化辅助; TODO: 同上。",
    # ---- assembly 域内部: rules 子包复用 checker/schema 私有工具 ----
    ("iesplan.assembly.rules.completeness", "_split_model"):
        "assembly 域内 rules 复用 checker 私有模型拆分函数; TODO: 提升公开。",
    ("iesplan.assembly.rules.solvability", "_PEAK_PARAM_BY_LOAD"):
        "assembly 域内 rules 复用 checker 私有峰值参数表; TODO: 提升公开。",
    ("iesplan.assembly.rules.solvability", "_to_watts"):
        "assembly 域内 rules 复用 checker 私有单位换算; TODO: 提升公开。",
    ("iesplan.assembly.checker", "_QUANTITY_DIMS"):
        "assembly 域内 checker 复用 schema 私有量纲常量; TODO: 提升公开。",
    # ---- engines 域内部: planning 复用 eval_run 私有取参函数 ----
    ("iesplan.engines.planning", "_param"):
        "engines 域内 planning 复用 eval_run 私有运行参数读取; TODO: 提升公开。",
    # ---- worker → analysis: 执行器复用 wrapper 私有财务输入构造 ----
    ("iesplan.worker.executors", "_project_financial_inputs"):
        "worker 复用 analysis.wrapper 私有项目财务输入构造; TODO: 提升公开 API。",
    ("iesplan.worker.executors", "_CAPACITY_KEYS"):
        "worker 复用 analysis.wrapper 私有容量键集合; TODO: 提升公开常量。",
    # ---- api → services: API 层直接访问服务私有函数 ----
    ("iesplan.api.config", "config_service._row_to_config"):
        "API 层访问 services.config 私有配置序列化(现状违规); "
        "TODO: services.config 提供公开 serializer 后移除。",
    ("iesplan.api.projects", "project_service._is_admin"):
        "API 层访问 services.project 私有权限判定(现状违规); "
        "TODO: 提升公开权限 API 后移除。",
    # ---- services 域内部: identity 复用 project 私有审计写入 ----
    ("iesplan.services.identity", "project_service._audit"):
        "服务层间复用 project 私有审计写入; TODO: 提升公开审计 API。",
    # ---- devices 域内部: parser 复用 contracts 递归深度冻结 helper ----
    ("iesplan.devices.parser", "_freeze"):
        "devices 域 parser 复用 contracts 私有递归冻结函数(roadmap 0.5.0 review: "
        "extensions/参数 default 深度不可变); TODO: 提升 contracts 公开 deep_freeze 后移除。",
}

# ---------------------------------------------------------------------------
# 门禁 3 白名单: api → ORM 直接导入 (键 = (模块路径, 行号), 值为该行豁免的符号集)
# ---------------------------------------------------------------------------
# 仅豁免导入语句本身及其列出的符号: 同文件新行导入新 ORM 符号仍会报错。
# 每条目均需整改: 经 services 公开面访问数据, 整改后移除条目。
WHITELIST_API_ORM: dict[tuple[str, int], frozenset[str]] = {
    # ---- admin.py: 管理端运维直接查询 ORM ----
    ("iesplan.api.admin", 34): frozenset({"RetentionRule"}),
    ("iesplan.api.admin", 35): frozenset({"ComputeSlot", "Task", "TaskAttempt", "TaskDiagnostic", "TaskLease"}),
    ("iesplan.api.admin", 36): frozenset({"User"}),
    ("iesplan.api.admin", 36): frozenset({"AdminMaintenanceAction"}),
    # ---- health.py: 健康检查直接计数 ORM ----
    ("iesplan.api.health", 26): frozenset({"Task"}),
    ("iesplan.api.health", 27): frozenset({"User"}),
    ("iesplan.api.health", 28): frozenset({"Project"}),
    # ---- results.py: 结果查询任务状态与校验正则 ----
    ("iesplan.api.results", 29): frozenset({"Task"}),
    ("iesplan.api.results", 30): frozenset({"HASH64_RE"}),
    # ---- limits.py: 配额统计函数内局部导入(非模块顶层) ----
    ("iesplan.api.limits", 260): frozenset({"Dataset", "DatasetFile", "DatasetVersion"}),
    ("iesplan.api.limits", 276): frozenset({"Project"}),
    # ---- auth.py: 认证/会话 ORM ----
    ("iesplan.api.auth", 28): frozenset({"User", "WindowSession"}),
    # ---- objects.py: 对象归属校验 ORM ----
    ("iesplan.api.objects", 28): frozenset({"User"}),
    # ---- tasks.py: 幂等键校验正则常量 ----
    ("iesplan.api.tasks", 25): frozenset({"IDEMPOTENCY_KEY_RE"}),
}

#: iesplan.db 中禁止 api 直接导入的 ORM 会话符号(get_db 依赖注入本身合法, 不在列)
_DB_ORM_NAMES = frozenset({"Base", "Session", "sessionmaker", "session"})


# ---------------------------------------------------------------------------
# 辅助函数(纯 AST, 不 import 业务模块)
# ---------------------------------------------------------------------------

def _module_path(rel: Path) -> str:
    """把相对于业务包根的 .py 相对路径转成模块路径。"""
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts.pop()
    else:
        parts[-1] = parts[-1][:-3]
    return "iesplan." + ".".join(parts)


def _iter_modules(scan_root: Path, pkg_root: Path = _PKG_ROOT):
    """遍历扫描目录下所有 .py, 产出 (绝对路径, 模块路径)。"""
    for path in sorted(scan_root.rglob("*.py")):
        yield path, _module_path(path.relative_to(pkg_root))


def _is_private(name: str) -> bool:
    """私有符号判定: 下划线开头, 排除 __dunder__(属公开约定, 如 __version__)。"""
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _relative_target(mod: str, node: ast.ImportFrom) -> str:
    """解析 ImportFrom 的目标模块(绝对导入原样; 相对导入按当前模块定位)。

    相对层级: level=1 → 当前包(去掉模块名本身), level=2 → 父包, 依此类推;
    module 为 None(如 from . import x)时只取包基。
    示例: 模块 iesplan.api.config 中 from .auth import x → iesplan.api.auth;
    from ..models import x → iesplan.models。
    """
    if node.level == 0 and node.module:
        return node.module
    parts = mod.split(".")
    base = parts[: len(parts) - node.level] if node.level <= len(parts) else []
    if not base:
        return ""  # 相对层级超出 iesplan 根(不会发生, 但防御)
    if node.module:
        base = base + node.module.split(".")
    return ".".join(base)


def _is_business_import(module: str) -> bool:
    """门禁 1 判定: module 是否为 core 禁止依赖的业务模块。

    允许: 根包 iesplan(仅 __version__)与 iesplan.core 子树;
    其余 iesplan.* 子包(services/api/models/storage/worker/engines/…)均禁止。
    """
    if not module.startswith("iesplan."):
        return False
    return not (module == "iesplan.core" or module.startswith("iesplan.core."))


def _find_core_business_imports(
    scan_root: Path = _CORE_DIR, pkg_root: Path = _PKG_ROOT
) -> list[tuple[str, int, str]]:
    """门禁 1: 扫描 core 下所有 import/from, 返回 (模块, 行号, 导入原文)。"""
    found: list[tuple[str, int, str]] = []
    for path, mod in _iter_modules(scan_root, pkg_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if _is_business_import(a.name):
                        found.append((mod, node.lineno, f"import {a.name}"))
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if _is_business_import(node.module):
                    names = ", ".join(a.name for a in node.names)
                    found.append((mod, node.lineno, f"from {node.module} import {names}"))
    return found


def _find_private_symbol_imports(
    scan_root: Path = _PKG_ROOT, pkg_root: Path = _PKG_ROOT
) -> list[tuple[str, str]]:
    """门禁 2: 扫描全部 iesplan 模块, 返回 (模块, 符号) 违规清单。

    覆盖三种形式:
      - from X import _y
      - import X._y(点号私有子模块)
      - 模块别名上的私有属性访问(如 config_service._row_to_config)
    """
    found: list[tuple[str, str]] = []
    for path, mod in _iter_modules(scan_root, pkg_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # 收集本模块导入的本地名 -> 完整模块路径(仅 iesplan 内部;
        # 含相对导入 level>0, 如 from ..checker import _x)
        local_to_module: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("iesplan"):
                        local_to_module[a.asname or a.name] = a.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                target = _relative_target(mod, node)
                if target.startswith("iesplan"):
                    for a in node.names:
                        if a.name != "*":
                            local_to_module[a.asname or a.name] = f"{target}.{a.name}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("iesplan."):
                        tail = a.name[len("iesplan") + 1 :]
                        if any(_is_private(p) for p in tail.split(".")[1:]):
                            found.append((mod, f"import {a.name}"))
            elif isinstance(node, ast.ImportFrom) and node.module:
                target = _relative_target(mod, node)
                if target.startswith("iesplan"):
                    for a in node.names:
                        if _is_private(a.name):
                            found.append((mod, a.name))
            elif isinstance(node, ast.Attribute) and _is_private(node.attr):
                base = node.value
                if isinstance(base, ast.Name) and base.id in local_to_module:
                    target = local_to_module[base.id]
                    if target.startswith("iesplan.") and not target.startswith(mod):
                        found.append((mod, f"{base.id}.{node.attr}"))
    return found


def _find_api_orm_imports(
    scan_root: Path = _API_DIR, pkg_root: Path = _PKG_ROOT
) -> list[tuple[str, int, frozenset[str]]]:
    """门禁 3: 扫描 api 下 iesplan.models.* 与 iesplan.db 会话符号导入。

    返回 (模块, 行号, 该行导入的违规符号集合)。get_db 依赖注入不在扫描范围。
    """
    found: list[tuple[str, int, frozenset[str]]] = []
    for path, mod in _iter_modules(scan_root, pkg_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        by_line: dict[int, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == "iesplan.models" or node.module.startswith("iesplan.models."):
                    by_line.setdefault(node.lineno, set()).update(
                        a.name for a in node.names if a.name != "*"
                    )
                elif node.module.startswith("iesplan.db"):
                    bad = {a.name for a in node.names if a.name in _DB_ORM_NAMES}
                    if bad:
                        by_line.setdefault(node.lineno, set()).update(bad)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "iesplan.models" or a.name.startswith("iesplan.models."):
                        by_line.setdefault(node.lineno, set()).add(a.name)
        found.extend((mod, line, frozenset(names)) for line, names in sorted(by_line.items()))
    return found


# ---------------------------------------------------------------------------
# 门禁测试
# ---------------------------------------------------------------------------

def test_core_no_business_dependencies():
    """宪法 §14.3: 禁止 core 依赖业务模块。基线全绿, 新增即报错。"""
    detected = _find_core_business_imports()
    new = [(m, line, src) for (m, line, src) in detected if (m, line) not in WHITELIST_CORE_BUSINESS_DEPS]
    assert not new, (
        f"core 依赖业务模块(新增违规, 需整改或登记白名单): {new}"
    )


def test_no_cross_module_private_imports():
    """宪法 §14.3: 禁止跨模块导入私有符号。现状违规在白名单, 新增即报错。"""
    detected = _find_private_symbol_imports()
    new = [(m, s) for (m, s) in detected if (m, s) not in WHITELIST_PRIVATE_IMPORTS]
    assert not new, (
        f"跨模块私有符号导入(新增违规, 需整改或登记白名单): {new}"
    )


def test_api_no_direct_orm_imports():
    """宪法 §14.3: 禁止 API 直接导入 ORM(get_db 依赖注入合法)。

    白名单按 (模块, 行号) 豁免, 并校验该行导入符号 ⊆ 白名单符号集,
    同文件新行或白名单行新增符号都会报错。
    """
    detected = _find_api_orm_imports()
    new: list[tuple[str, int, list[str]]] = []
    for mod, line, symbols in detected:
        allowed = WHITELIST_API_ORM.get((mod, line))
        if allowed is None or not symbols.issubset(allowed):
            new.append((mod, line, sorted(symbols)))
    assert not new, (
        f"API 直接导入 ORM(新增违规, 需整改或登记白名单): {new}"
    )


# ---------------------------------------------------------------------------
# 门禁自校验(构造 AST 断言检测逻辑, 不依赖真实代码状态)
# ---------------------------------------------------------------------------

def _parse_src(src: str) -> ast.Module:
    return ast.parse(src, mode="exec")


def test_gate_private_import_relative_detection():
    """门禁 2 自校验: 相对导入(from ..x import _y)私有符号必须被检出。"""
    # 模拟 iesplan.api.config 中 from ..services import config 私有访问
    tree = _parse_src("from ..services import config\nconfig._row_to_config()\n")
    mod = "iesplan.api.config"
    local_to_module: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            target = _relative_target(mod, node)
            for a in node.names:
                if a.name != "*":
                    local_to_module[a.asname or a.name] = f"{target}.{a.name}"
    assert local_to_module["config"] == "iesplan.services.config"
    # 私有属性访问必须命中
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_private(node.attr):
            base = node.value
            if isinstance(base, ast.Name) and base.id in local_to_module:
                hits.append(node.attr)
    assert "_row_to_config" in hits


def test_gate_relative_target_levels():
    """_relative_target 相对层级解析自校验。"""
    cases = {
        "from .auth import x": "iesplan.api.auth",
        "from ..models import x": "iesplan.models",
        "from . import x": "iesplan.api",
        "from iesplan.db import get_db": "iesplan.db",
    }
    for src, expected in cases.items():
        (node,) = [n for n in ast.walk(_parse_src(src)) if isinstance(n, ast.ImportFrom)]
        assert _relative_target("iesplan.api.config", node) == expected
