"""文件系统 BlobStore 适配器(STO-04/07: 字节可靠存取 + 可替换 provider)。

- 临时文件完整写入 → fsync → 计算摘要 → 以确定性路径原子 rename 提交;
- 目录扫描(reconcile)报告磁盘孤儿(有文件无记录)与缺失(有记录无文件);
- 本适配器不读数据库、不理解业务引用; 生命周期与元数据由 storage.service 编排。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from iesplan.config import settings
from iesplan.storage.contracts import BlobMissingError


def _objects_root() -> Path:
    """对象存储根目录(settings.data_dir/objects), 不存在则自动创建。"""
    root = settings.data_dir / "objects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _tmp_root() -> Path:
    """对象临时区目录(settings.data_dir/objects/tmp), 不存在则自动创建。"""
    root = _objects_root() / "tmp"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _path_of(storage_path: str) -> Path:
    """storage_path(相对 data_dir 的路径)→ 磁盘绝对路径。

    STO-01: storage_path 的解释只在本适配器发生, 全仓库唯一。
    """
    return settings.data_dir / storage_path


class FileSystemBlobStore:
    """本地文件系统字节存储(STO-07: 可被测试内存适配器替换)。"""

    def put_blob(self, content: bytes) -> tuple[str, str]:
        """完整字节原子提交 → (storage_path, digest)。

        流程: 临时区写入 → fsync → 计算 sha256 → 原子 rename 到
        data_dir/objects/{sha256}。失败时清理临时文件, 不留下半成品。
        """
        import hashlib

        digest = hashlib.sha256(content).hexdigest()
        final_path = _objects_root() / digest
        fd, tmp_name = tempfile.mkstemp(dir=_tmp_root(), prefix="put-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, final_path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return f"objects/{digest}", digest

    def get_blob(self, storage_path: str) -> bytes:
        """按 storage_path 读取字节; 文件缺失抛 BlobMissingError。"""
        path = _path_of(storage_path)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise BlobMissingError(
                "",
                params={"reason": "missing", "path": str(path)},
            ) from exc

    def delete_blob(self, storage_path: str) -> None:
        """删除字节(不存在幂等)。"""
        try:
            _path_of(storage_path).unlink()
        except OSError:
            pass

    def exists(self, storage_path: str) -> bool:
        return _path_of(storage_path).is_file()

    def list_final_files(self) -> list[str]:
        """扫描最终目录全部文件(不含 tmp), 返回相对 data_dir 的 storage_path。

        供 reconcile 查找磁盘孤儿(有文件无元数据记录)。
        """
        root = _objects_root()
        return [str(p.relative_to(settings.data_dir)) for p in root.rglob("*") if p.is_file()]

    def cleanup_tmp(self, max_age_seconds: int = 86400) -> list[str]:
        """清理超龄临时文件(中断残留), 返回已删除列表。"""
        removed: list[str] = []
        now_epoch = int(__import__("time").time())
        for p in _tmp_root().glob("*.tmp"):
            try:
                age = now_epoch - int(p.stat().st_mtime)
            except OSError:
                continue
            if age > max_age_seconds:
                try:
                    p.unlink()
                    removed.append(p.name)
                except OSError:
                    pass
        return removed
