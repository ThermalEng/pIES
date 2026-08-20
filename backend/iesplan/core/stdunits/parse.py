"""非标准单位字符串解析(审查意见第 0 条;方案 01 §3)。

词法文法(01 §3.1):

    quantity    := number [ws] unit-string?
    number      := sign? int (',' int)* ['.' frac]? [('e'|'E') sign? digits]?
                 | sign? digits ['.' frac]? MULT?
    unit-string := compound ('/' compound)?      # 分母至多一层,拒绝嵌套除法与括号
    compound    := token (('·'|'*') token)*
    token       := MULT? symbol                  # 如 万m³、kW、元、月
    symbol      := UNITS 键 或 别名(大小写不敏感,含 kWp/MWp/°C 别名)
    MULT        := 百(1e2) | 千(1e3) | 万(1e4) | 亿(1e8)

约束:
1. 数值与单位之间允许零个或多个空格("1000kW"/"1000 kW"/"3 元/kWh" 均合法);
2. 单位串缺失时:调用方提供 context(期望单位)则取 context,否则抛 UnitParseError;
3. 仿射单位(C/F)只允许独立出现,禁止进入复合/分母(宽松化:独立 C 允许,01 §3.3);
4. 纯乘数无单位符号("0.5 万")不合法,必须带 symbol 或 context。

本模块只依赖 0 层(core.stdunits.registry / core.errors / core.units),无业务依赖。
"""

from __future__ import annotations

import difflib
import re
from typing import Final

from iesplan.core.errors import AppError
from iesplan.core.stdunits.registry import (
    AFFINE_UNITS,
    ALIAS_MAP,
    MULTIPLIERS,
    SI_BASE_SYMBOL,
    UNITS,
    UnitSpec,
)

# ---------------------------------------------------------------------------
# 词法
# ---------------------------------------------------------------------------

#: 数值正则(十进制/千分位/科学计数/中文乘数后缀,01 §3.1 number)
NUMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"[+-]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?:[eE][+-]?\d+)?[百千万亿]?"
)

#: token 内部连乘分隔符(01 §3.1 compound)
_TOKEN_SEPARATORS: Final[str] = "·*"

#: 单位串最大长度防护(防止病态输入拖慢分词)
_MAX_UNIT_LEN: Final[int] = 64


class UnitParseError(AppError):
    """单位字符串解析失败(01 §3.4:code=PARAM-UNIT-001,现空闲码)。

    params: {"text": 原文, "position": 失败偏移, "expected": 期望单位/类别,
    "suggestions": [ALIAS_MAP 近匹配]}。
    """

    code = "PARAM-UNIT-001"
    message_key = "ies.diag.param.unit_parse"


def _suggestions(text: str) -> list[str]:
    """对未识别片段给出 ALIAS_MAP 近匹配建议(最多 3 条)。"""
    candidates = sorted(set(ALIAS_MAP) | set(UNITS))
    return difflib.get_close_matches(text.lower(), candidates, n=3, cutoff=0.5)


def parse_number(text: str) -> float:
    """解析数值文本(含千分位逗号与中文乘数后缀)。

    示例: "1,000" → 1000.0、"1.5万" → 15000.0、"2e3" → 2000.0。
    """
    mult = 1.0
    tail = text[-1]
    if tail in MULTIPLIERS:
        mult = MULTIPLIERS[tail]
        text = text[:-1]
    return float(text.replace(",", "")) * mult


