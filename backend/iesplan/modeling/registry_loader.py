"""建模命令注册流程(启动时遍历设备目录构建命令, 03 §5.2/§14.4)。

审查意见与 03-module-decoupling.md §6.4:应用启动(或注册表热加载)时遍历设备目录,
把每个设备 yaml 转 modeling DeviceSpec 并 ``build_command`` 注册到命令注册表;
data_repeat 设备从同目录同名标准 csv 加载典型曲线(profile)传入。
注册失败(设备 yaml 校验失败/命令冲突)抛 AppError 整体拒绝启动(受控加载语义)。

调用点:main.py 启动流程 / 注册表热加载后重载命令。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from iesplan.core.errors import AppError
from iesplan.devices.loader import DEFAULT_CATALOG_DIR, load_all_devices
from iesplan.devices.pricing import load_price_book
from iesplan.devices.profile import read_standard_csv
from iesplan.devices.spec import DeviceYamlSpec, to_modeling_spec
from iesplan.modeling.build import build_command
from iesplan.modeling.command import clear_commands, init_compute_commands

logger = logging.getLogger(__name__)


def _load_profile_csv(spec: DeviceYamlSpec) -> dict[str, np.ndarray] | None:
    """data_repeat 设备: 从 yaml 同目录同名 csv 读取典型曲线(列名 → 一维数组)。

    路径推导统一走 spec.standard_csv_path(03 §6.4 唯一规则, codex 二次审核
    Medium-4); 无 csv 返回 None(由 build_command 的 data_file 校验给出明确错误)。
    读取/校验失败抛 AppError(不降级为 warning: 原始文件/列错误必须可见,
    由注册调用方按受控加载语义整体处理)。
    """
    from iesplan.devices.spec import standard_csv_path

    candidate = standard_csv_path(spec)
    if candidate is None:
        return None
    df = read_standard_csv(Path(candidate), spec)
    return {col: df[col].to_numpy(dtype=np.float64) for col in df.columns if col != "timestamp"}


def register_catalog_commands(base_dir: Path | None = None) -> int:
    """遍历设备目录, 为每个设备生成并注册建模命令; 返回注册命令数。

    - 任一设备 build_command 失败 → 抛 AppError 并携带设备 id(受控加载, 不静默跳过);
    - data_repeat 设备缺 csv 时抛 AppError(与 build_command data_file 校验一致)。
    """
    base = Path(base_dir) if base_dir is not None else DEFAULT_CATALOG_DIR
    book = load_price_book()
    specs = load_all_devices(base, book)  # 受控加载: 任一设备校验失败整体拒绝
    clear_commands()
    init_compute_commands()  # 计算引擎命令(03 §9.3: 与设备命令同表注册)
    registered = 0
    for spec in specs:
        mspec = to_modeling_spec(spec)
        profile = None
        if spec.model_method == "data_repeat":
            profile = _load_profile_csv(spec)
            if profile is None and mspec.data_file is None:
                raise AppError(
                    f"data_repeat 设备 {spec.type_id} 缺少标准 csv 数据(启动注册拒绝)",
                    code="SYS-CFG-001",
                    message_key="ies.diag.store.config_invalid",
                    params={"device_id": spec.type_id},
                )
        try:
            cmd = build_command(mspec, profile=profile)
        except Exception as exc:
            raise AppError(
                f"设备 {spec.type_id} 建模命令生成失败(启动注册拒绝): {exc}",
                code="SYS-CFG-001",
                message_key="ies.diag.store.config_invalid",
                params={"device_id": spec.type_id},
            ) from exc
        registered += 1
        logger.info("建模命令已注册: %s → %s", spec.type_id, cmd.command_id)
    return registered


def reload_catalog_commands(base_dir: Path | None = None) -> int:
    """热加载重载建模命令(注册表插件热加载后调用)。"""
    return register_catalog_commands(base_dir)


__all__ = ["register_catalog_commands", "reload_catalog_commands"]
