"""装配 → 计算方案桥接(03 §6.2:装配层产出 evaluate_plan 输入)。

审查意见第 4/5 条定案:计算模块的输入是装配文件(第 4 步产物)+ 模块调用命令(第 3 步
产物)+ 计算要求;本桥接把装配对象(AssemblySpec)收敛为计算引擎(evaluate_plan)
消费的方案 dict,同时提供 ``plan_from_content`` 入口(项目内容 → 装配 → 方案,
供 analysis wrapper 与既有任务装配复用)。

设计要点(05 架构总览 §4 依赖图,③→④ 契约):
- ``plan_from_assembly(spec, axis=None)``:装配对象 → 方案 dict(设备实例/参数
  逐时序列 → 引擎输入结构);
- ``plan_from_content(content, data, axis)``:项目内容 → 装配 → 方案;
- 设备参数以业务单位原样透传(引擎在内部换算,装配检查只做量纲一致性);
- 无回退路径(§9.5):装配错误原样向上报告, 调用方必须显式提供合法
  AssemblySpec 或处理装配异常(不再跳过端口/边/管道/建模方法静默降级)。
"""

from __future__ import annotations

from typing import Any

from iesplan.assembly.schema import AssemblySpec


def plan_from_assembly(spec: AssemblySpec, axis: Any = None) -> dict:
    """装配对象 → 计算方案 dict(evaluate_plan 输入, 02 §7.4 结构)。

    映射规则:
    - devices: 装配设备实例 → {type, params(业务单位原样), is_new};
    - 模型类型标志(model_method/stateful)随设备附带, 供引擎按方法分发
      (机理/数据-周期重复/数据-预测, 有/无状态);
    - 计算要求: 算法/容差/种子从 spec.requirements 透传(options 段)。
    """
    devices: list[dict] = []
    for dev in spec.devices:
        device_type = dev.model.split("@", 1)[0] if "@" in dev.model else dev.model
        devices.append(
            {
                "type": device_type,
                "params": dict(dev.params),
                "is_new": dev.kind == "new",
                "model_method": dev.model_method,
                "stateful": dev.stateful,
                "ports": [
                    {
                        "name": p.name,
                        "carrier": p.carrier,
                        "direction": p.direction,
                        "quantity": p.quantity,
                        "unit": p.unit,
                        "nature": p.nature,
                        "delay_steps": p.delay_steps,
                        "capacity": p.capacity,
                    }
                    for p in dev.ports
                ],
            }
        )
    options: dict[str, Any] = {}
    if spec.requirements is not None:
        options["algorithm"] = spec.requirements.algorithm
        options["tolerances"] = dict(spec.requirements.tolerances)
        if spec.requirements.seed is not None:
            options["seed"] = spec.requirements.seed
    plan: dict[str, Any] = {
        "devices": devices,
        "options": options,
    }
    # 边与管道(有状态传输)随方案携带, 供引擎(或后续装配感知引擎)使用
    plan["edges"] = [
        {"id": e.id, "from": e.from_port, "to": e.to_port, "capacity": e.capacity}
        for e in spec.edges
    ]
    plan["pipelines"] = [
        {"id": p.id, "model": p.model, "params": dict(p.params)} for p in spec.pipelines
    ]
    return plan


def plan_from_content(content: dict, data: dict | None = None, axis: Any = None) -> dict:
    """项目内容 → 计算方案(经装配层, 03 §6.2)。

    content 为项目草稿内容(model.devices/connections + calc_config);
    装配层负责边-端解析与端口方向派生, 本函数把装配结果收敛为方案 dict。

    无回退路径(§9.5): 装配失败原样向上报告, 不跳过装配层降级为旧式直接映射;
    调用方需要显式保证项目内容可装配, 或自行捕获装配异常。
    """
    from iesplan.assembly.builder import build_assembly

    graph = {"devices": content.get("model", {}).get("devices", [])}
    connections = content.get("model", {}).get("connections", [])
    if connections:
        graph["connections"] = connections
    cfg = content.get("calc_config") or {}
    spec = build_assembly(graph, calc_config=cfg)
    return plan_from_assembly(spec, axis=axis)


__all__ = ["plan_from_assembly", "plan_from_content"]