def decompose(unit_string: str) -> tuple[list[tuple[str, str, UnitSpec]], list[tuple[str, str, UnitSpec]]]:
    """单位串 → (分子 [(乘数字符串, 规范 token, UnitSpec)...], 分母 [...])。

    - 全串/单 token 直查优先(含 kWp、千m³ 等别名与复合注册词条);
    - 每 token 按 MULT?(symbol) 最长匹配分解,分子分母内部用 · 或 * 连乘;
    - 拒绝:嵌套除法(多于一层 /)、括号、空 token、仿射单位进入复合/分母(01 §3.1)。

    异常:
        UnitParseError: 词法/语法不合法。
    """
    s = unit_string.strip()
    if not s:
        raise UnitParseError(
            f"单位串为空", params={"text": unit_string, "position": 0, "expected": "非空单位串", "suggestions": []}
        )
    if len(s) > _MAX_UNIT_LEN:
        raise UnitParseError(
            f"单位串过长({len(s)} 字符)",
            params={
                "text": unit_string,
                "position": 0,
                "expected": f"长度 ≤ {_MAX_UNIT_LEN}",
                "suggestions": [],
            },
        )
    if "(" in s or ")" in s:
        pos = s.find("(") if "(" in s else s.find(")")
        raise UnitParseError(
            f"单位串不允许括号: {s!r}",
            params={"text": unit_string, "position": pos, "expected": "无括号", "suggestions": []},
        )

    def _side(part: str) -> list[tuple[str, str, UnitSpec]]:
        raw = re.split(f"[{_TOKEN_SEPARATORS}]", part)
        # 空 token(连续分隔符/首尾分隔符)拒绝
        if not part or any(t == "" for t in raw):
            raise UnitParseError(
                f"单位串存在空 token: {part!r}",
                params={"text": unit_string, "position": 0, "expected": "token 非空", "suggestions": []},
            )
        result = []
        for token in raw:
            match = _match_token(token)
            if match is None:
                raise UnitParseError(
                    f"无法识别的单位符号: {token!r}",
                    params={
                        "text": unit_string,
                        "position": unit_string.find(token),
                        "expected": "已注册单位或其别名",
                        "suggestions": _suggestions(token),
                    },
                )
            mult, canon, spec = match
            result.append((mult, canon, spec))
        return result

    parts = s.split("/")
    if len(parts) > 2:
        raise UnitParseError(
            f"嵌套除法被拒绝(仅允许一层 /): {s!r}",
            params={"text": unit_string, "position": s.find("/"), "expected": "最多一个 /", "suggestions": []},
        )
    numerator = _side(parts[0])
    denominator = _side(parts[1]) if len(parts) == 2 else []
    if not numerator:
        raise UnitParseError(
            f"分子为空: {s!r}",
            params={"text": unit_string, "position": 0, "expected": "分子非空", "suggestions": []},
        )
    # 仿射单位(C/F)只允许独立出现(01 §3.1 约束 3;宽松化:独立 C 允许,01 §3.3)
    all_tokens = numerator + denominator
    affine = [(tok, spec) for _, tok, spec in all_tokens if spec.offset != 0.0]
    if affine and len(all_tokens) > 1:
        tok, spec = affine[0]
        raise UnitParseError(
            f"仿射单位 {spec.symbol_en!r} 禁止进入复合/分母,应独立出现",
            params={
                "text": unit_string,
                "position": unit_string.find(tok),
                "expected": "温度(C/F)独立书写",
                "suggestions": [tok],
            },
        )
    return numerator, denominator


def _match_token(token: str) -> tuple[str, str, UnitSpec] | None:
    """单个 token → (乘数字符串, 规范 token, UnitSpec);无法识别返回 None。

    查找顺序:整体直查(含注册的乘数词条如 千m³)→ MULT 前缀 + 符号分解。
    """
    uid = ALIAS_MAP.get(token.lower())
    if uid is not None:
        return ("", uid, UNITS[uid])
    for mult in sorted(MULTIPLIERS, key=len, reverse=True):
        if token.startswith(mult):
            rest = token[len(mult):]
            uid = ALIAS_MAP.get(rest.lower())
            if uid is not None:
                return (mult, mult + uid, UNITS[uid])
    return None


def parse_unit_string(s: str) -> tuple[str, float]:
    """单位串 → (规范形, 组合系数 to_si)。

    组合系数 = ∏(分子 to_si × MULT) / ∏(分母 to_si × MULT);仿射单位的
    加法偏移不含在此系数内(由 to_si/parse_quantity 对独立仿射单位另行处理)。

    示例:
        parse_unit_string("kWp") == ("kW", 1e3)
        parse_unit_string("元/kWh") == ("CNY/kWh", 1 / 3.6e6)
        parse_unit_string("CNY/kW·月") == ("CNY/kW·月", 1 / (1e3 * 2.592e6))
        parse_unit_string("tCO2/万m³") == ("tCO2/万m³", 1e3 / 1e4)

    异常:
        UnitParseError: 词法/语法不合法。
    """
    numerator, denominator = decompose(s)

    def _canon(tokens: list[tuple[str, str, UnitSpec]]) -> str:
        return "·".join(tok for _, tok, _ in tokens)

    def _factor(tokens: list[tuple[str, str, UnitSpec]]) -> float:
        prod = 1.0
        for mult, _, spec in tokens:
            prod *= MULTIPLIERS.get(mult, 1.0) * spec.to_si
        return prod

    canonical = _canon(numerator)
    factor = _factor(numerator)
    if denominator:
        canonical = f"{canonical}/{_canon(denominator)}"
        factor = factor / _factor(denominator)
    return canonical, factor


