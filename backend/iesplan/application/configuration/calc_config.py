"""计算配置用例薄封装(Wave 3-C: application/configuration)。

``api/config.py`` 由直调 ``iesplan.services.config`` 改经本模块，
依赖方向 ``api → application.configuration.calc_config → services.config``
（旧服务保留未删，待 Wave 5 删除）。

本模块只做透传，不新增校验/哈希/回退：
- ``get_config`` / ``load_work_graph`` / ``validate_config`` /
  ``get_default_config`` / ``parameter_metadata`` /
  ``list_algorithms_meta`` / ``save_config`` 均直调旧服务同名函数；
- ``row_to_config`` 为公开序列化（替代旧 ``services.config._row_to_config``
  私有函数，供保存端点组织响应；映射逻辑与旧函数一致）；
- 事务：读用例不提交；``save_config`` 由旧服务内部提交（与迁移前一致，
  API 层不提交）。

调用方向：``api → application.configuration.calc_config → services.config``；
不导入 ORM、不导入其他域内部模块。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan.configuration.contracts import CalcConfigRecord
from iesplan.core.errors import NotFoundError
from iesplan.engines.registry import DEFAULT_ALGORITHM, get_algorithm
from iesplan.services import config as config_service
from iesplan.services.config import ALGO_DB_CLASS

__all__ = [
    "get_config",
    "get_default_config",
    "list_algorithms_meta",
    "load_work_graph",
    "parameter_metadata",
    "row_to_config",
    "save_config",
    "validate_config",
]

#: DB 算法列短名 → 注册表算法 id（``ALGO_DB_CLASS`` 的逆映射，本模块内构造，
#: 不引用旧服务私有 ``_DB_CLASS_TO_ALGO``）。
_DB_CLASS_TO_ALGO: dict[str, str] = {v: k for k, v in ALGO_DB_CLASS.items()}


def load_work_graph(db: Session, project_id: int) -> dict:
    """加载项目当前工作图（设备清单），直调旧服务。"""
    return config_service.load_work_graph(db, project_id)


def get_config(db: Session, project_id: int) -> dict:
    """读取当前计算配置（未保存返回默认配置），直调旧服务。"""
    return config_service.get_config(project_id, db)


def get_default_config(db: Session, project_id: int) -> dict:
    """重新生成默认计算配置（不保存），直调旧服务。"""
    return config_service.get_default_config(project_id, db)


def validate_config(config: dict, graph: dict) -> list:
    """校验计算配置（不保存），直调旧服务。"""
    return config_service.validate_config(config, graph)


def save_config(
    db: Session,
    project_id: int,
    config: dict,
    expected_revision: int,
    *,
    user_id: int | None = None,
) -> CalcConfigRecord:
    """保存计算配置（与草稿修订绑定；提交由旧服务内部完成），直调旧服务。"""
    return config_service.save_config(
        db, project_id, config, expected_revision, user_id=user_id
    )


def parameter_metadata(graph: dict) -> dict:
    """参数元数据（单位/范围/帮助键），直调旧服务。"""
    return config_service.parameter_metadata(graph)


def list_algorithms_meta() -> list[dict]:
    """算法注册表列表，直调旧服务。"""
    return config_service.list_algorithms_meta()


def row_to_config(row: CalcConfigRecord) -> dict:
    """CalcConfig 行 → 计算配置 dict（公开序列化，与旧私有函数同形）。

    DB 算法列 → 配置算法段：NULL 为 auto 模式；其余映射回注册表算法 id，
    未注册短名原样返回 manual。
    """
    if row.algorithm is None:
        algorithm = {"mode": "auto", "name": DEFAULT_ALGORITHM}
    else:
        algo_id = _DB_CLASS_TO_ALGO.get(row.algorithm, row.algorithm)
        try:
            get_algorithm(algo_id)
        except NotFoundError:
            algorithm = {"mode": "manual", "name": row.algorithm}
        else:
            algorithm = {"mode": "manual", "name": algo_id}
    return {
        "parameters": row.params or {},
        "variables": row.variables or [],
        "objectives": row.objectives or [],
        "constraints": row.constraints or [],
        "algorithm": algorithm,
        "irr_floor": float(row.min_irr) if row.min_irr is not None else None,
        "tolerances": row.tolerances or {},
        "random_seed": row.random_seed,
    }
