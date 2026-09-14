"""静态架构门禁(宪法 §14.2 原则): 依赖只能指向公开门面, 禁止反向与穿透依赖。

依赖方向: core 不依赖任何业务模块; application 只做跨域编排与事务,
不直引 models/ORM 与裸表; api 只调完整 application 用例, 不直引 ORM、
领域行为, 不持有事务提交; worker/analysis 只消费统一计算结果、回执与
声明输出, 不穿透计算执行, 不驱动引擎; 领域间直接业务依赖禁止,
跨域组合只归 application, 表真相只归领域 persistence。

通用禁止形态: 跨模块私有符号导入与私有属性访问、直接导入 ORM、裸表与
裸 SQL、非所有者持有事务提交、未登记的跨表访问。静态门禁只查依赖事实
(AST import 与调用点), 不锁定私有符号名, 不阻止未来正式实现;
语义行为由行为测试证明。

稳定允许项只有三类(各见对应门禁注释): models.common 共享基元复用、
无状态领域公开纯函数复用、不可变 contract 复用; 其余门禁均为硬强制,
检出即失败。

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
_WORKER_DIR = _PKG_ROOT / "worker"
_APPLICATION_DIR = _PKG_ROOT / "application"

#: 架构门禁中视为"业务模块"的 iesplan 顶层子包(core 一律禁止依赖)。
#: 判定规则见 _is_business_import: 允许根包 iesplan(仅 __version__)与
#: iesplan.core 子树, 其余 iesplan.* 子包(api/models/storage/worker/
#: engines/analysis/…)均视为业务模块。

# ---------------------------------------------------------------------------
# 门禁 1: core → 业务模块依赖(硬强制)
# ---------------------------------------------------------------------------
# core 不得依赖任何业务模块, 检出即失败。

# ---------------------------------------------------------------------------
# 门禁 2: 跨模块私有符号导入(硬强制)
# ---------------------------------------------------------------------------
# 覆盖形式:
#   - 导入语句: from X import _y / import X._y(imported 名字以下划线开头,
#     排除 __dunder__(如 __version__ 属公开约定));
#   - 属性访问: 已导入模块别名上的私有属性(如 svc._helper)。

# ---------------------------------------------------------------------------
# 门禁 3: api → ORM 直接导入(硬强制)
# ---------------------------------------------------------------------------
# 扫描 api 下 iesplan.models.* 与 iesplan.db 会话符号导入, 检出即失败。
# get_db 依赖注入合法, 不在扫描范围。

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
    其余 iesplan.* 子包(api/models/storage/worker/engines/…)均禁止。
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
                    by_line.setdefault(node.lineno, set()).update(a.name for a in node.names if a.name != "*")
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
    """架构门禁: 禁止 core 依赖业务模块(宪法 §14.2)。"""
    detected = _find_core_business_imports()
    assert not detected, f"core 依赖业务模块: {detected}"


def test_no_cross_module_private_imports():
    """架构门禁: 禁止跨模块导入私有符号(宪法 §14.2)。"""
    detected = _find_private_symbol_imports()
    assert not detected, f"跨模块私有符号导入: {detected}"


def test_api_no_direct_orm_imports():
    """架构门禁: 禁止 API 直接导入 ORM(宪法 §14.2, get_db 依赖注入合法)。"""
    detected = _find_api_orm_imports()
    assert not detected, f"API 直接导入 ORM: {detected}"


def test_worker_no_direct_orm_imports():
    """架构门禁: 禁止 Worker 直接导入 ORM(ORM 查询归 application.worker)。

    硬强制: worker 下出现 iesplan.models.* 或 iesplan.db 会话符号导入即失败。
    """
    detected = _find_api_orm_imports(scan_root=_WORKER_DIR)
    assert not detected, f"Worker 直接导入 ORM(需上收至 application.worker 用例): {detected}"


# ---------------------------------------------------------------------------
# 门禁 4: api → 事务提交(硬强制)
# ---------------------------------------------------------------------------
# 事务只由 application 提交/回滚(宪法 §5.4); api 下出现 .commit()/.rollback()
# 调用即失败。

# ---------------------------------------------------------------------------
# 门禁 6: worker → services 直接依赖(硬强制)
# ---------------------------------------------------------------------------
# 业务快照读取与跨模块编排归 application.worker; worker 下出现
# iesplan.services.* 导入即失败。

# ---------------------------------------------------------------------------
# 门禁 7: analysis → 计算执行直接依赖(硬强制)
# ---------------------------------------------------------------------------
# analysis 只消费 ComputeResult、ExecutionReceipt 与声明输出; 出现
# engines/services/assembly.plan 导入即失败。

# ---------------------------------------------------------------------------
# 门禁 8: 表归属清单 + 跨表访问允许项 (键 = (访问方模块, models 子模块))
# ---------------------------------------------------------------------------
# TABLE_OWNERS 声明每张业务表(以 models 子模块计)的唯一领域归属; 除下述
# 稳定允许项外, 跨归属访问直接失败。
# 说明: models.common 仅为共享基元(bigint_pk/正则, 无业务表); models.__init__
# 为兼容重导出, 不视为领域归属。
TABLE_OWNERS: dict[str, str] = {
    "audit": "audit",
    "calc": "task",
    "common": "shared(无业务表, 仅基元)",
    "config_revision": "config",
    "dataset": "dataset",
    "draft_revision": "model-template-draft",
    "identity": "identity",
    "immutable_triggers": "infra(无业务表, 触发器常量)",
    "model": "model",
    "model_template": "model-template",
    "project": "project",
    "project_model": "project-model",
    "result": "result",
    "uncertainty": "uncertainty",
}

#: 稳定允许项: 跨域表访问必须为零。无业务表的共享列基元(bigint_pk/
#: 正则等)经 iesplan.db 基础设施复用, 不构成跨域业务访问, 故不计入;
#: 允许集保持为空, 任何新增跨域表访问都会使门禁 8 失败。
ALLOWED_SHARED_PRIMITIVE_IMPORTS: set[tuple[str, str]] = set()

#: 门禁 8 扫描范围: 各表所有者域的 persistence.py/tables.py。
#: 非表所有者(application/api/worker/migrations 等)由其他门禁覆盖;
#: db.py 的集中 metadata 注册是 Wave3 迁入 bootstrap 前的过渡位, 不参评。
_NON_OWNER_DIRS = frozenset(
    {
        "application",
        "api",
        "worker",
        "core",
        "migrations",
        "cli",
        "engines",
        "__pycache__",
    }
)


def _find_api_commit_calls(scan_root: Path = _API_DIR, pkg_root: Path = _PKG_ROOT) -> list[tuple[str, int]]:
    """门禁 4: 扫描 api 下所有 .commit()/.rollback() 调用, 返回 (模块, 行号)。"""
    found: list[tuple[str, int]] = []
    for path, mod in _iter_modules(scan_root, pkg_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("commit", "rollback")
            ):
                found.append((mod, node.lineno))
    return found


def _find_worker_service_imports(
    scan_root: Path = _PKG_ROOT / "worker", pkg_root: Path = _PKG_ROOT
) -> set[tuple[str, str]]:
    """门禁 6: 扫描 worker 下所有 iesplan.services.* 导入, 返回 (模块, 目标) 集合。"""
    found: set[tuple[str, str]] = set()
    for path, mod in _iter_modules(scan_root, pkg_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == "iesplan.services":
                    found.update((mod, f"iesplan.services.{a.name}") for a in node.names if a.name != "*")
                elif node.module.startswith("iesplan.services."):
                    found.add((mod, node.module))
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "iesplan.services" or a.name.startswith("iesplan.services."):
                        found.add((mod, a.name))
    return found


def _find_analysis_engine_imports(
    scan_root: Path = _PKG_ROOT / "analysis", pkg_root: Path = _PKG_ROOT
) -> set[tuple[str, str]]:
    """门禁 7: 扫描 analysis 下 engines/services/assembly.plan 导入。"""
    found: set[tuple[str, str]] = set()
    for path, mod in _iter_modules(scan_root, pkg_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                target = node.module
                if target == "iesplan.engines" or target.startswith("iesplan.engines."):
                    found.add((mod, target))
                elif target == "iesplan.services":
                    found.update((mod, f"iesplan.services.{a.name}") for a in node.names if a.name != "*")
                elif target.startswith("iesplan.services."):
                    found.add((mod, target))
                elif target == "iesplan.assembly.plan" or target.startswith("iesplan.assembly.plan."):
                    found.add((mod, target))
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if (
                        a.name == "iesplan.engines"
                        or a.name.startswith("iesplan.engines.")
                        or a.name == "iesplan.services"
                        or a.name.startswith("iesplan.services.")
                        or a.name == "iesplan.assembly.plan"
                        or a.name.startswith("iesplan.assembly.plan.")
                    ):
                        found.add((mod, a.name))
    return found


def _owner_domains(pkg_root: Path = _PKG_ROOT) -> set[str]:
    """返回表所有者候选域: 含 __init__.py 的顶级包目录, 去掉非所有者。"""
    domains: set[str] = set()
    for child in pkg_root.iterdir():
        if child.is_dir() and (child / "__init__.py").is_file():
            if child.name not in _NON_OWNER_DIRS:
                domains.add(child.name)
    return domains


def _find_cross_model_imports(pkg_root: Path = _PKG_ROOT) -> set[tuple[str, str]]:
    """门禁 8: 扫描各表所有者域的 persistence/tables 对他域同类模块的直接导入,
    返回 (访问方, 目标)。顶层混合 models/ 已删除, 故不再扫描 iesplan.models.*。
    """
    found: set[tuple[str, str]] = set()
    domains = _owner_domains(pkg_root)
    for domain in sorted(domains):
        for fname in ("persistence.py", "tables.py"):
            path = pkg_root / domain / fname
            if not path.is_file():
                continue
            mod = f"iesplan.{domain}.{fname[:-3]}"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                targets: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    targets.append(node.module)
                elif isinstance(node, ast.Import):
                    targets.extend(a.name for a in node.names)
                for target in targets:
                    parts = target.split(".")
                    if (
                        len(parts) == 3
                        and parts[0] == "iesplan"
                        and parts[2] in ("persistence", "tables")
                        and parts[1] != domain
                    ):
                        found.add((mod, target))
    return found


def test_api_no_transaction_commit():
    """架构门禁: API 不得提交/回滚事务(宪法 §5.4, 事务只归 application)。"""
    detected = _find_api_commit_calls()
    assert not detected, f"API 层事务提交/回滚(须迁移到 application 用例): {detected}"


def test_worker_no_direct_domain_services():
    """架构门禁: Worker 不得直接依赖 services(业务快照读取与编排归 application.worker)。"""
    detected = _find_worker_service_imports()
    assert not detected, f"Worker 直接依赖 services(须经 application.worker 边界): {sorted(detected)}"


def test_analysis_no_direct_engine_or_services():
    """架构门禁: analysis 不得直接驱动引擎/services/拼装 plan(只消费统一计算结果与声明输出)。"""
    detected = _find_analysis_engine_imports()
    assert not detected, f"analysis 计算执行直接依赖: {sorted(detected)}"


def test_table_ownership_no_new_cross_imports():
    """架构门禁: 跨表 ORM 访问只允许稳定允许项(见 ALLOWED_SHARED_PRIMITIVE_IMPORTS)。"""
    detected = _find_cross_model_imports()
    assert detected == ALLOWED_SHARED_PRIMITIVE_IMPORTS, (
        f"跨表 ORM 访问变化(新增: {sorted(detected - ALLOWED_SHARED_PRIMITIVE_IMPORTS)}, "
        f"缺失: {sorted(ALLOWED_SHARED_PRIMITIVE_IMPORTS - detected)})"
    )


# ---------------------------------------------------------------------------
# 门禁 9–11: application/services 残留、application 裸表/SQL、Worker 事务(硬强制)
# ---------------------------------------------------------------------------
# application 不得导入 iesplan.services, 不得在应用层重声明表/裸 SQL;
# Worker 不得持有 commit/rollback。检出即失败。


def _find_app_service_imports(
    scan_root: Path = _APPLICATION_DIR, pkg_root: Path = _PKG_ROOT
) -> list[tuple[str, int, str]]:
    """门禁 9: 扫描 application 下 iesplan.services.* 导入。返回 (模块, 行号, 目标)。"""
    found: list[tuple[str, int, str]] = []
    for path, mod in _iter_modules(scan_root, pkg_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == "iesplan.services":
                    found.extend((mod, node.lineno, f"iesplan.services:{a.name}") for a in node.names)
                elif node.module.startswith("iesplan.services."):
                    found.append((mod, node.lineno, node.module))
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "iesplan.services" or a.name.startswith("iesplan.services."):
                        found.append((mod, node.lineno, a.name))
    return sorted(found)


def _find_app_bare_sql(
    scan_root: Path = _APPLICATION_DIR, pkg_root: Path = _PKG_ROOT
) -> list[tuple[str, int, str]]:
    """门禁 10: 扫描 application 下 sa.table/Table/text 与 sa insert/update/delete。

    领域 persistence 实现不受该禁止(仅扫描 application/)。返回 (模块, 行号, 形态)。
    """
    found: list[tuple[str, int, str]] = []
    for path, mod in _iter_modules(scan_root, pkg_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sa"
                and node.func.attr in ("table", "Table", "text", "insert", "update", "delete")
            ):
                found.append((mod, node.lineno, f"sa.{node.func.attr}"))
    return sorted(found)


def _find_worker_transactions(
    scan_root: Path = _WORKER_DIR, pkg_root: Path = _PKG_ROOT
) -> list[tuple[str, int, str]]:
    """门禁 11: 扫描 worker 下 .commit()/.rollback() 调用。返回 (模块, 行号, 形态)。"""
    found: list[tuple[str, int, str]] = []
    for path, mod in _iter_modules(scan_root, pkg_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("commit", "rollback")
            ):
                found.append((mod, node.lineno, f".{node.func.attr}()"))
    return sorted(found)


def test_application_no_direct_services():
    """架构门禁 9: application 禁止导入 iesplan.services。"""
    detected = set(_find_app_service_imports())
    assert not detected, f"application→services 直调: {sorted(detected)}"


def test_application_no_bare_sql():
    """架构门禁 10: application 禁止裸表/SQL(表真相只归领域 persistence)。"""
    detected = set(_find_app_bare_sql())
    assert not detected, f"application 裸表/SQL: {sorted(detected)}"


def test_worker_no_transactions():
    """架构门禁 11: Worker 禁止 commit/rollback(事务归 application.worker 用例)。"""
    detected = set(_find_worker_transactions())
    assert not detected, f"Worker 事务调用: {sorted(detected)}"


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


# ---------------------------------------------------------------------------
# 门禁 12–16: 依赖方向门禁(硬强制)
# ---------------------------------------------------------------------------
# 门禁只查依赖事实(AST import), 键为 (模块, 目标域)(同一模块多行合并为一项),
# 不绑定私有文件名与行号。sqlalchemy Session 类型注解属事务管道, 不在 ORM
# 门禁之列; 他域 contracts/contracts2 属不可变 contract 复用, 始终允许。

#: 门禁 12–16 共用的业务域包集合(均有 __init__ 公开门面; core/config/db/models 不在列)。
_BUSINESS_DOMAIN_PACKAGES: frozenset[str] = frozenset(
    {
        "audit",
        "configuration",
        "dataset",
        "devices",
        "engines",
        "finance",
        "identity",
        "metrics",
        "model",
        "modeling",
        "package",
        "planning",
        "project",
        "results",
        "storage",
        "tasks",
    }
)

#: 门禁 15/16 的计算执行包集合(Worker/analysis 禁止穿透)。
_COMPUTE_PACKAGES: frozenset[str] = frozenset({"engines", "metrics", "finance", "analysis", "assembly"})

#: 门禁 16 的 analysis 禁止驱动集合(计算执行包 + services + 任务执行 worker)。
_ANALYSIS_FORBIDDEN_PACKAGES: frozenset[str] = frozenset(
    {"engines", "assembly", "services", "finance", "metrics", "worker"}
)


def _top_pkg_of(target: str) -> str:
    """取 iesplan 导入目标的顶层子包名(如 iesplan.metrics.financial → metrics)。"""
    parts = target.split(".")
    return parts[1] if len(parts) > 1 else ""


def _is_contract_target(target: str) -> bool:
    """不可变 contract 复用判定: 他域 contracts/contracts2 子模块一律允许。"""
    return ".contracts" in target or "contracts2" in target


#: 常设复用豁免: (导入方模块前缀, 目标模块前缀)。
#: 四维评估规则归 results 域所有, 其状态词汇与 IRR 分类仍以无状态的
#: iesplan.metrics.validity / iesplan.metrics.financial 为唯一权威(领域公开
#: 纯函数复用, 不复制枚举值)。仅豁免 results 域对该两模块的导入; results
#: 对 metrics 其他子模块的导入仍属违规。
ALLOWED_STATE_MODEL_REUSE: frozenset[tuple[str, str]] = frozenset(
    {
        ("iesplan.results", "iesplan.metrics.validity"),
        ("iesplan.results", "iesplan.metrics.financial"),
    }
)


def _is_exempt_reuse(mod: str, target: str, exempt: frozenset[tuple[str, str]]) -> bool:
    """常设复用豁免判定: 导入方与目标同时命中同一豁免条目前缀即豁免。"""
    return any(
        (mod == importer or mod.startswith(importer + "."))
        and (target == allowed or target.startswith(allowed + "."))
        for importer, allowed in exempt
    )


def _iter_domain_imports(
    scan_root: Path,
    forbidden: frozenset[str],
    own_top: str | None = None,
    pkg_root: Path = _PKG_ROOT,
    exempt: frozenset[tuple[str, str]] = frozenset(),
) -> set[tuple[str, str]]:
    """通用依赖事实扫描: 返回 (模块, 目标顶层包) 集合。

    覆盖三种绝对导入形态: import iesplan.X[.Y]、from iesplan[.X…] import …、
    from iesplan import X(含根包直引领域形态, 如 project/access 经根包调用 identity)。
    contract 目标与包外目标自动排除; own_top 指定时排除自身域;
    exempt 命中(ALLOWED_STATE_MODEL_REUSE)时排除常设复用。
    """
    found: set[tuple[str, str]] = set()
    for path, mod in _iter_modules(scan_root, pkg_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == "iesplan":
                    targets.extend(f"iesplan.{a.name}" for a in node.names if a.name != "*")
                else:
                    targets.append(node.module)
            for target in targets:
                if not target.startswith("iesplan."):
                    continue
                if _is_contract_target(target):
                    continue
                if _is_exempt_reuse(mod, target, exempt):
                    continue
                top = _top_pkg_of(target)
                if top not in forbidden:
                    continue
                if own_top is not None and top == own_top:
                    continue
                found.add((mod, top))
    return found


def _find_application_orm_imports(
    scan_root: Path = _APPLICATION_DIR, pkg_root: Path = _PKG_ROOT
) -> set[tuple[str, str]]:
    """门禁 12: 扫描 application 下 iesplan.models.* 与 iesplan.db 导入。

    返回 (模块, 目标) 集合, 目标为 models 子模块名或 db。sqlalchemy 类型注解
    不在扫描范围(事务管道, 非 ORM 表依赖)。
    """
    found: set[tuple[str, str]] = set()
    for path, mod in _iter_modules(scan_root, pkg_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == "iesplan.models" or node.module.startswith("iesplan.models."):
                    found.add((mod, node.module.split(".")[2] if "." in node.module[8:] else "models"))
                elif node.module == "iesplan.db" or node.module.startswith("iesplan.db."):
                    found.add((mod, "db"))
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name == "iesplan.models" or a.name.startswith("iesplan.models."):
                        found.add((mod, a.name.split(".")[2] if a.name.count(".") > 1 else "models"))
                    elif a.name == "iesplan.db" or a.name.startswith("iesplan.db."):
                        found.add((mod, "db"))
    return found


def test_application_no_models_orm():
    """架构门禁 12: application 禁止直引 iesplan.models/ORM(表真相只归领域 persistence)。"""
    detected = _find_application_orm_imports()
    assert not detected, f"application→models/ORM 穿透: {sorted(detected)}"


def test_api_no_direct_domain_behavior():
    """架构门禁 13: API 禁止直接调用领域行为组织业务(只调完整 application 用例)。

    领域 *.contracts DTO 复用允许; 其余领域根包/行为子模块导入即违规。
    """
    detected = _iter_domain_imports(_API_DIR, _BUSINESS_DOMAIN_PACKAGES)
    assert not detected, f"API→领域行为直调: {sorted(detected)}"


def _find_cross_domain_behavior_imports(pkg_root: Path = _PKG_ROOT) -> set[tuple[str, str]]:
    """门禁 14: 扫描各业务域对他域行为的直接依赖。返回 (模块, 他域) 集合。

    领域间不得直接组合业务: 他域根包/行为子模块导入即违规; 他域 contracts
    属不可变 contract 复用, 允许; ALLOWED_STATE_MODEL_REUSE 属常设复用, 允许。
    跨领域授权与工作流归 application。
    """
    found: set[tuple[str, str]] = set()
    for domain in sorted(_BUSINESS_DOMAIN_PACKAGES):
        scan_root = pkg_root / domain
        if not scan_root.is_dir():
            continue
        found |= _iter_domain_imports(
            scan_root, _BUSINESS_DOMAIN_PACKAGES, own_top=domain, exempt=ALLOWED_STATE_MODEL_REUSE
        )
    return found


def test_no_forbidden_cross_domain_deps():
    """架构门禁 14: 禁止领域间直接业务依赖(跨域组合只归 application)。"""
    detected = _find_cross_domain_behavior_imports()
    assert not detected, f"领域间直接业务依赖: {sorted(detected)}"


def test_worker_no_compute_penetration():
    """架构门禁 15: Worker 禁止计算穿透(不拼 solver 命令、不解释装配、不承担结果分析)。

    Worker 只保留任务领取、租约、调用 application.worker 与隔离执行壳。
    """
    detected = _iter_domain_imports(_WORKER_DIR, _COMPUTE_PACKAGES)
    assert not detected, f"Worker 计算穿透: {sorted(detected)}"


def test_analysis_no_engine_driving():
    """架构门禁 16: analysis 禁止驱动 engine(不构造 plan、不调用引擎, 只消费统一计算结果)。

    门禁 7 已覆盖 engines/services/assembly.plan 直接导入; 本门禁覆盖
    finance/metrics/worker 等计算执行穿透。
    """
    detected = _iter_domain_imports(_PKG_ROOT / "analysis", _ANALYSIS_FORBIDDEN_PACKAGES)
    assert not detected, f"analysis 驱动 engine: {sorted(detected)}"


def test_stable_allowed_items_still_present():
    """架构门禁 17: 稳定允许项必须仍被对应扫描器命中(消失即失败)。"""
    detected = _find_cross_model_imports()
    assert ALLOWED_SHARED_PRIMITIVE_IMPORTS <= detected, (
        f"稳定允许项已消失(须同步更新 ALLOWED_SHARED_PRIMITIVE_IMPORTS): "
        f"{sorted(ALLOWED_SHARED_PRIMITIVE_IMPORTS - detected)}"
    )


def _is_application_impl_module_import(
    target: str, imported_name: str, *, pkg_root: Path = _PKG_ROOT
) -> bool:
    """Return whether an imported name resolves to an application implementation module.

    ``from iesplan.application.<family> import <name>`` has a three-segment
    import target, so checking only the target depth misses module objects such
    as ``maintenance`` or ``views``.  Resolve only names backed by a real
    ``.py`` module or package directory; ordinary public functions with the
    same import shape remain allowed.
    """
    parts = target.split(".")
    if len(parts) != 3 or parts[:2] != ["iesplan", "application"]:
        return False
    family_dir = pkg_root / "application" / parts[2]
    return (family_dir / f"{imported_name}.py").is_file() or (
        family_dir / imported_name / "__init__.py"
    ).is_file()


def _find_application_impl_imports() -> set[tuple[str, str]]:
    """扫描 api/worker 对 application 子包实现文件的直接导入。

    只允许 ``iesplan.application`` 根与 ``iesplan.application.<族>``
    用例族公开门面(深度 ≤ 3); ``iesplan.application.<族>.<实现文件>``
    (深度 > 3)一律记录, 不设例外表。
    """
    found: set[tuple[str, str]] = set()
    for scan_root in (_API_DIR, _WORKER_DIR):
        for path, mod in _iter_modules(scan_root, _PKG_ROOT):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                targets: list[str] = []
                if isinstance(node, ast.Import):
                    targets.extend(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    # Keep the imported names so a three-segment family import
                    # can distinguish a module object from a public symbol.
                    if node.module.startswith("iesplan.application."):
                        for alias in node.names:
                            if _is_application_impl_module_import(node.module, alias.name):
                                found.add((mod, f"{node.module}.{alias.name}"))
                    targets.append(node.module)
                for target in targets:
                    parts = target.split(".")
                    if len(parts) > 3 and parts[:2] == ["iesplan", "application"]:
                        found.add((mod, target))
    return found


def test_api_worker_use_application_public_facades():
    """架构门禁: api/worker 只经 application 用例族公开门面, 不穿透实现文件。

    跨模块只调用 ``iesplan.application.<族>`` 门面; 实现文件
    (auth_cases/views/maintenance/quotas/endpoint_cases/lifecycle/…)只由
    各族 __init__ 选择性重导出。本门禁只查依赖形态(模块深度), 不锁定
    函数名、行号, 不设白名单/债务表。
    """
    detected = _find_application_impl_imports()
    assert not detected, f"api/worker 穿透 application 实现文件: {sorted(detected)}"


def test_application_gate_detects_family_module_object_import():
    """三段式族导入中的实现模块对象必须被门禁识别。"""
    assert _is_application_impl_module_import("iesplan.application.tasks", "maintenance")
    assert not _is_application_impl_module_import("iesplan.application.tasks", "enqueue_task")


# ---------------------------------------------------------------------------
# 门禁 18: application 用例族之间禁止穿透他族实现文件(硬强制)
# ---------------------------------------------------------------------------
# 各 application 子包(用例族)之间只经对方 ``iesplan.application.<族>``
# 公开门面组合; 直接导入他族实现文件即失败。覆盖两种形态:
#   - ``iesplan.application.<他族>.<实现...>``(深度 > 3, 含 import 与 from);
#   - 三段式族导入中的实现模块对象
#     (``from iesplan.application.<他族> import <实现模块>``, 以文件系统判定,
#     复用 _is_application_impl_module_import)。
# 允许: 同族内部导入、族公开门面导入(``iesplan.application.<族>``)、
# application 根直属模块(族外共享核, 如 namespace)不参评。
# 本门禁只查依赖形态(导入方模块, 目标实现), 不锁定函数名/行号, 不设白名单。


def _application_family_of(path: Path, scan_root: Path = _APPLICATION_DIR) -> str | None:
    """取 application 下源码文件所属用例族名(首级子包目录名)。

    仅首级为包目录(含 __init__.py)时返回族名; application 根直属模块
    返回 None(不参评)。
    """
    try:
        rel = path.relative_to(scan_root)
    except ValueError:
        return None
    if len(rel.parts) < 2:
        return None
    family_dir = scan_root / rel.parts[0]
    if family_dir.is_dir() and (family_dir / "__init__.py").is_file():
        return rel.parts[0]
    return None


def _find_cross_application_family_impl_imports(
    scan_root: Path = _APPLICATION_DIR, pkg_root: Path = _PKG_ROOT
) -> set[tuple[str, str]]:
    """门禁 18: 扫描 application 各子包对他族实现文件的直接导入。

    返回 (模块, 目标实现) 集合, 同一模块多行合并为一项。
    """
    found: set[tuple[str, str]] = set()
    for path, mod in _iter_modules(scan_root, pkg_root):
        family = _application_family_of(path, scan_root)
        if family is None:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    parts = a.name.split(".")
                    if len(parts) > 3 and parts[:2] == ["iesplan", "application"]:
                        if parts[2] != family:
                            found.add((mod, a.name))
            elif isinstance(node, ast.ImportFrom) and (node.module or node.level):
                target = _relative_target(mod, node)
                if not target.startswith("iesplan.application."):
                    continue
                parts = target.split(".")
                if len(parts) == 3:
                    # 族门面导入: 仅实现模块对象属穿透, 公开符号允许。
                    for a in node.names:
                        if a.name == "*":
                            continue
                        if _is_application_impl_module_import(
                            target, a.name, pkg_root=pkg_root
                        ) and parts[2] != family:
                            found.add((mod, f"{target}.{a.name}"))
                elif len(parts) > 3 and parts[2] != family:
                    found.add((mod, target))
    return found


def test_application_no_cross_family_impl_imports():
    """架构门禁 18: application 用例族之间禁止穿透他族实现文件(只经对方族公开门面组合)。"""
    detected = _find_cross_application_family_impl_imports()
    assert not detected, f"application 用例族实现文件穿透: {sorted(detected)}"
