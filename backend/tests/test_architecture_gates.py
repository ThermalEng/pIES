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
  5. test_api_no_multi_service_fanout   — 已删除(该门禁只扫描已删除
     services 包的导入扇出, 无法检出同一端点对多个 application 用例的调用;
     指南 F 否决此类形态, 不再保留);
  6. test_worker_no_direct_domain_services — worker 不允许直接 import
     iesplan.services.*(业务快照读取与编排归 application.worker);
  7. test_analysis_no_direct_engine_or_services — analysis 不允许直接 import
     iesplan.engines.* / iesplan.services.* / iesplan.assembly.plan
     (目标: 只消费 ComputeResult、回执与声明输出);
  8. test_table_ownership_no_new_cross_imports — services/worker/analysis/
     storage/application 对 iesplan.models.* 的跨表访问收敛到
     WHITELIST_CROSS_MODEL_IMPORTS, 表归属见 TABLE_OWNERS。

纠偏 Wave 0(临时指导 architecture-refactor-workflow.md 纠偏波次)追加:

  9. test_application_no_direct_services — application 禁止导入
     iesplan.services(临时债务集合须与检测精确相等, 最终归零);
  10. test_application_no_bare_sql — application 禁止 sa.table/Table/text 与
     sa insert/update/delete(表真相只归领域 persistence);
  11. test_worker_no_transactions — Worker 禁止 .commit()/.rollback()
     (事务归 application.worker 用例)。

策略: 现有违规列入文件头部的 WHITELIST_* 常量(白名单基线, 注释写明
整改 TODO), 新增违规直接断言失败。后续切片按注释逐条整改后移除白名单条目,
白名单清空后门禁转为硬强制。

最终收口 Wave 0(临时指导 docs/development/backend-decoupling-finalization.md
§五, 基线 1a07146)追加:

  12. test_application_no_models_orm — application 禁止直引 iesplan.models/ORM
     (表真相只归领域 persistence; 现状 1 项债务, Wave 1-A 归零);
  13. test_api_no_direct_domain_behavior — API 禁止直接调用领域行为组织业务
     (只调完整 application 用例, contracts DTO 复用除外; 现状 5 对, Wave 2-A/B 归零);
  14. test_no_forbidden_cross_domain_deps — 禁止领域间直接业务依赖
     (跨域组合只归 application, 他域 contracts 复用除外; 现状 2 对, Wave 2-B/3 归零);
  15. test_worker_no_compute_penetration — Worker 禁止计算穿透
     (engines/metrics/finance/analysis/assembly; 现状 4 对, Wave 4 归零);
  16. test_analysis_no_engine_driving — analysis 禁止驱动 engine
     (不自装 plan、不调用引擎, 只消费统一计算结果; Wave 0 基线 4 对,
     Wave 4-B 清除 wrapper 驱动后剩 3 对门面转发, Wave 4-C 随门面归属收尾归零)。
  17. test_whitelists_have_no_stale_entries — 旧式“只查新增”门禁的白名单条目
     必须仍被命中, 过期即失败。

Wave 0 门禁 8 改为精确相等: WHITELIST_CROSS_MODEL_IMPORTS 已按实测裁剪
(删除 services/project/Worker 11 项过期条目, 仅剩 namespace→identity 与
storage→common)。门禁 12–16 债务集合 TEMP_DEBT_* 亦与检测精确相等
(新增与过期都失败); 整改由 Wave 1–4 在对应切片落地, 本文件只建门禁。

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
    # (Wave 5 集成: analysis 两 wrapper 辅助与 engines eval_run 取参函数已提升
    #  为公开 API, 移除 3 项; 白名单清空。)
    # (Wave 1 集成: assembly 收敛为 parser→context→rules→validator→artifact,
    #  共享能力经 context 公开, 4 项私有复用已消除, 移除本组。)
    # worker → analysis 私有穿透已整改(0.6.5): 符号提升为 analysis 公开 API。
    # ---- api → services: API 层直接访问服务私有函数 ----
    # (Wave 3 集成: config._row_to_config 私有访问随 calc_config 用例消除;
    #  projects._is_admin 私有访问随 application 迁移消除, 移除两项)
    # (Wave 2 集成: services.identity 已删除，delete_user 改经 audit 域门面，移除本项)
}

