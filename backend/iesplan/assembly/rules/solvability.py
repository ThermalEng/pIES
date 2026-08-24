"""阶段 D:整体可解性(母线级约束不足/过度)。

母线构造:按载体 + 无向连通分量(边连通;双向端口视为双向连通,管道设备的两条边
同属一个分量)分组,得 BusSummary{carrier, ports, sources, sinks, controllable,
fixed_supply_max, demand_max, has_storage}。

规则(04 §3.4):
- ASM-SOLV-001  约束不足:母线无源(只有汇);
- ASM-SOLV-002  约束不足/能量无归处:母线无汇(只有源,且无储能/无 export 通道;
                grid 端口语义上可进口可反送,但 grid 禁反送时不算汇);
- ASM-SOLV-003  必然不可行:无任何可调手段且 Σ固定供给上限 < Σ需求最大值;
- ASM-SOLV-004  约束过度:互斥固定约束(无调节手段且供给>需求;grid 禁反送与可再生过剩并存);
- ASM-SOLV-005  因果环:有状态设备(管道/延迟输出)构成闭环(时间不一致);
- ASM-SOLV-006  孤立设备(无任何边,告警);
- ASM-SOLV-007  自由度提示(可控变量数 vs 平衡方程数,info);
- ASM-EDGE-008  双向-双向母线悬空风险(母线无任何确定方向端口,告警)。

阶段 D 为结构性前置筛查,不替代求解器运行期检查(04 §8.1)。
"""

from __future__ import annotations

from iesplan.assembly.checker import (
    _PEAK_PARAM_BY_LOAD,
    BusSummary,
    _to_watts,
    ensure_ports,
    resolve_model,
)
from iesplan.assembly.diags import (
    ASM_EDGE_LOOSE_BIDI,
    ASM_SOLV_CAUSAL_CYCLE,
    ASM_SOLV_DOF,
    ASM_SOLV_INFEASIBLE,
    ASM_SOLV_NO_SINK,
    ASM_SOLV_NO_SOURCE,
    ASM_SOLV_ORPHAN,
    ASM_SOLV_OVER_CONSTRAINED,
)
from iesplan.assembly.diags import make_asm_diag as make_diag
from iesplan.assembly.schema import AssemblySpec
from iesplan.core.diagnostics import Diagnostic

# ---------------------------------------------------------------------------
# 母线构造
# ---------------------------------------------------------------------------


def build_buses(spec: AssemblySpec, ctx) -> list[dict]:
    """载体 × 无向连通分量 → 母线字典列表(内部表示,阶段 D 与外部审计共用)。

    连通性:同一载体下经边连通的设备集合;边载体取 from 端端口载体(未定义端口的边跳过)。
    无任何边的设备也构成单设备母线(便于统一告警)。
    返回 [{carrier, device_ids, port_refs, source_refs, sink_refs, edge_ids}]。
    """
    ports = ensure_ports(spec, ctx)
    carrier_adj: dict[str, dict[str, set[str]]] = {}
    edge_by_carrier: dict[str, list] = {}
    for edge in spec.edges:
        from_port = ports.get(edge.from_port)
        if from_port is None:
            continue
        carrier = from_port.carrier
        from_dev, _, _ = edge.from_port.partition(".")
        to_dev, _, _ = edge.to_port.partition(".")
        adj = carrier_adj.setdefault(carrier, {})
        adj.setdefault(from_dev, set()).add(to_dev)
        adj.setdefault(to_dev, set()).add(from_dev)
        edge_by_carrier.setdefault(carrier, []).append(edge)

    buses: list[dict] = []
    for carrier, adj in carrier_adj.items():
        visited: set[str] = set()
        for start in adj:
            if start in visited:
                continue
            component = {start}
            frontier = [start]
            while frontier:
                node = frontier.pop()
                for nb in adj.get(node, ()):
                    if nb not in component:
                        component.add(nb)
                        frontier.append(nb)
            visited |= component
            bus_ports = sorted(
                p.ref for p in ports.values() if p.device in component and p.carrier == carrier
            )
            edge_ids = sorted(
                e.id
                for e in edge_by_carrier.get(carrier, ())
                if {e.from_port.partition(".")[0], e.to_port.partition(".")[0]} & component
            )
            buses.append(
                {
                    "carrier": carrier,
                    "device_ids": sorted(component),
                    "port_refs": bus_ports,
                    "source_refs": [
                        r
                        for r in bus_ports
                        if ports[r].direction in ("out", "bidirectional")
                    ],
                    "sink_refs": [r for r in bus_ports if ports[r].direction in ("in", "bidirectional")],
                    "edge_ids": edge_ids,
                }
            )
    # 无任何边的设备 → 单设备母线(孤立)
    connected = {d for bus in buses for d in bus["device_ids"]}
    for dev in [*spec.devices, *spec.pipelines]:
        if dev.id in connected:
            continue
        dev_port_refs = sorted(p.ref for p in ports.values() if p.device == dev.id)
        if not dev_port_refs:
            continue
        carrier = ports[dev_port_refs[0]].carrier
        buses.append(
            {
                "carrier": carrier,
                "device_ids": [dev.id],
                "port_refs": dev_port_refs,
                "source_refs": [
                    r for r in dev_port_refs if ports[r].direction in ("out", "bidirectional")
                ],
                "sink_refs": [r for r in dev_port_refs if ports[r].direction in ("in", "bidirectional")],
                "edge_ids": [],
            }
        )
    return buses


