"""离线迁移 CLI（任务书 §三）：已发布/草稿旧稳定 ID → 新稳定 ID。

本模块是“可实际调用的正式离线迁移命令”，不经过 HTTP API。
复用 application 层的迁移用例保持事务/引用/审计一致，命令行仅做
参数解析与会话管理（不得把用户上传内容运行期加载到 API/Worker）。

用法示例::

    python -m iesplan.cli.migrate_templates --old-id acme.device.old --new-slug my-new-slug
    python -m iesplan.cli.migrate_templates --old-id acme.device.old --new-slug my-new-slug --user-id 42
    python -m iesplan.cli.migrate_templates --old-id acme.device.old --new-slug my-new-slug --dry-run

"""
from __future__ import annotations

import argparse
import sys
from typing import Any

from iesplan.db import SessionLocal
from iesplan.models.identity import User


def _get_user(db, *, user_id: int | None, username: str | None) -> Any:
    if user_id is not None:
        user = db.get(User, user_id)
        if user is None:
            raise SystemExit(f"用户不存在: id={user_id}")
        return user
    if username is not None:
        import sqlalchemy as sa
        row = db.execute(sa.select(User).where(User.username == username)).scalar_one_or_none()
        if row is None:
            raise SystemExit(f"用户不存在: username={username!r}")
        return row
    raise SystemExit("需指定 --user-id 或 --username")


def _resolve_old_id(db, user, old_id: str) -> str:
    """校验旧 ID 存在且属于当前用户（不泄露他人模板存在性沿用 404 语义）。"""
    import sqlalchemy as sa
    from iesplan.models.model_template import ModelTemplate
    row = db.execute(sa.select(ModelTemplate).where(ModelTemplate.template_id == old_id, ModelTemplate.owner_id == user.id)).scalar_one_or_none()
    if row is None:
        raise SystemExit(f"模板不存在或不属于当前用户: {old_id}")
    return old_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线迁移模板稳定 ID（旧 → 新）")
    parser.add_argument("--old-id", required=True, help="旧模板稳定 ID（如 acme.device.old）")
    parser.add_argument("--new-slug", required=True, help="新 slug（小写字母/数字，点/下划线/连字符分段）")
    parser.add_argument("--user-id", type=int, default=None, help="执行用户 ID")
    parser.add_argument("--username", default=None, help="执行用户名（与 --user-id 二选一）")
    parser.add_argument("--dry-run", action="store_true", help="仅校验与展示映射，不提交")
    parser.add_argument("--published", action="store_true", help="迁移已发布模板（默认按模板状态自动选择）")
    parser.add_argument("--draft", action="store_true", help="迁移未发布草稿")
    args = parser.parse_args(argv)
    if args.published and args.draft:
        print("不能同时指定 --published 与 --draft", file=sys.stderr)
        return 2
    if args.user_id is None and args.username is None:
        print("需指定 --user-id 或 --username", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        user = _get_user(db, user_id=args.user_id, username=args.username)
        old_id = _resolve_old_id(db, user, args.old_id)
        from iesplan.application.model_templates.service import (
            migrate_draft_to_new_stable_id,
            migrate_published_template,
        )
        from iesplan.core.namespace import build_stable_id
        from iesplan.application.namespace import ensure_public_namespace

        if args.dry_run:
            # dry-run 不持久化命名空间（只读展示，若缺失则提示）
            if not user.public_namespace:
                from iesplan.core.namespace import generate_namespace as _gen
                namespace = _gen()
                print(f"[dry-run] 用户暂无 public_namespace，预生成示例: {namespace}（未写入）", file=sys.stderr)
            else:
                namespace = user.public_namespace
        else:
            namespace = user.public_namespace or ensure_public_namespace(db, user)
        # dry-run: 展示映射与摘要，不提交
        try:
            new_id = build_stable_id(namespace, args.new_slug)
        except Exception as exc:
            print(f"新 slug 非法: {exc}", file=sys.stderr)
            return 2
        if args.dry_run:
            print(f"old_id={old_id}")
            print(f"new_id={new_id}")
            print(f"namespace={namespace}")
            print("dry-run: 未提交")
            return 0

        # 实际迁移：application 用例内部单事务提交/回滚
        # 若强制指定草稿
        if args.draft:
            result = migrate_draft_to_new_stable_id(db, user, old_id, args.new_slug)
            print(f"草稿迁移成功: {old_id} -> {result['new_template_id']}")
            print(f"old_sha={result['old_content_sha256']}")
            print(f"new_sha={result['new_content_sha256']}")
            return 0
        if args.published:
            result = migrate_published_template(db, user, old_id, args.new_slug)
            receipt = result.get("receipt", result)
            print(f"发布迁移成功: {old_id} -> {receipt['new_template_id']}")
            if "old_content_sha256" in receipt:
                print(f"old_sha={receipt['old_content_sha256']}")
                print(f"new_sha={receipt['new_content_sha256']}")
            if result.get("duplicate"):
                print("幂等命中（已有回执）")
            return 0
        # 自动模式：仅当已发布路径明确报“尚未发布/无需迁移”时回退到草稿，避免掩盖真实错误
        from iesplan.core.errors import AppError as _AppError
        try:
            result = migrate_published_template(db, user, old_id, args.new_slug)
            receipt = result.get("receipt", result)
            print(f"发布迁移成功: {old_id} -> {receipt['new_template_id']}")
            if "old_content_sha256" in receipt:
                print(f"old_sha={receipt['old_content_sha256']}")
                print(f"new_sha={receipt['new_content_sha256']}")
            if result.get("duplicate"):
                print("幂等命中（已有回执）")
            return 0
        except _AppError as exc:
            code = getattr(exc, "code", "")
            # 仅 TPL-MDL-006（未发布）才回退为草稿迁移；其余错误直接失败不掩盖
            if code == "TPL-MDL-006":
                result = migrate_draft_to_new_stable_id(db, user, old_id, args.new_slug)
                print(f"草稿迁移成功: {old_id} -> {result['new_template_id']}")
                print(f"old_sha={result['old_content_sha256']}")
                print(f"new_sha={result['new_content_sha256']}")
                return 0
            msg = str(getattr(exc, "message_key", "") or exc)
            print(f"迁移失败: {exc} code={code} key={msg}", file=sys.stderr)
            return 1
    except SystemExit as exc:
        # 保留 SystemExit 码
        raise
    except Exception as exc:
        code = getattr(exc, "code", "")
        print(f"迁移失败: {exc} code={code}", file=sys.stderr)
        return 1
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
