"""建模命令注册流程(BE-REG-01/02/RR-P1-02: 公开协议消费 + 无副作用原子发布)。

模块边界(BE-REG-01/RR-P2-04):
- 设备描述只经 ``iesplan.devices.list_devices()`` 公开门面获取 2.0
  ``DeviceModelDocument``，不再消费 1.0 ``DeviceModelDescriptor`` /
  ``list_device_descriptors`` / ``get_profile_columns``；
- 设备方程语义经 ``modeling.contract2.build_math_contribution`` 纯协议校验
  （properties / interfaces / equations / device.id / schema_version），
  不建立每设备命令注册表（宪法 §5.2）；
- 计算引擎命令经 ``iesplan.modeling.command.compute_command_refs()`` 公开
  只读视图获取，不导入模块内私有常量。

原子发布(BE-REG-02/RR-P1-02):
- 计算命令先在临时 dict 中完整构建并校验（函数可解析），任一失败保留旧
  快照并上抛，不暴露部分新状态；
- 全部 2.0 设备文档的方程贡献校验通过后，才一次性
  ``replace_all_commands(staged, generated=callables)`` 原子替换；
- 任一设备缺稳定身份或方程贡献含阻断诊断 → 抛 AppError 阻断发布，
  不跳过、不占位、不降级。

调用点:main.py / worker 启动流程; 正式发布前不提供运行期热加载
(不保留 reload_catalog_commands 入口)。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from iesplan.core.errors import AppError
from iesplan.devices import list_devices
from iesplan.modeling.command import (
    ModuleCommand,
    compute_command_refs,
    replace_all_commands,
    resolve_function_ref,
)
from iesplan.modeling.contract2 import build_math_contribution

logger = logging.getLogger(__name__)


def register_catalog_commands() -> int:
    """经公开设备门面校验 2.0 方程贡献并**原子替换**建模命令注册表。

    返回校验通过的设备文档数。

    - 临时 dict 完整构建全部计算命令（函数引用启动阶段校验，失败则整个
      注册流程拒绝）；
    - 全部 2.0 设备文档逐一经 ``build_math_contribution`` 校验方程贡献，
      任一阻断诊断 → 抛 AppError 且**旧快照完整保留**
      (BE-REG-02/RR-P1-02: 无副作用构建，不先清空再逐项注册)；
    - 全部成功 → ``replace_all_commands(staged, generated=callables)``
      一次性替换全局快照（命令与 callable 同快照原子发布）。
    """
    staged: dict[str, ModuleCommand] = {}
    callables: dict[str, Callable] = {}
    # 计算引擎命令: 先解析函数引用(启动阶段校验, 失败则整个注册流程拒绝)。
    # 宪法 4.10 "Worker 启动时必须验证所需命令可解析": 计算命令只登记字符串
    # 引用, 若等到任务执行时才 importlib 解析, 配置错误会延迟到运行期暴露
    # (任务批量失败/租约占用), 违反失败前置。
    for command_id, ref in compute_command_refs().items():
        resolve_function_ref(ref)  # 失败抛 NotFoundError, 不发布任何新状态
        staged[command_id] = ModuleCommand(
            command_id=command_id, function_ref=ref, version="1.0.0", stateful=False,
        )
    # 2.0 设备文档: 稳定身份 + 方程贡献校验(任一失败阻断发布, 不发布部分状态)。
    validated = 0
    for doc in list_devices():
        device_id = doc.device.id if doc.device is not None else ""
        if not device_id:
            raise AppError(
                "设备文档缺少稳定身份 device.id(启动注册拒绝)",
                code="SYS-CFG-001",
                message_key="ies.diag.store.config_invalid",
                params={"device_id": ""},
            )
        result = build_math_contribution(doc)
        if not result.ok:
            blocking = [d for d in result.diagnostics if d.blocking]
            raise AppError(
                f"设备 {device_id} 方程贡献校验失败(启动注册拒绝)",
                code="SYS-CFG-001",
                message_key="ies.diag.store.config_invalid",
                params={
                    "device_id": device_id,
                    "diagnostics": [
                        {"code": d.code, "message_key": d.message_key, "params": d.params}
                        for d in blocking
                    ],
                },
            )
        validated += 1
        logger.info("设备方程贡献已校验: %s", device_id)
    # 全部构建校验成功 → 一次性替换全局快照(命令 + callable 同快照原子发布)
    replace_all_commands(staged, generated=callables)
    return validated
