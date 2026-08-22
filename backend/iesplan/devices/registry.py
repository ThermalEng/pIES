"""运行期设备注册表(替代 core/registry.py 静态注册;02 §6.4;05 §7.6)。

- 插件式: 新增设备 = 放入 catalog/<id>.yaml(+csv), 无需改代码, reload() 热加载;
- 受控加载: 任一设备校验失败即整体拒绝(load_all_devices 语义);
- 运行期只读: get/list/snapshot; 快照格式沿用 core/registry.py 的 id@version,
  计算快照引用 id@version, reload 后旧版本仍可解析。

core/registry.py 的静态注册退化为内置兜底副本, 本注册表为 yaml 优先路径。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from iesplan.core.errors import AppError, NotFoundError
from iesplan.devices.loader import DEFAULT_CATALOG_DIR, load_all_devices
from iesplan.devices.pricing import PriceBook, load_price_book
from iesplan.devices.spec import DeviceYamlSpec

#: 能力字典 → 设备粗分类别(镜像 services/model.py::_DEVICE_COARSE_CATEGORY, yaml 派生)
_CAPABILITY_COARSE: dict[str, str] = {
    "grid_connection": "source",
    "pv": "pv",
    "storage": "storage",
    "load": "load",
    "heat_pump": "converter",
    "thermal_generation": "boiler",
    "cooling_generation": "chiller",
}


class DeviceRegistry:
    """运行期设备注册表(启动时 init_registry 初始化; 运行期只读)。

    RR-P2-04: 正式发布前不实现运行期热加载(架构宪法 5.3); 不暴露 reload 入口。
    """

    def __init__(self, base_dir: Path, price_book: PriceBook) -> None:
        self.base_dir = Path(base_dir)
        self.price_book = price_book
        self._specs: dict[str, DeviceYamlSpec] = {}

    def load(self) -> None:
        """幂等加载: 目录下全部设备; 任一失败整体拒绝并抛 AppError。"""
        self._specs = {s.type_id: s for s in load_all_devices(self.base_dir, self.price_book)}

    def get(self, type_id: str) -> DeviceYamlSpec:
        """按注册 id 取设备类型(未注册抛 NotFoundError, 码 CONN-TYPE-002)。"""
        spec = self._specs.get(type_id)
        if spec is None:
            raise NotFoundError(
                f"设备类型未注册: {type_id}",
                code="CONN-TYPE-002",
                message_key="ies.diag.conn.type_unregistered",
                params={"device_id": "", "type_id": type_id},
            )
        return spec

    def list(self) -> list[DeviceYamlSpec]:
        """列出全部已注册设备(按注册顺序, 确定性)。"""
        return list(self._specs.values())

    def csv_path_for(self, type_id: str) -> Path:
        """data_repeat 设备标准 csv 路径（路径解析只存在于 devices 模块内部）。

        权威规则：``<yaml 完整路径去后缀>.csv``；文件不存在返回明确错误。
        外部模块不感知该规则（经 get_profile_columns 消费）。
        """
        spec = self.get(type_id)
        if not spec.source_path:
            raise AppError(
                f"设备 {type_id} 缺少源 yaml 路径, 无法推导标准 csv",
                code="SYS-CFG-001",
                message_key="ies.diag.store.config_invalid",
                params={"device_id": type_id},
            )
        candidate = Path(spec.source_path).with_suffix(".csv")
        if not candidate.exists():
            raise AppError(
                f"data_repeat 设备 {type_id} 缺少标准 csv: {candidate.name}",
                code="SYS-CFG-001",
                message_key="ies.diag.store.config_invalid",
                params={"device_id": type_id, "file": candidate.name},
            )
        return candidate

    def snapshot(self) -> list[str]:
        """注册表快照: ["ies.device.pv@1.4.0", ...](与 core/registry.py 格式一致)。"""
        return [f"{s.type_id}@{s.version}" for s in self._specs.values()]

    def command_id(self, type_id: str) -> str:
        """标准调用命令 id: 'ies.command.model.{type_id}.{method}.{version}' (03 §5.2)。"""
        spec = self.get(type_id)
        return f"ies.command.model.{spec.type_id}.{spec.model_method}.{spec.version}"

    def get_entry_function(self, type_id: str) -> Callable:
        """按 type_id 返回统一调用契约 device_entry 函数(roadmap 0.5.0)。

        - mechanism: 经设备 model_commands 的稳定命令 ID 在 modeling provider
          内解析机理函数（组合根/provider 解析，设备文件不暴露函数入口）;
        - data_repeat / data_predict: 函数生成(周期外推/模型加载)归 modeling 模块,
          经 modeling.command 命令注册表取函数。
        """
        spec = self.get(type_id)
        commands = dict(spec.model_commands) if spec.model_commands else {}
        if spec.model_method == "mechanism":
            ref = next(iter(commands.values()), "")
            command_id = ref.split("@", 1)[0] if "@" in ref else ref
            if not command_id:
                raise AppError(
                    f"设备 {type_id} 缺少机理命令(model_commands)",
                    code="SYS-CFG-001",
                    message_key="ies.diag.store.config_invalid",
                    params={"device_id": type_id},
                )
            from iesplan.modeling.functions import (
                as_device_entry,
                mechanism_spec_for,
                resolve_command_function,
            )

            model_function = resolve_command_function(command_id)
            ms = mechanism_spec_for(model_function)
            if ms is None:
                raise AppError(
                    f"机理映射表缺少函数: {command_id!r}(设备 {type_id})",
                    code="SYS-CFG-001",
                    message_key="ies.diag.store.config_invalid",
                    params={"device_id": type_id, "command_id": command_id},
                )
            return as_device_entry(
                ms.fn,
                series_keys=ms.series_keys,
                param_bindings=ms.param_bindings,
                output_name=ms.output_name,
                state_key=ms.state_key,
                state_arg=ms.state_arg,
                takes_dt=ms.takes_dt,
            )
        # data_repeat / data_predict: 函数由 modeling 生成并注册到命令注册表
        from iesplan.modeling.command import get_entry_function as modeling_entry

        command_id = self.command_id(type_id)
        try:
            return modeling_entry(command_id)
        except Exception as exc:
            raise AppError(
                f"{type_id} 的设备函数由 modeling 模块生成(05 §7.6), 绑定不可用; "
                f"命令: {command_id}",
                code="SYS-CFG-001",
                message_key="ies.diag.store.config_invalid",
                params={"device_id": type_id, "command": command_id},
            ) from exc

    def port_directions(self, type_id: str) -> dict[str, str]:
        """载体 → 端口方向(替代 services/model.py::_DEVICE_PORT_DIRECTIONS; yaml ports 派生)。

        同一载体多个端口时: 任一 bidirectional → bidirectional; 方向冲突 → bidirectional。
        """
        spec = self.get(type_id)
        merged: dict[str, str] = {}
        for port in spec.ports:
            cur = merged.get(port.energy_carrier)
            if cur is None:
                merged[port.energy_carrier] = port.direction
            elif port.direction == "bidirectional" or cur == "bidirectional":
                merged[port.energy_carrier] = "bidirectional"
            elif cur != port.direction:
                merged[port.energy_carrier] = "bidirectional"
        return merged

    def coarse_category(self, type_id: str) -> str:
        """设备粗分类别(替代 services/model.py::_DEVICE_COARSE_CATEGORY; yaml 派生)。"""
        spec = self.get(type_id)
        for cap in spec.capabilities:
            if cap in _CAPABILITY_COARSE:
                return _CAPABILITY_COARSE[cap]
        if spec.is_load:
            return "load"
        carriers = set(spec.energy_carriers)
        if "gas" in carriers:
            return "boiler"
        if "cool" in carriers:
            return "chiller"
        if "heat" in carriers:
            return "converter"
        return "other"


# ---------------------------------------------------------------------------
# 进程内单例(与 core/registry.py 现语义一致, 启动时由 main.py 初始化)
# ---------------------------------------------------------------------------

_registry: DeviceRegistry | None = None


def get_registry() -> DeviceRegistry:
    """进程内单例; 未初始化抛 AppError(SYS-CFG-001)。"""
    if _registry is None:
        raise AppError(
            "设备注册表尚未初始化",
            code="SYS-CFG-001",
            message_key="ies.diag.store.config_invalid",
            params={},
        )
    return _registry


def init_registry(base_dir: Path | None = None, book: PriceBook | None = None) -> DeviceRegistry:
    """初始化(或重初始化)进程内设备注册表; 返回注册表实例。

    缺省 base_dir 为内置 catalog/, 缺省价格书为内置 prices.yaml。

    原子发布(架构宪法 5.3 "全部成功或不发布新状态"): 候选注册表先完整
    ``load()``(读取/校验所有 YAML、价格引用、CSV), 成功后才一次性替换
    全局引用; 加载失败不触碰旧注册表, 调用方可继续使用先前有效状态。
    """
    global _registry
    base = Path(base_dir) if base_dir is not None else DEFAULT_CATALOG_DIR
    price_book = book if book is not None else load_price_book()
    candidate = DeviceRegistry(base, price_book)
    candidate.load()  # 失败抛异常, 全局 _registry 保持不变
    _registry = candidate  # 全部成功后一次性发布
    return candidate