def si_unit_of(canonical: str) -> str:
    """规范形单位串 → SI 基准单位描述(复合如 "CNY/J"、"kg/m³"、"CNY/(W·s)")。

    分母多于一个 token 时加括号(01 §3.3 示例);无量纲为 "1"。
    """
    numerator, denominator = decompose(canonical)

    def _si(tokens: list[tuple[str, str, UnitSpec]]) -> str:
        return "·".join(SI_BASE_SYMBOL[spec.category] for _, _, spec in tokens)

    num = _si(numerator)
    if not denominator:
        return num
    den = _si(denominator)
    if "·" in den:
        return f"{num}/({den})"
    return f"{num}/{den}"


def parse_quantity(text: str, *, context: str | None = None) -> "Quantity":
    """解析 "数值+单位" 字符串为 Quantity(唯一解析入口,01 §4.3)。

    参数:
        text: 输入原文(如 "1000 kW"、"1.5MWh"、"3 元/kWh"、"25℃"、"1.5万")。
        context: 期望单位(注册表 ParameterSpec.unit / 数据集声明单位);
            单位串缺失时兜底(01 §3.1 约束 2),未提供则抛 UnitParseError。

    返回:
        Quantity(value=原数值, unit=规范形, si_value=SI 数值, si_unit=SI 单位)。
        仿射单位(温度)经 to_si 语义(含偏移);其余线性 × 组合系数。

    异常:
        UnitParseError: 数字缺失 / 单位无法识别 / 单位缺失且无 context。
    """
    stripped = text.strip()
    if not stripped:
        raise UnitParseError(
            f"输入为空", params={"text": text, "position": 0, "expected": "数值或 数值+单位", "suggestions": []}
        )
    match = NUMBER_RE.match(stripped)
    if match is None:
        raise UnitParseError(
            f"数值格式无法识别: {stripped!r}",
            params={
                "text": text,
                "position": 0,
                "expected": "数字格式示例: 1000 / 1,000.5 / 1.5e3 / 1.5万",
                "suggestions": [],
            },
        )
    value = parse_number(match.group())
    rest = stripped[match.end():].strip()
    unit_s = rest
    position = match.end()
    if not rest:
        if context is None:
            raise UnitParseError(
                f"单位缺失且未提供期望单位(context)",
                params={
                    "text": text,
                    "position": match.end(),
                    "expected": "已注册单位",
                    "suggestions": [],
                },
            )
        unit_s = context
        position = len(stripped)
    elif all(ch in MULTIPLIERS for ch in rest):
        # 纯乘数无单位符号("0.5 万"):必须带 symbol 或 context(01 §3.1 约束 4)
        if context is None:
            raise UnitParseError(
                f"纯乘数无单位符号: {rest!r},必须带单位符号或 context",
                params={"text": text, "position": match.end(), "expected": "单位符号或期望单位", "suggestions": []},
            )
        for ch in rest:
            value *= MULTIPLIERS[ch]
        unit_s = context
        position = len(stripped)
    try:
        norm, factor = parse_unit_string(unit_s)
    except UnitParseError as exc:
        # 失败偏移修正到原文绝对位置(单位串起始处)
        raise UnitParseError(
            exc.args[0] if exc.args else "单位无法识别",
            params={**exc.params, "text": text, "position": position},
        ) from None
    if norm in AFFINE_UNITS:
        spec = UNITS[norm]
        si_value = value * spec.to_si + spec.offset
    else:
        si_value = value * factor
    from iesplan.core.stdunits.convert import Quantity  # 延迟导入避免 convert↔parse 循环

    return Quantity(value=value, unit=norm, si_value=si_value, si_unit=si_unit_of(norm))
