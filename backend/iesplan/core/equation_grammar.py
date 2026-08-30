"""受限声明式方程的公共语法契约（core 无业务状态层）。

供 devices(设备文件校验)与 modeling(公共数学贡献)共同消费的方程语法纯函数：
- ``split_relation``：把 ``lhs = rhs`` 拆分；
- ``reference_atoms``：从表达式一侧提取标识符引用与时间索引（白名单词法，
  禁止任意函数调用、属性访问、未知下标与未来引用）；
- ``check_cycles``：变量定义之间的循环引用检测（忽略自环状态递推）。

本模块不包含任何设备/建模业务规则，只定义语法与拓扑约束；
禁止 eval、动态导入、函数/模块路径与任意代码入口（宪法 §4.2/§4.3）。
"""

from __future__ import annotations

import re
from typing import Any

#: 时间索引模式（``name[t]``、``name[t-1]``、``name[t+1]``；纯 [t] 无偏移）
_TIME_INDEX_PATTERN = re.compile(r"\[t\s*(?:([-+])\s*([0-9]+))?\]")

#: 关系式左右两侧拆分（允许行内注释与前后空白）
_RELATION_SPLIT = re.compile(r"\s*=\s*")

#: 标识符（允许的引用原子）：小写/大写字母开头，含数字与下划线
_IDENT_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class EquationSyntaxError(Exception):
    """方程语法/拓扑非法。"""


def split_relation(expression: str) -> tuple[str, str]:
    """把 ``lhs = rhs`` 拆分为左右两侧；不满足时报错。"""
    parts = _RELATION_SPLIT.split(expression, maxsplit=1)
    if len(parts) != 2:
        raise EquationSyntaxError("relation.expression 必须为 'lhs = rhs' 形式")
    lhs, rhs = (p.strip() for p in parts)
    if not lhs or not rhs:
        raise EquationSyntaxError("relation.expression 两侧不能为空")
    if _RELATION_SPLIT.search(rhs):
        raise EquationSyntaxError("relation.expression 只能有一个 '='")
    return lhs, rhs


def reference_atoms(side: str, relation_id: str) -> tuple[tuple[str, ...], tuple[int, ...]]:
    """从表达式一侧提取标识符引用与时间索引。

    解析规则：标识符按允许字符集切分；``name[t±k]`` 形式的时间索引被识别；
    其余含 ``[`` 的引用（未知函数调用、属性访问、任意下标）直接报错。
    返回 (标识符元组, 偏移元组)；偏移 0 表示当前步，负偏移表示过去步。
    """
    atoms: list[str] = []
    offsets: list[int] = []
    pos = 0
    while pos < len(side):
        ch = side[pos]
        if ch.isspace():
            pos += 1
            continue
        if ch.isdigit() or ch in "+-*/^%(),<>=":
            pos += 1
            continue
        m = _IDENT_TOKEN.match(side, pos)
        if not m:
            raise EquationSyntaxError(
                f"relation {relation_id!r} 表达式含非法字符 {ch!r}（只允许标识符、数字、运算符与 [t±k] 索引）"
            )
        name = m.group(0)
        pos = m.end()
        if pos < len(side) and side[pos] == "(":
            raise EquationSyntaxError(
                f"relation {relation_id!r} 禁止函数调用 {name}(...)（只允许标识符、数字、运算符与 [t±k] 索引）"
            )
        if pos < len(side) and side[pos] == "[":
            tm = _TIME_INDEX_PATTERN.match(side, pos)
            if not tm:
                raise EquationSyntaxError(
                    f"relation {relation_id!r} 标识符 {name!r} 的索引必须是 [t]、[t+k] 或 [t-k]"
                )
            offset = 0
            if tm.group(1) is not None:
                offset = int(tm.group(2))
                if tm.group(1) == "-":
                    offset = -offset
                if offset > 0:
                    raise EquationSyntaxError(
                        f"relation {relation_id!r} 禁止未来引用 {name}[t+{offset}]（跨步只允许过去或当前步）"
                    )
            offsets.append(offset)
            pos = tm.end()
        else:
            offsets.append(0)
        atoms.append(name)
    return tuple(atoms), tuple(offsets)


def check_cycles(variable_ids: Any, edges: list[tuple[str, tuple[str, ...]]]) -> None:
    """检测变量定义之间的循环引用（a 依赖 b 且 b 依赖 a）。

    edges: (输出变量, 右侧引用变量元组)。只考虑变量间依赖；properties/interfaces
    是常量/输入，不构成循环。变量在自身关系右侧出现（如 ``soc[t] = soc[t-1] + …``）
    是合法的时间状态递推，不构成循环，忽略自环。
    """
    graph: dict[str, set[str]] = {vid: set() for vid in variable_ids}
    for out_var, refs in edges:
        if out_var in graph:
            graph[out_var].update(r for r in refs if r in graph and r != out_var)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {vid: WHITE for vid in graph}
    stack: list[str] = []

    def visit(vid: str) -> None:
        color[vid] = GRAY
        stack.append(vid)
        for nxt in sorted(graph[vid]):
            if color[nxt] == GRAY:
                idx = stack.index(nxt)
                cycle = stack[idx:] + [nxt]
                raise EquationSyntaxError(f"equations 存在循环引用: {' -> '.join(cycle)}")
            if color[nxt] == WHITE:
                visit(nxt)
        stack.pop()
        color[vid] = BLACK

    for vid in graph:
        if color[vid] == WHITE:
            visit(vid)
