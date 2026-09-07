"""离线清理 CLI：退役旧序列预备(sequence_prep)路径的历史残留。

0.6.5 事项 3-4 完整删除旧装配前物化路径。本命令只做一次性历史痕迹清理
(草稿内容中的 ``prepared_sequences`` 键 + ``sequence_prep:*`` 目的引用),
退役后不存在任何运行期读取, 不在 API/Worker 中调用。

用法示例::

    python -m iesplan.cli.purge_sequence_prep                 # dry-run 扫描报告
    python -m iesplan.cli.purge_sequence_prep --apply         # 实际清理
    python -m iesplan.cli.purge_sequence_prep --apply \
        --receipt-path /var/lib/pies/migration-receipt-preassembly-0.6.5.json

退出码: 0 成功; 2 参数错误; 1 数据库会话异常。
"""
from __future__ import annotations

import argparse
import json
import sys

from iesplan.db import SessionLocal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理旧 sequence_prep 路径残留(幂等, 离线)")
    parser.add_argument("--apply", action="store_true", help="执行清理(默认 dry-run 只报告)")
    parser.add_argument("--receipt-path", default=None,
                        help="把回执 JSON 写入该路径(执行成功时)")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        from iesplan.services.project import purge_legacy_sequence_prep

        receipt = purge_legacy_sequence_prep(db, dry_run=not args.apply)
        if args.apply:
            db.commit()
    except Exception as exc:  # noqa: BLE001 - CLI 顶层捕获, 输出诊断后退出
        db.rollback()
        print(f"清理失败(已回滚): {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    if args.apply and args.receipt_path:
        with open(args.receipt_path, "w", encoding="utf-8") as fh:
            json.dump(receipt, fh, ensure_ascii=False, sort_keys=True, indent=2)
        print(f"回执已写入: {args.receipt_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