# ---------------------------------------------------------------------------
# 母线级数值估算辅助
# ---------------------------------------------------------------------------


def _float_param(params: dict, key: str) -> float | None:
    """参数数值化(字典/布尔/缺失 → None)。"""
    val = params.get(key)
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _grid_caps(spec: AssemblySpec, bus_device_ids: list[str], ctx) -> tuple[bool, bool, bool]:
    """母线电网能力:(has_grid, 可进口, 可反送)。

    可进口:max_import_power_kw 缺省/ > 0;可反送:max_export_power_kw > 0 或 export_tariff > 0。
    """
    grid_dev = next(
        (
            d
            for d in spec.devices
            if d.id in bus_device_ids
            and resolve_model(ctx, d.model)[0] is not None
            and "grid_connection" in resolve_model(ctx, d.model)[0].capabilities
        ),
        None,
    )
    if grid_dev is None:
        return False, False, False
    max_import = _float_param(grid_dev.params, "max_import_power_kw")
    max_export = _float_param(grid_dev.params, "max_export_power_kw")
    export_tariff = _float_param(grid_dev.params, "export_tariff")
    can_import = max_import is None or max_import > 0
    can_export = (max_export is not None and max_export > 0) or (
        export_tariff is not None and export_tariff > 0
    )
    return True, can_import, can_export


def _bus_caps(spec: AssemblySpec, ctx, bus: dict) -> tuple[float | None, float | None]:
    """母线固定供给上限(W)与需求上限(W)(可调/可控设备不计入固定侧)。

    供给侧:非可控源端口 capacity + 电网 max_import_power_kw(kW→W);
    需求侧:负荷 peak 参数 + 端口 capacity。未知数值不参与(返回 None 表示不可估算)。
    """
    ports = ensure_ports(spec, ctx)
    by_device = {d.id: d for d in spec.devices}
    fixed_supply = 0.0
    has_fixed_supply = False
    demand = 0.0
    has_demand = False
    for ref in bus["source_refs"]:
        port = ports[ref]
        device = by_device.get(port.device)
        if device is None:
            continue
        type_spec, _ = resolve_model(ctx, device.model)
        caps = type_spec.capabilities if type_spec is not None else []
        if "controllable" in caps or "storage" in caps:
            continue  # 可调源不计入固定供给
        if "grid_connection" in caps:
            val = _to_watts(_float_param(device.params, "max_import_power_kw"), "kW")
            if val is not None:
                fixed_supply += val
                has_fixed_supply = True
            continue
        if port.capacity is not None:
            fixed_supply += port.capacity
            has_fixed_supply = True
    for ref in bus["sink_refs"]:
        port = ports[ref]
        device = by_device.get(port.device)
        if device is None:
            continue
        type_spec, _ = resolve_model(ctx, device.model)
        if type_spec is not None and type_spec.is_load:
            peak_key = _PEAK_PARAM_BY_LOAD.get(type_spec.type_id)
            if peak_key is not None:
                val = _to_watts(_float_param(device.params, peak_key), "kW")
                if val is not None:
                    demand += val
                    has_demand = True
        if port.capacity is not None:
            demand += port.capacity
            has_demand = True
    return (fixed_supply if has_fixed_supply else None), (demand if has_demand else None)


def _bus_controllable(spec: AssemblySpec, ctx, bus: dict) -> int:
    """母线可控变量数(储能 2 变量 + 可控源 1 + 电网进出口,自由度提示)。"""
    by_device = {d.id: d for d in spec.devices}
    n = 0
    for dev_id in bus["device_ids"]:
        device = by_device.get(dev_id)
        if device is None:
            continue
        type_spec, _ = resolve_model(ctx, device.model)
        caps = type_spec.capabilities if type_spec is not None else []
        if "storage" in caps:
            n += 2
        elif "controllable" in caps:
            n += 1
        elif "grid_connection" in caps:
            _, can_import, can_export = _grid_caps(spec, bus["device_ids"], ctx)
            n += (1 if can_import else 0) + (1 if can_export else 0)
    return n


