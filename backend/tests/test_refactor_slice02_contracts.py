"""解耦重构切片 2：九域公开 contract 契约测试。

覆盖 audit/project/identity/dataset/configuration/tasks/results/package/model：
- 门面可导入且 `__all__` 与导出一致；
- 全部 Record 为 frozen dataclass（跨模块只传不可变值）；
- 领域错误复用 core 基类诊断码（不新增码）；
- 源码纯度：不导入 ORM/services/application/api/worker/engines，
  无 commit/rollback 调用；contracts.py 只依赖标准库与 core。

说明（Wave 1 切片 C）：各域 `*Repository` Protocol 已删除——生产代码中
无真实端口注入消费（仅门面 re-export 与存在性断言引用），按复用裁决
直接删除，不再保留无消费者抽象。
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import iesplan.audit as audit
import iesplan.configuration as configuration
import iesplan.dataset as dataset
import iesplan.identity as identity
import iesplan.model as model
import iesplan.package as package
import iesplan.project as project
import iesplan.results as results
import iesplan.tasks as tasks
from iesplan.core.errors import ConflictError, NotFoundError

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_PKG_ROOT = _BACKEND_DIR / "iesplan"

_DOMAIN_FACADES = {
    "audit": audit,
    "project": project,
    "identity": identity,
    "dataset": dataset,
    "configuration": configuration,
    "tasks": tasks,
    "results": results,
    "package": package,
    "model": model,
}

_ERROR_BASES = {
    "NotFound": NotFoundError,
    "Conflict": ConflictError,
}

#: 域源码中禁止出现的跨层导入前缀（本域 contracts 模块自身除外，见纯度测试）。
#: iesplan.db 不在此列：它是 Base/无状态列基元的基础设施宿主（Wave 2 起顶层
#: 混合 models/ 已删除），域 persistence 复用其声明基元不构成跨域访问；
#: 连接获取（下 _DB_CONNECTION_NAMES）另行禁止。
_BANNED_PREFIXES = (
    "iesplan.models",
    "iesplan.services",
    "iesplan.application",
    "iesplan.api",
    "iesplan.worker",
    "iesplan.engines",
    "iesplan.devices",
    "iesplan.assembly",
    "iesplan.finance",
    "iesplan.analysis",
    "iesplan.storage",
    "iesplan.metrics",
    "iesplan.planning",
)

#: 域源码禁止从 iesplan.db 获取连接/会话/引擎（连接只归 bootstrap 装配与
#: 调用方传入的 session_factory；声明基元 Base/列类型允许）。
_DB_CONNECTION_NAMES = frozenset({"SessionLocal", "get_db", "init_db", "engine"})


def _db_connection_violation(mod: str, names: list[str]) -> str | None:
    """判定对 iesplan.db 的导入是否为连接获取；返回违规名或 None。"""
    if mod != "iesplan.db":
        return None
    for name in names:
        if name in _DB_CONNECTION_NAMES:
            return name
    return None


def _public_names(module) -> dict[str, object]:
    return {name: getattr(module, name) for name in module.__all__}


def test_domain_facades_export_contracts():
    """九域门面可导入，__all__ 非空且全部可解析，含 contract 记录与领域错误。"""
    for domain, facade in _DOMAIN_FACADES.items():
        assert facade.__all__, f"{domain} 门面 __all__ 为空"
        exported = _public_names(facade)
        names = set(exported)
        assert any(n.endswith("Record") or n.endswith("Page") for n in names), (
            f"{domain} 未导出 contract 记录"
        )
        assert any(n.endswith("Error") for n in names), f"{domain} 未导出领域错误"


def test_contract_records_are_frozen():
    """全部公开记录为 frozen dataclass（slots 可选，不强制）。"""
    checked = 0
    for domain, facade in _DOMAIN_FACADES.items():
        for name, obj in _public_names(facade).items():
            if not (name.endswith("Record") or name.endswith("Page")):
                continue
            assert dataclasses.is_dataclass(obj), f"{domain}.{name} 不是 dataclass"
            assert obj.__dataclass_params__.frozen, f"{domain}.{name} 必须 frozen"
            checked += 1
    assert checked >= 20, f"公开记录过少({checked})，契约疑似缺失"


def test_domain_errors_reuse_base_codes():
    """领域错误继承 core 基类且不新增诊断码（诊断码登记在切片外管理）。"""
    checked = 0
    for domain, facade in _DOMAIN_FACADES.items():
        for name, obj in _public_names(facade).items():
            if not (name.endswith("Error") and inspect.isclass(obj)):
                continue
            for suffix, base in _ERROR_BASES.items():
                if suffix in name:
                    assert issubclass(obj, base), f"{domain}.{name} 必须继承 {base.__name__}"
                    assert obj.code == base.code, f"{domain}.{name} 不得新增诊断码"
                    checked += 1
                    break
    assert checked >= 7, f"领域错误过少({checked})"


#: 各域 persistence 实现允许访问的归属 models 子模块（对应 TABLE_OWNERS；
#: configuration 的 calc_configs 与 tasks 表同文件，tasks 读快照不写配置表）
OWNED_MODELS: dict[str, frozenset[str]] = {
    "audit": frozenset({"audit"}),
    "project": frozenset({"project"}),
    "identity": frozenset({"identity"}),
    "dataset": frozenset({"dataset"}),
    "configuration": frozenset({"config_revision", "calc"}),
    "tasks": frozenset({"calc", "uncertainty"}),
    "results": frozenset({"result"}),
    "package": frozenset({"audit"}),
    "model": frozenset({"model", "draft_revision", "model_template", "project_model"}),
    # (Wave 5 集成: 切片 7 起模板/草稿/项目模型三表归 model 域持久化实现，
    #  门禁 TABLE_OWNERS 划归 model 系且仅 model/persistence.py 使用，补齐。)
}


def _iter_domain_files():
    for domain in _DOMAIN_FACADES:
        for path in sorted((_PKG_ROOT / domain).rglob("*.py")):
            yield domain, path


#: 纯函数复用豁免(与 test_architecture_gates.ALLOWED_STATE_MODEL_REUSE 同义，
#: 此处独立声明以免测试间相互导入)：metrics.validity/financial 仅依赖标准库
#: 与 numpy 的纯词汇/纯函数，results 复用其枚举与纯函数而不是复制第二份
#: 事实源（复用领域公开纯函数，避免复制第二份事实源；复制枚举值才是本测试要防的）。
_PURE_METRICS_REUSE: frozenset[tuple[str, str]] = frozenset(
    {
        ("results", "iesplan.metrics.validity"),
        ("results", "iesplan.metrics.financial"),
    }
)


def _is_pure_reuse(domain: str, mod: str) -> bool:
    return any(
        domain == owner and (mod == target or mod.startswith(target + "."))
        for owner, target in _PURE_METRICS_REUSE
    )


def test_domain_source_purity():
    """域源码纯度：无 ORM/跨层导入，无 commit/rollback；contracts 只靠标准库+core。

    唯一例外是各域 persistence.py（领域持久化实现），它只允许访问本域归属表
    （OWNED_MODELS），且同样禁跨层导入与 commit/rollback；另一例外是
    _PURE_METRICS_REUSE 的纯词汇/纯函数复用（复用而非复制，不记违规）。
    """
    violations: list[str] = []
    for domain, path in _iter_domain_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        own_contracts = f"iesplan.{domain}.contracts"
        is_persistence = path.name == "persistence.py"
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mod = node.module
                if mod == "__future__":
                    continue
                if path.name == "contracts.py":
                    # contracts 只允许标准库与 iesplan.core
                    if mod.startswith("iesplan.") and not mod.startswith("iesplan.core."):
                        violations.append(f"{domain}/{path.name}:{node.lineno}: {mod}")
                    continue
                else:
                    if mod == own_contracts or mod.startswith("iesplan.core."):
                        continue
                    if mod.split(".")[0] == "sqlalchemy":
                        continue
                    if is_persistence and mod.startswith("iesplan.models."):
                        leaf = mod.split(".")[2]
                        if leaf in OWNED_MODELS[domain]:
                            continue
                        violations.append(f"{domain}/{path.name}:{node.lineno}: 非归属表 {mod}")
                        continue
                if _is_pure_reuse(domain, mod):
                    continue
                bad = _db_connection_violation(mod, [a.name for a in node.names])
                if bad is not None:
                    violations.append(f"{domain}/{path.name}:{node.lineno}: iesplan.db.{bad}")
                    continue
                for prefix in _BANNED_PREFIXES:
                    if mod == prefix or mod.startswith(prefix + "."):
                        violations.append(f"{domain}/{path.name}:{node.lineno}: {mod}")
                        break
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_pure_reuse(domain, alias.name):
                        continue
                    if alias.name == "iesplan.db":
                        violations.append(
                            f"{domain}/{path.name}:{node.lineno}: import iesplan.db(整模块, 含连接)"
                        )
                        continue
                    for prefix in _BANNED_PREFIXES:
                        if alias.name == prefix or alias.name.startswith(prefix + "."):
                            violations.append(f"{domain}/{path.name}:{node.lineno}: {alias.name}")
                            break
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("commit", "rollback")
            ):
                violations.append(f"{domain}/{path.name}:{node.lineno}: .{node.func.attr}()")
    assert not violations, f"域源码纯度违规: {violations}"
