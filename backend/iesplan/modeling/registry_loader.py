"""建模命令注册流程(BE-REG-01/02/RR-P1-02: 公开协议消费 + 无副作用原子发布)。

模块边界(BE-REG-01/RR-P2-04):
- 设备描述只经 ``iesplan.devices.list_device_descriptors()`` 公开门面获取,
  本模块不再导入 devices.loader/pricing/profile/spec 内部实现;
- 标准 csv 路径由 devices 推导并随描述导出(standard_csv_path 字段),
  modeling 不感知目录扫描/价格解析/CSV 路径规则;
- 计算引擎命令经 ``iesplan.modeling.command.compute_command_refs()`` 公开
  只读视图获取, 不导入模块内私有常量。

原子发布(BE-REG-02/RR-P1-02):
- ``build_command`` 为无副作用构建器, 返回 (ModuleCommand, 统一 callable);
- 本模块先在临时 dict 中完整构建并校验全部命令(计算命令 + 设备命令,
  校验 id 唯一 / 函数可解析 / profile 完整), 全部成功后才一次性
  ``replace_all_commands(staged, generated=callables)`` 原子替换;
- 任一失败保留旧快照并上抛, 不暴露部分新状态。

调用点:main.py / worker 启动流程; 正式发布前不提供运行期热加载
(不保留 reload_catalog_commands 入口)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace

import numpy as np

from iesplan.core.errors import AppError
from iesplan.devices import DeviceModelDescriptor, list_device_descriptors
from iesplan.modeling.build import build_command
from iesplan.modeling.command import (
    ModuleCommand,
    compute_command_refs,
    replace_all_commands,
    resolve_function_ref,
)
from iesplan.modeling.devspec import DeviceSpec as ModelingDeviceSpec
from iesplan.modeling.devspec import PortSpec as ModelingPortSpec
from iesplan.modeling.devspec import SeriesSpec as ModelingSeriesSpec
from iesplan.modeling.devspec import StateSpec as ModelingStateSpec

logger = logging.getLogger(__name__)


def _descriptor_to_modeling_spec(desc: DeviceModelDescriptor) -> ModelingDeviceSpec:
    """公开设备描述 → modeling 规格(roadmap 0.5.0 阶段 ①→② 契约)。

    命令解析只在 provider 内部完成：
    - mechanism: 取设备 model_commands 首个稳定命令 ID，经
      ``iesplan.modeling.functions.resolve_command_function`` 解析为机理函数;
    - data_repeat: 标准 csv 数据经 ``iesplan.devices.get_profile_columns`` 读取
      (devices 内部解析路径, 公开描述不含宿主机路径)。
    """
    commands = dict(desc.model_commands) if desc.model_commands else {}
    model_function = ""
    if desc.model_method == "mechanism":
        ref = next(iter(commands.values()), "")
        command_id = ref.split("@", 1)[0] if "@" in ref else ref
        if command_id:
            from iesplan.modeling.functions import resolve_command_function

            model_function = resolve_command_function(command_id)
    return ModelingDeviceSpec(
        type_id=desc.type_id,
        version=desc.version,
        name_zh=desc.name_zh,
        name_en=desc.name_en,
        energy_carriers=list(desc.energy_carriers),
        is_load=desc.is_load,
        capabilities=list(desc.capabilities),
        extends=desc.extends,
        parameters=dict(desc.parameters),
        help_topic=desc.help_topic,
        model_method=desc.model_method,
        stateful=desc.stateful,
        fidelity=desc.fidelity,
        model_function=model_function,
        model_file=None,
        data_file=None,
        ports=tuple(
            ModelingPortSpec(
                name=p.name,
                port_type=p.port_type,
                direction=p.direction,
                energy_carrier=p.energy_carrier,
                capacity_ref=p.capacity_ref,
            )
            for p in desc.ports
        ),
        time_series={
            "inputs": tuple(
                ModelingSeriesSpec(
                    key=s.key, unit=s.unit, resolution=s.resolution,
                    required=s.required, period=s.period,
                )
                for s in desc.time_series.get("inputs", [])
            ),
            "outputs": tuple(
                ModelingSeriesSpec(
                    key=s.key, unit=s.unit, resolution=s.resolution,
                    required=s.required, period=s.period,
                )
                for s in desc.time_series.get("outputs", [])
            ),
        },
        states=tuple(
            ModelingStateSpec(
                key=s.key, unit=s.unit, initial_ref=s.initial_ref, bounds=s.bounds
            )
            for s in desc.states
        ),
    )


def _load_profile_csv(desc: DeviceModelDescriptor) -> dict[str, np.ndarray] | None:
    """data_repeat 设备: 读取 devices 推导的标准 csv(列名 → 一维数组)。

    经 ``iesplan.devices.get_profile_columns`` 公开门面按 type_id 读取;
    读取/校验失败抛 AppError(不降级为 warning: 原始文件/列错误必须可见)。
    """
    from iesplan.devices import get_profile_columns

    if desc.model_method != "data_repeat":
        return None
    return get_profile_columns(desc.type_id)


def register_catalog_commands() -> int:
    """经公开设备门面构建并**原子替换**建模命令注册表; 返回注册命令数。

    - 临时 dict 完整构建全部命令(计算命令 + 设备命令 + 生成 callable);
    - 任一设备 build_command 失败 → 抛 AppError 且**旧快照完整保留**
      (BE-REG-02/RR-P1-02: 无副作用构建, 不先清空再逐项注册);
    - 全部成功 → ``replace_all_commands(staged, generated=callables)``
      一次性替换快照(命令与 callable 同快照发布, 不会出现统一 wrapper 丢失);
    - data_repeat 设备缺 csv 时抛 AppError(与 build_command data_file 校验一致)。
    """
    descriptors = list_device_descriptors()
    staged: dict[str, ModuleCommand] = {}
    callables: dict[str, Callable] = {}
    # 计算引擎命令: 先解析函数引用(启动阶段校验, 失败则整个注册流程拒绝)。
    # 宪法 4.10 "Worker 启动时必须验证所需命令可解析": 计算命令只登记字符串
    # 引用, 若等到任务执行时才 importlib 解析, 配置错误会延迟到运行期暴露
    # (任务批量失败/租约占用), 违反失败前置。设备命令在 build_command 内已
    # 构建真实 callable, 无此问题。
    for command_id, ref in compute_command_refs().items():
        resolve_function_ref(ref)  # 失败抛 NotFoundError, 不发布任何新状态
        staged[command_id] = ModuleCommand(
            command_id=command_id, function_ref=ref, version="1.0.0", stateful=False,
        )
    registered = 0
    for desc in descriptors:
        mspec = _descriptor_to_modeling_spec(desc)
        profile = None
        if desc.model_method == "data_repeat":
            profile = _load_profile_csv(desc)
            if profile is None:
                raise AppError(
                    f"data_repeat 设备 {desc.type_id} 缺少标准 csv 数据(启动注册拒绝)",
                    code="SYS-CFG-001",
                    message_key="ies.diag.store.config_invalid",
                    params={"device_id": desc.type_id},
                )
            # 数据已成功读取; data_file 只作逻辑引用(非宿主机路径, 不参与计算)。
            mspec = replace(mspec, data_file=f"ies.profile:{desc.type_id}")
        try:
            cmd, entry = build_command(mspec, profile=profile)
        except Exception as exc:
            raise AppError(
                f"设备 {desc.type_id} 建模命令生成失败(启动注册拒绝): {exc}",
                code="SYS-CFG-001",
                message_key="ies.diag.store.config_invalid",
                params={"device_id": desc.type_id},
            ) from exc
        if cmd.command_id in staged:
            raise AppError(
                f"命令 id 冲突: {cmd.command_id}(设备 {desc.type_id})",
                code="SYS-CFG-001",
                message_key="ies.diag.store.config_invalid",
                params={"device_id": desc.type_id, "command_id": cmd.command_id},
            )
        staged[cmd.command_id] = cmd
        callables[cmd.command_id] = entry
        registered += 1
        logger.info("建模命令已构建: %s → %s", desc.type_id, cmd.command_id)
    # 全部构建成功 → 一次性替换全局快照(命令 + callable 同快照原子发布)
    replace_all_commands(staged, generated=callables)
    return registered
