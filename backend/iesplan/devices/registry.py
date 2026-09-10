"""运行期设备注册表（ies.device-model 2.0.0 最小实现）。

- 插件式：新增设备 = 放入 catalog/<id>.yaml，无需改代码；
- 受控加载：任一设备校验失败即整体拒绝；
- 运行期只读：get/list；不实现热加载（宪法 5.3）；
- 仅持有 2.0 不可变文档（DeviceModelDocument），不持有旧 1.0 spec/pricing。
"""

from __future__ import annotations

from pathlib import Path

from iesplan.core.errors import AppError, NotFoundError
from iesplan.devices.contracts2 import DeviceModelDocument
from iesplan.devices.loader import DEFAULT_CATALOG_DIR, load_all_devices


class DeviceRegistry:
    """运行期设备注册表（2.0 文档）。"""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self._docs: dict[str, DeviceModelDocument] = {}

    def load(self) -> None:
        """幂等加载：目录下全部设备；任一失败整体拒绝。"""
        self._docs = {d.device.id: d for d in load_all_devices(self.base_dir) if d.device}

    def get(self, type_id: str) -> DeviceModelDocument:
        """按 type_id 取文档；未注册抛 NotFoundError（CONN-TYPE-002）。"""
        doc = self._docs.get(type_id)
        if doc is None:
            raise NotFoundError(
                f"设备类型未注册: {type_id}",
                code="CONN-TYPE-002",
                message_key="ies.diag.conn.type_unregistered",
                params={"device_id": "", "type_id": type_id},
            )
        return doc

    def list(self) -> list[DeviceModelDocument]:
        """列出全部已注册设备（按 id 确定性排序）。"""
        return sorted(self._docs.values(), key=lambda d: d.device.id if d.device else "")

    def snapshot(self) -> list[str]:
        """注册表快照：device id 列表（2.0 不含独立版本，返回 id）。"""
        return sorted(self._docs.keys())


# ---------------------------------------------------------------------------
# 进程内单例
# ---------------------------------------------------------------------------

_registry: DeviceRegistry | None = None


def get_registry() -> DeviceRegistry:
    """进程内单例；未初始化抛 AppError。"""
    if _registry is None:
        raise AppError(
            "设备注册表尚未初始化",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={},
        )
    return _registry


def init_registry(base_dir: Path | None = None) -> DeviceRegistry:
    """初始化（或重初始化）进程内设备注册表；返回实例。

    原子发布：候选注册表先完整 load，成功后一次性替换全局引用；失败不触碰旧状态。
    """
    global _registry
    base = Path(base_dir) if base_dir is not None else DEFAULT_CATALOG_DIR
    candidate = DeviceRegistry(base)
    candidate.load()
    _registry = candidate
    return candidate
