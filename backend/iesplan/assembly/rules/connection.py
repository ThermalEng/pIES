"""阶段 B:连接合法性规则(输入对输出、参数性质一致)。

边-端语义(04 §2.3):
- 边有向,`from` 端必须是输出端口(out/bidirectional),`to` 端必须是输入端口(in/bidirectional);
- **母线汇合写法**:"输出对输出"(from=out/bidir → to=out)合法 —— 同一母线上各源输出端口
  即母线汇合点(04 §2.3.4,如 e_grid_pv: grid.electric_out → pv1.electric_out);
  "输入对输出"(from=in)才是方向倒挂,报 ASM-EDGE-002;
- 两端载体/物理量/单位量纲必须一致(ASM-EDGE-003/004/005);
- 自环(ASM-EDGE-006)、同两端重复边(ASM-EDGE-007)、零/负容量(ASM-EDGE-009);
- 双向-双向直连的母线悬空风险(ASM-EDGE-008)在阶段 D 母线构造后统一检查。

未定义端口(ASM-REF-003)由阶段 C 报,本阶段跳过无法判定的检查。
"""

from __future__ import annotations

from iesplan.assembly.checker import ensure_ports, units_compatible
from iesplan.assembly.diags import (
    ASM_EDGE_BAD_SINK,
    ASM_EDGE_BAD_SOURCE,
    ASM_EDGE_CARRIER,
    ASM_EDGE_DUPLICATE,
    ASM_EDGE_QUANTITY,
    ASM_EDGE_SELF_LOOP,
    ASM_EDGE_UNIT_DIM,
    ASM_EDGE_ZERO_CAP,
)
from iesplan.assembly.schema import AssemblySpec
from iesplan.core.diagnostics import Diagnostic, make_diag

OUTPUT_DIRECTIONS = ("out", "bidirectional")
INPUT_DIRECTIONS = ("in", "bidirectional")


def run_phase_b(spec: AssemblySpec, ctx) -> list[Diagnostic]:
    """阶段 B:连接合法性(输入对输出、参数性质一致),逐边检查。"""
    diags: list[Diagnostic] = []
    ports = ensure_ports(spec, ctx)
    seen_ends: dict[tuple[str, str], str] = {}  # (from, to) → 首个边 id
    for edge in spec.edges:
        loc = {"object_type": "edge", "object_id": edge.id}
        from_port = ports.get(edge.from_port)
        to_port = ports.get(edge.to_port)

        # 自环(同一设备同一端口连到自身)
        if edge.from_port == edge.to_port:
            diags.append(
                make_diag(
                    ASM_EDGE_SELF_LOOP,
                    severity="error",
                    blocking=True,
                    params={"edge": edge.id, "ref": edge.from_port},
                    location={**loc, "field": "ends"},
                )
            )
        if from_port is None or to_port is None:
            continue  # 未定义端口由阶段 C 的 ASM-REF-003 报

        # 起点方向:必须是输出端口
        if from_port.direction not in OUTPUT_DIRECTIONS:
            diags.append(
                make_diag(
                    ASM_EDGE_BAD_SOURCE,
                    severity="error",
                    blocking=True,
                    params={
                        "edge": edge.id,
                        "from": edge.from_port,
                        "direction": from_port.direction,
                    },
                    location={**loc, "field": "from"},
                )
            )
        # 终点方向:输入端口;输出对输出为母线汇合写法,合法
        if to_port.direction not in INPUT_DIRECTIONS:
            if from_port.direction == "in":
                diags.append(
                    make_diag(
                        ASM_EDGE_BAD_SINK,
                        severity="error",
                        blocking=True,
                        params={
                            "edge": edge.id,
                            "to": edge.to_port,
                            "from_direction": from_port.direction,
                            "to_direction": to_port.direction,
                        },
                        location={**loc, "field": "to"},
                    )
                )
            # 否则:输出对输出(母线汇合),合法放行

        # 两端载体一致
        if from_port.carrier != to_port.carrier:
            diags.append(
                make_diag(
                    ASM_EDGE_CARRIER,
                    severity="error",
                    blocking=True,
                    params={
                        "edge": edge.id,
                        "from_carrier": from_port.carrier,
                        "to_carrier": to_port.carrier,
                    },
                    location={**loc, "field": "carrier"},
                )
            )
        # 两端物理量一致
        if from_port.quantity != to_port.quantity:
            diags.append(
                make_diag(
                    ASM_EDGE_QUANTITY,
                    severity="error",
                    blocking=True,
                    params={
                        "edge": edge.id,
                        "from_quantity": from_port.quantity,
                        "to_quantity": to_port.quantity,
                    },
                    location={**loc, "field": "quantity"},
                )
            )
        # 两端单位量纲可换算
        if not units_compatible(from_port.unit, to_port.unit):
            diags.append(
                make_diag(
                    ASM_EDGE_UNIT_DIM,
                    severity="error",
                    blocking=True,
                    params={"edge": edge.id, "from_unit": from_port.unit, "to_unit": to_port.unit},
                    location={**loc, "field": "unit"},
                )
            )
        # 同两端同载体重复边(多边并行)
        key = (edge.from_port, edge.to_port)
        first = seen_ends.get(key)
        if first is not None:
            diags.append(
                make_diag(
                    ASM_EDGE_DUPLICATE,
                    severity="error",
                    blocking=True,
                    params={"edge": edge.id, "dup_of": first, "from": edge.from_port, "to": edge.to_port},
                    location=loc,
                )
            )
        else:
            seen_ends[key] = edge.id
        # 边容量为 0 或负值
        if edge.capacity is not None and edge.capacity <= 0:
            diags.append(
                make_diag(
                    ASM_EDGE_ZERO_CAP,
                    severity="warning",
                    blocking=False,
                    params={"edge": edge.id, "capacity": edge.capacity},
                    location={**loc, "field": "capacity"},
                )
            )
    return diags