# ---------------------------------------------------------------------------
# 门禁 3 白名单: api → ORM 直接导入 (键 = (模块路径, 行号), 值为该行豁免的符号集)
# ---------------------------------------------------------------------------
# 仅豁免导入语句本身及其列出的符号: 同文件新行导入新 ORM 符号仍会报错。
# 每条目均需整改: 经 services 公开面访问数据, 整改后移除条目。
WHITELIST_API_ORM: dict[tuple[str, int], frozenset[str]] = {
    # ---- admin.py: 管理端运维直接查询 ORM ----
    # (Wave 5 集成: admin 改经 application 用例取数, 移除 4 项)
    # ---- health.py: 健康检查直接计数 ORM ----
    # (Wave 4 集成: health 改经 application.health 只读探针, 移除 3 项)
    # ---- results.py: 结果查询任务状态与校验正则 ----
    # (切片 5: assess 端点经 tasks_service.ensure_task_belongs 取任务, 移除 Task)
    # (Wave 3 集成: results.py 改经 application 用例, HASH64_RE 直引已消除)
    # (Wave 4 集成: limits 配额统计收敛到 datasets.quotas, auth 会话取数收敛到
    #  identity 门面, objects 归属校验收敛到 application.objects, 移除 4 项)
    # (Wave 4 集成: tasks.py 幂等键正则改接 tasks 域门面, 豁免删除)
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


def test_worker_no_direct_orm_imports():
    """架构门禁: 禁止 Worker 直接导入 ORM(最终验收矩阵, ORM 查询归 application.worker)。

    无白名单, 硬强制: worker 下出现 iesplan.models.* 或 iesplan.db 会话符号
    导入即失败。
    """
    detected = _find_api_orm_imports(scan_root=_WORKER_DIR)
    assert not detected, f"Worker 直接导入 ORM(需上收至 application.worker 用例): {detected}"


# ---------------------------------------------------------------------------
# 门禁 4 白名单: api → 事务提交 (键 = (模块路径, 行号))
# ---------------------------------------------------------------------------
# 基线核查(2026-09-11, 解耦重构切片 1): API 层共 36 处 .commit() 调用, 事务
# 所有权落在路由层, 违反"事务只由 application 提交/回滚"(宪法 §5.4)。
# 后续切片按资源域迁移到 application 用例后逐条移除; 白名单清空后硬强制。
WHITELIST_API_COMMIT: set[tuple[str, int]] = set()
# (Wave 5 集成: admin 运维解锁提交已上收至 application 用例, 白名单清空。)

# ---------------------------------------------------------------------------
# 门禁 6 白名单: worker → services 直接依赖 (键 = (worker 模块, services 目标))
# ---------------------------------------------------------------------------
# 基线核查(2026-09-11, 切片 1): Worker 直接读取业务 service, 任务执行边界锁死
# 在领域实现上。目标是业务快照读取与跨模块编排移入 application.worker, worker
# 只保留领取任务、租约、取消、分派和结果提交。迁移后逐项移除。
WHITELIST_WORKER_SERVICES: set[tuple[str, str]] = set()
# (Wave 3 集成: lease/runner/main/executors 的 services 直引已上收至
#  application.worker 用例, 白名单清空。)

# ---------------------------------------------------------------------------
# 门禁 7 白名单: analysis → 计算执行直接依赖 (键 = (analysis 模块, 目标))
# ---------------------------------------------------------------------------
# 基线核查(2026-09-11, 切片 1): analysis 直接驱动旧计算引擎/拼装 plan/调用
# services, 结果分析与计算执行无法分别演进。目标是 analysis 只消费 ComputeResult、
# ExecutionReceipt 与声明输出。0.8 计算链实现前计算入口保持显式未实现, 不恢复
# 旧命令注册表或旧机理函数。迁移后逐项移除。
# (Wave 1 集成: analysis 只消费计算结果/回执/声明输出, engines/services/
#  assembly.plan 直接依赖已删除, 白名单清空。Worker 侧调用归 Wave 4。)
WHITELIST_ANALYSIS_ENGINE: set[tuple[str, str]] = set()

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
    # Wave 0 收口(基线 1a07146, 全仓 AST 实测): 检测仅剩以下 2 项, 白名单与之精确相等。
    # 过期删除 11 项: services.tasks→common(services 包已删, 不可达);
    # project.persistence→project(project/ 不在 _SCAN_OWNERSHIP_DIRS, 永不可达);
    # worker 9 项(lease/executors/runner 跨表直引已收敛, 实测为零)。
    # (Wave 1-A 已删除穿透 helper, 债务归零, 移除本项)
    # 永久允许: models.common 仅共享基元(bigint_pk/正则, 无业务表), 非跨域业务访问。
    ("iesplan.storage.persistence", "common"),
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
    """架构门禁: 跨表 ORM 访问白名单必须与检测精确相等(新增与过期都失败)。

    Wave 0 收口: 白名单已按基线实测裁剪, 后续波次逐项归零(仅 models.common 共享基元永久允许)。
    """
    detected = _find_cross_model_imports()
    assert detected == WHITELIST_CROSS_MODEL_IMPORTS, (
        f"跨表 ORM 访问债务漂移(新增: {sorted(detected - WHITELIST_CROSS_MODEL_IMPORTS)}, "
        f"过期: {sorted(WHITELIST_CROSS_MODEL_IMPORTS - detected)})"
    )


# ---------------------------------------------------------------------------
# 纠偏 Wave 0 门禁: application/services 残留、application 裸表/SQL、Worker 事务
# ---------------------------------------------------------------------------
# 审查裁决(fe3d83b): application 直调旧 services、application 内重声明表/裸 SQL、
# Worker 持有 commit/rollback。以下三门禁检测真实形态, 临时债务集合必须与检测
# 结果精确相等(漏报与过期项都失败); 纠偏波次逐项归零, 最终验收时全部为空。


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


#: 门禁 9 临时债务: application → services 直调(C1-A/B/C/D 已全部归零)。
TEMP_DEBT_APP_SERVICES: set[tuple[str, int, str]] = set()


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


#: 门禁 10 临时债务: application 裸表/裸 SQL(C1-A 已随领域收敛归零)。
TEMP_DEBT_APP_BARE_SQL: set[tuple[str, int, str]] = set()


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


#: 门禁 11 临时债务: Worker 事务调用(纠偏 Wave 3 已上收 application.worker, 归零)。
TEMP_DEBT_WORKER_TX: set[tuple[str, int, str]] = set()


def test_application_no_direct_services():
    """架构门禁 9: application 禁止导入 iesplan.services(纠偏最终零导入)。

    临时债务集合必须与检测结果精确相等: 新增直调失败, 已整改未移除条目也失败。
    """
    detected = set(_find_app_service_imports())
    assert detected == TEMP_DEBT_APP_SERVICES, (
        f"application→services 债务漂移(新增: {sorted(detected - TEMP_DEBT_APP_SERVICES)}, "
        f"过期: {sorted(TEMP_DEBT_APP_SERVICES - detected)})"
    )


def test_application_no_bare_sql():
    """架构门禁 10: application 禁止裸表/SQL(表真相只归领域 persistence)。"""
    detected = set(_find_app_bare_sql())
    assert detected == TEMP_DEBT_APP_BARE_SQL, (
        f"application 裸表/SQL 债务漂移(新增: {sorted(detected - TEMP_DEBT_APP_BARE_SQL)}, "
        f"过期: {sorted(TEMP_DEBT_APP_BARE_SQL - detected)})"
    )


def test_worker_no_transactions():
    """架构门禁 11: Worker 禁止 commit/rollback(事务归 application.worker 用例)。"""
    detected = set(_find_worker_transactions())
    assert detected == TEMP_DEBT_WORKER_TX, (
        f"Worker 事务债务漂移(新增: {sorted(detected - TEMP_DEBT_WORKER_TX)}, "
        f"过期: {sorted(TEMP_DEBT_WORKER_TX - detected)})"
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


# ---------------------------------------------------------------------------
# 最终收口 Wave 0 门禁 12–16: 依赖方向债务(全仓 AST 实测建档, 精确相等)
# ---------------------------------------------------------------------------
# 临时指导 docs/development/backend-decoupling-finalization.md §五 Wave 0:
# 记录真实生产依赖图, 把债务集合改为与检测精确相等, 删除过期条目, 并新增
# application→models/ORM、API 直接调用领域行为、禁止的领域间依赖、Worker
# 计算穿透、analysis 驱动 engine 五类门禁。每项门禁与对应整改在同一可通过
# 切片落地: 本文件只建门禁并如实记录现状债务(全绿), 整改由 Wave 1–4 执行。
#
# 设计约束(同 §五 Wave 0): 门禁只查依赖事实(AST import), 不绑定私有文件名
# 与行号(键为 (模块, 目标域), 同一模块多行合并为一项), 不强迫多一层包装,
# 不复制被测实现。sqlalchemy Session 类型注解属事务管道, 不在 ORM 门禁之列;
# 他域 *.contracts/contracts2 属不可变 contract 复用, 始终允许。

#: Wave 0 门禁 12–16 共用的业务域包集合(均有 __init__ 公开门面; core/config/db/models 不在列)。
_WAVE0_DOMAIN_PKGS: frozenset[str] = frozenset(
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

#: Wave 0 门禁 15/16 的计算执行包集合(Worker/analysis 禁止穿透)。
_WAVE0_COMPUTE_PKGS: frozenset[str] = frozenset({"engines", "metrics", "finance", "analysis", "assembly"})

#: Wave 0 门禁 16 的 analysis 禁止驱动集合(计算执行 + 旧链 services + 任务执行 worker)。
_WAVE0_ANALYSIS_FORBIDDEN_PKGS: frozenset[str] = frozenset(
    {"engines", "assembly", "services", "finance", "metrics", "worker"}
)


def _top_pkg_of(target: str) -> str:
    """取 iesplan 导入目标的顶层子包名(如 iesplan.metrics.financial → metrics)。"""
    parts = target.split(".")
    return parts[1] if len(parts) > 1 else ""


def _is_contract_target(target: str) -> bool:
    """不可变 contract 复用判定: 他域 contracts/contracts2 子模块一律允许。"""
    return ".contracts" in target or "contracts2" in target


#: 常设复用豁免(非临时债务): (导入方模块前缀, 目标模块前缀)。
#: Wave 3-B: 四维评估规则归 results 域所有, 其状态词汇与 IRR 分类仍以
#: 无状态的 iesplan.metrics.validity / iesplan.metrics.financial 为唯一权威
#: (收口 §六“领域公开纯函数”复用, 不复制枚举值)。仅豁免 results 域对该两
#: 模块的导入; results 对 metrics 其他子模块的导入仍记债务。
#: Wave 5: engines/planning 对 metrics.financial 的复用同属此类
#: (metrics.financial 仅依赖标准库/numpy 的纯计算; planning 取现金流/NPV/IRR
#: 纯函数与 IRRStatus 做候选评分, 引擎内评估, 非跨域业务组合)。
_WAVE0_STATE_MODEL_REUSE: frozenset[tuple[str, str]] = frozenset(
    {
        ("iesplan.results", "iesplan.metrics.validity"),
        ("iesplan.results", "iesplan.metrics.financial"),
        ("iesplan.engines", "iesplan.metrics.financial"),
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
    exempt 命中(_WAVE0_STATE_MODEL_REUSE)时排除常设复用。
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


#: 门禁 12 临时债务: application → models/ORM(Wave 1-A 已删除穿透 helper, 归零)。
TEMP_DEBT_APP_ORM: set[tuple[str, str]] = set()


def test_application_no_models_orm():
    """架构门禁 12: application 禁止直引 iesplan.models/ORM(表真相只归领域 persistence)。

    临时债务集合必须与检测结果精确相等: 新增穿透失败, 已整改未移除条目也失败。
    """
    detected = _find_application_orm_imports()
    assert detected == TEMP_DEBT_APP_ORM, (
        f"application→models/ORM 债务漂移(新增: {sorted(detected - TEMP_DEBT_APP_ORM)}, "
        f"过期: {sorted(TEMP_DEBT_APP_ORM - detected)})"
    )


#: 门禁 13 临时债务: API 直接调用领域行为(归零)。
#: Wave 2-A: config/model/validation 的授权与设备选择器收进 application 用例
#: (authorization/selector); Wave 2-C: tasks 幂等键正则改接 core.patterns 权威;
#: R2 Wave 3-D: 用户名/邮箱/幂等键规则回归 identity/tasks contracts, core.patterns 删除。
#: api/auth 经 identity.contracts 属 DTO 传输映射, 不在债务之列。
TEMP_DEBT_API_DOMAIN_BEHAVIOR: set[tuple[str, str]] = set()


def test_api_no_direct_domain_behavior():
    """架构门禁 13: API 禁止直接调用领域行为组织业务(只调完整 application 用例)。

    领域 *.contracts DTO 复用允许; 其余领域根包/行为子模块导入即记债务。
    临时债务集合必须与检测结果精确相等。
    """
    detected = _iter_domain_imports(_API_DIR, _WAVE0_DOMAIN_PKGS)
    assert detected == TEMP_DEBT_API_DOMAIN_BEHAVIOR, (
        f"API→领域行为债务漂移(新增: {sorted(detected - TEMP_DEBT_API_DOMAIN_BEHAVIOR)}, "
        f"过期: {sorted(TEMP_DEBT_API_DOMAIN_BEHAVIOR - detected)})"
    )


def _find_cross_domain_behavior_imports(pkg_root: Path = _PKG_ROOT) -> set[tuple[str, str]]:
    """门禁 14: 扫描各业务域对他域行为的直接依赖。返回 (模块, 他域) 集合。

    领域间不得直接组合业务: 他域根包/行为子模块导入即违规; 他域 contracts
    属不可变 contract 复用, 允许; _WAVE0_STATE_MODEL_REUSE 属常设复用, 允许。
    跨领域授权与工作流归 application。
    """
    found: set[tuple[str, str]] = set()
    for domain in sorted(_WAVE0_DOMAIN_PKGS):
        scan_root = pkg_root / domain
        if not scan_root.is_dir():
            continue
        found |= _iter_domain_imports(
            scan_root, _WAVE0_DOMAIN_PKGS, own_top=domain, exempt=_WAVE0_STATE_MODEL_REUSE
        )
    return found


#: 门禁 14 临时债务: 禁止的领域间依赖(归零)。
#: results/engines 对 metrics 纯函数与状态词汇的复用属常设豁免
#: (见 _WAVE0_STATE_MODEL_REUSE), 不记入本债务集合。
TEMP_DEBT_CROSS_DOMAIN: set[tuple[str, str]] = set()


def test_no_forbidden_cross_domain_deps():
    """架构门禁 14: 禁止领域间直接业务依赖(跨域组合只归 application)。

    临时债务集合必须与检测结果精确相等。
    """
    detected = _find_cross_domain_behavior_imports()
    assert detected == TEMP_DEBT_CROSS_DOMAIN, (
        f"领域间依赖债务漂移(新增: {sorted(detected - TEMP_DEBT_CROSS_DOMAIN)}, "
        f"过期: {sorted(TEMP_DEBT_CROSS_DOMAIN - detected)})"
    )


#: 门禁 15 临时债务: Worker 计算穿透(Wave 4-A 已删除 executors 旧计算链,
#: engines/metrics/finance/analysis 穿透归零)。
#: worker→application.worker 用例与 main 进程启停属正确形状, 不在债务之列。
TEMP_DEBT_WORKER_COMPUTE: set[tuple[str, str]] = set()


def test_worker_no_compute_penetration():
    """架构门禁 15: Worker 禁止计算穿透(不拼 solver 命令、不解释装配、不承担结果分析)。

    Worker 只保留任务领取、租约、调用 application.worker 与隔离执行壳。
    临时债务集合必须与检测结果精确相等。
    """
    detected = _iter_domain_imports(_WORKER_DIR, _WAVE0_COMPUTE_PKGS)
    assert detected == TEMP_DEBT_WORKER_COMPUTE, (
        f"Worker 计算穿透债务漂移(新增: {sorted(detected - TEMP_DEBT_WORKER_COMPUTE)}, "
        f"过期: {sorted(TEMP_DEBT_WORKER_COMPUTE - detected)})"
    )


#: 门禁 16 临时债务: analysis 驱动 engine(Wave 4-C 门面归属收尾后归零)。
#: Wave 4-B 已清除 wrapper 引擎驱动: _local_plan/run_sweep/run_batch/逐点财务
#: 执行删除, wrapper 只消费不可变扫描点结果并做纯分析, 不再导入 finance。
#: Wave 4-C 消费者证据裁决: summarize_four_dimensions 唯一生产消费者经
#: metrics.validity 直调(application/results/writes), 指标实现经 metrics 域
#: 直调, 财务计算经 finance 包直调; analysis 侧 assessment/indicators/
#: _minfinance 三转发零生产消费者, 整体删除(未复制实现, 未新增校验)。
TEMP_DEBT_ANALYSIS_ENGINE_DRIVING: set[tuple[str, str]] = set()


def test_analysis_no_engine_driving():
    """架构门禁 16: analysis 禁止驱动 engine(不构造 plan、不调用引擎, 只消费统一计算结果)。

    门禁 7 已覆盖 engines/services/assembly.plan 直接导入(当前为零); 本门禁覆盖
    finance/metrics/worker 等计算执行穿透。临时债务集合必须与检测结果精确相等。
    """
    detected = _iter_domain_imports(_PKG_ROOT / "analysis", _WAVE0_ANALYSIS_FORBIDDEN_PKGS)
    assert detected == TEMP_DEBT_ANALYSIS_ENGINE_DRIVING, (
        f"analysis 驱动 engine 债务漂移(新增: {sorted(detected - TEMP_DEBT_ANALYSIS_ENGINE_DRIVING)}, "
        f"过期: {sorted(TEMP_DEBT_ANALYSIS_ENGINE_DRIVING - detected)})"
    )


def test_whitelists_have_no_stale_entries():
    """架构门禁 17: 旧式白名单条目必须仍被对应扫描器命中(过期即失败)。

    精确相等门禁(8/9/10/11/12/13/14/15/16)已自带过期检查; 本门禁覆盖仍用
    “只查新增”形态的门禁 1/2/3/4/6/7(门禁 5 已删除), 防止全绿掩盖残留。
    """
    stale: list[str] = []
    for key in WHITELIST_CORE_BUSINESS_DEPS:
        if key not in {(m, line) for (m, line, _src) in _find_core_business_imports()}:
            stale.append(f"core-business-deps: {key!r}")
    for key in WHITELIST_PRIVATE_IMPORTS:
        if key not in {(m, s) for (m, s) in _find_private_symbol_imports()}:
            stale.append(f"private-imports: {key!r}")
    for key in WHITELIST_API_ORM:
        if key not in {(mod, line) for (mod, line, _symbols) in _find_api_orm_imports()}:
            stale.append(f"api-orm: {key!r}")
    for key in WHITELIST_API_COMMIT:
        if key not in {tuple(item) for item in _find_api_commit_calls()}:
            stale.append(f"api-commit: {key!r}")
    for key in WHITELIST_WORKER_SERVICES:
        if key not in set(_find_worker_service_imports()):
            stale.append(f"worker-services: {key!r}")
    for key in WHITELIST_ANALYSIS_ENGINE:
        if key not in set(_find_analysis_engine_imports()):
            stale.append(f"analysis-engine: {key!r}")
    assert not stale, f"白名单存在过期条目(检测已无命中, 须删除): {stale}"


"""R2 Wave 4 职责回流门禁(18/19/20)与门禁 5 已按指南 F/H 删除, 不再保留:

- 门禁 18/19 按 USERNAME_RE/ensure_access 等私有符号名锁定所在模块,
  改名即失效, 也无法检出换名复制的规则;
- 门禁 20 要求三个未实现执行器函数永久存在、有 raise 且无 return,
  阻止未来正式实现;
- 门禁 5 只扫描已删 services 导入的 API fanout, 无法检出同一端点对多个
  application 用例的调用。
语义行为由行为测试证明; 静态门禁只保留稳定的依赖方向和通用禁止形态。
"""
