"""0.3.0 C5: 静态架构门禁测试(宪法 §14.2 原则)。

架构门禁要求 CI 逐步加入并最终强制(宪法 §14.2 原则，门禁细则见
docs/development/development-workflow.md「架构门禁」): 禁止 core 依赖业务模块、
禁止跨模块导入私有符号、禁止 API 直接导入 ORM。本文件建立 0.3.0 基线门禁:

  1. test_core_no_business_dependencies  — core 不允许 import 任何业务模块;
  2. test_no_cross_module_private_imports — 禁止 from X import _y / import X._y
     以及模块对象上的私有属性访问(如 config_service._row_to_config);
  3. test_api_no_direct_orm_imports      — api 不允许直接 import iesplan.models.*
     与 iesplan.db 的 Session/Base 等(get_db 依赖注入除外)。

解耦重构切片 1(2026-09-11, 临时指导 docs/reviews/architecture-refactor-workflow.md
§1)在不改变业务行为的前提下追加基线门禁:

  4. test_api_no_transaction_commit     — api 不允许调用 .commit()/.rollback(),
     事务只由 application 提交/回滚;
  5. test_api_no_multi_service_fanout   — 每个 api 模块至多依赖一个
     iesplan.services.* 子模块(目标: 单个 application 用例);
  6. test_worker_no_direct_domain_services — worker 不允许直接 import
     iesplan.services.*(业务快照读取与编排归 application.worker);
  7. test_analysis_no_direct_engine_or_services — analysis 不允许直接 import
     iesplan.engines.* / iesplan.services.* / iesplan.assembly.plan
     (目标: 只消费 ComputeResult、回执与声明输出);
  8. test_table_ownership_no_new_cross_imports — services/worker/analysis/
     storage/application 对 iesplan.models.* 的跨表访问收敛到
     WHITELIST_CROSS_MODEL_IMPORTS, 表归属见 TABLE_OWNERS。

策略: 现有违规列入文件头部的 WHITELIST_* 常量(白名单基线, 注释写明
整改 TODO), 新增违规直接断言失败。后续切片按注释逐条整改后移除白名单条目,
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

#: 架构门禁中视为"业务模块"的 iesplan 顶层子包(core 一律禁止依赖)。
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
    (
        "iesplan.analysis.sensitivity",
        "_financial_to_dict",
    ): "同域 wrapper 私有财务结果序列化辅助; TODO: 提升为 analysis 公开 API 后移除。",
    ("iesplan.analysis.sensitivity", "_jsonable_kpi"): "同域 wrapper 私有 KPI 可 JSON 化辅助; TODO: 同上。",
    # ---- assembly 域内部: rules 子包复用 checker/schema 私有工具 ----
    (
        "iesplan.assembly.rules.completeness",
        "_split_model",
    ): "assembly 域内 rules 复用 checker 私有模型拆分函数; TODO: 提升公开。",
    (
        "iesplan.assembly.rules.solvability",
        "_PEAK_PARAM_BY_LOAD",
    ): "assembly 域内 rules 复用 checker 私有峰值参数表; TODO: 提升公开。",
    (
        "iesplan.assembly.rules.solvability",
        "_to_watts",
    ): "assembly 域内 rules 复用 checker 私有单位换算; TODO: 提升公开。",
    (
        "iesplan.assembly.checker",
        "_QUANTITY_DIMS",
    ): "assembly 域内 checker 复用 schema 私有量纲常量; TODO: 提升公开。",
    # ---- engines 域内部: planning 复用 eval_run 私有取参函数 ----
    (
        "iesplan.engines.planning",
        "_param",
    ): "engines 域内 planning 复用 eval_run 私有运行参数读取; TODO: 提升公开。",
    # worker → analysis 私有穿透已整改(0.6.5): 符号提升为 analysis 公开 API。
    # ---- api → services: API 层直接访问服务私有函数 ----
    (
        "iesplan.api.config",
        "config_service._row_to_config",
    ): "API 层访问 services.config 私有配置序列化(现状违规); "
    "TODO: services.config 提供公开 serializer 后移除。",
    (
        "iesplan.api.projects",
        "project_service._is_admin",
    ): "API 层访问 services.project 私有权限判定(现状违规); TODO: 提升公开权限 API 后移除。",
    # ---- services 域内部: identity 复用 project 私有审计写入 ----
    (
        "iesplan.services.identity",
        "project_service._audit",
    ): "服务层间复用 project 私有审计写入; TODO: 提升公开审计 API。",
}

# ---------------------------------------------------------------------------
# 门禁 3 白名单: api → ORM 直接导入 (键 = (模块路径, 行号), 值为该行豁免的符号集)
# ---------------------------------------------------------------------------
# 仅豁免导入语句本身及其列出的符号: 同文件新行导入新 ORM 符号仍会报错。
# 每条目均需整改: 经 services 公开面访问数据, 整改后移除条目。
WHITELIST_API_ORM: dict[tuple[str, int], frozenset[str]] = {
    # ---- admin.py: 管理端运维直接查询 ORM ----
    ("iesplan.api.admin", 33): frozenset({"RetentionRule"}),
    ("iesplan.api.admin", 34): frozenset(
        {"ComputeSlot", "Task", "TaskAttempt", "TaskDiagnostic", "TaskLease"}
    ),
    ("iesplan.api.admin", 35): frozenset({"User"}),
    ("iesplan.api.admin", 36): frozenset({"AdminMaintenanceAction"}),
    # ---- health.py: 健康检查直接计数 ORM ----
    ("iesplan.api.health", 26): frozenset({"Task"}),
    ("iesplan.api.health", 27): frozenset({"User"}),
    ("iesplan.api.health", 28): frozenset({"Project"}),
    # ---- results.py: 结果查询任务状态与校验正则 ----
    # (切片 5: assess 端点经 tasks_service.ensure_task_belongs 取任务, 移除 Task)
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
    """架构门禁: 禁止 core 依赖业务模块(宪法 §14.2)。基线全绿, 新增即报错。"""
    detected = _find_core_business_imports()
    new = [(m, line, src) for (m, line, src) in detected if (m, line) not in WHITELIST_CORE_BUSINESS_DEPS]
    assert not new, f"core 依赖业务模块(新增违规, 需整改或登记白名单): {new}"


def test_no_cross_module_private_imports():
    """架构门禁: 禁止跨模块导入私有符号(宪法 §14.2)。现状违规在白名单, 新增即报错。"""
    detected = _find_private_symbol_imports()
    new = [(m, s) for (m, s) in detected if (m, s) not in WHITELIST_PRIVATE_IMPORTS]
    assert not new, f"跨模块私有符号导入(新增违规, 需整改或登记白名单): {new}"


def test_api_no_direct_orm_imports():
    """架构门禁: 禁止 API 直接导入 ORM(宪法 §14.2, get_db 依赖注入合法)。

    白名单按 (模块, 行号) 豁免, 并校验该行导入符号 ⊆ 白名单符号集,
    同文件新行或白名单行新增符号都会报错。
    """
    detected = _find_api_orm_imports()
    new: list[tuple[str, int, list[str]]] = []
    for mod, line, symbols in detected:
        allowed = WHITELIST_API_ORM.get((mod, line))
        if allowed is None or not symbols.issubset(allowed):
            new.append((mod, line, sorted(symbols)))
    assert not new, f"API 直接导入 ORM(新增违规, 需整改或登记白名单): {new}"


# ---------------------------------------------------------------------------
# 门禁 4 白名单: api → 事务提交 (键 = (模块路径, 行号))
# ---------------------------------------------------------------------------
# 基线核查(2026-09-11, 解耦重构切片 1): API 层共 36 处 .commit() 调用, 事务
# 所有权落在路由层, 违反"事务只由 application 提交/回滚"(宪法 §5.4)。
# 后续切片按资源域迁移到 application 用例后逐条移除; 白名单清空后硬强制。
WHITELIST_API_COMMIT: set[tuple[str, int]] = {
    ("iesplan.api.tasks", 93),
    ("iesplan.api.tasks", 148),
    ("iesplan.api.tasks", 169),
    ("iesplan.api.admin", 253),
    # (切片 4: get_auth_context 会话写入收敛到 services, 移除 230/240/249 三处提交)
    ("iesplan.api.auth", 568),
    ("iesplan.api.auth", 639),
    ("iesplan.api.auth", 647),
    ("iesplan.api.datasets", 204),
    ("iesplan.api.objects", 139),
    ("iesplan.api.objects", 172),
    ("iesplan.api.objects", 188),
    ("iesplan.api.validation", 62),
    ("iesplan.api.validation", 78),
    ("iesplan.api.projects", 132),
    ("iesplan.api.projects", 188),
    ("iesplan.api.projects", 203),
    ("iesplan.api.projects", 246),
    ("iesplan.api.projects", 265),
    ("iesplan.api.projects", 277),
    ("iesplan.api.projects", 292),
    ("iesplan.api.projects", 315),
    ("iesplan.api.projects", 361),
    ("iesplan.api.projects", 373),
    ("iesplan.api.config_revisions", 139),
    ("iesplan.api.config_revisions", 178),
    ("iesplan.api.config_revisions", 199),
    ("iesplan.api.config_revisions", 244),
    ("iesplan.api.config_revisions", 274),
    ("iesplan.api.exports", 68),
    ("iesplan.api.exports", 134),
    ("iesplan.api.results", 121),
    ("iesplan.api.results", 145),
    ("iesplan.api.results", 221),
}

# ---------------------------------------------------------------------------
# 门禁 5 白名单: api 跨 service 编排 (值为现状多 service 依赖的模块路径)
# ---------------------------------------------------------------------------
# 基线核查(2026-09-11, 切片 1): 以下 api 模块同时依赖 2~3 个 services 子模块,
# 即在路由层组织跨 service 业务流程。目标是每个端点只调用一个 application 用例
# (model_templates/project_models 已示范该方向)。迁移一个模块就从本集合移除一项。
WHITELIST_API_FANOUT: set[str] = {
    "iesplan.api.tasks",  # services.project + services.tasks
    "iesplan.api.model",  # services.model + services.project
    "iesplan.api.config",  # services.config + services.project
    "iesplan.api.admin",  # services.audit + services.queue + services.tasks
    "iesplan.api.auth",  # services.identity + services.project + services.external_auth
    "iesplan.api.datasets",  # services.dataset + services.project
    "iesplan.api.validation",  # services.project + services.validation
    "iesplan.api.projects",  # services.package + services.project
    "iesplan.api.config_revisions",  # services.config_revisions + services.project
    "iesplan.api.results",  # services.project + services.results + services.tasks
}

# ---------------------------------------------------------------------------
# 门禁 6 白名单: worker → services 直接依赖 (键 = (worker 模块, services 目标))
# ---------------------------------------------------------------------------
# 基线核查(2026-09-11, 切片 1): Worker 直接读取业务 service, 任务执行边界锁死
# 在领域实现上。目标是业务快照读取与跨模块编排移入 application.worker, worker
# 只保留领取任务、租约、取消、分派和结果提交。迁移后逐项移除。
WHITELIST_WORKER_SERVICES: set[tuple[str, str]] = {
    ("iesplan.worker.lease", "iesplan.services.queue"),
    ("iesplan.worker.lease", "iesplan.services.tasks"),
    ("iesplan.worker.main", "iesplan.services.queue"),
    ("iesplan.worker.runner", "iesplan.services.dataset"),
    ("iesplan.worker.runner", "iesplan.services.project"),
    ("iesplan.worker.executors", "iesplan.services.queue"),
    ("iesplan.worker.executors", "iesplan.services.tasks"),
}

# ---------------------------------------------------------------------------
# 门禁 7 白名单: analysis → 计算执行直接依赖 (键 = (analysis 模块, 目标))
# ---------------------------------------------------------------------------
# 基线核查(2026-09-11, 切片 1): analysis 直接驱动旧计算引擎/拼装 plan/调用
# services, 结果分析与计算执行无法分别演进。目标是 analysis 只消费 ComputeResult、
# ExecutionReceipt 与声明输出。0.8 计算链实现前计算入口保持显式未实现, 不恢复
# 旧命令注册表或旧机理函数。迁移后逐项移除。
WHITELIST_ANALYSIS_ENGINE: set[tuple[str, str]] = {
    ("iesplan.analysis.wrapper", "iesplan.engines.eval_run"),
    ("iesplan.analysis.wrapper", "iesplan.assembly.plan"),
    ("iesplan.analysis.sensitivity", "iesplan.services.tasks"),
    ("iesplan.analysis.sensitivity", "iesplan.services.identity"),
}

# ---------------------------------------------------------------------------
# 门禁 8: 表归属清单 + 跨表访问白名单 (键 = (访问方模块, models 子模块))
# ---------------------------------------------------------------------------
# TABLE_OWNERS 声明每张业务表(以 models 子模块计)的唯一领域归属; 跨归属访问
# 必须登记在 WHITELIST_CROSS_MODEL_IMPORTS, 新增跨表访问直接失败。后续切片按
# project → identity → dataset → config → task → result → package 顺序把 ORM 查询
# 收敛到归属领域 repository, 每收敛一条就从白名单移除一项。
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

WHITELIST_CROSS_MODEL_IMPORTS: set[tuple[str, str]] = {
    # ---- services ----
    # (切片 4: services.external_auth 经 identity 域, 移除本项)
    ("iesplan.services.config", "audit"),
    # (切片 5: services.config CalcConfig 改经 configuration 域, 移除 calc)
    ("iesplan.services.config", "model"),
    ("iesplan.services.model", "model"),
    ("iesplan.services.identity", "common"),  # 仅正则基元(无业务表); 切片 4 已移除 identity 表访问
    # (切片 5: services.config_revisions 经 configuration 域, 移除本项)
    ("iesplan.services.tasks", "common"),  # 仅幂等键正则基元(无业务表)
    # (切片 5: services.tasks 改经领域门面, 移除 calc/dataset/identity/result/uncertainty)
    ("iesplan.services.validation", "audit"),
    ("iesplan.services.validation", "dataset"),
    ("iesplan.services.validation", "identity"),
    ("iesplan.services.results", "audit"),
    # (切片 5: services.results 经 results/tasks/project 域门面, 移除 calc/identity/result)
    # ---- project 域 repository 实现（切片 3；唯一允许访问 projects 系表的实现） ----
    ("iesplan.project.persistence", "project"),
    ("iesplan.services.package", "audit"),
    ("iesplan.services.package", "calc"),
    # (切片 5: services.package 经 configuration/results 域门面, 移除 config_revision/result)
    ("iesplan.services.package", "dataset"),
    ("iesplan.services.package", "identity"),
    ("iesplan.services.package", "project"),
    ("iesplan.services.audit", "audit"),
    ("iesplan.services.audit", "identity"),
    # (切片 4: services.dataset 经 dataset/identity/project 域 repository, 移除 3 项)
    ("iesplan.services.project", "audit"),
    ("iesplan.services.project", "calc"),
    ("iesplan.services.project", "identity"),
    # ---- worker ----
    ("iesplan.worker.lease", "calc"),
    ("iesplan.worker.lease", "result"),
    ("iesplan.worker.executors", "calc"),
    ("iesplan.worker.executors", "result"),
    ("iesplan.worker.executors", "uncertainty"),
    ("iesplan.worker.runner", "calc"),
    ("iesplan.worker.runner", "dataset"),
    ("iesplan.worker.runner", "project"),
    ("iesplan.worker.runner", "uncertainty"),
    # ---- storage ----
    # service 直接写 AuditLog/RetentionRule, 切片 11 改由 audit facade/application 记录。
    ("iesplan.storage.service", "audit"),
    # persistence 仅用 common 基元, 无业务表访问, 保留。
    ("iesplan.storage.persistence", "common"),
    # ---- application(目标编排层, 先登记现状; 切片 6 改调领域公开接口) ----
    ("iesplan.application.model_templates.service", "audit"),
    ("iesplan.application.model_templates.service", "model_template"),
    ("iesplan.application.model_templates.service", "draft_revision"),
    ("iesplan.application.projects.model_save", "audit"),
    ("iesplan.application.projects.model_save", "project"),
    ("iesplan.application.projects.model_save", "project_model"),
    ("iesplan.application.namespace", "identity"),
}

#: 门禁 8 扫描范围(api 由门禁 3 覆盖, models 自身与 core 不参评)
_SCAN_OWNERSHIP_DIRS = ("services", "worker", "analysis", "storage", "application")


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


def _find_api_service_fanout(scan_root: Path = _API_DIR, pkg_root: Path = _PKG_ROOT) -> dict[str, set[str]]:
    """门禁 5: 统计每个 api 模块依赖的 iesplan.services.* 子模块集合。"""
    fanout: dict[str, set[str]] = {}
    for path, mod in _iter_modules(scan_root, pkg_root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        leaves: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module == "iesplan.services":
                    leaves.update(f"services.{a.name}" for a in node.names if a.name != "*")
                elif node.module.startswith("iesplan.services."):
                    leaves.add(node.module[len("iesplan.") :])
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("iesplan.services."):
                        leaves.add(a.name[len("iesplan.") :])
        if leaves:
            fanout[mod] = leaves
    return fanout


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


def _find_cross_model_imports(pkg_root: Path = _PKG_ROOT) -> set[tuple[str, str]]:
    """门禁 8: 扫描领域目录下所有 iesplan.models.* 导入, 返回 (访问方, models 子模块)。"""
    found: set[tuple[str, str]] = set()
    for dirname in _SCAN_OWNERSHIP_DIRS:
        scan_root = pkg_root / dirname
        if not scan_root.is_dir():
            continue
        for path, mod in _iter_modules(scan_root, pkg_root):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    if node.module == "iesplan.models":
                        found.update((mod, a.name) for a in node.names if a.name != "*")
                    elif node.module.startswith("iesplan.models."):
                        found.add((mod, node.module.split(".")[2]))
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.startswith("iesplan.models."):
                            found.add((mod, a.name.split(".")[2]))
    return found


def test_api_no_transaction_commit():
    """架构门禁: API 不得提交/回滚事务(宪法 §5.4)。现状 36 处在白名单, 新增即报错。"""
    detected = _find_api_commit_calls()
    new = [(m, line) for (m, line) in detected if (m, line) not in WHITELIST_API_COMMIT]
    assert not new, f"API 层新增事务提交/回滚(需迁移到 application 用例): {new}"


def test_api_no_multi_service_fanout():
    """架构门禁: 每个 API 模块至多依赖一个 services 子模块。现状多依赖在白名单。"""
    fanout = _find_api_service_fanout()
    new = {
        m: sorted(leaves) for m, leaves in fanout.items() if len(leaves) > 1 and m not in WHITELIST_API_FANOUT
    }
    assert not new, f"API 模块新增跨 service 编排(需收敛为单个 application 用例): {new}"


def test_worker_no_direct_domain_services():
    """架构门禁: Worker 不得直接依赖 services(切片 1 基线, 迁移后逐项移除白名单)。"""
    detected = _find_worker_service_imports()
    new = sorted((m, t) for (m, t) in detected if (m, t) not in WHITELIST_WORKER_SERVICES)
    assert not new, f"Worker 新增 services 直接依赖(需经 application.worker 边界): {new}"


def test_analysis_no_direct_engine_or_services():
    """架构门禁: analysis 不得直接驱动引擎/services/拼装 plan(切片 1 基线)。"""
    detected = _find_analysis_engine_imports()
    new = sorted((m, t) for (m, t) in detected if (m, t) not in WHITELIST_ANALYSIS_ENGINE)
    assert not new, f"analysis 新增计算执行直接依赖(目标只消费 ComputeResult/回执/声明输出): {new}"


def test_table_ownership_no_new_cross_imports():
    """架构门禁: 跨表 ORM 访问不得新增, 只能按 TABLE_OWNERS 收敛后从白名单移除。"""
    detected = _find_cross_model_imports()
    new = sorted((m, t) for (m, t) in detected if (m, t) not in WHITELIST_CROSS_MODEL_IMPORTS)
    assert not new, f"新增跨表 ORM 访问(需收敛到归属领域 repository): {new}"


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
