"""项目内容对象用例（application/projects/content_objects.py）。

内容字典 ↔ 对象存储对象（规范化 JSON + owner 引用）的 IO 实现，供
application 调用方使用：编码/解析与错误构造经 project 域纯函数
（content_to_bytes/parse_content_object/corrupt_error），本模块只做
storage put/get/attach。每次写入新建对象行（无内容去重）。

依赖方向：application →（project 域纯函数 + storage 公开门面）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from iesplan import project as project_domain
from iesplan.core.errors import NotFoundError
from iesplan.storage import ObjectCorruptError, attach, get_object, put_object


def store_content_object(db: Session, content: dict) -> int:
    """内容字典 → 对象存储对象并建立草稿内容引用，返回对象 id（每次写入新行）。

    规范化 JSON → 对象存储对象（架构宪法 §10/§12、domain-model §对象生命周期）；
    storage_path 的解释/分桶/临时文件全部由 iesplan.storage 内部实现，
    本模块不拼路径、不导入 StoredObject ORM。
    """
    handle = put_object(
        db,
        project_domain.content_to_bytes(content),
        "application/json",
        source_category="project_content",
    )
    # owner 引用（对象生命周期权威事实）：草稿/版本内容对象不可清理；
    # 引用键为稳定的对象 id（重复写入幂等复用同一引用行）。
    attach(
        db,
        handle.id,
        "draft_content",
        handle.id,
        ref_entity_type="drafts",
        purpose="草稿内容文档",
    )
    return handle.id


def load_content_bytes(db: Session, content_object_id: int) -> bytes:
    """按对象 id 读取内容字节（对象缺失/损坏抛 AppError，供字节级一致性比对）。"""
    try:
        return get_object(db, content_object_id)
    except NotFoundError as exc:
        raise project_domain.corrupt_error(
            "内容对象缺失(数据损坏)", object_id=content_object_id
        ) from exc
    except ObjectCorruptError as exc:
        raise project_domain.corrupt_error(
            "内容对象读取失败(数据损坏)", object_id=content_object_id
        ) from exc


def load_content_object(db: Session, content_object_id: int) -> dict:
    """按对象 id 读取内容对象（缺失/损坏/结构非法一律按数据损坏明确报错）。"""
    return project_domain.parse_content_object(load_content_bytes(db, content_object_id))


def merge_patch(base: dict, patch: dict) -> None:
    """递归合并补丁到内容文档字典(值为 dict 时继续下钻, 其余覆盖)。

    草稿语义命令（layout.patch / config.patch / set_extensions）与版本应用
    （apply_result）的补丁语义唯一实现；调用方就地修改 ``base``。
    """
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge_patch(base[key], value)
        else:
            base[key] = value
