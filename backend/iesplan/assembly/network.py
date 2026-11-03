"""Pure port partition shared by assembly diagnostics and model generation.

Inputs describe existing, validated ideal connections. Device-internal
relations do not participate in this graph. Unconnected connectable ports
are retained separately as closed boundaries, not implicit singleton supplies.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PortBus:
    carrier: str
    port_refs: tuple[str, ...]
    connection_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PortNetwork:
    buses: tuple[PortBus, ...]
    closed_ports: tuple[str, ...]


def build_port_network(
    port_carriers: Mapping[str, str],
    connections: Mapping[str, tuple[str, str]],
) -> PortNetwork:
    """Partition all connectable ports into ideal buses or closed boundaries.

    Callers pass only in/out/bidirectional ports, never predefined or blind
    interfaces. Closure is a zero-flow relation, not an input error.
    """
    buses = build_port_buses(port_carriers, connections)
    connected = {ref for bus in buses for ref in bus.port_refs}
    return PortNetwork(buses, tuple(sorted(set(port_carriers) - connected)))


def build_port_buses(
    port_carriers: Mapping[str, str],
    connections: Mapping[str, tuple[str, str]],
) -> tuple[PortBus, ...]:
    """Partition ideal-connected ports, with deterministic member/bus order.

    A port may join multiple ideal connections but appears only once in its
    bus. The caller owns input validation and port metadata. No device
    identity, direction, unit conversion or business inference is needed.
    """
    adjacency: dict[str, set[str]] = {}
    port_edges: dict[str, list[str]] = {}
    for identity, (left, right) in connections.items():
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
        port_edges.setdefault(left, []).append(identity)
        port_edges.setdefault(right, []).append(identity)

    seen: set[str] = set()
    buses: list[PortBus] = []
    for root in sorted(adjacency):
        if root in seen:
            continue
        seen.add(root)
        members: list[str] = []
        edge_ids: set[str] = set()
        frontier = [root]
        while frontier:
            port = frontier.pop()
            members.append(port)
            edge_ids.update(port_edges[port])
            for neighbor in adjacency[port]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    frontier.append(neighbor)
        buses.append(PortBus(
            port_carriers[root], tuple(sorted(members)), tuple(sorted(edge_ids)),
        ))
    return tuple(sorted(buses, key=lambda bus: (bus.carrier, bus.port_refs)))