def _causal_cycles(spec: AssemblySpec, ctx) -> list[tuple[str, ...]]:
    """有状态设备(管道/延迟输出)构成的有向环(设备级;边方向 from→to)。

    任意时刻输入依赖未来输出即时间不一致;返回含延迟设备的环(去重,按字典序稳定)。
    """
    ports = ensure_ports(spec, ctx)
    delayed: set[str] = set()
    for pipe in spec.pipelines:
        delayed.add(pipe.id)
    for dev in spec.devices:
        if dev.stateful or any(
            p.nature == "delayed" for p in ports.values() if p.device == dev.id
        ):
            delayed.add(dev.id)
    if not delayed:
        return []
    device_ids = sorted({d.id for d in [*spec.devices, *spec.pipelines]})
    adj: dict[str, list[str]] = {d: [] for d in device_ids}
    for edge in spec.edges:
        from_dev, _, _ = edge.from_port.partition(".")
        to_dev, _, _ = edge.to_port.partition(".")
        if from_dev in adj and to_dev in adj and from_dev != to_dev:
            adj[from_dev].append(to_dev)
    cycles: list[tuple[str, ...]] = []
    seen: set[frozenset] = set()
    path: list[str] = []
    on_path: set[str] = set()
    done: set[str] = set()

    def dfs(node: str) -> None:
        if node in done:
            return
        if node in on_path:
            idx = path.index(node)
            cycle = tuple(path[idx:])
            if any(d in cycle for d in delayed):
                key = frozenset(cycle)
                if key not in seen:
                    seen.add(key)
                    cycles.append(cycle)
            return
        on_path.add(node)
        path.append(node)
        for nb in adj.get(node, ()):
            dfs(nb)
        path.pop()
        on_path.discard(node)
        done.add(node)

    for node in device_ids:
        dfs(node)
    return cycles


# ---------------------------------------------------------------------------
# 阶段 D 主流程
# ---------------------------------------------------------------------------


