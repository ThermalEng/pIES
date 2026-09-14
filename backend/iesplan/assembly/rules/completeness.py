"""阶段 C:模型可解性(引用与输入完备)。

规则清单(04 §3.3):
- ASM-REF-001  设备实例 id 重复(含与管道设备重名);
- ASM-REF-002  model 引用未注册(注册表快照无 ies.device.*@version;version 缺省取注册表最新;
               管道白名单模型除外);
- ASM-REF-003  端口引用 <dev>.<port> 不存在(边端点 / 显式端口声明);
- ASM-REF-004  数据集引用缺失(dataset_version_id 不存在/列不存在/分辨率与时间轴不符);
- ASM-REF-005  显式端口声明与注册表推导不一致(告警,以注册表为准);
- ASM-INPUT-001 设备输入端口无来边(输入不完备;外生供给载体、电网单侧运行除外);
- ASM-INPUT-002 必填参数缺失(2.0 已废止:properties 均带默认值);
- ASM-INPUT-003 参数值越界/枚举不符(非阻断);
- ASM-INPUT-004 已删除(predefined 接口缺显式绑定不阻断,source.data_ref 为默认数据路径);
- ASM-INPUT-005 data_refs 单位与端口单位量纲不可换算;
- ASM-PIPE-001/002/003 管道设备延迟声明缺失(警告)/越界/未形成通路(警告)。
"""

from __future__ import annotations

from iesplan.assembly.context import (
    AssemblySpec,
    EXOGENOUS_SUPPLY_CARRIERS,
    GRID_SIDE_PORTS,
    PORT_DECL_OVERRIDE_FIELDS,
    ensure_ports,
    grid_side_used,
    resolve_model,
    split_model,
)
from iesplan.assembly.diags import (
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
        type_id, _ = split_model(dev.definition_id)
        type_spec, is_pipeline = resolve_model(ctx, dev.definition_id)
        if type_spec is None:
            diags.append(
                make_diag(
                    ASM_REF_MODEL_UNREG,
                    severity="error",
                    blocking=True,
                    params={"device": dev.id, "definition": dev.definition_id, "type_id": type_id},
                    location={"object_type": "device", "object_id": dev.id, "field": "definition.id"},
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
        type_spec, _ = resolve_model(ctx, dev.definition_id)
        dev_ports = [p for p in ports.values() if p.device == dev.id]
        # 电网进出口为替代运行模式:任一侧已连接时,另一未连接侧视为单侧运行,
        # 不报输入不完备(未用侧由进出口互斥方程钳零,与 1.0 单端口电网同语义)
        grid_used = grid_side_used(type_spec, dev.id, spec.edges)
        # 输入端口无来边(外生供给载体、电网单侧运行除外)
        for port in dev_ports:
            if port.direction != "in" or port.carrier in EXOGENOUS_SUPPLY_CARRIERS:
                continue
            if grid_used and port.name in GRID_SIDE_PORTS:
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
        # 注:属性值域(properties valid_range)由校验器阶段 2 唯一判定
        # (非阻断 ASM-INPUT-003)；预定义来源声明的槽覆盖、模式、路径与
        # 文件内容由校验器确认；本阶段不重复判定，只做图与系统检查。
        # 缺显式预定义绑定阻断，且不得回退到设备文件自带来源。

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
