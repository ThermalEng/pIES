"""阶段 C:模型可解性(引用与输入完备)。

规则清单(04 §3.3):
- ASM-REF-001  设备实例 id 重复(含与管道设备重名);
- ASM-REF-002  model 引用未注册(注册表快照无 ies.device.*@version;version 缺省取注册表最新;
               管道白名单模型除外);
- ASM-REF-003  端口引用 <dev>.<port> 不存在(边端点 / 显式端口声明);
- ASM-REF-004  数据集引用缺失(dataset_version_id 不存在/列不存在/分辨率与时间轴不符);
- ASM-REF-005  显式端口声明与注册表推导不一致(告警,以注册表为准);
- ASM-INPUT-001 设备输入端口无来边(输入不完备;solar 环境侧除外);
- ASM-INPUT-002 必填参数缺失(注册表 default=None 的引用类参数,data_refs 可替代);
- ASM-INPUT-003 参数值越界/枚举不符(非阻断);
- ASM-INPUT-004 负荷类设备缺 data_refs;
- ASM-INPUT-005 data_refs 单位与端口单位量纲不可换算;
- ASM-PIPE-001/002/003 管道设备延迟声明缺失(警告)/越界/未形成通路(警告)。
"""

from __future__ import annotations

from iesplan.assembly.checker import (
    _split_model,
    ensure_ports,
    resolve_model,
    units_compatible,
)
from iesplan.assembly.diags import (
    ASM_INPUT_DATA_UNIT,
    ASM_INPUT_LOAD_DATA,
    ASM_INPUT_PARAM,
    ASM_INPUT_RANGE,
    ASM_INPUT_UNFED,
    ASM_PIPE_DELAY_MISSING,
    ASM_PIPE_DELAY_RANGE,
    ASM_PIPE_NOT_PATH,
    ASM_REF_DATASET,
    ASM_REF_DUP_DEVICE,
    ASM_REF_MODEL_UNREG,
    ASM_REF_PORT_DECL,
    ASM_REF_PORT_UNDEF,
)
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.assembly.parser import PORT_DECL_OVERRIDE_FIELDS
from iesplan.assembly.schema import AssemblySpec
from iesplan.core.diagnostics import Diagnostic