def run_phase_d(spec: AssemblySpec, ctx) -> tuple[list[Diagnostic], list[BusSummary]]:
    """阶段 D:母线构造 + 整体可解性(约束不足/过度),返回 (诊断, 母线汇总)。"""
    diags: list[Diagnostic] = []
    ports = ensure_ports(spec, ctx)
    n_steps = ctx.steps_per_year(spec)
    buses: list[BusSummary] = []

    for bus in build_buses(spec, ctx):
        carrier = bus["carrier"]
        loc = {"object_type": "bus", "field": f"carrier:{carrier}"}
        has_grid, can_import, can_export = _grid_caps(spec, bus["device_ids"], ctx)
        devices = [d for d in spec.devices if d.id in bus["device_ids"]]
        type_caps = {
            d.id: (
                resolve_model(ctx, d.model)[0].capabilities
                if resolve_model(ctx, d.model)[0] is not None
                else []
            )
            for d in devices
        }
        has_storage = any("storage" in caps for caps in type_caps.values())
        has_controllable = any("controllable" in caps for caps in type_caps.values())
        has_renewable = any("pv" in caps or "renewable" in caps for caps in type_caps.values())

        # 电网端口语义:可进口 → 计入源;可反送 → 计入汇(方向表为 out,需显式并入)
        source_refs = list(bus["source_refs"])
        sink_refs = list(bus["sink_refs"])
        if has_grid:
            grid_dev_ids = {
                d.id
                for d in spec.devices
                if d.id in bus["device_ids"]
                and resolve_model(ctx, d.model)[0] is not None
                and "grid_connection" in resolve_model(ctx, d.model)[0].capabilities
            }
            grid_port_refs = [r for r in bus["port_refs"] if r.partition(".")[0] in grid_dev_ids]
            if can_import:
                source_refs = sorted(set(source_refs) | set(grid_port_refs))
            if can_export:
                sink_refs = sorted(set(sink_refs) | set(grid_port_refs))

        # ASM-SOLV-001:母线无源(只有汇)
        if not source_refs:
            diags.append(
                make_diag(
                    ASM_SOLV_NO_SOURCE,
                    severity="error",
                    blocking=True,
                    params={
                        "carrier": carrier,
                        "bus_ports": bus["port_refs"],
                        "sink_devices": sorted(bus["device_ids"]),
                    },
                    location=loc,
                    ref_ids=["help.modeling.bus_balance", "ASM-SOLV-002"],
                )
            )
        # ASM-SOLV-002:母线无汇(只有源,且无储能/无 export 通道)
        if not sink_refs and not has_storage:
            diags.append(
                make_diag(
                    ASM_SOLV_NO_SINK,
                    severity="error",
                    blocking=True,
                    params={
                        "carrier": carrier,
                        "bus_ports": bus["port_refs"],
                        "source_devices": sorted(bus["device_ids"]),
                        "grid_export_disabled": has_grid and not can_export,
                    },
                    location=loc,
                )
            )

        fixed_supply, demand = _bus_caps(spec, ctx, bus)
        adjustable = has_storage or has_controllable or can_import
        # ASM-SOLV-003:必然不可行(无任何可调手段且 Σ固定供给 < Σ需求)
        if not adjustable and fixed_supply is not None and demand is not None:
            if fixed_supply < demand:
                diags.append(
                    make_diag(
                        ASM_SOLV_INFEASIBLE,
                        severity="error",
                        blocking=True,
                        params={
                            "carrier": carrier,
                            "fixed_supply_max": fixed_supply,
                            "demand_max": demand,
                        },
                        location=loc,
                        ref_ids=["help.modeling.bus_balance"],
                    )
                )
            elif fixed_supply > demand:
                # ASM-SOLV-004:约束过度(互斥固定约束:供给 > 需求且无调节手段)
                diags.append(
                    make_diag(
                        ASM_SOLV_OVER_CONSTRAINED,
                        severity="error",
                        blocking=True,
                        params={
                            "carrier": carrier,
                            "fixed_supply_max": fixed_supply,
                            "demand_max": demand,
                            "reason": "fixed_supply_exceeds_demand_no_adjustment",
                        },
                        location=loc,
                    )
                )
        # ASM-SOLV-004(其二):grid 禁反送且存在无储能调节的非可控可再生源(过剩无处可去)
        if has_grid and not can_export and not has_storage and has_renewable:
            diags.append(
                make_diag(
                    ASM_SOLV_OVER_CONSTRAINED,
                    severity="error",
                    blocking=True,
                    params={
                        "carrier": carrier,
                        "reason": "grid_export_disabled_with_renewable_surplus",
                        "grid_export_disabled": True,
                    },
                    location=loc,
                )
            )

        # ASM-EDGE-008:母线无任何确定方向端口(双向-双向悬空风险)
        directional = [r for r in bus["port_refs"] if ports[r].direction in ("in", "out")]
        if not directional:
            diags.append(
                make_diag(
                    ASM_EDGE_LOOSE_BIDI,
                    severity="warning",
                    blocking=False,
                    params={"carrier": carrier, "bus_ports": bus["port_refs"]},
                    location={"object_type": "bus", "field": f"carrier:{carrier}"},
                )
            )

        # ASM-SOLV-007:自由度提示(可控变量数 vs 平衡方程数)
        if bus["edge_ids"]:
            n_vars = _bus_controllable(spec, ctx, bus)
            n_eq = n_steps
            diags.append(
                make_diag(
                    ASM_SOLV_DOF,
                    severity="info",
                    blocking=False,
                    params={
                        "carrier": carrier,
                        "n_vars": n_vars,
                        "n_eq": n_eq,
                        "ratio": round(n_vars / n_eq, 6),
                    },
                    location=loc,
                )
            )

        buses.append(
            BusSummary(
                carrier=carrier,
                port_refs=bus["port_refs"],
                device_ids=bus["device_ids"],
                source_port_refs=bus["source_refs"],
                sink_port_refs=bus["sink_refs"],
                has_storage=has_storage,
                has_grid=has_grid,
                fixed_supply_max_w=fixed_supply,
                demand_max_w=demand,
                n_controllable=_bus_controllable(spec, ctx, bus),
                n_balance_eq=n_steps,
            )
        )

    # ASM-SOLV-006:孤立设备(无任何边;以边端点集合判定,排除单设备母线误报)
    edge_devices = {
        dev
        for e in spec.edges
        for dev in (e.from_port.partition(".")[0], e.to_port.partition(".")[0])
    }
    for dev in spec.devices:
        if dev.id not in edge_devices:
            diags.append(
                make_diag(
                    ASM_SOLV_ORPHAN,
                    severity="warning",
                    blocking=False,
                    params={"device": dev.id},
                    location={"object_type": "device", "object_id": dev.id},
                )
            )

    # ASM-SOLV-005:因果环(有状态设备构成闭环)
    for cycle in _causal_cycles(spec, ctx):
        pipeline_ids = {p.id for p in spec.pipelines}
        delayed_member = next((d for d in cycle if d in pipeline_ids), cycle[0])
        diags.append(
            make_diag(
                ASM_SOLV_CAUSAL_CYCLE,
                severity="error",
                blocking=True,
                params={"cycle": list(cycle), "delayed_member": delayed_member},
                location={"object_type": "pipeline", "object_id": delayed_member},
            )
        )
    return diags, buses