def run_phase_c(spec: AssemblySpec, ctx) -> list[Diagnostic]:
    """阶段 C:模型可解性(引用与输入完备),全量收集。"""
    diags: list[Diagnostic] = []
    ports = ensure_ports(spec, ctx)
    n_steps = ctx.steps_per_year(spec)

    # -- 引用:设备 id 重复 / 模型注册 / 端口定义 / 数据集 ---------------------
    seen_ids: dict[str, str] = {}
    for dev in [*spec.devices, *spec.pipelines]:
        first = seen_ids.get(dev.id)
        if first is not None:
            diags.append(
                make_diag(
                    ASM_REF_DUP_DEVICE,
                    severity="error",
                    blocking=True,
                    params={"device": dev.id, "dup_of": first},
                    location={"object_type": "device", "object_id": dev.id},
                )
            )
        else:
            seen_ids[dev.id] = dev.id

    for dev in spec.devices:
        type_id, version = _split_model(dev.model)
        type_spec, is_pipeline = resolve_model(ctx, dev.model)
        if type_spec is None:
            diags.append(
                make_diag(
                    ASM_REF_MODEL_UNREG,
                    severity="error",
                    blocking=True,
                    params={"device": dev.id, "model": dev.model, "type_id": type_id},
                    location={"object_type": "device", "object_id": dev.id, "field": "model"},
                )
            )
        elif version is not None and version != type_spec.version:
            # 类型已注册但版本陈旧(设备创建时固化的注册表快照版本 ≠ 当前版本):
            # 非阻断 —— 引擎按当前注册版本运行, 拒绝会破坏既有项目在目录升级后的提交
            diags.append(
                make_diag(
                    ASM_REF_MODEL_UNREG,
                    severity="warning",
                    blocking=False,
                    params={
                        "device": dev.id,
                        "model": dev.model,
                        "type_id": type_id,
                        "registered": f"{type_id}@{type_spec.version}",
                    },
                    location={"object_type": "device", "object_id": dev.id, "field": "model"},
                )
            )

    for edge in spec.edges:
        for side, ref in (("from", edge.from_port), ("to", edge.to_port)):
            if ref not in ports:
                diags.append(
                    make_diag(
                        ASM_REF_PORT_UNDEF,
                        severity="error",
                        blocking=True,
                        params={"edge": edge.id, "side": side, "ref": ref},
                        location={"object_type": "edge", "object_id": edge.id, "field": side},
                    )
                )

    # 显式端口声明:未推导出的端口 → REF-003;与推导不一致(载体/方向) → REF-005
    for dev in spec.devices:
        for ep in dev.ports:
            derived = ports.get(ep.ref)
            if derived is None:
                diags.append(
                    make_diag(
                        ASM_REF_PORT_UNDEF,
                        severity="error",
                        blocking=True,
                        params={"device": dev.id, "ref": ep.ref, "reason": "port_not_derivable"},
                        location={"object_type": "port", "object_id": ep.ref},
                    )
                )
                continue
            mismatch = [
                f for f in PORT_DECL_OVERRIDE_FIELDS if getattr(ep, f) != getattr(derived, f)
            ]
            if mismatch:
                diags.append(
                    make_diag(
                        ASM_REF_PORT_DECL,
                        severity="warning",
                        blocking=False,
                        params={
                            "port": ep.ref,
                            "fields": mismatch,
                            "declared": {f: getattr(ep, f) for f in mismatch},
                            "derived": {f: getattr(derived, f) for f in mismatch},
                        },
                        location={"object_type": "port", "object_id": ep.ref},
                    )
                )
    for ep in spec.explicit_pipeline_ports:
        derived = ports.get(ep.ref)
        if derived is None:
            diags.append(
                make_diag(
                    ASM_REF_PORT_UNDEF,
                    severity="error",
                    blocking=True,
                    params={"device": ep.device, "ref": ep.ref, "reason": "port_not_derivable"},
                    location={"object_type": "port", "object_id": ep.ref},
                )
            )

    # 数据集引用(元信息缺失时不判定,由调用侧提供)
    if ctx.datasets is not None:
        for dev in spec.devices:
            for ref in dev.data_refs:
                meta = ctx.datasets.get(ref.dataset_version_id)
                if meta is None:
                    diags.append(
                        make_diag(
                            ASM_REF_DATASET,
                            severity="error",
                            blocking=True,
                            params={
                                "device": dev.id,
                                "ref": ref.key,
                                "dataset_version_id": ref.dataset_version_id,
                                "reason": "dataset_version_not_found",
                            },
                            location={
                                "object_type": "device",
                                "object_id": dev.id,
                                "field": f"data_refs.{ref.key}",
                            },
                        )
                    )
                    continue
                ds_cols = meta.get("columns") if isinstance(meta, dict) else None
                if ref.columns and isinstance(ds_cols, list) and ds_cols:
                    missing_cols = [c for c in ref.columns if c not in ds_cols]
                    if missing_cols:
                        diags.append(
                            make_diag(
                                ASM_REF_DATASET,
                                severity="error",
                                blocking=True,
                                params={
                                    "device": dev.id,
                                    "ref": ref.key,
                                    "dataset_version_id": ref.dataset_version_id,
                                    "reason": "column_not_found",
                                    "columns": missing_cols,
                                },
                                location={
                                    "object_type": "device",
                                    "object_id": dev.id,
                                    "field": f"data_refs.{ref.key}.columns",
                                },
                            )
                        )
                ds_res = meta.get("resolution") if isinstance(meta, dict) else None
                if ref.resolution and isinstance(ds_res, str) and ds_res:
                    if spec.time_axis is not None and ds_res != spec.time_axis.resolution:
                        diags.append(
                            make_diag(
                                ASM_REF_DATASET,
                                severity="error",
                                blocking=True,
                                params={
                                    "device": dev.id,
                                    "ref": ref.key,
                                    "dataset_version_id": ref.dataset_version_id,
                                    "reason": "resolution_mismatch",
                                    "declared": ref.resolution,
                                    "dataset": ds_res,
                                    "time_axis": spec.time_axis.resolution,
                                },
                                location={
                                    "object_type": "device",
                                    "object_id": dev.id,
                                    "field": f"data_refs.{ref.key}.resolution",
                                },
                            )
                        )

    # -- 输入完备:端口来边 / 必填参数 / 参数范围 / 负荷数据 ---------------------
    in_edges: dict[str, list[str]] = {}
    for edge in spec.edges:
        in_edges.setdefault(edge.to_port, []).append(edge.id)

    for dev in spec.devices:
        type_spec, _ = resolve_model(ctx, dev.model)
        dev_ports = [p for p in ports.values() if p.device == dev.id]
        # 输入端口无来边(solar 环境侧除外)
        for port in dev_ports:
            if port.direction != "in" or port.carrier == "solar":
                continue
            if not in_edges.get(port.ref):
                diags.append(
                    make_diag(
                        ASM_INPUT_UNFED,
                        severity="error",
                        blocking=True,
                        params={"device": dev.id, "port": port.name},
                        location={"object_type": "device", "object_id": dev.id, "field": port.name},
                    )
                )
        if type_spec is None:
            continue
        # 必填参数缺失(负荷类设备的引用参数/无默认值参数;data_refs.key 可替代;
        # 热泵/制冷机 cop_profile 等可选参考不在此列)
        data_keys = {dr.key for dr in dev.data_refs}
        required = [
            name
            for name, ps in type_spec.parameters.items()
            if ps.default is None or (ps.unit == "reference" and type_spec.is_load)
        ]
        for name in required:
            if name not in dev.params and name not in data_keys:
                diags.append(
                    make_diag(
                        ASM_INPUT_PARAM,
                        severity="error",
                        blocking=True,
                        params={"device": dev.id, "param": name},
                        location={"object_type": "device", "object_id": dev.id, "field": f"params.{name}"},
                    )
                )
        # 参数值越界/枚举不符(非阻断;复用 PARAM-RNG-003 语义)
        for name, value in dev.params.items():
            if isinstance(value, (dict, list)) or value is None:
                continue
            ps = type_spec.parameters.get(name)
            if ps is None:
                continue
            if ps.enum is not None and value not in ps.enum:
                diags.append(
                    make_diag(
                        ASM_INPUT_RANGE,
                        severity="error",
                        blocking=False,
                        params={"device": dev.id, "param": name, "value": value, "enum": list(ps.enum)},
                        location={"object_type": "device", "object_id": dev.id, "field": f"params.{name}"},
                    )
                )
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                if ps.min is not None and value < ps.min:
                    diags.append(
                        make_diag(
                            ASM_INPUT_RANGE,
                            severity="error",
                            blocking=False,
                            params={"device": dev.id, "param": name, "value": value, "min": ps.min},
                            location={
                                "object_type": "device",
                                "object_id": dev.id,
                                "field": f"params.{name}",
                            },
                        )
                    )
                elif ps.max is not None and value > ps.max:
                    diags.append(
                        make_diag(
                            ASM_INPUT_RANGE,
                            severity="error",
                            blocking=False,
                            params={"device": dev.id, "param": name, "value": value, "max": ps.max},
                            location={
                                "object_type": "device",
                                "object_id": dev.id,
                                "field": f"params.{name}",
                            },
                        )
                    )
        # 负荷类设备缺 data_refs
        if type_spec.is_load and not dev.data_refs:
            diags.append(
                make_diag(
                    ASM_INPUT_LOAD_DATA,
                    severity="error",
                    blocking=True,
                    params={"device": dev.id},
                    location={"object_type": "device", "object_id": dev.id, "field": "data_refs"},
                )
            )
        # data_refs 单位与端口单位量纲不可换算
        in_ports = [p for p in dev_ports if p.direction in ("in", "bidirectional")]
        for ref in dev.data_refs:
            if not ref.unit:
                continue
            if in_ports and not any(units_compatible(ref.unit, p.unit) for p in in_ports):
                diags.append(
                    make_diag(
                        ASM_INPUT_DATA_UNIT,
                        severity="error",
                        blocking=True,
                        params={
                            "device": dev.id,
                            "ref": ref.key,
                            "unit": ref.unit,
                            "port_units": sorted({p.unit for p in in_ports}),
                        },
                        location={
                            "object_type": "device",
                            "object_id": dev.id,
                            "field": f"data_refs.{ref.key}.unit",
                        },
                    )
                )

    # -- 管道设备:延迟声明 / 延迟范围 / 通路 -----------------------------------
    for pipe in spec.pipelines:
        loc = {"object_type": "pipeline", "object_id": pipe.id}
        delay = pipe.params.get("delay_steps")
        if delay is None:
            diags.append(
                make_diag(
                    ASM_PIPE_DELAY_MISSING,
                    severity="warning",
                    blocking=False,
                    params={"pipeline": pipe.id, "default": 1},
                    location={**loc, "field": "params.delay_steps"},
                )
            )
            delay = 1
        try:
            delay_int = int(delay)
        except (TypeError, ValueError):
            delay_int = 1
        if delay_int < 1 or delay_int >= n_steps:
            diags.append(
                make_diag(
                    ASM_PIPE_DELAY_RANGE,
                    severity="error",
                    blocking=True,
                    params={"pipeline": pipe.id, "delay_steps": delay_int, "n_steps": n_steps},
                    location={**loc, "field": "params.delay_steps"},
                )
            )
        pipe_ports = [p for p in ports.values() if p.device == pipe.id]
        in_ref = next((p.ref for p in pipe_ports if p.direction == "in"), None)
        out_ref = next((p.ref for p in pipe_ports if p.direction == "out"), None)
        has_in = in_ref is not None and any(e.to_port == in_ref for e in spec.edges)
        has_out = out_ref is not None and any(e.from_port == out_ref for e in spec.edges)
        if not has_in or not has_out:
            diags.append(
                make_diag(
                    ASM_PIPE_NOT_PATH,
                    severity="warning",
                    blocking=False,
                    params={"pipeline": pipe.id, "has_in": has_in, "has_out": has_out},
                    location=loc,
                )
            )
    return diags
